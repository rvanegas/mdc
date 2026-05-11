from __future__ import annotations

import datetime
import difflib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path


MANIFEST_FILENAME = "MANIFEST.md"
INDEX_FILENAME = "INDEX.md"
KEYS_FILENAME = "KEYS.md"
REFERENCES_FILENAME = "REFERENCES.md"
RELATED_FILENAME = "RELATED.md"
from mdc.config import _state_dir as _mdc_state_dir
_STATE_DIR = _mdc_state_dir
_STATE_PATH = _STATE_DIR / "library-manifest.json"
_TERMS_STATE_PATH = _STATE_DIR / "library-index.json"
_RELATIONS_STATE_PATH = _STATE_DIR / "library-relations.json"

_REFS_SECTION_RE = re.compile(r"^## References\s*$", re.MULTILINE)
_RELATED_SECTION_RE = re.compile(r"^## Related\s*$", re.MULTILINE)
_NEXT_H2_RE = re.compile(r"^## ", re.MULTILINE)
_STRUCTURAL_HEADINGS = frozenset({"References", "Notes", "Related"})


@dataclass(frozen=True)
class DocEntry:
    rel_path: str
    title: str
    word_count: int
    summary: str
    terms: tuple[str, ...] = field(default_factory=tuple)
    indexed_at: float = 0.0  # Unix timestamp; used for cache invalidation
    refs: tuple[str, ...] | None = None      # None = not yet extracted
    related: tuple[str, ...] | None = None   # None = not yet extracted


def _extract_title(content: str, fallback: str) -> str:
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return fallback


def _fallback_summary(content: str) -> str:
    for line in content.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped
    return ""


def _word_count(content: str) -> int:
    return len(content.split())


def _extract_section(content: str, section_re: re.Pattern) -> tuple[str, ...]:
    m = section_re.search(content)
    if not m:
        return ()
    start = m.end()
    next_h = _NEXT_H2_RE.search(content, start)
    section = content[start: next_h.start() if next_h else len(content)]
    return tuple(line.strip() for line in section.splitlines() if line.strip())


def _extract_refs(content: str) -> tuple[str, ...]:
    lines = _extract_section(content, _REFS_SECTION_RE)
    return tuple(line[2:] if line.startswith("| ") else line for line in lines)


def _extract_related(content: str) -> tuple[str, ...]:
    return _extract_section(content, _RELATED_SECTION_RE)


def is_library_transcript(
    content: str,
    user_names: tuple[str, ...],
    llm_names: tuple[str, ...],
) -> bool:
    """Return True if content looks like a library transcript.

    Rules:
      1. Every ## heading is a single word.
      2. At least one heading (excluding structural ones) matches a known name.
    """
    from mdc.transcript import HEADING_RE
    headings = [m.group(2).strip() for m in HEADING_RE.finditer(content)]
    if not headings:
        return False
    if any(len(h.split()) != 1 for h in headings):
        return False
    known = frozenset(user_names) | frozenset(llm_names)
    return any(h in known for h in headings if h not in _STRUCTURAL_HEADINGS)


def extract_voice_sections(
    content: str,
    user_names: tuple[str, ...],
    llm_names: tuple[str, ...],
) -> dict[str, list[str]]:
    """Split a library transcript into sections labelled by voice.

    Returns {"user": [...], "llm": [...], "other": [...]}.
    Structural headings (References, Notes, Related) are excluded.
    """
    from mdc.transcript import HEADING_RE
    user_set = frozenset(user_names)
    llm_set = frozenset(llm_names)
    sections: dict[str, list[str]] = {"user": [], "llm": [], "other": []}
    matches = list(HEADING_RE.finditer(content))
    for idx, m in enumerate(matches):
        speaker = m.group(2).strip()
        if speaker in _STRUCTURAL_HEADINGS:
            continue
        body_start = m.end()
        body_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(content)
        body = content[body_start:body_end].strip()
        if not body:
            continue
        if speaker in user_set:
            sections["user"].append(body)
        elif speaker in llm_set:
            sections["llm"].append(body)
        else:
            sections["other"].append(body)
    return sections


