#!/usr/bin/env python3
"""Put a library into its name-derived layout (SERVER.md, *Library layout*).

Names are unique across a library, so a dated file names its role and lives
in the year directory its name gives, links name assets bare, and assets are
dated like documents. Decided 2026-10-26 for rodvandur/philosophy. This:

1. moves editor backups (`*~`) to the Trash;
2. names each undated asset a source, dated by the earliest document linking
   it, its old name the slug: `parfit2011.pdf` → `2026-10-19-parfit2011.source.pdf`;
3. gives each document of TOPICS' "related" groups a Related entry for every
   member before it, so the last relates them all (a Related title must
   precede its document: `mdc validate`);
4. writes a hub document for each of TOPICS' "hub" groups, dated by its last
   member so that all of them precede it, whose Related lists the group (the
   folder kept the relation; the hub keeps it now);
5. names the role of every bare dated file: a markdown file is a chat or a
   document by mdc's old guess from its headings (`is_library_transcript`,
   made once here), any other format takes the role of its stem's document;
6. rewrites the links to renamed files;
7. moves every file to its name and year directory in one step, carrying the
   index state (library.move_documents), and removes the emptied folders;
8. re-keys the asset cache by content (mdc/asset_cache.py) and drops the
   retired OpenAI and Ollama settings from config.toml.

Prints the plan; changes nothing without --apply.

    python3 scripts/migrate_layout.py ~/Documents/Writing/Chats
    python3 scripts/migrate_layout.py ~/Documents/Writing/Chats --apply
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
from pathlib import Path

from mdc.config import load_config
from mdc.form import slugify
from mdc.library import _extract_related, is_library_transcript, lacks_role, move_documents, role_of
from mdc.transcript import update_related_section

TOPICS = [
    {
        "folder": "2026/_phil184",
        "kind": "related",  # a short seminar, the same participants throughout
    },
    {
        "folder": "2025/mmaa",
        "kind": "hub",
        "date": "2025-06-22",
        "title": "Sources for Money and Marriage by Authoritative Assertion",
        "text": (
            "Source material for *Money and Marriage by Authoritative Assertion*"
            " (2025-05-19), which does not reference it."
        ),
    },
]

RETIRED_CONFIG_KEYS = ("openai_api_key", "ollama_base_url")

_DATED_RE = re.compile(r"^(\d{4})-\d{2}-\d{2}-")
_LINK_RE = re.compile(r"\]\(([^)\s/]+)\)")
_SKIP_DIRS = {"REVISIONS", ".mdc"}
_UNSORTED_DIRS = {"REVISIONS", "ASSESSMENTS", ".mdc"}


def _files(library: Path):
    for path in sorted(library.rglob("*")):
        if path.is_file() and not _SKIP_DIRS.intersection(path.relative_to(library).parts):
            yield path


def _title(path: Path) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    raise SystemExit(f"✗ {path} has no '# Title'")


def _members(directory: Path) -> list[Path]:
    """The documents of a topic folder, by name (so by date): not reviews or companions."""
    return sorted(p for p in directory.glob("*.md") if role_of(p.name) in (None, "document"))


def trash_backups(library: Path, trash: Path, apply: bool) -> None:
    for path in _files(library):
        if path.name.endswith("~"):
            print(f"  {path.relative_to(library)} → Trash")
            if apply:
                trash.mkdir(parents=True, exist_ok=True)
                path.rename(trash / path.name)


def plan_sources(library: Path) -> dict[Path, str]:
    linked: dict[str, list[str]] = {}
    for doc in _files(library):
        if doc.suffix == ".md" and (m := re.match(r"^(\d{4}-\d{2}-\d{2})-", doc.name)):
            for name in _LINK_RE.findall(doc.read_text(encoding="utf-8")):
                linked.setdefault(name, []).append(m.group(1))
    names = {}
    for asset in _files(library):
        if asset.suffix == ".md" or asset.name == ".DS_Store" or asset.name.endswith("~") or _DATED_RE.match(asset.name):
            continue
        if _UNSORTED_DIRS.intersection(asset.relative_to(library).parts):
            continue
        dates = sorted(linked.get(asset.name, []))
        if not dates:
            print(f"  ! {asset.relative_to(library)}: undated and linked from no dated document; left as is")
            continue
        stem = re.sub(r"(?<=[a-z])(?=[A-Z])", "-", asset.stem)
        names[asset] = f"{dates[0]}-{slugify(stem)}.source{asset.suffix.lower()}"
    return names


def relate_to_preceding(folder: Path, apply: bool) -> None:
    docs = _members(folder)
    for i, doc in enumerate(docs):
        text = doc.read_text(encoding="utf-8")
        existing = list(_extract_related(text))
        added = [f"| *{_title(d)}*" for d in docs[:i] if f"| *{_title(d)}*" not in existing]
        print(f"  {doc.name}: + {len(added)} related")
        if apply and added:
            doc.write_text(update_related_section(text, existing + added), encoding="utf-8")


def write_hub(library: Path, folder: Path, topic: dict, apply: bool) -> None:
    hub = library / topic["date"][:4] / f"{topic['date']}-{slugify(topic['title'])}.document.md"
    members = _members(folder)
    related = "\n".join(f"| *{_title(d)}*" for d in members)
    text = f"\n# {topic['title']}\n{topic['date']}\n\n{topic['text']}\n\n## Related\n\n{related}\n"
    print(f"  hub {hub.relative_to(library)} relating {len(members)} documents")
    if apply:
        if hub.exists():
            raise SystemExit(f"✗ {hub} already exists")
        hub.write_text(text, encoding="utf-8")


def plan_roles(library: Path) -> dict[Path, str]:
    config = load_config()
    names: dict[Path, str] = {}
    md_roles: dict[tuple[Path, str], str] = {}
    unnamed = [p for p in _files(library) if lacks_role(p.name) and not p.name.endswith("~")]
    for path in unnamed:
        if path.suffix == ".md":
            text = path.read_text(encoding="utf-8", errors="replace")
            role = "chat" if is_library_transcript(text, config.user_names, config.llm_names) else "document"
            md_roles[(path.parent, path.name.split(".")[0])] = role
            names[path] = f"{path.stem}.{role}.md"
    for path in unnamed:
        if path.suffix != ".md":
            role = md_roles.get((path.parent, path.name.split(".")[0]))
            if role is None:
                print(f"  ! {path.relative_to(library)}: no document of its stem to take a role from; left as is")
                continue
            names[path] = f"{path.stem}.{role}{path.suffix}"
    return names


def rewrite_links(library: Path, renames: dict[str, str], apply: bool) -> None:
    for doc in _files(library):
        if doc.suffix != ".md":
            continue
        text = doc.read_text(encoding="utf-8")
        new_text = _LINK_RE.sub(lambda m: f"]({renames.get(m.group(1), m.group(1))})", text)
        if new_text != text:
            print(f"  links in {doc.relative_to(library)}")
            if apply:
                doc.write_text(new_text, encoding="utf-8")


def plan_moves(library: Path, names: dict[Path, str]) -> dict[Path, Path]:
    moves = {}
    for path in _files(library):
        if path.name.endswith("~") or path.name == ".DS_Store":
            continue
        name = names.get(path, path.name)
        directory = path.parent
        if not _UNSORTED_DIRS.intersection(path.relative_to(library).parts[:-1]):
            if m := _DATED_RE.match(name):
                directory = library / m.group(1)
        if directory / name != path:
            moves[path] = directory / name
    return moves


def remove_empty_dirs(library: Path, apply: bool) -> None:
    for d in sorted((p for p in library.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        if _SKIP_DIRS.intersection(d.relative_to(library).parts):
            continue
        if not [p for p in d.iterdir() if p.name != ".DS_Store"]:
            print(f"  remove {d.relative_to(library)}/")
            if apply:
                (d / ".DS_Store").unlink(missing_ok=True)
                d.rmdir()


def rekey_cache(path: Path, apply: bool) -> None:
    if not path.exists():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    if all("sha256" not in v for v in data.values()):
        return  # already by content
    rekeyed = {
        v["sha256"]: {"file_id": v["file_id"], "kind": v["kind"], "size": v["size"], "path": key}
        for key, v in data.items()
    }
    print(f"  {path.name}: {len(data)} entries by path → {len(rekeyed)} by content")
    if apply:
        path.write_text(json.dumps(rekeyed, indent=2, sort_keys=True), encoding="utf-8")


def drop_retired_settings(config: Path, apply: bool) -> None:
    if not config.exists():
        return
    lines = config.read_text(encoding="utf-8").splitlines()
    retired = re.compile(rf"^\s*({'|'.join(RETIRED_CONFIG_KEYS)})\s*=")
    kept = [line for line in lines if not retired.match(line)]
    if len(kept) != len(lines):
        print(f"  {config.name}: drop {', '.join(k for k in RETIRED_CONFIG_KEYS if any(re.match(rf'^\s*{k}\s*=', l) for l in lines))}")
        if apply:
            config.write_text("\n".join(kept) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("library", type=Path)
    parser.add_argument("--cache-dir", type=Path, default=Path("~/.cache/mdc"))
    parser.add_argument("--config-dir", type=Path, default=Path("~/.config/mdc"))
    parser.add_argument("--trash", type=Path, default=Path(f"~/.Trash/mdc-{datetime.date.today().isoformat()}"))
    parser.add_argument("--apply", action="store_true", help="Make the changes; without it, only print them.")
    args = parser.parse_args(argv)
    library = args.library.expanduser().resolve()
    apply = args.apply

    print("1. editor backups")
    trash_backups(library, args.trash.expanduser(), apply)

    print("2. sources")
    names = plan_sources(library)
    for old, new in names.items():
        print(f"  {old.relative_to(library)} → {new}")

    print("3-4. topics")
    for topic in TOPICS:
        folder = library / topic["folder"]
        if not folder.is_dir():
            print(f"  {topic['folder']}: gone (already migrated)")
        elif topic["kind"] == "related":
            relate_to_preceding(folder, apply)
        else:
            write_hub(library, folder, topic, apply)

    print("5. roles")
    roles = plan_roles(library)
    counts: dict[str, int] = {}
    for new in roles.values():
        key = ".".join(new.split(".")[-2:])
        counts[key] = counts.get(key, 0) + 1
    for key, n in sorted(counts.items()):
        print(f"  {n} → .{key}")
    names.update(roles)

    print("6. links")
    rewrite_links(library, {old.name: new for old, new in names.items()}, apply)

    print("7. names and year directories")
    moves = plan_moves(library, names)
    print(f"  {len(moves)} files: {sum(1 for o, n in moves.items() if o.parent != n.parent)} change directory,"
          f" {sum(1 for o, n in moves.items() if o.name != n.name)} change name")
    if apply and moves:
        move_documents(library, moves)
    remove_empty_dirs(library, apply)

    print("8. cache and config")
    rekey_cache(args.cache_dir.expanduser() / "anthropic-asset-cache.json", apply)
    obsolete = args.cache_dir.expanduser() / "asset-cache.json"
    if obsolete.exists():
        print(f"  remove {obsolete.name} (OpenAI's, retired)")
        if apply:
            obsolete.unlink()
    drop_retired_settings(args.config_dir.expanduser() / "config.toml", apply)

    print("applied" if apply else "plan only; run again with --apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
