"""mdc's HTTP API, run by `mdc serve` (SERVER.md, Phase 2).

Every route but /healthz needs a bearer token. /data/users.toml maps the
SHA-256 of each token to a user, or to "*" for a service (the nightly
index), and a user reaches only their own libraries, /data/libraries/<user>/
<library>. A library is files to sync and commands to run:

- GET    /libraries                          the caller's library ids
- GET    /libraries/{user}/{library}/files   {rel_path: sha256}
- GET    …/files/{rel_path}                  the bytes, ETag their sha256
- PUT    …/files/{rel_path}                  If-None-Match: * creates; If-Match: "<sha>" replaces
- DELETE …/files/{rel_path}                  If-Match: "<sha>"
- POST   …/run {argv, user_names}            server-sent events: output, then done

/run runs `mdc --lib <library> <argv>` in a subprocess from the library root:
file arguments are names, which mdc resolves across the library. Its output
streams as it is written (reply prints tokens without newlines), and the
last event says which files the run changed or deleted. One run at a time
per library; while it runs, writes to the library get 409. The run belongs
to the server, not the request: a client that goes away does not cut it short.

Dotfiles, and so the index state in .mdc/, are never listed, read or written.
"""

from __future__ import annotations

import asyncio
import codecs
import hashlib
import hmac
import json
import os
import re
import sys
import tomllib
from pathlib import Path, PurePosixPath

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

# Written by bin/deploy into the package before the image is built, so it is
# baked in with the code it describes. Absent in a checkout, where the answer
# is "dev".
_DEPLOYED = Path(__file__).resolve().parent / "deployed.json"

# Subcommands a client may run. edit, config, sync and serve belong to the
# client's own machine or to the operator. files lists the files uploaded
# with the server's key, which only the server holds.
RUNNABLE = frozenset({
    "check", "validate", "fix", "new", "reply", "index",
    "argue", "analyze", "audit", "export", "pdf", "diff", "review", "files",
})
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_USER_NAME_RE = re.compile(r"^[^\s,]+$")

app = FastAPI(title="mdc")

_locks: dict[Path, asyncio.Lock] = {}
_runs: set[asyncio.Task] = set()


def _deployed() -> dict:
    try:
        return json.loads(_DEPLOYED.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"commit": "dev"}


def _data_dir() -> Path:
    return Path(os.environ.get("MDC_DATA_DIR", "/data"))


@app.get("/healthz")
def healthz() -> dict:
    """Unauthenticated: it says nothing but which commit is running."""
    return {"ok": True, **_deployed()}


# ---------------------------------------------------------------------------
# Callers and libraries
# ---------------------------------------------------------------------------

def caller(authorization: str | None = Header(default=None)) -> str:
    """The user a bearer token belongs to, or "*" for a service."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "a bearer token is required")
    digest = hashlib.sha256(authorization.removeprefix("Bearer ").strip().encode()).hexdigest()
    try:
        users = tomllib.loads((_data_dir() / "users.toml").read_text(encoding="utf-8"))
    except FileNotFoundError:
        users = {}
    for token in users.get("tokens", []):
        if hmac.compare_digest(str(token.get("sha256", "")), digest):
            return str(token["user"])
    raise HTTPException(401, "unknown token")


def library(user: str, library: str, who: str = Depends(caller)) -> Path:
    if who not in (user, "*"):
        raise HTTPException(403, "not your library")
    if not (_NAME_RE.match(user) and _NAME_RE.match(library)):
        raise HTTPException(404, "no such library")
    root = _data_dir() / "libraries" / user / library
    if not root.is_dir():
        raise HTTPException(404, "no such library")
    return root


@app.get("/libraries")
def libraries(who: str = Depends(caller)) -> dict:
    base = _data_dir() / "libraries"
    users = [base / who] if who != "*" else sorted(p for p in base.iterdir() if p.is_dir()) if base.is_dir() else []
    return {"libraries": [
        f"{u.name}/{lib.name}"
        for u in users if u.is_dir()
        for lib in sorted(u.iterdir()) if lib.is_dir() and _NAME_RE.match(lib.name)
    ]}


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_hashes(root: Path) -> dict[str, str]:
    """{rel_path: sha256} of a library's visible files: no dotfiles, no symlinks."""
    hashes = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if any(part.startswith(".") for part in rel.parts) or path.is_symlink() or not path.is_file():
            continue
        hashes[rel.as_posix()] = _sha256(path)
    return hashes


def _file(root: Path, rel_path: str) -> Path:
    """A path within the library, refusing escapes, dotfiles and symlinks."""
    rel = PurePosixPath(rel_path)
    if rel.is_absolute() or not rel.parts or any(p in ("", ".", "..") or p.startswith(".") for p in rel.parts):
        raise HTTPException(400, f"not a library path: {rel_path!r}")
    path = root
    for part in rel.parts:
        path = path / part
        if path.is_symlink():
            raise HTTPException(400, f"a symlink is in the way: {rel_path!r}")
    return path


def _etag(sha256: str) -> str:
    return f'"{sha256}"'


def _unquote(tag: str) -> str:
    return tag.strip().removeprefix("W/").strip('"')


