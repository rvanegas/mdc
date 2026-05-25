from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path


_REVIEW_PROMPTS_DIR = Path("~/.config/mdc/review").expanduser()

_REVIEW_GUIDANCE = """\
The documents are of different kinds. Sections headed "Claude" or "GPT" identify \
transcripts of conversations with a language model — thinking out loud, not publication. \
The medium licenses directness: positions are stated quickly, premises are not always \
laid before conclusions, and the interlocutor requires no persuasion. Do not read features \
of this medium as features of the author. Speed is not carelessness. Incompleteness is \
not incapacity. Directness to a machine is not indifference to the standards of the field. \
Other documents — dialogues with human interlocutors, standalone essays — are intended \
for a human reader and should be read accordingly.

In particular: extensive engagement with professional philosophical literature is evidence \
of engagement with professional opinion, not indifference to it. Draw inferences about \
the author's relationship to the field from what they actually do with the literature — \
how they deploy it, correct it, extend it — not from the absence of the argumentative \
formalities that publication requires.

Write with the confidence of someone who has read carefully and formed genuine views. \
Stay sharp and assessorial — never exhaust the argument.
"""

_DEFAULT_DOC_REVIEW_SYSTEM_PROMPT = (
    "Document Review\n\n"
    "Your task is literary as much as analytical: read the document given to you and assess "
    "it on its own terms — its arguments, its voice, its quality. "
    "You may see one or a few related documents for context, but you do not see the wider "
    "collection they belong to. Do not generalize about the collection, compare this document "
    "to works you have not read, or characterize it as early, late, typical, or exceptional "
    "relative to documents you cannot see. Assess only what is here.\n\n"
    + _REVIEW_GUIDANCE
)

_DEFAULT_FINAL_PROMPT = """\
Comprehensive Assessment

The thematic assessments above cover the full body of work, organized by theme. Treat \
them as load-bearing evidence. Draw on all of this material to give a full and considered \
assessment covering:

(Do not begin your response with a top-level heading or title. Start directly with the \
body of the assessment.)

1. The Body of Work — what has been built across these themes? How unified is it? What \
are its genuine contributions to the fields it engages?

2. The Tensions — what remains unresolved across themes? What is underdeveloped or missing?

3. The Verdict — what is the significance of this body of work, assessed against the fields \
it engages and the ambitions it declares?

4. The Author — what kind of mind is behind this work? Do not recapitulate earlier sections. \
Focus on character, disposition, and intellectual signature as they emerge from the whole.

Take the space this requires.
"""


@dataclass
class ReviewState:
    doc_reviews: list[dict] = field(default_factory=list)  # {"filename", "label", "text", "reviewed_at"}
    cumulative_cost: float = 0.0


def load_review_state(path: Path) -> ReviewState:
    if not path.exists():
        return ReviewState()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        raw = list(data.get("doc_reviews") or data.get("responses", []))
        # Deduplicate by filename, keeping the most recently reviewed entry.
        # Duplicates can accumulate when the first-match update (existing_idx)
        # and the last-match dict lookup (review_by_filename) diverge.
        seen: dict[str, dict] = {}
        for r in raw:
            fn = r.get("filename", "")
            if not fn:
                continue
            if fn not in seen or r.get("reviewed_at", "") > seen[fn].get("reviewed_at", ""):
                seen[fn] = r
        return ReviewState(
            doc_reviews=list(seen.values()),
            cumulative_cost=float(data.get("cumulative_cost", 0.0)),
        )
    except (json.JSONDecodeError, ValueError):
        return ReviewState()


def save_review_state(state: ReviewState, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(state), indent=2), encoding="utf-8")


def extract_doc_heading(path: Path) -> str:
    """Return the first '# Title' heading from a document, or a slug derived from the filename."""
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("# "):
                return line[2:].strip()
    except OSError:
        pass
    return path.stem.replace("-", " ").title()


_SUBSCRIPT_MAP = str.maketrans(
    "₀₁₂₃₄₅₆₇₈₉ₐₑₒₓₔₕₖₗₘₙₚₛₜ",
    "0123456789aeoxehklmnpst",
)
_SUPERSCRIPT_MAP = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")


def sanitize_for_pandoc(text: str) -> str:
    """Replace characters that cause pandoc/LaTeX failures."""
    return text.translate(_SUBSCRIPT_MAP).translate(_SUPERSCRIPT_MAP)


def _doc_review_word_limit(word_count: int) -> int:
    if word_count < 1000:
        return 300
    elif word_count <= 3000:
        return 500
    return 800


def _resolve_related_docs(doc_path: Path, title_to_path: dict[str, Path]) -> list[Path]:
    """Return paths for Related-section titles that exist in the library."""
    from mdc.library import _extract_related
    try:
        content = doc_path.read_text(encoding="utf-8")
    except OSError:
        return []
    titles = _extract_related(content)
    result = []
    for raw in titles:
        title = raw.strip("| *")
        p = title_to_path.get(title)
        if p and p != doc_path and p.exists():
            result.append(p)
    return result