def summary_target(word_count: int) -> str:
    if word_count < 1_000:   return "2-3 sentences"
    if word_count < 3_000:   return "4-6 sentences"
    if word_count < 6_000:   return "7-10 sentences"
    if word_count < 15_000:  return "11-16 sentences"
    return "17-25 sentences"


def terms_target(word_count: int) -> str:
    if word_count < 1_000:   return "5-8"
    if word_count < 3_000:   return "8-12"
    if word_count < 6_000:   return "12-18"
    if word_count < 15_000:  return "18-25"
    return "25-35"


def _md_escape(text: str) -> str:
    """Escape markdown special characters that would alter rendering in plain text."""
    return re.sub(r'([_*`\[\]\\])', r'\\\1', text)


def _normalize_initials(term: str) -> str:
    """Insert a space after a period that falls between two capital letters: J.L. → J. L."""
    return re.sub(r'(?<=[A-Z])\.(?=[A-Z])', '. ', term)


def write_manifest(library_path: Path, entries: list[DocEntry], timestamp: datetime.datetime) -> None:
    lines = [
        "",
        "# Manifest",
        timestamp.replace(microsecond=0).isoformat(),
        "",
        f"{len(entries)} document(s).",
        "",
    ]
    for e in entries:
        lines.append(_md_escape(e.rel_path))
        lines.append(f"{_md_escape(e.title)} - {e.word_count}w")
        lines.append("")
        if e.terms:
            lines.append("  " + "; ".join(_md_escape(t) for t in e.terms))
            lines.append("")
        if e.summary:
            lines.append("  " + _md_escape(e.summary))
        lines.append("")
        lines.append("---")
    (library_path / MANIFEST_FILENAME).write_text("\n".join(lines), encoding="utf-8")


def parse_keys_md(library_path: Path) -> tuple[dict[str, str], set[str], set[str]]:
    """Return ({alias: canonical}, canonicals, exclusions) from KEYS.md."""
    keys_path = library_path / KEYS_FILENAME
    if not keys_path.exists():
        return {}, set(), set()
    alias_map: dict[str, str] = {}
    canonicals: set[str] = set()
    exclusions: set[str] = set()
    section = ""
    current = ""
    for line in keys_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("## "):
            section = stripped[3:].strip().lower()
            current = ""
            continue
        if section == "plural":
            if stripped.endswith("s"):
                canonicals.add(stripped)
                alias_map[stripped[:-1]] = stripped
            else:
                canonicals.add(stripped)
                alias_map[stripped + "s"] = stripped
        elif section == "alias":
            if stripped.startswith("- "):
                alias = stripped[2:].strip()
                if current and alias:
                    alias_map[alias] = current
            else:
                current = stripped
                canonicals.add(current)
        elif section == "exclude":
            exclusions.add(stripped)
    return alias_map, canonicals, exclusions