def _writable(root: Path) -> None:
    lock = _locks.get(root)
    if lock is not None and lock.locked():
        raise HTTPException(409, "a run is in progress in this library")


@app.get("/libraries/{user}/{library}/files")
def list_files(root: Path = Depends(library)) -> dict:
    return {"files": tree_hashes(root)}


@app.get("/libraries/{user}/{library}/files/{rel_path:path}")
def read_file(rel_path: str, root: Path = Depends(library)) -> Response:
    path = _file(root, rel_path)
    if not path.is_file():
        raise HTTPException(404, "no such file")
    data = path.read_bytes()
    return Response(data, media_type="application/octet-stream",
                    headers={"ETag": _etag(hashlib.sha256(data).hexdigest())})


@app.put("/libraries/{user}/{library}/files/{rel_path:path}")
async def write_file(
    rel_path: str,
    request: Request,
    root: Path = Depends(library),
    if_match: str | None = Header(default=None),
    if_none_match: str | None = Header(default=None),
) -> Response:
    path = _file(root, rel_path)
    _writable(root)
    if if_none_match is not None:
        if if_none_match.strip() != "*":
            raise HTTPException(400, "If-None-Match takes only *")
        if path.exists():
            raise HTTPException(412, "the file already exists")
    elif if_match is not None:
        if not path.is_file() or _sha256(path) != _unquote(if_match):
            raise HTTPException(412, "the file has changed")
    else:
        raise HTTPException(428, 'If-Match: "<sha256>" to replace, If-None-Match: * to create')
    data = await request.body()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.put")
    tmp.write_bytes(data)
    tmp.replace(path)
    return Response(status_code=200, headers={"ETag": _etag(hashlib.sha256(data).hexdigest())})


@app.delete("/libraries/{user}/{library}/files/{rel_path:path}")
def delete_file(
    rel_path: str,
    root: Path = Depends(library),
    if_match: str | None = Header(default=None),
) -> Response:
    path = _file(root, rel_path)
    _writable(root)
    if if_match is None:
        raise HTTPException(428, 'If-Match: "<sha256>" is required')
    if not path.is_file() or _sha256(path) != _unquote(if_match):
        raise HTTPException(412, "the file has changed")
    path.unlink()
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------

class Run(BaseModel):
    argv: list[str]
    user_names: list[str] = []


def _check_run(run: Run) -> None:
    if not run.argv or run.argv[0] not in RUNNABLE:
        raise HTTPException(400, f"runnable subcommands: {', '.join(sorted(RUNNABLE))}")
    if any(a == "--lib" or a.startswith("--lib=") for a in run.argv):
        raise HTTPException(400, "the library is the route's, not an argument")
    if run.argv[0] == "reply" and any(a in ("-W", "--watch") for a in run.argv):
        raise HTTPException(400, "reply --watch belongs to the client")
    bad = [n for n in run.user_names if not _USER_NAME_RE.match(n)]
    if bad:
        raise HTTPException(400, f"a user name is one word: {bad}")


@app.post("/libraries/{user}/{library}/run")
async def run_command(run: Run, root: Path = Depends(library)) -> StreamingResponse:
    _check_run(run)
    lock = _locks.setdefault(root, asyncio.Lock())
    if lock.locked():
        raise HTTPException(409, "a run is in progress in this library")
    await lock.acquire()
    events: asyncio.Queue = asyncio.Queue()
    task = asyncio.create_task(_execute(root, run, events, lock))
    _runs.add(task)
    task.add_done_callback(_runs.discard)

    async def stream():
        while (event := await events.get()) is not None:
            kind, payload = event
            yield f"event: {kind}\ndata: {json.dumps(payload)}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


async def _execute(root: Path, run: Run, events: asyncio.Queue, lock: asyncio.Lock) -> None:
    try:
        before = await asyncio.to_thread(tree_hashes, root)
        env = {k: v for k, v in os.environ.items() if k != "MDC_LIBRARY"}
        # MDC_RUN_LOCAL: this mdc is the server's, never a client of it.
        env.update(MDC_USER_NAMES=",".join(run.user_names), PYTHONUNBUFFERED="1", MDC_RUN_LOCAL="1")
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "mdc", "--lib", str(root), *run.argv,
            cwd=root, env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while chunk := await process.stdout.read(4096):
            if text := decoder.decode(chunk):
                await events.put(("output", {"text": text}))
        if text := decoder.decode(b"", final=True):
            await events.put(("output", {"text": text}))
        exit_code = await process.wait()
        after = await asyncio.to_thread(tree_hashes, root)
        await events.put(("done", {
            "exit_code": exit_code,
            "changed": {rel: sha for rel, sha in after.items() if before.get(rel) != sha},
            "deleted": sorted(before.keys() - after.keys()),
        }))
    except Exception as exc:  # the stream must end, whatever happened
        await events.put(("error", {"message": str(exc)}))
    finally:
        lock.release()
        await events.put(None)


def run_serve(host: str, port: int) -> int:
    import uvicorn
    uvicorn.run(app, host=host, port=port)
    return 0