def build_doc_review_messages(
    doc_path: Path,
    title_to_path: dict[str, Path] | None = None,
    reviews: dict[str, str] | None = None,
) -> list[dict]:
    """Build messages for a single-document review call (for final context)."""
    date = doc_path.name[:10] if len(doc_path.name) > 10 and doc_path.name[4] == "-" else ""
    title = extract_doc_heading(doc_path)
    label = f'"{title}" ({date})' if date else f'"{title}"'
    text = doc_path.read_text(encoding="utf-8")
    word_count = len(text.split())
    word_limit = _doc_review_word_limit(word_count)

    related_docs = _resolve_related_docs(doc_path, title_to_path or {})

    content: list[dict] = []
    parts = []
    for p in related_docs:
        rel_date = p.name[:10] if len(p.name) > 10 and p.name[4] == "-" else ""
        rel_title = extract_doc_heading(p)
        rel_label = f'"{rel_title}" ({rel_date})' if rel_date else f'"{rel_title}"'
        if reviews is None or p.name not in reviews:
            continue
        parts.append(f"Review of {rel_label}:\n\n{reviews[p.name]}")
    if parts:
        related_text = "\n\n---\n\n".join(parts)
        content.append({"type": "text", "text": f"Related document reviews:\n\n{related_text}", "cache_control": {"type": "ephemeral"}})

    content.append({"type": "text", "text": f"Document: {label}\n\n{text}"})

    related_clause = (
        " Where relevant, draw on the related document reviews provided for context."
        if parts else ""
    )
    prompt = (
        f"Write a {word_limit}-word assessment of {label}."
        " What is it about? What are its central claims, arguments, or concerns?"
        " Be specific and assessorial."
        " Close with a frank verdict: what the document achieves and what it leaves"
        " unresolved or underdeveloped." + related_clause
    )
    content.append({"type": "text", "text": prompt})

    return [{"role": "user", "content": content}]


