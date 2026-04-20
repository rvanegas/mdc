from __future__ import annotations

import datetime
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path


MANIFEST_FILENAME = "MANIFEST.md"
INDEX_FILENAME = "INDEX.md"
KEYS_FILENAME = "KEYS.md"
_STATE_PATH = Path("~/.local/state/mdc/library-manifest.json").expanduser()
_TERMS_STATE_PATH = Path("~/.local/state/mdc/library-index.json").expanduser()
_RELATIONS_STATE_PATH = Path("~/.local/state/mdc/library-relations.json").expanduser()


@dataclass(frozen=True)
class DocEntry:
    rel_path: str
    title: str
    word_count: int
    summary: str
    terms: tuple[str, ...] = field(default_factory=tuple)
    indexed_at: float = 0.0  # Unix timestamp; used for cache invalidation


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
        "# Index",
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


def parse_keys_md(library_path: Path) -> tuple[dict[str, str], set[str], set[str], dict[str, list[str]]]:
    """Return ({alias: canonical}, canonicals, exclusions, {parent: [subterms]}) from KEYS.md."""
    keys_path = library_path / KEYS_FILENAME
    if not keys_path.exists():
        return {}, set(), set(), {}
    alias_map: dict[str, str] = {}
    canonicals: set[str] = set()
    exclusions: set[str] = set()
    groups: dict[str, list[str]] = {}
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
        elif section == "group":
            if stripped.startswith("- "):
                subterm = stripped[2:].strip()
                if current and subterm:
                    groups.setdefault(current, []).append(subterm)
            else:
                current = stripped
    return alias_map, canonicals, exclusions, groups


def write_index(library_path: Path, entries: list[DocEntry]) -> list[str]:
    """Write an inverted term→files index as JSON and a markdown mirror.

    Returns a list of warning strings for irrelevant KEYS.md entries.
    """
    alias_map, canonicals, exclusions, groups = parse_keys_md(library_path)

    # Validate: subterms in Group must not be non-canonical aliases in Alias.
    for parent, subterms in groups.items():
        for subterm in subterms:
            if subterm in alias_map:
                raise ValueError(
                    f"KEYS.md: '{subterm}' is a subentry under '{parent}' in ## Group "
                    f"but is also a non-canonical alias for '{alias_map[subterm]}' in ## Alias."
                )

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

    # Resolve group parents and subterms through Alias canonicals.
    resolved_groups: dict[str, list[str]] = {}
    for parent, subterms in groups.items():
        rparent = canonical_for.get(parent.lower(), parent)
        resolved_groups[rparent] = [canonical_for.get(s.lower(), s) for s in subterms]

    # Subterms that appear under a group parent are suppressed from top-level.
    grouped_subterms: set[str] = {s for subs in resolved_groups.values() for s in subs}

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
        "groups": {p: subs for p, subs in sorted(resolved_groups.items(), key=lambda x: x[0].casefold())},
        "terms": {t: files for t, files in sorted(inverted.items(), key=lambda x: x[0].casefold())},
    }
    _TERMS_STATE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")

    # Markdown mirror
    relations = load_relations(library_path)
    top_level = [t for t in sorted(inverted, key=str.casefold) if t not in grouped_subterms]
    total = len(inverted)
    lines = ["", "# Terms", "", f"{total} term(s).", ""]
    for term in top_level:
        lines.append(_md_escape(term))
        for f in sorted(inverted[term], key=lambda x: x["rel_path"]):
            lines.append(f"  {_md_escape(f['rel_path'])} — {_md_escape(f['title'])}")
        related = relations.get(term, [])
        if related:
            lines.append("  Related: " + "; ".join(_md_escape(r) for r in sorted(related, key=str.casefold)))
        subterms = resolved_groups.get(term, [])
        if subterms:
            lines.append("")
        for subterm in sorted(subterms, key=str.casefold):
            if subterm not in inverted:
                continue
            lines.append(f"  {_md_escape(subterm)}")
            for f in sorted(inverted[subterm], key=lambda x: x["rel_path"]):
                lines.append(f"    {_md_escape(f['rel_path'])} — {_md_escape(f['title'])}")
            sub_related = relations.get(subterm, [])
            if sub_related:
                lines.append("    Related: " + "; ".join(_md_escape(r) for r in sorted(sub_related, key=str.casefold)))
            lines.append("")
        if not subterms:
            lines.append("")
        lines.append("---")
    (library_path / INDEX_FILENAME).write_text("\n".join(lines), encoding="utf-8")

    # Report irrelevant KEYS.md entries.
    warnings: list[str] = []
    for alias in alias_map:
        if _normalize_initials(alias).lower() not in raw_terms:
            warnings.append(f"KEYS.md ## Alias: alias '{alias}' not found in any document")
    for excl in exclusions:
        if excl.lower() not in raw_terms:
            warnings.append(f"KEYS.md ## Exclude: '{excl}' not found in any document")
    for subterms in resolved_groups.values():
        for subterm in subterms:
            if subterm not in inverted:
                warnings.append(f"KEYS.md ## Group: subterm '{subterm}' not found in index")
    return warnings


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
    }


