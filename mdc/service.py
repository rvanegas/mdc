"""What mdc's HTTP API and its MCP server share: callers, libraries, locks.

Tokens are minted by scripts/add_token.py; users.toml keeps their SHA-256,
each with the user it belongs to, or "*" for a service (the nightly index).
A user reaches only their own libraries, $MDC_DATA_DIR/libraries/<user>/
<library>; one write at a time per library, whoever writes.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import re
import tomllib
from pathlib import Path

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

# One run or MCP write at a time per library (SERVER.md, Phases 2 and 5).
locks: dict[Path, asyncio.Lock] = {}


class LibraryError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def data_dir() -> Path:
    return Path(os.environ.get("MDC_DATA_DIR", "/data"))


def user_for_token(token: str) -> str | None:
    """The user a bearer token belongs to, "*" for a service, None if unknown."""
    return user_for_token_hash(hashlib.sha256(token.strip().encode()).hexdigest())


def user_for_token_hash(digest: str) -> str | None:
    """As user_for_token, given the token's SHA-256."""
    try:
        users = tomllib.loads((data_dir() / "users.toml").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    for entry in users.get("tokens", []):
        if hmac.compare_digest(str(entry.get("sha256", "")), digest):
            return str(entry["user"])
    return None


def library_root(user: str, library: str, who: str) -> Path:
    if who not in (user, "*"):
        raise LibraryError(403, "not your library")
    if not (NAME_RE.match(user) and NAME_RE.match(library)):
        raise LibraryError(404, "no such library")
    root = data_dir() / "libraries" / user / library
    if not root.is_dir():
        raise LibraryError(404, "no such library")
    return root


def library_ids(who: str) -> list[str]:
    """The ids of the libraries a caller reaches."""
    base = data_dir() / "libraries"
    if not base.is_dir():
        return []
    users = [base / who] if who != "*" else sorted(p for p in base.iterdir() if p.is_dir())
    return [
        f"{u.name}/{lib.name}"
        for u in users if u.is_dir()
        for lib in sorted(u.iterdir()) if lib.is_dir() and NAME_RE.match(lib.name)
    ]


def lock_for(root: Path) -> asyncio.Lock:
    return locks.setdefault(root, asyncio.Lock())