def build_final_messages(assessments: list[tuple[str, str]], final_prompt: str) -> list[dict]:
    """Build messages for the final cross-theme assessment call.

    assessments: list of (theme_name, assessment_text)
    """
    block = "\n\n---\n\n".join(f"{name}:\n\n{text}" for name, text in assessments)
    return [{"role": "user", "content": [
        {"type": "text", "text": block, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": final_prompt},
    ]}]


def _demote_headings(text: str, levels: int = 2) -> str:
    """Shift all ATX headings in text down by the given number of levels."""
    prefix = "#" * levels
    lines = []
    for line in text.splitlines():
        if line.startswith("#"):
            lines.append(prefix + line)
        else:
            lines.append(line)
    return "\n".join(lines)


def build_reviews_md(state: ReviewState) -> str:
    """REVIEWS.md: individual doc reviews in saved order."""
    parts = []
    if state.doc_reviews:
        for review in state.doc_reviews:
            parts.append(f"\n## {review['label']}\n\n{_demote_headings(review['text'])}\n\n---\n")
    return sanitize_for_pandoc("\n# Individual Reviews\n" + "".join(parts)) if parts else ""


def load_prompt(prompt_file: Path | None, default: str) -> str:
    if prompt_file and prompt_file.exists():
        return prompt_file.read_text(encoding="utf-8")
    return default


_DEFAULT_THEMED_SYNTHESIS_PROMPT = """\
Thematic Assessment

The individual document reviews above were selected around a shared theme. \
Synthesize them into a single thematic assessment:

(Do not begin your response with a top-level heading or title. Start directly with \
the body of the assessment.)

1. The Theme — what concerns, questions, or problems unite these documents? \
How coherently do they form a cluster? What is at its edges or missing?

2. The Contribution — what has been built within this theme? What is the quality \
and character of the thinking?

3. The Tensions — what remains unresolved? What is underdeveloped?

4. The Verdict — assessed against the field this work engages, what is the \
significance of these documents as a thematic unit?

Take the space this requires.
"""

THEMES_FILENAME = "THEMES.md"


def generate_themes_template() -> str:
    return (
        "# Themes\n"
        "<!-- Add themes: - code : full name -->\n\n"
        "# Documents\n"
        "<!-- Populated automatically by mdc review --theme -->\n"
    )


def parse_themes_md(path: Path) -> tuple[dict[str, str], dict[str, set[str]]]:
    """Parse THEMES.md.

    Returns:
        themes: {code: full_name}
        doc_assignments: {title: set_of_codes}
    """
    content = path.read_text(encoding="utf-8")
    themes: dict[str, str] = {}
    doc_assignments: dict[str, set[str]] = {}

    section: str | None = None

    for line in content.splitlines():
        stripped = line.strip()
        if stripped == "# Themes":
            section = "themes"
            continue
        elif stripped == "# Documents":
            section = "documents"
            continue
        elif stripped.startswith("# "):
            section = None
            continue

        if section == "themes":
            if stripped.startswith("- ") and not stripped.startswith("<!-- "):
                rest = stripped[2:]
                if " : " in rest:
                    code, name = rest.split(" : ", 1)
                    code = code.strip()
                    name = name.strip()
                    if code:
                        themes[code] = name

        elif section == "documents":
            if stripped.startswith("- ") and not stripped.startswith("<!-- "):
                rest = stripped[2:]
                if " : " in rest:
                    codes_str, title = rest.split(" : ", 1)
                    codes_str = codes_str.strip()
                    title = title.strip()
                    if title:
                        doc_assignments[title] = set(c for c in codes_str if not c.isspace())

    return themes, doc_assignments


def parse_combinations(path: Path) -> list[list[str]]:
    """Parse the # Combinations section of THEMES.md.

    Returns a list of combinations, each a list of theme names.
    """
    content = path.read_text(encoding="utf-8")
    combos: list[list[str]] = []
    in_section = False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped == "# Combinations":
            in_section = True
            continue
        elif stripped.startswith("# "):
            in_section = False
            continue
        if in_section and stripped.startswith("- ") and not stripped.startswith("<!-- "):
            names = [n.strip() for n in stripped[2:].split(",") if n.strip()]
            if names:
                combos.append(names)
    return combos


def write_themes_md(
    path: Path,
    doc_assignments: dict[str, set[str]],
    title_order: list[str] | None = None,
) -> None:
    """Rewrite the # Documents section of THEMES.md, preserving # Themes."""
    content = path.read_text(encoding="utf-8")
    lines = content.splitlines()
    doc_start = next((i for i, ln in enumerate(lines) if ln.strip() == "# Documents"), None)
    themes_block = "\n".join(lines[:doc_start]).rstrip() if doc_start is not None else content.rstrip()

    order = list(dict.fromkeys(title_order or [])) if title_order else []
    for t in sorted(doc_assignments):
        if t not in order:
            order.append(t)

    max_code_len = max((len("".join(sorted(doc_assignments.get(t, set())))) for t in order), default=1)
    max_code_len = max(max_code_len, 1)

    doc_lines = ["# Documents"]
    for title in order:
        codes = doc_assignments.get(title, set())
        code_str = "".join(sorted(codes))
        doc_lines.append(f"- {code_str:<{max_code_len}} : {title}")

    path.write_text(themes_block + "\n\n" + "\n".join(doc_lines) + "\n", encoding="utf-8")


def ensure_theme_subsection(themes_path: Path, theme_code: str, theme_name: str) -> bool:
    """Append a ## theme_name subsection at the end of # Themes if absent. Returns True if added."""
    content = themes_path.read_text(encoding="utf-8")
    if re.search(rf"^## {re.escape(theme_name)}\s*$", content, re.MULTILINE | re.IGNORECASE):
        return False
    lines = content.splitlines()
    doc_start = next((i for i, ln in enumerate(lines) if ln.strip() == "# Documents"), None)
    themes_block = "\n".join(lines[:doc_start]).rstrip() if doc_start is not None else content.rstrip()
    rest = ("\n\n" + "\n".join(lines[doc_start:])) if doc_start is not None else ""
    themes_path.write_text(themes_block + f"\n\n## {theme_name}\n" + rest, encoding="utf-8")
    return True


def sync_themes_docs(themes_path: Path, lib_path: Path) -> int:
    """Add missing library docs to # Documents as unclassified. Returns count added."""
    from mdc.library import load_entries
    themes, doc_assignments = parse_themes_md(themes_path)
    entries = sorted(load_entries(lib_path), key=lambda e: e.rel_path)
    title_order = [e.title for e in entries]
    added = 0
    for e in entries:
        if e.title not in doc_assignments:
            doc_assignments[e.title] = set()
            added += 1
    write_themes_md(themes_path, doc_assignments, title_order)
    return added


def build_themed_synthesis_messages(
    reviews: list[dict],
    synthesis_prompt: str,
    collection_context: str | None = None,
) -> list[dict]:
    """Build messages for a single themed synthesis call over a list of doc reviews."""
    block = "\n\n---\n\n".join(
        f"{r['label']}:\n\n{r['text']}" for r in reviews
    )
    content: list[dict] = []
    if collection_context:
        content.append({"type": "text", "text": collection_context, "cache_control": {"type": "ephemeral"}})
    content.append({"type": "text", "text": block, "cache_control": {"type": "ephemeral"}})
    content.append({"type": "text", "text": synthesis_prompt})
    return [{"role": "user", "content": content}]

