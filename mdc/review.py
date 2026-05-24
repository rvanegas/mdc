from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path


SEGMENT_TOKEN_LIMIT = 190_000

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

_DEFAULT_SYSTEM_PROMPT = (
    "Document Review\n\n"
    "Your task is literary as much as analytical: read the documents given to you and form "
    "a portrait of the author — their intellectual character, their ambitions, their voice, "
    "their development over time. Exhaustive analysis of individual arguments is secondary. "
    "What matters is: what kind of mind is this, and what is it building?\n\n"
    + _REVIEW_GUIDANCE
)

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

_DEFAULT_INTERIM_PROMPT = """\
Segment Assessment

The documents above form one segment of a larger collection, delivered in chronological \
order. Read them as a unit. Set aside the word limit for this assessment.

(Do not begin your response with a top-level heading or title. Start directly with the \
body of the assessment.)

1. THE AUTHOR — intellectual character, voice, ambitions as they appear in this segment. \
What is the quality of the work?

2. THE MAIN THREADS — what ideas and concerns are developing across these documents? \
How unified are they?

3. THE ARC — how does the work develop from the earliest to the latest document in this \
segment? What changes, what remains constant?

4. WHAT TO WATCH FOR — tensions, open questions, threads not yet resolved.

Name specific documents sparingly — only those that mark a turning point, best exemplify \
a thread, or are essential for tracing the arc. Aim for no more than ten titles across \
the entire assessment.

Write with the confidence of someone who has read carefully and formed genuine views.
"""


_DEFAULT_FINAL_PROMPT = """\
Comprehensive Assessment

The segment assessments above cover the full collection in chronological order. Treat \
them as load-bearing evidence. The document assessments that follow are fresh readings \
of the documents you selected — use them to ground specific claims and deepen the \
assessment. Draw on all of this material to give a full and considered assessment covering:

Important: each segment assessment was written without access to the rest of the \
collection. Where a segment flags something as absent, underdeveloped, or unaddressed, \
do not carry that judgment forward unless you can confirm the gap holds across the \
whole collection. A concern raised in one segment may be answered in another. Only \
treat something as genuinely missing if it is missing from the body of work as a whole.

(Do not begin your response with a top-level heading or title. Start directly with the \
body of the assessment.)

1. The Author — who is this person, what kind of mind, what is the quality and character \
of the work? Assess honestly and without flattery.

2. The Body of Work — what has been built across these documents? How unified is it? What \
are its genuine contributions to the field it engages?

3. The Arc — how did the work develop over time? What changed, what remained constant, \
what does the trajectory reveal?

4. The Tensions — what remains unresolved? What would need to be done to advance the project?

5. The Verdict — what is the significance of this body of work, assessed against the field \
it engages and the ambitions it declares?

Take the space this requires.
"""


@dataclass
class ReviewState:
    doc_index: int = 0
    interims: list[dict] = field(default_factory=list)  # {"header", "text", "after_doc"}
    doc_reviews: list[dict] = field(default_factory=list)  # {"filename", "label", "text"} — pre-final checkpoints
    cumulative_cost: float = 0.0
    final_done: bool = False
    final_text: str | None = None


def load_review_state(path: Path) -> ReviewState:
    if not path.exists():
        return ReviewState()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        # Migrate old schema: "responses" was renamed to "doc_reviews"; old "doc_index" tracked reviews, not segments.
        old_schema = "responses" in data and "doc_reviews" not in data
        doc_reviews = list(data.get("doc_reviews") or data.get("responses", []))
        return ReviewState(
            doc_index=0 if old_schema else int(data.get("doc_index", 0)),
            interims=list(data.get("interims", [])),
            doc_reviews=doc_reviews,
            cumulative_cost=float(data.get("cumulative_cost", 0.0)),
            final_done=bool(data.get("final_done", False)),
            final_text=data.get("final_text"),
        )
    except (json.JSONDecodeError, ValueError):
        return ReviewState()


def save_review_state(state: ReviewState, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(state), indent=2), encoding="utf-8")


def load_doc_word_counts(library_path: Path) -> dict[str, int]:
    """Return {filename: word_count} for all indexed documents."""
    from mdc.library import load_entries
    return {Path(e.rel_path).name: e.word_count for e in load_entries(library_path)}