def _entry_from_dict(d: dict) -> DocEntry:
    return DocEntry(
        rel_path=d["rel_path"],
        title=d["title"],
        word_count=d.get("word_count", 0),
        summary=d["summary"],
        terms=tuple(d.get("terms", [])),
        indexed_at=_parse_indexed_at(d.get("indexed_at")),
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


def build_index(
    library_path: Path,
    summarize: Callable[[str, int], tuple[str, list[str]]] | None = None,
    on_progress: Callable[[str, str], None] | None = None,
    on_warning: Callable[[str], None] | None = None,
) -> tuple[list[DocEntry], list[str]]:
    """Build or update MANIFEST.md and INDEX.md for library_path.

    summarize(content, word_count) -> (summary, terms)
    on_progress(rel_path, status) where status is 'cached' or 'indexed'
    """
    entries_by_path = _load_state(library_path)
    now_ts = datetime.datetime.now().timestamp()
    result: list[DocEntry] = []

    for md_path in sorted(library_path.rglob("*.md")):
        if md_path.name in (MANIFEST_FILENAME, INDEX_FILENAME, KEYS_FILENAME):
            continue
        rel_path = str(md_path.relative_to(library_path))

        existing = entries_by_path.get(rel_path)
        if existing is not None and md_path.stat().st_mtime <= existing.indexed_at:
            result.append(existing)
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
    _save_state(library_path, final_by_path)
    write_manifest(library_path, result, datetime.datetime.now())
    warnings = write_index(library_path, result)
    return result, warnings


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
    alias_map, _, _, _ = parse_keys_md(library_path)
    resolved = alias_map.get(term) or alias_map.get(term.casefold())
    if resolved is not None:
        term = resolved
    key = next((k for k in term_map if k.casefold() == term.casefold()), None)
    if key is None:
        return f"Term not found: '{term}'"
    lines = [key]
    docs = sorted(term_map[key], key=lambda x: x["rel_path"])
    if exclude:
        docs = [d for d in docs if d["rel_path"] != exclude]
    if docs:
        lines.append("  Documents:")
        for d in docs:
            lines.append(f"    {d['rel_path']} — {d['title']}")
    relations = load_relations(library_path)
    related = relations.get(key, [])
    if related:
        lines.append("  Related: " + "; ".join(sorted(related, key=str.casefold)))
    return "\n".join(lines)


def get_summary(library_path: Path, rel_path: str, exclude: str | None = None) -> str:
    """Return the summary block for a document by relative path."""
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
    if not str(target).startswith(str(library_path.resolve()) + "/"):
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
        "description": "Look up an index term and get the list of library documents tagged with it, plus semantically related terms to follow up on.",
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
        "name": "get_summary",
        "description": "Get the title, word count, summary, and index terms for a specific library document.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path of the document (e.g. 'philosophy/stoicism.md').",
                }
            },
            "required": ["path"],
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
