#!/usr/bin/env python3
"""Move a library's index state and the user's system prompt into the library.

The data migration for mdc's library layout (SERVER.md, *Where things live*).
Before it, index state was one set of files per home, stamped with the
library's path, and the reply system prompt began with the user's
~/.config/mdc/system.md. After it, a library carries both:

- ~/.local/state/mdc/library-{manifest,index,relations}.json move to
  <library>/.mdc/{manifest,index,relations}.json, without the path stamp;
  a state file stamped with another library is left alone;
- system.md (its "//" comment lines dropped) goes at the top of
  <library>/SYSTEM_PROMPT.md, and system.md and its editor backup are deleted;
- system_prompt_file, which nothing read, leaves config.toml.

    python3 scripts/migrate_library.py ~/Documents/Writing/Chats

Stdlib only, idempotent: run again, it finds nothing left to move.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

STATE_FILES = {
    "library-manifest.json": "manifest.json",
    "library-index.json": "index.json",
    "library-relations.json": "relations.json",
}


def migrate_state(library: Path, state_dir: Path) -> list[str]:
    done = []
    target_dir = library / ".mdc"
    for old_name, new_name in STATE_FILES.items():
        old = state_dir / old_name
        if not old.exists():
            continue
        data = json.loads(old.read_text(encoding="utf-8"))
        stamp = data.pop("library_path", None)
        if stamp != str(library):
            done.append(f"left {old_name}: it belongs to {stamp}")
            continue
        new = target_dir / new_name
        if new.exists():
            raise SystemExit(f"✗ {new} already exists; not overwriting it with {old}")
        target_dir.mkdir(exist_ok=True)
        new.write_text(json.dumps(data, indent=2), encoding="utf-8")
        old.unlink()
        done.append(f"moved {old_name} → {new.relative_to(library)}")
    leftovers = sorted(p.name for p in state_dir.iterdir()) if state_dir.is_dir() else []
    if state_dir.is_dir() and not leftovers:
        state_dir.rmdir()
        done.append(f"removed the empty {state_dir}")
    elif leftovers:
        done.append(f"still in {state_dir} (mdc no longer reads it): {', '.join(leftovers)}")
    return done


def migrate_system_prompt(library: Path, config_dir: Path) -> list[str]:
    done = []
    system = config_dir / "system.md"
    if system.exists():
        lines = system.read_text(encoding="utf-8").splitlines()
        user_prompt = "\n".join(line for line in lines if not line.startswith("//")).strip()
        if user_prompt:
            lib_prompt = library / "SYSTEM_PROMPT.md"
            existing = lib_prompt.read_text(encoding="utf-8").strip() if lib_prompt.exists() else ""
            if user_prompt not in existing:
                lib_prompt.write_text(f"{user_prompt}\n\n{existing}\n" if existing else f"{user_prompt}\n", encoding="utf-8")
                done.append(f"put system.md at the top of {lib_prompt.name}")
        system.unlink()
        done.append("removed system.md")
    backup = config_dir / "system.md~"
    if backup.exists():
        backup.unlink()
        done.append("removed system.md~")

    config = config_dir / "config.toml"
    if config.exists():
        text = config.read_text(encoding="utf-8")
        kept = [line for line in text.splitlines() if not re.match(r"^\s*system_prompt_file\s*=", line)]
        if len(kept) != len(text.splitlines()):
            config.write_text("\n".join(kept) + "\n", encoding="utf-8")
            done.append("dropped system_prompt_file from config.toml")
    return done


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("library", type=Path, help="The library directory.")
    parser.add_argument("--state-dir", type=Path, default=Path("~/.local/state/mdc"))
    parser.add_argument("--config-dir", type=Path, default=Path("~/.config/mdc"))
    args = parser.parse_args(argv)

    library = args.library.expanduser().resolve()
    if not library.is_dir():
        raise SystemExit(f"✗ {library} is not a directory")
    report = migrate_state(library, args.state_dir.expanduser())
    report += migrate_system_prompt(library, args.config_dir.expanduser())
    for line in report or ["nothing to migrate"]:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
