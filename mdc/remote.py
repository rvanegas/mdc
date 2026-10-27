"""The local CLI as a client of mdc's API (SERVER.md, Phase 3).

The server is canonical; the local library is a working copy for editing.
Model-backed commands run on the server: before a run the local library and
the server's copy are synced, the run's output streams back as it is
written, and the files it changed are synced back after.

Syncing compares each file three ways: as it is here (L), on the server (R),
and as both last agreed (B, kept in the local library's .mdc/sync.json,
which is never synced). Only L changed: push it. Only R changed: pull it.
Both changed differently: a conflict. The server's version is saved beside
the local one as `<name>.server` and nothing is merged; deleting that copy
says the local version wins, and the next sync pushes it. A conflict before
a run stops the run.

The first sync, with no sync.json, takes the server's state as B, so the
local library wins: it re-seeds the server's copy.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from mdc.config import LIBRARY_STATE_DIRNAME, PROMPT_HEADING, AppConfig

# Model-backed commands, which need the server's keys and its index.
REMOTE_COMMANDS = frozenset({"reply", "index", "argue", "analyze", "audit", "export", "pdf", "review", "files"})

CONFLICT_SUFFIX = ".server"
SYNC_STATE = "sync.json"


class RemoteError(RuntimeError):
    pass


def mirror_for(config: AppConfig, cwd: Path) -> tuple[str, Path] | None:
    """The mirrored library this run works in, if any: by --lib, else by cwd."""
    if os.environ.get("MDC_RUN_LOCAL") or not (config.server_url and config.server_token):
        return None
    for library_id, root in config.mirrors:
        if config.library_path and config.library_path.resolve() == root:
            return library_id, root
    here = cwd.resolve()
    for library_id, root in config.mirrors:
        if here == root or root in here.parents:
            return library_id, root
    return None


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class Client:
    def __init__(self, url: str, token: str, library_id: str) -> None:
        self.base = f"{url}/libraries/{library_id}"
        self.token = token

    def _request(self, method: str, path: str, data: bytes | None = None,
                 headers: dict[str, str] | None = None, stream: bool = False):
        request = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Authorization": f"Bearer {self.token}", **(headers or {})},
        )
        try:
            response = urllib.request.urlopen(request, timeout=None if stream else 120)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise RemoteError(f"{method} {path}: {exc.code} {detail}") from None
        except urllib.error.URLError as exc:
            raise RemoteError(f"{self.base}: {exc.reason}") from None
        return response if stream else response.read()

    @staticmethod
    def _file_path(rel: str) -> str:
        return "/files/" + urllib.parse.quote(rel)

    def list_files(self) -> dict[str, str]:
        return json.loads(self._request("GET", "/files"))["files"]

    def get(self, rel: str) -> bytes:
        return self._request("GET", self._file_path(rel))

    def put(self, rel: str, data: bytes, base_sha: str | None) -> None:
        precondition = {"If-Match": f'"{base_sha}"'} if base_sha else {"If-None-Match": "*"}
        self._request("PUT", self._file_path(rel), data, {"Content-Type": "application/octet-stream", **precondition})

    def delete(self, rel: str, base_sha: str) -> None:
        self._request("DELETE", self._file_path(rel), headers={"If-Match": f'"{base_sha}"'})

    def run(self, argv: list[str], user_names: list[str], on_text) -> dict:
        body = json.dumps({"argv": argv, "user_names": user_names}).encode()
        response = self._request("POST", "/run", body, {"Content-Type": "application/json"}, stream=True)
        event, done = None, None
        with response:
            for raw in response:
                line = raw.decode("utf-8").rstrip("\n")
                if line.startswith("event: "):
                    event = line[7:]
                elif line.startswith("data: "):
                    payload = json.loads(line[6:])
                    if event == "output":
                        on_text(payload["text"])
                    elif event == "done":
                        done = payload
                    elif event == "error":
                        raise RemoteError(f"the run failed on the server: {payload['message']}")
        if done is None:
            raise RemoteError("the server ended the run without a result")
        return done


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def local_hashes(root: Path) -> dict[str, str]:
    """{rel_path: sha256} of what syncs: as the server lists it, less conflict
    copies and editor scratch files."""
    hashes = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if (any(part.startswith(".") for part in rel.parts) or path.is_symlink()
                or not path.is_file() or path.name.endswith(CONFLICT_SUFFIX)
                or _editor_scratch(path.name)):
            continue
        hashes[rel.as_posix()] = _sha256(path.read_bytes())
    return hashes


def _editor_scratch(name: str) -> bool:
    """An editor's backup (`x.md~`) or autosave (`#x.md#`): not the library's."""
    return name.endswith("~") or (name.startswith("#") and name.endswith("#"))


def _state_path(root: Path) -> Path:
    return root / LIBRARY_STATE_DIRNAME / SYNC_STATE


def _load_state(root: Path, library_id: str) -> dict | None:
    try:
        state = json.loads(_state_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return state if state.get("library") == library_id else None


def _save_state(root: Path, library_id: str, files: dict[str, str], conflicts: dict[str, str | None]) -> None:
    path = _state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"library": library_id, "files": files, "conflicts": conflicts},
                               indent=2, sort_keys=True), encoding="utf-8")


def sync(client: Client, root: Path, library_id: str, say=print) -> list[str]:
    """Bring the local library and the server's copy together; return conflicts."""
    local = local_hashes(root)
    remote = client.list_files()
    state = _load_state(root, library_id)
    if state is None:
        say("First sync: this copy is canonical; the server's copy follows it.")
        base, open_conflicts = dict(remote), {}
    else:
        base, open_conflicts = dict(state["files"]), dict(state.get("conflicts", {}))

    agreed: dict[str, str] = {}
    conflicts: dict[str, str | None] = {}
    pushed = pulled = 0
    for rel in sorted(set(local) | set(remote) | set(base)):
        L, R, B = local.get(rel), remote.get(rel), base.get(rel)
        path = root / rel
        server_copy = path.with_name(path.name + CONFLICT_SUFFIX)
        if rel in open_conflicts and not server_copy.exists() and R == open_conflicts[rel]:
            B = R  # the conflict is resolved for the local version: push it
        if L == R:
            if L:
                agreed[rel] = L
            server_copy.unlink(missing_ok=True)
        elif R == B:
            if L is None:
                client.delete(rel, R)
            else:
                client.put(rel, path.read_bytes(), R)
                agreed[rel] = L
            pushed += 1
        elif L == B:
            if R is None:
                path.unlink()
            else:
                data = client.get(rel)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                agreed[rel] = R
            pulled += 1
        else:
            if R is not None:
                server_copy.write_bytes(client.get(rel))
            conflicts[rel] = R
            if B:
                agreed[rel] = B
    _save_state(root, library_id, agreed, conflicts)
    if pushed or pulled:
        say(f"Synced {library_id}: {pushed} sent, {pulled} received.")
    for rel in conflicts:
        say(f"Conflict: {rel} changed here and on the server. The server's version is "
            f"{rel}{CONFLICT_SUFFIX}; delete it to keep yours, or replace yours with it.")
    return list(conflicts)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def run_remote(config: AppConfig, library_id: str, root: Path, argv: list[str]) -> int:
    """Sync, run `mdc <argv>` on the server, stream its output, sync back."""
    client = Client(config.server_url, config.server_token, library_id)
    try:
        if sync(client, root, library_id):
            print("Not running: resolve the conflicts first.")
            return 1
        if not argv:
            return 0
        names = [n for n in config.user_names if n != PROMPT_HEADING]

        def write(text: str) -> None:
            sys.stdout.write(text)
            sys.stdout.flush()

        done = client.run(argv, names, write)
        sync(client, root, library_id)
        return int(done["exit_code"])
    except RemoteError as exc:
        print(f"Error: {exc}")
        return 1