def write_index(library_path: Path, entries: list[DocEntry]) -> list[str]:
    """Write an inverted term→files index as JSON and a markdown mirror.

    Returns a list of warning strings for irrelevant KEYS.md entries.
    """
    alias_map, canonicals, exclusions = parse_keys_md(library_path)

    # Map every lowercase form that should route to a canonical.
    canonical_for: dict[str, str] = {c.lower(): c for c in canonicals}
    for alias, canonical in alias_map.items():
        canonical_for[alias.lower()] = canonical

    # Auto-infer surname aliases: "Kant" → "Kant, Immanuel" when unambiguous.
    surname_to_canonical: dict[str, list[str]] = {}
    for c in canonicals:
        if "," in c:
            surname = c.split(",", 1)[0].strip()
            surname_to_canonical.setdefault(surname.lower(), []).append(c)
    for surname_lower, matches in surname_to_canonical.items():
        if len(matches) == 1 and surname_lower not in canonical_for:
            canonical_for[surname_lower] = matches[0]

    exclusions_lower = {e.lower() for e in exclusions}

    # Collect raw normalized terms to detect irrelevant KEYS.md entries.
    raw_terms: set[str] = set()
    for e in entries:
        for term in e.terms:
            raw_terms.add(_normalize_initials(term).lower())

    inverted: dict[str, list[dict[str, str]]] = {}
    for e in entries:
        for term in e.terms:
            term = _normalize_initials(term)
            if term.lower() in exclusions_lower:
                continue
            key = canonical_for.get(term.lower(), term)
            file_entry = {"rel_path": e.rel_path, "title": e.title}
            bucket = inverted.setdefault(key, [])
            if not any(f["rel_path"] == e.rel_path for f in bucket):
                bucket.append(file_entry)

    # JSON
    _TERMS_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "library_path": str(library_path),
        "terms": {t: files for t, files in sorted(inverted.items(), key=lambda x: x[0].casefold())},
    }
    _TERMS_STATE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")

    # Markdown mirror
    relations = load_relations(library_path)
    total = len(inverted)
    lines = ["", "# Index", "", f"{total} term(s).", ""]
    for term in sorted(inverted, key=str.casefold):
        lines.append(_md_escape(term))
        for f in sorted(inverted[term], key=lambda x: x["rel_path"]):
            lines.append(f"  {_md_escape(f['rel_path'])} — {_md_escape(f['title'])}")
        related = relations.get(term, [])
        if related:
            lines.append("  Related: " + "; ".join(_md_escape(r) for r in sorted(related, key=str.casefold)))
        lines.append("")
        lines.append("---")
    (library_path / INDEX_FILENAME).write_text("\n".join(lines), encoding="utf-8")

    # Report irrelevant KEYS.md entries.
    indexed_terms = {t.casefold() for t in inverted}
    warnings: list[str] = []
    for alias, canonical in alias_map.items():
        if (_normalize_initials(alias).lower() not in raw_terms
                and canonical.casefold() not in indexed_terms):
            warnings.append(f"KEYS.md ## Alias: alias '{alias}' not found in any document")
    for excl in exclusions:
        if excl.lower() not in raw_terms:
            warnings.append(f"KEYS.md ## Exclude: '{excl}' not found in any document")
    return warnings


def write_refs(library_path: Path, entries: list[DocEntry]) -> None:
    inverted: dict[str, list[tuple[str, str]]] = {}
    for e in entries:
        for ref in (e.refs or ()):
            bucket = inverted.setdefault(ref, [])
            if not any(p == e.rel_path for p, _ in bucket):
                bucket.append((e.rel_path, e.title))
    lines = ["", "# References", "", f"{len(inverted)} reference(s).", ""]
    for ref in sorted(inverted, key=str.casefold):
        lines.append(ref)
        for rel_path, title in sorted(inverted[ref]):
            lines.append(f"  {_md_escape(rel_path)} — {_md_escape(title)}")
        lines.append("")
        lines.append("---")
    (library_path / REFERENCES_FILENAME).write_text("\n".join(lines), encoding="utf-8")