def next_segment(
    all_docs: list[Path],
    start_idx: int,
    word_counts: dict[str, int],
    token_limit: int = SEGMENT_TOKEN_LIMIT,
) -> list[Path]:
    """Return the next segment of docs starting at start_idx, sized to fit within token_limit."""
    result = []
    total = 0.0
    for doc in all_docs[start_idx:]:
        tokens = word_counts.get(doc.name, 400) * 1.3
        if result and total + tokens > token_limit:
            break
        result.append(doc)
        total += tokens
    return result


def all_segments(
    all_docs: list[Path],
    word_counts: dict[str, int],
    token_limit: int = SEGMENT_TOKEN_LIMIT,
) -> list[list[Path]]:
    """Return the full list of dynamic segments for a review run."""
    segments = []
    i = 0
    while i < len(all_docs):
        seg = next_segment(all_docs, i, word_counts, token_limit)
        if not seg:
            break
        segments.append(seg)
        i += len(seg)
    return segments


def list_review_docs(library_path: Path) -> list[Path]:
    """Return indexed documents in chronological order (by rel_path date prefix)."""
    from mdc.library import load_entries
    entries = load_entries(library_path)
    return [library_path / e.rel_path for e in sorted(entries, key=lambda e: Path(e.rel_path).name)]


def estimate_review_cost(
    library_path: Path,
    docs: list[Path],
    window: int,
    system_prompt: str,
    rates: tuple[float, float] | None,
) -> float | None:
    """Return a rough cost estimate in USD for reviewing the given documents."""
    if not rates:
        return None
    from mdc.library import load_entries
    word_count_by_rel = {e.rel_path: e.word_count for e in load_entries(library_path)}

    num_docs = len(docs)
    num_segments = max(1, (num_docs + window - 1) // window)
    system_tokens = max(200, len(system_prompt) // 4)

    total_doc_words = sum(
        word_count_by_rel.get(doc.relative_to(library_path).as_posix(), 400)
        for doc in docs
    )
    avg_doc_tokens = total_doc_words * 1.3 / max(num_docs, 1)

    # Each segment: system + raw docs + interim prompt (~400 tokens)
    segment_input = num_segments * (system_tokens + window * avg_doc_tokens + 400)
    segment_output = num_segments * 1500

    # Final: system + all interims (~1500 tokens each) + final prompt
    final_input = system_tokens + num_segments * 1500 + 400
    final_output = 2000

    in_rate, out_rate = rates
    return (segment_input + final_input) * in_rate / 1_000_000 + (segment_output + final_output) * out_rate / 1_000_000


def extract_doc_heading(path: Path) -> str:
    """Return the first '# Title' heading from a document, or a slug derived from the filename."""
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("# "):
                return line[2:].strip()
    except OSError:
        pass
    return path.stem.replace("-", " ").title()


_TOC_BLOCK = """\
```{=latex}
\\tableofcontents
\\newpage
```

"""

_SUBSCRIPT_MAP = str.maketrans(
    "₀₁₂₃₄₅₆₇₈₉ₐₑₒₓₔₕₖₗₘₙₚₛₜ",
    "0123456789aeoxehklmnpst",
)
_SUPERSCRIPT_MAP = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")


def sanitize_for_pandoc(text: str) -> str:
    """Replace characters that cause pandoc/LaTeX failures."""
    return text.translate(_SUBSCRIPT_MAP).translate(_SUPERSCRIPT_MAP)


def _interim_label(header: str) -> str:
    """Return the model-facing label for an interim, stripping doc-count info."""
    import re
    m = re.match(r'((?:Final )?Interim \d*)', header)
    return m.group(1).strip() if m else header.split("(")[0].strip()


def build_segment_content(docs: list[Path]) -> str:
    """Concatenate raw document texts for a segment interim call."""
    parts = []
    for doc_path in docs:
        date = doc_path.name[:10] if len(doc_path.name) > 10 and doc_path.name[4] == "-" else ""
        title = extract_doc_heading(doc_path)
        label = f'"{title}" ({date})' if date else f'"{title}"'
        text = doc_path.read_text(encoding="utf-8")
        parts.append(f"Document: {label}\n\n{text}")
    return "\n\n---\n\n".join(parts)


def build_interim_messages(docs: list[Path], interim_prompt: str) -> list[dict]:
    """Build messages for an interim (segment) assessment call."""
    segment_text = build_segment_content(docs)
    return [{"role": "user", "content": [
        {"type": "text", "text": segment_text, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": interim_prompt},
    ]}]


def extract_mentioned_titles(texts: list[str], known_titles: list[str], min_length: int = 6) -> list[str]:
    """Return known titles that appear (case-insensitive) in any of the given texts."""
    combined = " ".join(texts).lower()
    return [t for t in known_titles if len(t) >= min_length and t.lower() in combined]



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


def build_manifest_summaries(entries, exclude_titles: set[str]) -> str:
    """Build a compact title+summary block for docs not covered by full reviews."""
    from pathlib import Path as _Path
    parts = []
    for e in entries:
        if e.title in exclude_titles or not e.summary:
            continue
        name = _Path(e.rel_path).name
        date = name[:10] if len(name) > 10 and name[4] == "-" else ""
        date_str = f" ({date})" if date else ""
        parts.append(f'"{e.title}"{date_str}: {e.summary}')
    return "\n\n".join(parts)


def build_final_messages(
    interims: list[dict],
    final_prompt: str,
    selected_reviews: str | None = None,
    manifest_summaries: str | None = None,
) -> list[dict]:
    """Build messages for the final assessment call."""
    block = "\n\n---\n\n".join(
        f"{_interim_label(entry['header'])}:\n{entry['text']}" for entry in interims
    )
    content: list[dict] = [
        {"type": "text", "text": block, "cache_control": {"type": "ephemeral"}},
    ]
    if manifest_summaries:
        content.append({"type": "text", "text": manifest_summaries, "cache_control": {"type": "ephemeral"}})
    if selected_reviews:
        content.append({"type": "text", "text": selected_reviews})
    content.append({"type": "text", "text": final_prompt})
    return [{"role": "user", "content": content}]


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


def build_assessments_md(state: ReviewState, include_toc: bool = False) -> str:
    """Reconstruct REVIEW.md content from state (interims and final only)."""
    parts = [_TOC_BLOCK] if include_toc else []
    for interim in state.interims:
        parts.append(f"\n# {interim['header']}\n\n{_demote_headings(interim['text'])}\n\n---\n")
    if state.final_text:
        parts.append(f"\n# Final Assessment\n\n{_demote_headings(state.final_text)}\n\n---\n")
    return "".join(parts)


def build_assessment_md(state: ReviewState) -> str:
    """ASSESSMENT.md: just the final assessment."""
    if not state.final_text:
        return ""
    return sanitize_for_pandoc(f"# Final Assessment\n\n{_demote_headings(state.final_text)}\n")


def build_reviews_md(state: ReviewState, entries) -> str:
    """REVIEWS.md: all review material in canonical order."""
    parts = [_TOC_BLOCK]

    if state.final_text:
        parts.append(f"\n# Final Assessment\n\n{_demote_headings(state.final_text)}\n\n---\n")

    if state.interims:
        parts.append("\n# Segment Assessments\n")
        for interim in state.interims:
            parts.append(f"\n## {interim['header']}\n\n{_demote_headings(interim['text'])}\n\n---\n")

    if state.doc_reviews:
        parts.append("\n# Individual Reviews\n")
        for review in state.doc_reviews:
            parts.append(f"\n## {review['label']}\n\n{_demote_headings(review['text'])}\n\n---\n")

    # Document summaries — exclude titles covered by individual reviews.
    reviewed_titles: set[str] = set()
    for r in state.doc_reviews:
        label = r["label"]
        if label.startswith('"'):
            reviewed_titles.add(label.split('"')[1])
    summary_text = build_manifest_summaries(entries, reviewed_titles)
    if summary_text:
        parts.append(f"\n# Document Summaries\n\n{summary_text}\n\n---\n")

    return sanitize_for_pandoc("".join(parts))


def generate_include_list(titles: list[str]) -> str:
    lines = [
        "<!-- Documents to review before the final assessment. One title per line.",
        "     Titles must match library entries exactly. -->",
        "",
    ]
    lines += [f"- {t}" for t in titles]
    return "\n".join(lines) + "\n"


def load_include_list(path: Path) -> list[str]:
    titles = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("- "):
            titles.append(line[2:].strip())
    return titles


def load_prompt(prompt_file: Path | None, default: str) -> str:
    if prompt_file and prompt_file.exists():
        return prompt_file.read_text(encoding="utf-8")
    return default


_DEFAULT_THEMED_INTERIM_PROMPT = """\
Thematic Assessment

The documents above were selected around a shared theme. Read them as a unit. \
Set aside the word limit for this assessment.

(Do not begin your response with a top-level heading or title. Start directly with \
the body of the assessment.)

1. THE THEME — what concerns, questions, or problems unite these documents? How \
coherently do they form a cluster? What is missing or underrepresented?

2. THE AUTHOR — intellectual character, voice, and approach as they appear in these \
documents. What is the quality of the work on this theme?

3. THE MAIN THREADS — what ideas and arguments develop across these documents? \
What tensions or open questions appear within this cluster?

4. WHAT TO WATCH FOR — threads not yet resolved, connections to work outside this cluster.

Name specific documents sparingly — only those that best exemplify a thread or mark \
a key development. Aim for no more than ten titles.

Write with the confidence of someone who has read carefully and formed genuine views.
"""

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

_DEFAULT_THEMED_FINAL_PROMPT = """\
Thematic Assessment — Final

The segment assessments above cover documents organized around a shared theme. \
Treat them as load-bearing evidence. The individual document reviews that follow are \
fresh readings — use them to ground specific claims. Draw on all of this to give a \
full assessment of this thematic cluster:

(Do not begin your response with a top-level heading or title. Start directly with \
the body of the assessment.)

1. The Theme — what is the central concern of this cluster? How coherently does it \
hold together? What are its boundaries and edges?

2. The Contribution — what has been built within this theme? What is the quality and \
character of the thinking?

3. The Tensions — what remains unresolved? What is underdeveloped or missing?

4. The Verdict — assessed against the field this work engages, what is the significance \
of these documents as a thematic unit?

Take the space this requires.
"""


THEMES_FILENAME = "THEMES.md"


def generate_themes_template() -> str:
    return (
        "# Themes\n"
        "<!-- Add themes: - code : full name -->\n"
        "<!-- Add terms under ## full_name subsections -->\n\n"
        "# Documents\n"
        "<!-- Populated automatically by mdc review --theme -->\n"
    )


def parse_themes_md(path: Path) -> tuple[dict[str, str], dict[str, list[str]], dict[str, set[str]]]:
    """Parse THEMES.md.

    Returns:
        themes: {code: full_name}
        theme_terms: {code: [term, ...]}
        doc_assignments: {title: set_of_codes}
    """
    content = path.read_text(encoding="utf-8")
    themes: dict[str, str] = {}
    name_to_code: dict[str, str] = {}
    theme_terms: dict[str, list[str]] = {}
    doc_assignments: dict[str, set[str]] = {}

    section: str | None = None
    current_term_code: str | None = None

    for line in content.splitlines():
        stripped = line.strip()
        if stripped == "# Themes":
            section = "themes"
            current_term_code = None
            continue
        elif stripped == "# Documents":
            section = "documents"
            current_term_code = None
            continue
        elif stripped.startswith("# "):
            section = None
            current_term_code = None
            continue

        if section == "themes":
            if stripped.startswith("## "):
                name = stripped[3:].strip()
                current_term_code = name_to_code.get(name.lower())
            elif stripped.startswith("- ") and not stripped.startswith("<!-- "):
                rest = stripped[2:]
                if current_term_code is None:
                    if " : " in rest:
                        code, name = rest.split(" : ", 1)
                        code = code.strip()
                        name = name.strip()
                        if code:
                            themes[code] = name
                            name_to_code[name.lower()] = code
                            theme_terms.setdefault(code, [])
                else:
                    terms = [t.strip() for t in rest.split(",") if t.strip()]
                    theme_terms.setdefault(current_term_code, []).extend(terms)

        elif section == "documents":
            if stripped.startswith("- ") and not stripped.startswith("<!-- "):
                rest = stripped[2:]
                if " : " in rest:
                    codes_str, title = rest.split(" : ", 1)
                    codes_str = codes_str.strip()
                    title = title.strip()
                    if title:
                        doc_assignments[title] = set(c for c in codes_str if not c.isspace())

    return themes, theme_terms, doc_assignments


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
    themes, theme_terms, doc_assignments = parse_themes_md(themes_path)
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
) -> list[dict]:
    """Build messages for a single themed synthesis call over a list of doc reviews."""
    block = "\n\n---\n\n".join(
        f"{r['label']}:\n\n{r['text']}" for r in reviews
    )
    return [{"role": "user", "content": [
        {"type": "text", "text": block, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": synthesis_prompt},
    ]}]


def write_theme_selection(
    path: Path,
    titles: list[str],
    total_tokens: float,
) -> None:
    """Replace the ## Selection section of a theme file with the given titles."""
    content = path.read_text(encoding="utf-8")
    content = re.sub(r"\n## Auto-Included\b.*", "", content, flags=re.DOTALL).rstrip()
    n = len(titles)
    token_str = f"~{total_tokens / 1000:.0f}k tokens estimated"
    lines = [f"\n\n## Auto-Included\n<!-- {n} documents, {token_str} -->\n"]
    lines += [f"- {t}" for t in titles]
    path.write_text(content + "\n".join(lines) + "\n", encoding="utf-8")


def select_docs_by_theme(
    lib_path: Path,
    terms: list[str],
    include_titles: list[str],
    exclude_titles: list[str],
) -> tuple[list[Path], dict[str, int], list[str]]:
    """Score and sort documents by theme relevance.

    Returns (scored_paths, scores_by_rel_path, related_term_keys).
    scored_paths is the full scored list sorted by score desc, date asc — no token packing.
    Force-includes are prepended; excludes are removed.
    Callers are responsible for packing to a token limit.
    """
    from mdc.library import (
        _RELATIONS_STATE_PATH,
        _TERMS_STATE_PATH,
        load_entries,
        parse_keys_md,
    )

    try:
        terms_data = json.loads(_TERMS_STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        terms_data = {}
    term_map: dict[str, list[dict]] = terms_data.get("terms", {})

    try:
        rel_data = json.loads(_RELATIONS_STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        rel_data = {}
    relations: dict[str, list[str]] = rel_data.get("relations", {})

    alias_map, _, _ = parse_keys_md(lib_path)

    def resolve(t: str) -> str:
        return alias_map.get(t) or alias_map.get(t.casefold()) or t

    folded_map = {k.casefold(): k for k in term_map}

    primary_keys: set[str] = set()
    related_keys: list[str] = []
    seen_related: set[str] = set()
    for ct in (resolve(t) for t in terms):
        key = folded_map.get(ct.casefold())
        if key:
            primary_keys.add(key)
    for key in primary_keys:
        for rel in relations.get(key, []):
            rel_key = folded_map.get(rel.casefold())
            if rel_key and rel_key not in primary_keys and rel_key not in seen_related:
                related_keys.append(rel_key)
                seen_related.add(rel_key)

    scores: dict[str, int] = {}
    for key in primary_keys:
        for doc in term_map.get(key, []):
            scores[doc["rel_path"]] = scores.get(doc["rel_path"], 0) + 2
    for key in related_keys:
        for doc in term_map.get(key, []):
            scores[doc["rel_path"]] = scores.get(doc["rel_path"], 0) + 1

    entries = load_entries(lib_path)
    title_to_rel: dict[str, str] = {e.title: e.rel_path for e in entries}
    rel_to_path: dict[str, Path] = {e.rel_path: lib_path / Path(e.rel_path) for e in entries}

    exclude_rels: set[str] = set()
    for title in exclude_titles:
        rel = title_to_rel.get(title)
        if rel:
            exclude_rels.add(rel)

    include_rels: list[str] = []
    include_rel_set: set[str] = set()
    for title in include_titles:
        rel = title_to_rel.get(title)
        if rel and rel not in exclude_rels:
            include_rels.append(rel)
            include_rel_set.add(rel)

    for rel in exclude_rels:
        scores.pop(rel, None)

    scored_rels = sorted(
        ((rel, s) for rel, s in scores.items() if rel not in exclude_rels and rel not in include_rel_set),
        key=lambda x: (-x[1], Path(x[0]).name),
    )

    all_selected = list(dict.fromkeys(include_rels + [r for r, _ in scored_rels]))
    paths = [rel_to_path[r] for r in all_selected if r in rel_to_path]
    return paths, scores, related_keys
