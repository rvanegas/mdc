from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


DEFAULT_WINDOW = 50

_REVIEW_PROMPTS_DIR = Path("~/.config/mdc/review").expanduser()

_DEFAULT_SYSTEM_PROMPT = """\
Document Review

You are reading a collection of documents delivered one at a time in chronological order.

Your task is literary as much as analytical: assess the quality and direction of the collection, \
and form a portrait of the author — their intellectual character, their ambitions, their voice, \
their development over time. Exhaustive analysis of individual arguments is secondary. What \
matters is: what kind of mind is this, and what is it building?

Keep alive the expectation of more to come until the full range is in view. The earliest \
documents may be exploratory; the latest may be mature. Watch for the arc.

READING PROTOCOL:
- Each user turn delivers one document. Read it and respond.
- Begin each response with a bold title in this exact format:
  **Doc N — "Document Title" (YYYY-MM-DD)**
  where N is the document number, the title is the document's own title, and the date is the \
document date.
- Respond in proportion to the document's length: roughly 300 words for short pieces \
(under 100 lines), 500 for medium (100–300 lines), 800 for long ones (over 300 lines). \
Stay sharp and assessorial — never exhaust the argument.

SCHEDULED ASSESSMENTS (full analytical depth permitted):
- Interim assessments — sent automatically every N documents. These are \
synthesis-before-compaction checkpoints: the interim itself becomes what survives into later \
context, so it should be specific and concrete (named threads, pivotal document titles, direct \
quotes where they matter) rather than abstract.
- Final interim assessment — sent before the final assessment, focused only on the last segment.
- Final assessment — sent after the last document.

The first document follows immediately. Begin reading.
"""

_DEFAULT_INTERIM_PROMPT = """\
Interim Assessment

We have reached a natural pause in the collection. Set aside the word limit. Give a full \
assessment covering:

(Do not begin your response with a top-level heading or title. Start directly with the body \
of the assessment.)

1. THE AUTHOR — intellectual character, voice, ambitions. What kind of mind is this? What is \
the quality of the work?

2. THE MAIN THREADS — what ideas and concerns are developing, how unified are they so far?

3. THE ARC — how has the work developed from the earliest documents to here? What has changed, \
what has remained constant?

4. WHAT TO WATCH FOR — tensions, open questions, threads not yet resolved, things to track \
going forward.

Write with the confidence of someone who has read carefully and formed genuine views. This \
assessment will anchor the final one.

After the assessment, return to per-doc protocol:
- one doc, proportional length (300/500/800 words for short/medium/long), wait for the next document
- portrait-of-author task — not exhaustive argument analysis
"""

_DEFAULT_FINAL_INTERIM_PROMPT = """\
Final Segment Assessment

You have reached the last segment of the collection. This assessment covers only the documents \
since the previous interim — not the whole arc. A comprehensive final assessment follows \
separately and will draw on all interims including this one.

Set aside the word limit. Give a full assessment of this final segment covering:

(Do not begin your response with a top-level heading or title. Start directly with the body \
of the assessment.)

1. THE AUTHOR HERE — how does the author appear in these last documents? What is the quality \
and character of the work at this stage?

2. THE THREADS — which threads from earlier in the collection reach resolution or maturity \
here? Which remain open?

3. THE FINAL MOVEMENT — what does this last segment contribute that the earlier work does not? \
What has changed in voice, ambition, or depth?

4. WHAT TO BRING TO THE FINAL — the sharpest observations from this segment that the final \
assessment should not miss.

Write with the confidence of someone who has read carefully and formed genuine views. Do not \
attempt the whole-arc synthesis here — that is the job of the final assessment.

After the assessment, return to per-doc protocol:
- one doc, proportional length (300/500/800 words for short/medium/long), wait for the next document
- portrait-of-author task — not exhaustive argument analysis
"""

