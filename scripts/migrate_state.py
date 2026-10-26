#!/usr/bin/env python3
"""Rewrite a staged copy of mdc's home directories for a server workspace.

SERVER.md Phase 1, *Seeding Rodrigo's workspace*. An operator task, run on the
Mac against a staging copy before anything is sent, so the keys never leave:

    stage=$(mktemp -d)
    mkdir -p $stage/.config $stage/.local/state $stage/.cache
    cp -R ~/.config/mdc $stage/.config/
    cp -R ~/.local/state/mdc $stage/.local/state/
    cp -R ~/.cache/mdc $stage/.cache/
    python3 scripts/migrate_state.py $stage \\
        --old-root ~/Documents/Writing/Chats \\
        --new-root /data/workspaces/rodrigo/files

Then $stage becomes /srv/mdc/workspaces/rodrigo/home on the box. It:

- rewrites `library_path` in the library-*.json state files, which library.py
  ignores when it does not match the configured library;
- rewrites the asset caches' keys (absolute paths) from the old root to the new;
- drops stale review-*.json~ / .bak state;
- strips secrets from config.toml (the server takes them from its .env, and a
  key in config.toml would win over it), points `library_path` at the new root,
  sets `index_model` (the box has no Ollama) and drops `ollama_base_url`;
- deletes editor and backup copies in the config directory, which hold keys.

Stdlib only, idempotent.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

SECRET_KEYS = ("anthropic_api_key", "openai_api_key", "roxana_api_key", "dianoia_token")
DROPPED_KEYS = SECRET_KEYS + ("ollama_base_url",)
STATE_FILES = ("library-manifest.json", "library-index.json", "library-relations.json")
CACHE_FILES = ("anthropic-asset-cache.json", "asset-cache.json")
SERVER_INDEX_MODEL = "claude-haiku-4-5"


def _rebase(path: str, old_root: str, new_root: str) -> str:
    if path == old_root:
        return new_root
    if path.startswith(old_root + "/"):
        return new_root + path[len(old_root):]
    return path


def migrate_state(state_dir: Path, old_root: str, new_root: str) -> list[str]:
    done = []
    for name in STATE_FILES:
        path = state_dir / name
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        before = data.get("library_path")
        data["library_path"] = _rebase(str(before), old_root, new_root)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        done.append(f"{name}: library_path {before} → {data['library_path']}")
    for path in sorted(state_dir.glob("review-*")):
        if path.name.endswith(("~", ".bak")):
            path.unlink()
            done.append(f"removed {path.name}")
    return done


def migrate_cache(cache_dir: Path, old_root: str, new_root: str) -> list[str]:
    done = []
    for name in CACHE_FILES:
        path = cache_dir / name
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        rebased = {_rebase(key, old_root, new_root): value for key, value in data.items()}
        moved = sum(1 for key in data if _rebase(key, old_root, new_root) != key)
        path.write_text(json.dumps(rebased, indent=2, sort_keys=True), encoding="utf-8")
        done.append(f"{name}: {moved} of {len(data)} keys rebased")
    return done


def migrate_config(config_dir: Path, new_root: str) -> list[str]:
    done = []
    for path in sorted(config_dir.iterdir()):
        if path.is_file() and (path.name.endswith("~") or ".bak" in path.name):
            path.unlink()
            done.append(f"removed {path.name}")
    config = config_dir / "config.toml"
    if not config.exists():
        return done
    key_line = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")
    lines = []
    seen = set()
    for line in config.read_text(encoding="utf-8").splitlines():
        match = key_line.match(line)
        key = match.group(1) if match else None
        if key in DROPPED_KEYS:
            done.append(f"config.toml: dropped {key}")
            continue
        if key == "library_path":
            line = f'library_path = "{new_root}"'
        elif key == "index_model":
            line = f'index_model = "{SERVER_INDEX_MODEL}"'
        if key:
            seen.add(key)
        lines.append(line)
    if "library_path" not in seen:
        lines.append(f'library_path = "{new_root}"')
    if "index_model" not in seen:
        lines.append(f'index_model = "{SERVER_INDEX_MODEL}"')
    config.write_text("\n".join(lines) + "\n", encoding="utf-8")
    config.chmod(0o600)
    done.append(f"config.toml: library_path = {new_root}, index_model = {SERVER_INDEX_MODEL}")
    return done


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("home", type=Path, help="Staged home: holds .config/mdc, .local/state/mdc, .cache/mdc.")
    parser.add_argument("--old-root", required=True, help="The library's path on the Mac.")
    parser.add_argument("--new-root", required=True, help="The library's path in the container.")
    args = parser.parse_args(argv)

    old_root = str(Path(args.old_root).expanduser()).rstrip("/")
    new_root = args.new_root.rstrip("/")
    home = args.home.expanduser()

    report = []
    if (home / ".config/mdc").is_dir():
        report += migrate_config(home / ".config/mdc", new_root)
    if (home / ".local/state/mdc").is_dir():
        report += migrate_state(home / ".local/state/mdc", old_root, new_root)
    if (home / ".cache/mdc").is_dir():
        report += migrate_cache(home / ".cache/mdc", old_root, new_root)
    for line in report:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