def _parse_indexed_at(value: object) -> float:
    if not value:
        return 0.0
    try:
        return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _entry_to_dict(e: DocEntry) -> dict:
    return {
        "rel_path": e.rel_path,
        "title": e.title,
        "word_count": e.word_count,
        "indexed_at": datetime.datetime.fromtimestamp(e.indexed_at, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if e.indexed_at else None,
        "terms": list(e.terms),
        "summary": e.summary,
        "refs": list(e.refs) if e.refs is not None else None,
        "related": list(e.related) if e.related is not None else None,
    }


def _entry_from_dict(d: dict) -> DocEntry:
    refs = d.get("refs")
    related = d.get("related")
    return DocEntry(
        rel_path=d["rel_path"],
        title=d["title"],
        word_count=d.get("word_count", 0),
        summary=d["summary"],
        terms=tuple(d.get("terms", [])),
        indexed_at=_parse_indexed_at(d.get("indexed_at")),
        refs=tuple(refs) if refs is not None else None,
        related=tuple(related) if related is not None else None,
    )


def _load_state(library_path: Path) -> dict[str, DocEntry]:
    """Return entries_by_path from the JSON state file for library_path."""
    if not _STATE_PATH.exists():
        return {}
    try:
        data = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if data.get("library_path") != str(library_path):
        return {}
    entries_by_path: dict[str, DocEntry] = {}
    for item in data.get("entries", []):
        try:
            e = _entry_from_dict(item)
            entries_by_path[e.rel_path] = e
        except (KeyError, TypeError):
            pass
    return entries_by_path


def _save_state(library_path: Path, entries_by_path: dict[str, DocEntry]) -> None:
    _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "library_path": str(library_path),
        "entries": [_entry_to_dict(e) for e in entries_by_path.values()],
    }
    _STATE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_entries(library_path: Path) -> list[DocEntry]:
    """Load indexed entries from the JSON state file for the given library."""
    return list(_load_state(library_path).values())


def cooccurrence_relations(library_path: Path, min_count: int = 2) -> dict[str, list[str]]:
    """Return term pairs that co-occur in at least min_count documents."""
    entries = load_entries(library_path)
    from collections import defaultdict
    cooc: dict[tuple[str, str], int] = defaultdict(int)
    for entry in entries:
        terms = list(entry.terms)
        for i in range(len(terms)):
            for j in range(i + 1, len(terms)):
                pair = (min(terms[i], terms[j]), max(terms[i], terms[j]))
                cooc[pair] += 1
    result: dict[str, list[str]] = defaultdict(list)
    for (a, b), count in cooc.items():
        if count >= min_count:
            result[a].append(b)
            result[b].append(a)
    return dict(result)