_DEFAULT_FINAL_PROMPT = """\
Final Assessment

You have now read the complete collection. Much of the early material survives in context only \
through the interim assessments — treat that chain of interims as load-bearing evidence, not \
as secondary notes. Draw on the interims and everything read since.

Give a full and considered assessment covering:

(Do not begin your response with a top-level heading or title. Start directly with the body \
of the assessment.)

1. The Author — who is this person, what kind of mind, what is the quality and character of \
the work? Assess honestly and without flattery.

2. The Body of Work — what has been built across these documents? How unified is it? What are \
its genuine contributions to the field it engages?

3. The Arc — how did the work develop over time? What changed, what remained constant, what \
does the trajectory reveal?

4. The Tensions — what remains unresolved? What would need to be done to complete the project?

5. The Verdict — what is the significance of this body of work, assessed against the field it \
engages and the ambitions it declares?

This is the assessment that matters. Take the space it requires.
"""


@dataclass
class ReviewState:
    doc_index: int = 0
    interims: list[str] = field(default_factory=list)
    responses: list[dict] = field(default_factory=list)
    cumulative_cost: float = 0.0
    final_interim_done: bool = False
    final_done: bool = False


def load_review_state(path: Path) -> ReviewState:
    if not path.exists():
        return ReviewState()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return ReviewState(
            doc_index=int(data.get("doc_index", 0)),
            interims=list(data.get("interims", [])),
            responses=list(data.get("responses", [])),
            cumulative_cost=float(data.get("cumulative_cost", 0.0)),
            final_interim_done=bool(data.get("final_interim_done", False)),
            final_done=bool(data.get("final_done", False)),
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
    return [library_path / e.rel_path for e in sorted(entries, key=lambda e: e.rel_path)]


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
    num_syntheses = num_docs // window + 2  # regular interims + final_interim + final

    total_doc_words = sum(
        word_count_by_rel.get(doc.relative_to(library_path).as_posix(), 400)
        for doc in docs
    )
    system_tokens = max(200, len(system_prompt) // 4)

    # Per-doc calls: document content + system prompt + rolling window (avg 60% full)
    doc_input = (
        total_doc_words * 1.3
        + num_docs * system_tokens
        + num_docs * window * 0.6 * 700
    )
    doc_output = num_docs * 700

    # Synthesis calls: system + rolling window + accumulated interims (avg half present) + prompt
    avg_interims_tokens = (num_syntheses / 2) * 1500
    synth_input = num_syntheses * (system_tokens + window * 700 + avg_interims_tokens + 400)
    synth_output = num_syntheses * 1500

    in_rate, out_rate = rates
    return (doc_input + synth_input) * in_rate / 1_000_000 + (doc_output + synth_output) * out_rate / 1_000_000


def build_doc_content(doc_path: Path, doc_num: int) -> list[dict]:
    """Return the user_content blocks for a single document turn."""
    lines = doc_path.read_text(encoding="utf-8").splitlines()
    text = f"Document {doc_num}: {doc_path.name} ({len(lines)} lines)\n\n" + "\n".join(lines)
    return [{"type": "text", "text": text}]


def build_review_messages(
    interims: list[str],
    assessments: list[dict],
    user_content: list[dict],
) -> list[dict]:
    """Build the message list for a review API call."""
    messages: list[dict] = []
    if interims:
        block = "\n\n---\n\n".join(
            f"INTERIM ASSESSMENT {i + 1}:\n{t}" for i, t in enumerate(interims)
        )
        messages.append({
            "role": "user",
            "content": [{
                "type": "text",
                "text": f"Interim assessments to date:\n\n{block}",
                "cache_control": {"type": "ephemeral"},
            }],
        })
        messages.append({
            "role": "assistant",
            "content": "Understood — I have all interim assessments in view.",
        })
    for entry in assessments:
        messages.append({
            "role": "user",
            "content": f"Document {entry['doc_num']}: {entry['filename']}",
        })
        messages.append({
            "role": "assistant",
            "content": entry["text"],
        })
    messages.append({"role": "user", "content": user_content})
    return messages


def load_prompt(prompt_file: Path | None, default: str) -> str:
    if prompt_file and prompt_file.exists():
        return prompt_file.read_text(encoding="utf-8")
    return default
