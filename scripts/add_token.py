#!/usr/bin/env python3
"""Mint a bearer token for mdc's API and record its hash in users.toml.

An operator task (SERVER.md, Phase 2). The token is printed once and stored
nowhere; users.toml keeps only its SHA-256. "*" is a service: it reaches
every library (the nightly index).

    docker compose run --rm mdc python scripts/add_token.py rodvandur --note "Mac CLI"

Stdlib only.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import secrets
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("user", help='a user name, or "*" for a service')
    parser.add_argument("--note", default="", help="what the token is for")
    parser.add_argument("--users", type=Path, default=Path(os.environ.get("MDC_DATA_DIR", "/data")) / "users.toml")
    args = parser.parse_args(argv)
    if args.user != "*" and not re.match(r"^[a-z0-9][a-z0-9_-]*$", args.user):
        raise SystemExit(f"✗ not a user name: {args.user!r}")
    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode()).hexdigest()
    note = args.note.replace('"', "'")
    entry = f'\n[[tokens]]\nuser = "{args.user}"\nsha256 = "{digest}"\nnote = "{note}"\n'
    args.users.parent.mkdir(parents=True, exist_ok=True)
    with args.users.open("a", encoding="utf-8") as handle:
        handle.write(entry)
    args.users.chmod(0o600)
    print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