def remote_argv(raw: list[str], cwd: Path) -> list[str]:
    """The run's argv for the server: without --lib, file arguments as names."""
    argv: list[str] = []
    skip = False
    for arg in raw:
        if skip:
            skip = False
            continue
        if arg == "--lib":
            skip = True
            continue
        if arg.startswith("--lib="):
            continue
        candidate = Path(arg) if Path(arg).is_absolute() else cwd / arg
        argv.append(candidate.name if not arg.startswith("-") and candidate.is_file() else arg)
    return argv


def watch_remote(config: AppConfig, library_id: str, root: Path, argv: list[str], name: str) -> int:
    """reply --watch against the server: whenever the transcript has a pending
    turn, reply to it there. The watching is local; the replying is remote."""
    import time
    from mdc.transcript import TranscriptError, parse_transcript
    argv = [a for a in argv if a not in ("-W", "--watch")]
    found = [p for p in root.rglob(Path(name).name) if p.is_file()]
    if len(found) != 1:
        print(f"Error: '{name}' must name one file of the library.")
        return 1
    path = found[0]
    print(f"Watching {path.name}; replies run on {config.server_url}.")
    try:
        while True:
            try:
                pending = parse_transcript(path.read_text(encoding="utf-8")).pending
            except (TranscriptError, OSError):
                pending = False
            if pending:
                print("Change detected, replying...", flush=True)
                run_remote(config, library_id, root, argv)
            time.sleep(1)
    except KeyboardInterrupt:
        return 0