def load_terms(library_path: Path) -> set[str]:
    """Load the set of term keys from the terms JSON state file."""
    if not _TERMS_STATE_PATH.exists():
        return set()
    try:
        data = json.loads(_TERMS_STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return set()
    if data.get("library_path") != str(library_path):
        return set()
    return set(data.get("terms", {}).keys())


def load_relations(library_path: Path) -> dict[str, list[str]]:
    """Load the relations map from the relations state file."""
    if not _RELATIONS_STATE_PATH.exists():
        return {}
    try:
        data = json.loads(_RELATIONS_STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if data.get("library_path") != str(library_path):
        return {}
    return data.get("relations", {})


def save_relations(library_path: Path, relations: dict[str, list[str]]) -> None:
    """Write the relations map to the relations state file."""
    _RELATIONS_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = {"library_path": str(library_path), "relations": relations}
    _RELATIONS_STATE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def prune_relations(library_path: Path, removed_terms: set[str]) -> None:
    """Remove deleted terms from the relations map."""
    if not removed_terms or not _RELATIONS_STATE_PATH.exists():
        return
    try:
        data = json.loads(_RELATIONS_STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    if data.get("library_path") != str(library_path):
        return
    relations: dict[str, list[str]] = data.get("relations", {})
    if not relations:
        return
    for term in removed_terms:
        relations.pop(term, None)
    for term in list(relations):
        relations[term] = [r for r in relations[term] if r not in removed_terms]
    data["relations"] = relations
    _RELATIONS_STATE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _sanitize_term(term: str) -> tuple[str, str | None]:
    """Replace ';' and ':' with ','. Returns (cleaned, warning_or_none)."""
    stripped = term.strip()
    cleaned = stripped.replace(";", ",").replace(":", ",").strip()
    if cleaned != stripped:
        return cleaned, f"term sanitized: \"{stripped}\" → \"{cleaned}\""
    return cleaned, None


def _build_slug_index(entries: list[DocEntry]) -> tuple[dict[str, str], list[str]]:
    from mdc.form import slugify
    slug_index: dict[str, str] = {}
    title_index: dict[str, str] = {}
    warnings: list[str] = []
    for e in entries:
        title_key = e.title.casefold()
        if title_key in title_index:
            warnings.append(f"Duplicate title: '{e.title}' in '{title_index[title_key]}' and '{e.rel_path}'")
        else:
            title_index[title_key] = e.rel_path
        slug = slugify(e.title)
        if slug in slug_index:
            warnings.append(f"Title slug collision: '{slug}' matches both '{slug_index[slug]}' and '{e.rel_path}'")
        else:
            slug_index[slug] = e.rel_path
    return slug_index, warnings


def write_related(library_path: Path, entries: list[DocEntry]) -> list[str]:
    from mdc.form import slugify
    slug_index, warnings = _build_slug_index(entries)
    title_by_path = {e.rel_path: e.title for e in entries}

    forward: dict[str, list[str]] = {}
    for e in entries:
        if not e.related:
            continue
        resolved = []
        for title in e.related:
            target = slug_index.get(slugify(title))
            if target is None:
                warnings.append(f"## Related in '{e.rel_path}': '{title}' not found in library")
            else:
                resolved.append(target)
        if resolved:
            forward[e.rel_path] = resolved

    backlinks: dict[str, list[str]] = {}
    for src, targets in forward.items():
        for tgt in targets:
            backlinks.setdefault(tgt, []).append(src)

    all_docs = sorted(set(forward) | set(backlinks))
    total = sum(len(v) for v in forward.values())
    lines = ["", "# Related", "", f"{total} cross-reference(s).", ""]
    for rel_path in all_docs:
        title = title_by_path.get(rel_path, rel_path)
        lines.append(f"{_md_escape(rel_path)} — {_md_escape(title)}")
        for tgt in sorted(forward.get(rel_path, [])):
            lines.append(f"  -> {_md_escape(tgt)} — {_md_escape(title_by_path.get(tgt, tgt))}")
        for src in sorted(backlinks.get(rel_path, [])):
            lines.append(f"  <- {_md_escape(src)} — {_md_escape(title_by_path.get(src, src))}")
        lines.append("")
        lines.append("---")
    (library_path / RELATED_FILENAME).write_text("\n".join(lines), encoding="utf-8")
    return warnings


def _finalize(library_path: Path, entries_by_path: dict[str, DocEntry], result: list[DocEntry]) -> list[str]:
    _save_state(library_path, entries_by_path)
    write_manifest(library_path, result, datetime.datetime.now())
    warnings = write_index(library_path, result)
    write_refs(library_path, result)
    warnings += write_related(library_path, result)
    return warnings


def build_index(
    library_path: Path,
    summarize: Callable[[str, int], tuple[str, list[str]]] | None = None,
    on_progress: Callable[[str, str], None] | None = None,
    on_warning: Callable[[str], None] | None = None,
    refs_only: bool = False,
) -> tuple[list[DocEntry], list[str]]:
    """Build or update MANIFEST.md and INDEX.md for library_path.

    summarize(content, word_count) -> (summary, terms)
    on_progress(rel_path, status) where status is 'cached' or 'indexed'
    """
    entries_by_path = _load_state(library_path)

    if refs_only:
        result = []
        for rel_path, existing in sorted(entries_by_path.items()):
            md_path = library_path / rel_path
            if not md_path.exists():
                continue
            refs: tuple[str, ...] = ()
            related: tuple[str, ...] = ()
            if md_path.stat().st_size <= 500_000:
                content = md_path.read_text(encoding="utf-8", errors="replace")
                refs = _extract_refs(content)
                related = _extract_related(content)
            updated = DocEntry(
                rel_path=existing.rel_path, title=existing.title,
                word_count=existing.word_count, summary=existing.summary,
                terms=existing.terms, indexed_at=existing.indexed_at,
                refs=refs,
                related=related,
            )
            result.append(updated)
            entries_by_path[rel_path] = updated
            if on_progress:
                on_progress(rel_path, "cached")
        return result, _finalize(library_path, entries_by_path, result)

    now_ts = datetime.datetime.now().timestamp()
    result = []

    for md_path in sorted(library_path.rglob("*.md")):
        if md_path.name in (MANIFEST_FILENAME, INDEX_FILENAME, KEYS_FILENAME, REFERENCES_FILENAME, RELATED_FILENAME):
            continue
        if (library_path / "REVISIONS") in md_path.parents:
            continue
        if re.search(r"--\d+\.md$", md_path.name):
            continue
        if md_path.name.endswith(".argument.md") or md_path.name.endswith(".chat.md"):
            continue
        rel_path = md_path.relative_to(library_path).as_posix()

        existing = entries_by_path.get(rel_path)
        if existing is not None and md_path.stat().st_mtime <= existing.indexed_at:
            if existing.refs is not None and existing.related is not None:
                result.append(existing)
                if on_progress:
                    on_progress(rel_path, "cached")
                continue
            # One-off: back-fill refs/related for entries cached before these fields existed.
            content = md_path.read_text(encoding="utf-8", errors="replace")
            updated = DocEntry(
                rel_path=existing.rel_path, title=existing.title,
                word_count=existing.word_count, summary=existing.summary,
                terms=existing.terms, indexed_at=existing.indexed_at,
                refs=existing.refs if existing.refs is not None else _extract_refs(content),
                related=existing.related if existing.related is not None else _extract_related(content),
            )
            result.append(updated)
            entries_by_path[rel_path] = updated
            _save_state(library_path, entries_by_path)
            if on_progress:
                on_progress(rel_path, "cached")
            continue

        if md_path.stat().st_size > 500_000:
            if on_progress:
                on_progress(rel_path, "skipped")
            continue

        content = md_path.read_text(encoding="utf-8", errors="replace")
        title = _extract_title(content, md_path.stem)
        wc = _word_count(content)

        if summarize:
            summary, raw_terms = summarize(content, wc)
        else:
            summary = _fallback_summary(content)
            raw_terms = []

        terms = []
        for t in raw_terms:
            clean, warn = _sanitize_term(t)
            if warn and on_warning:
                on_warning(warn)
            if clean:
                terms.append(clean)

        entry = DocEntry(
            rel_path=rel_path, title=title, word_count=wc,
            summary=summary, terms=tuple(terms), indexed_at=now_ts,
            refs=_extract_refs(content),
            related=_extract_related(content),
        )
        result.append(entry)
        if entry.summary and entry.terms:
            entries_by_path[rel_path] = entry
            _save_state(library_path, entries_by_path)
        write_manifest(library_path, result, datetime.datetime.now())
        if on_progress:
            on_progress(rel_path, "indexed")

    # Remove entries for files that no longer exist.
    final_by_path = {e.rel_path: e for e in result}
    return result, _finalize(library_path, final_by_path, result)


def render_manifest(entries: list[DocEntry]) -> str:
    lines = ["Available library documents:"]
    for e in entries:
        lines.append(f"{e.rel_path} — {e.title} — {e.word_count}w")
        if e.terms:
            lines.append("  " + "; ".join(e.terms))
        if e.summary:
            lines.append("  " + e.summary)
        lines.append("")
    return "\n".join(lines)


_DATE_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-")


def _normalize_to_slug(s: str) -> str:
    from mdc.form import slugify
    if s.lower().endswith(".md"):
        s = s[:-3]
    s = _DATE_PREFIX_RE.sub("", s)
    return slugify(s)


def resolve_title(library_path: Path, title: str) -> str | None:
    """Resolve a title, slug (with or without date/extension), or raw Related line to its rel_path."""
    from mdc.form import slugify
    query = _normalize_to_slug(title)
    for e in load_entries(library_path):
        if slugify(e.title) == query:
            return e.rel_path
        if _normalize_to_slug(e.rel_path) == query:
            return e.rel_path
    return None


def lookup_term(library_path: Path, term: str, exclude: str | None = None) -> str:
    """Look up a canonical index term; return matching docs and related terms."""
    if not _TERMS_STATE_PATH.exists():
        return f"Term not found: '{term}'"
    try:
        data = json.loads(_TERMS_STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return f"Term not found: '{term}'"
    if data.get("library_path") != str(library_path):
        return f"Term not found: '{term}'"
    term_map = data.get("terms", {})
    alias_map, _, _ = parse_keys_md(library_path)
    resolved = alias_map.get(term) or alias_map.get(term.casefold())
    if resolved is not None:
        term = resolved
    key = next((k for k in term_map if k.casefold() == term.casefold()), None)
    if key is None:
        all_terms = list(term_map.keys())
        folded_to_orig = {t.casefold(): t for t in all_terms}
        suggestions = difflib.get_close_matches(term.casefold(), folded_to_orig.keys(), n=5, cutoff=0.5)
        msg = f"Term not found: '{term}'."
        if suggestions:
            msg += " Similar index terms: " + "; ".join(folded_to_orig[s] for s in suggestions)
        return msg
    entries = _load_state(library_path)
    lines = [key]
    docs = sorted(term_map[key], key=lambda x: x["rel_path"])
    if exclude:
        docs = [d for d in docs if d["rel_path"] != exclude]
    if docs:
        lines.append("  Documents:")
        for d in docs:
            entry = entries.get(d["rel_path"])
            if entry:
                lines.append(f"    {entry.rel_path} — {entry.title} — {entry.word_count}w")
                if entry.summary:
                    lines.append(f"      {entry.summary}")
            else:
                lines.append(f"    {d['rel_path']} — {d['title']}")
    relations = load_relations(library_path)
    related = relations.get(key, [])
    if related:
        lines.append("  Related: " + "; ".join(sorted(related, key=str.casefold)))
    return "\n".join(lines)


def _get_summary(library_path: Path, rel_path: str, exclude: str | None = None) -> str:
    """Return the summary block for a document by relative path (used for pre-loading related docs)."""
    if exclude and rel_path == exclude:
        return f"'{rel_path}' is the document currently being replied to and is not available."
    entries = _load_state(library_path)
    entry = entries.get(rel_path)
    if entry is None:
        return f"No summary found for '{rel_path}'"
    lines = [f"{entry.rel_path} — {entry.title} — {entry.word_count}w"]
    if entry.terms:
        lines.append("  " + "; ".join(entry.terms))
    if entry.summary:
        lines.append("  " + entry.summary)
    return "\n".join(lines)


def read_document(library_path: Path, rel_path: str, exclude: str | None = None) -> str:
    if exclude and rel_path == exclude:
        return f"'{rel_path}' is the document currently being replied to and is not available."
    target = (library_path / rel_path).resolve()
    if not target.is_relative_to(library_path.resolve()):
        return f"Error: path '{rel_path}' is outside the library."
    if not target.exists():
        return f"Error: document '{rel_path}' not found in library."
    return target.read_text(encoding="utf-8", errors="replace")


def search_library(index: list[DocEntry], query: str) -> list[DocEntry]:
    tokens = set(re.findall(r"\w+", query.lower()))
    if not tokens:
        return []
    scored: list[tuple[int, DocEntry]] = []
    for entry in index:
        haystack = " ".join([entry.rel_path, entry.title, entry.summary, " ".join(entry.terms)]).lower()
        score = sum(1 for t in tokens if t in haystack)
        if score:
            scored.append((score, entry))
    scored.sort(key=lambda x: -x[0])
    return [e for _, e in scored[:5]]


LIBRARY_TOOLS = [
    {
        "name": "lookup_term",
        "description": "Look up an index term and get the matching documents with their summaries, plus related terms to follow up on. Use this to discover relevant prior writing; use read_document when you need the full text of a specific piece.",
        "input_schema": {
            "type": "object",
            "properties": {
                "term": {
                    "type": "string",
                    "description": "A canonical index term (case-insensitive).",
                }
            },
            "required": ["term"],
        },
    },
    {
        "name": "read_document",
        "description": "Read the full content of a library document.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path of the document.",
                }
            },
            "required": ["path"],
        },
    },
]
