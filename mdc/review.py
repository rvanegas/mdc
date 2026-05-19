from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


DEFAULT_WINDOW = 40

_REVIEW_PROMPTS_DIR = Path("~/.config/mdc/review").expanduser()

_DEFAULT_SYSTEM_PROMPT = """\
Document Review

Your task is literary as much as analytical: read the documents given to you and form \
a portrait of the author — their intellectual character, their ambitions, their voice, \
their development over time. Exhaustive analysis of individual arguments is secondary. \
What matters is: what kind of mind is this, and what is it building?

Write with the confidence of someone who has read carefully and formed genuine views. \
Stay sharp and assessorial — never exhaust the argument.
"""

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
        interims_raw = list(data.get("interims", []))
        interims = []
        for i, entry in enumerate(interims_raw):
            if isinstance(entry, str):
                after_doc = (i + 1) * DEFAULT_WINDOW
                interims.append({"header": f"Interim {i + 1} (after doc {after_doc})", "text": entry, "after_doc": after_doc})
            else:
                interims.append(entry)
        return ReviewState(
            doc_index=int(data.get("doc_index", 0)),
            interims=interims,
            doc_reviews=list(data.get("doc_reviews", [])),
            cumulative_cost=float(data.get("cumulative_cost", 0.0)),
            final_done=bool(data.get("final_done", False)),
            final_text=data.get("final_text"),
        )
    except (json.JSONDecodeError, ValueError):
        return ReviewState()


def save_review_state(state: ReviewState, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(state), indent=2), encoding="utf-8")


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
    if related_docs:
        related_text = build_segment_content(related_docs)
        content.append({"type": "text", "text": f"Related documents:\n\n{related_text}", "cache_control": {"type": "ephemeral"}})

    content.append({"type": "text", "text": f"Document: {label}\n\n{text}"})

    related_clause = (
        " Where relevant, draw on the related documents provided for context."
        if related_docs else ""
    )
    prompt = (
        f"Write a {word_limit}-word assessment of this document."
        " What is it about? What are its central claims, arguments, or concerns?"
        " Be specific and assessorial." + related_clause
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


def build_assessments_md(state: ReviewState, include_toc: bool = False) -> str:
    """Reconstruct REVIEW.md content from state (interims and final only)."""
    parts = [_TOC_BLOCK] if include_toc else []
    for interim in state.interims:
        parts.append(f"\n# {interim['header']}\n\n{interim['text']}\n\n---\n")
    if state.final_text:
        parts.append(f"\n# Final Assessment\n\n{state.final_text}\n\n---\n")
    return "".join(parts)


def build_assessment_md(state: ReviewState) -> str:
    """ASSESSMENT.md: just the final assessment."""
    if not state.final_text:
        return ""
    return sanitize_for_pandoc(f"# Final Assessment\n\n{state.final_text}\n")



def build_reviews_md(state: ReviewState, entries) -> str:
    """REVIEWS.md: all review material in canonical order."""
    parts = [_TOC_BLOCK]

    if state.final_text:
        parts.append(f"\n# Final Assessment\n\n{state.final_text}\n\n---\n")

    if state.interims:
        parts.append("\n# Segment Assessments\n")
        for interim in state.interims:
            parts.append(f"\n## {interim['header']}\n\n{interim['text']}\n\n---\n")

    if state.doc_reviews:
        parts.append("\n# Individual Reviews\n")
        for review in state.doc_reviews:
            parts.append(f"\n## {review['label']}\n\n{review['text']}\n\n---\n")

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
