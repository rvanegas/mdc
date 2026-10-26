#!/usr/bin/env python3
"""Put a library into its name-derived layout (SERVER.md, *Library layout*).

Names are unique across a library, so a dated file lives in the year
directory its name gives, links name assets bare, and assets are dated like
documents. Decided 2026-10-26 for rodvandur/philosophy. This:

1. names each undated asset after the earliest document linking it (its
   date) and its old name (the slug), rewriting those links;
2. gives each document of TOPICS' "related" groups a Related entry for every
   member before it, so the last relates them all (a Related title must
   precede its document: `mdc validate`);
3. writes a hub document for each of TOPICS' "hub" groups, dated by its last
   member so that all of them precede it, whose Related lists the group (the
   folder kept the relation; the hub keeps it now);
4. moves every misplaced dated file into its year directory, carrying the
   index state (library.move_documents), and removes the emptied folders;
5. re-keys the asset caches by content (mdc/asset_cache.py).

Prints the plan; changes nothing without --apply.

    python3 scripts/migrate_layout.py ~/Documents/Writing/Chats
    python3 scripts/migrate_layout.py ~/Documents/Writing/Chats --apply
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from mdc.form import slugify
from mdc.library import _extract_related, misplaced, move_documents
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

_DATED_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-")
_LINK_RE = re.compile(r"\]\(([^)\s/]+)\)")
_SKIP_DIRS = {"REVISIONS", ".mdc"}
_UNSORTED_DIRS = {"REVISIONS", "ASSESSMENTS", ".mdc"}


def _files(library: Path):
    for path in sorted(library.rglob("*")):
        if path.is_file() and not _SKIP_DIRS.intersection(path.relative_to(library).parts):
            yield path


def _documents(library: Path) -> list[Path]:
    return [p for p in _files(library) if p.suffix == ".md"]


def _title(path: Path) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    raise SystemExit(f"✗ {path} has no '# Title'")


def _doc_stems(directory: Path) -> list[Path]:
    """The documents of a folder: not their reviews or companions."""
    return sorted(
        p for p in directory.glob("*.md")
        if not p.name.endswith((".review.md", ".argument.md", ".analysis.md", ".chat.md"))
    )


def plan_asset_names(library: Path) -> dict[Path, Path]:
    docs = _documents(library)
    linked: dict[str, list[Path]] = {}
    for doc in docs:
        for name in _LINK_RE.findall(doc.read_text(encoding="utf-8")):
            linked.setdefault(name, []).append(doc)
    renames = {}
    for asset in _files(library):
        if asset.suffix == ".md" or asset.name == ".DS_Store" or _DATED_RE.match(asset.name):
            continue
        if _UNSORTED_DIRS.intersection(asset.relative_to(library).parts):
            continue
        dates = sorted(m.group(1) for d in linked.get(asset.name, []) if (m := _DATED_RE.match(d.name)))
        if not dates:
            print(f"  ! {asset.relative_to(library)}: undated and linked from no dated document; left as is")
            continue
        stem = re.sub(r"(?<=[a-z])(?=[A-Z])", "-", asset.stem)
        renames[asset] = asset.with_name(f"{dates[0]}-{slugify(stem)}{asset.suffix.lower()}")
    return renames


def rewrite_links(library: Path, renames: dict[Path, Path], apply: bool) -> None:
    by_name = {old.name: new.name for old, new in renames.items()}
    for doc in _documents(library):
        text = doc.read_text(encoding="utf-8")
        new_text = _LINK_RE.sub(lambda m: f"]({by_name.get(m.group(1), m.group(1))})", text)
        if new_text != text:
            print(f"  links in {doc.relative_to(library)}")
            if apply:
                doc.write_text(new_text, encoding="utf-8")


def relate_to_preceding(library: Path, folder: Path, apply: bool) -> None:
    docs = _doc_stems(folder)  # by name, so by date: the order validate checks
    for i, doc in enumerate(docs):
        text = doc.read_text(encoding="utf-8")
        existing = list(_extract_related(text))
        added = [f"| *{_title(d)}*" for d in docs[:i] if f"| *{_title(d)}*" not in existing]
        print(f"  {doc.name}: + {len(added)} related")
        if apply and added:
            doc.write_text(update_related_section(text, existing + added), encoding="utf-8")


def write_hub(library: Path, folder: Path, topic: dict, apply: bool) -> None:
    year = topic["date"][:4]
    hub = library / year / f"{topic['date']}-{slugify(topic['title'])}.md"
    related = "\n".join(f"| *{_title(d)}*" for d in _doc_stems(folder))
    text = f"\n# {topic['title']}\n{topic['date']}\n\n{topic['text']}\n\n## Related\n\n{related}\n"
    print(f"  hub {hub.relative_to(library)} relating {len(_doc_stems(folder))} documents")
    if apply:
        if hub.exists():
            raise SystemExit(f"✗ {hub} already exists")
        hub.write_text(text, encoding="utf-8")


def plan_moves(library: Path) -> dict[Path, Path]:
    return {p: target for p in _files(library) if (target := misplaced(library, p))}


def remove_empty_dirs(library: Path, apply: bool) -> None:
    for d in sorted((p for p in library.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        if _SKIP_DIRS.intersection(d.relative_to(library).parts):
            continue
        rest = [p for p in d.iterdir() if p.name != ".DS_Store"]
        if not rest:
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("library", type=Path)
    parser.add_argument("--cache-dir", type=Path, default=Path("~/.cache/mdc"))
    parser.add_argument("--apply", action="store_true", help="Make the changes; without it, only print them.")
    args = parser.parse_args(argv)
    library = args.library.expanduser().resolve()
    apply = args.apply

    print("1. asset names")
    renames = plan_asset_names(library)
    for old, new in renames.items():
        print(f"  {old.relative_to(library)} → {new.name}")
    rewrite_links(library, renames, apply)
    if apply:
        for old, new in renames.items():
            old.rename(new)

    print("2-3. topics")
    for topic in TOPICS:
        folder = library / topic["folder"]
        if not folder.is_dir():
            print(f"  {topic['folder']}: gone (already migrated)")
            continue
        if topic["kind"] == "related":
            relate_to_preceding(library, folder, apply)
        else:
            write_hub(library, folder, topic, apply)

    print("4. year directories")
    moves = plan_moves(library)
    for old, new in moves.items():
        print(f"  {old.relative_to(library)} → {new.parent.relative_to(library)}/")
    if apply and moves:
        move_documents(library, moves)
    remove_empty_dirs(library, apply)

    print("5. asset caches")
    for name in ("anthropic-asset-cache.json", "asset-cache.json"):
        rekey_cache(args.cache_dir.expanduser() / name, apply)

    print("applied" if apply else "plan only; run again with --apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
