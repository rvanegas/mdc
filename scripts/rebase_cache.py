#!/usr/bin/env python3
"""Copy an asset cache with its keys moved from one library root to another.

The asset caches are keyed by each asset's absolute path. Seeding a server
library from the Mac (SERVER.md, *Seeding*) carries the cache's remote file
IDs across, which stay valid because the server uses the same API key:

    python3 scripts/rebase_cache.py ~/.cache/mdc/anthropic-asset-cache.json out.json \\
        --old-root ~/Documents/Writing/Chats \\
        --new-root /data/libraries/rodvandur/philosophy

Entries outside the old root are dropped: they name files the server lacks.
Stdlib only.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def rebase(entries: dict, old_root: str, new_root: str) -> dict:
    return {
        new_root + key[len(old_root):]: value
        for key, value in entries.items()
        if key.startswith(old_root + "/")
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", type=Path)
    parser.add_argument("dest", type=Path)
    parser.add_argument("--old-root", required=True)
    parser.add_argument("--new-root", required=True)
    args = parser.parse_args(argv)

    old_root = str(Path(args.old_root).expanduser()).rstrip("/")
    new_root = args.new_root.rstrip("/")
    entries = json.loads(args.source.expanduser().read_text(encoding="utf-8"))
    rebased = rebase(entries, old_root, new_root)
    args.dest.write_text(json.dumps(rebased, indent=2, sort_keys=True), encoding="utf-8")
    print(f"{len(rebased)} of {len(entries)} entries rebased; {len(entries) - len(rebased)} outside {old_root} dropped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
