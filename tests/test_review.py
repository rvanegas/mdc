"""Tests for mdc.review message-building and storage functions."""

from pathlib import Path

import pytest

from mdc.review import (
    build_doc_review_messages,
    build_final_messages,
    extract_doc_heading,
    load_review_state,
    review_path_for,
    save_doc_review,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _doc(tmp_path: Path, name: str, title: str, body: str = "") -> Path:
    text = f"\n# {title}\n2024-01-01\n\n{body}"
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def _content_texts(messages: list[dict]) -> list[str]:
    return [block["text"] for block in messages[0]["content"]]


def _cache_controlled(messages: list[dict]) -> list[str]:
    return [
        block["text"]
        for block in messages[0]["content"]
        if "cache_control" in block
    ]


# ---------------------------------------------------------------------------
# extract_doc_heading
# ---------------------------------------------------------------------------

def test_extract_doc_heading_reads_title(tmp_path):
    p = _doc(tmp_path, "2024-01-01-foo.md", "My Title")
    assert extract_doc_heading(p) == "My Title"


def test_extract_doc_heading_falls_back_to_slug(tmp_path):
    p = tmp_path / "2024-01-01-some-doc.md"
    p.write_text("no heading here", encoding="utf-8")
    assert extract_doc_heading(p) == "2024 01 01 Some Doc"


# ---------------------------------------------------------------------------
# review_path_for
# ---------------------------------------------------------------------------

def test_review_path_for_bare_md(tmp_path):
    doc = tmp_path / "2024-01-01-essay.md"
    assert review_path_for(doc) == tmp_path / "2024-01-01-essay.review.md"


def test_review_path_for_document_md(tmp_path):
    doc = tmp_path / "2024-01-01-essay.document.md"
    assert review_path_for(doc) == tmp_path / "2024-01-01-essay.document.review.md"


# ---------------------------------------------------------------------------
# save_doc_review / load_review_state
# ---------------------------------------------------------------------------

def test_save_doc_review_writes_text(tmp_path):
    rp = tmp_path / "2024-01-01-essay.review.md"
    save_doc_review(rp, "This is the review.")
    assert rp.read_text(encoding="utf-8") == "This is the review."


def test_load_review_state_empty_dir(tmp_path):
    state = load_review_state(tmp_path)
    assert state.doc_reviews == []


def test_load_review_state_reads_review_files(tmp_path):
    _doc(tmp_path, "2024-01-15-alpha.md", "Alpha Doc")
    save_doc_review(tmp_path / "2024-01-15-alpha.review.md", "Alpha review text.")
    state = load_review_state(tmp_path)
    assert len(state.doc_reviews) == 1
    entry = state.doc_reviews[0]
    assert entry["filename"] == "2024-01-15-alpha.md"
    assert entry["text"] == "Alpha review text."
    assert '"Alpha Doc"' in entry["label"]


def test_load_review_state_derives_label(tmp_path):
    _doc(tmp_path, "2024-03-20-my-essay.md", "My Essay")
    save_doc_review(tmp_path / "2024-03-20-my-essay.review.md", "review")
    state = load_review_state(tmp_path)
    assert state.doc_reviews[0]["label"] == '"My Essay" (2024-03-20)'


def test_load_review_state_multiple_files(tmp_path):
    _doc(tmp_path, "2024-01-01-a.md", "A")
    _doc(tmp_path, "2024-02-01-b.md", "B")
    save_doc_review(tmp_path / "2024-01-01-a.review.md", "review a")
    save_doc_review(tmp_path / "2024-02-01-b.review.md", "review b")
    state = load_review_state(tmp_path)
    filenames = {e["filename"] for e in state.doc_reviews}
    assert filenames == {"2024-01-01-a.md", "2024-02-01-b.md"}


def test_load_review_state_document_companion(tmp_path):
    _doc(tmp_path, "2024-01-01-essay.document.md", "Essay Doc")
    save_doc_review(tmp_path / "2024-01-01-essay.document.review.md", "doc review")
    state = load_review_state(tmp_path)
    assert state.doc_reviews[0]["filename"] == "2024-01-01-essay.document.md"


# ---------------------------------------------------------------------------
# build_doc_review_messages — no related docs
# ---------------------------------------------------------------------------

def test_doc_review_messages_structure_no_related(tmp_path):
    doc = _doc(tmp_path, "2024-01-01-target.md", "Target Doc", "some content")
    msgs = build_doc_review_messages(doc)
    assert len(msgs) == 1
    assert msgs[0]["role"] == "user"
    content = msgs[0]["content"]
    assert len(content) == 2


def test_doc_review_messages_document_block(tmp_path):
    doc = _doc(tmp_path, "2024-01-01-target.md", "Target Doc", "specific body text")
    msgs = build_doc_review_messages(doc)
    doc_block = msgs[0]["content"][0]
    assert '"Target Doc" (2024-01-01)' in doc_block["text"]
    assert "specific body text" in doc_block["text"]
    assert "cache_control" not in doc_block


def test_doc_review_prompt_names_document_explicitly(tmp_path):
    doc = _doc(tmp_path, "2024-01-01-target.md", "Target Doc", "body")
    msgs = build_doc_review_messages(doc)
    prompt = msgs[0]["content"][-1]["text"]
    assert '"Target Doc" (2024-01-01)' in prompt


def test_doc_review_word_limit_short_doc(tmp_path):
    body = " ".join(["word"] * 500)
    doc = _doc(tmp_path, "2024-01-01-short.md", "Short Doc", body)
    msgs = build_doc_review_messages(doc)
    prompt = msgs[0]["content"][-1]["text"]
    assert "300-word" in prompt


def test_doc_review_word_limit_medium_doc(tmp_path):
    body = " ".join(["word"] * 2000)
    doc = _doc(tmp_path, "2024-01-01-medium.md", "Medium Doc", body)
    msgs = build_doc_review_messages(doc)
    prompt = msgs[0]["content"][-1]["text"]
    assert "500-word" in prompt


def test_doc_review_word_limit_long_doc(tmp_path):
    body = " ".join(["word"] * 4000)
    doc = _doc(tmp_path, "2024-01-01-long.md", "Long Doc", body)
    msgs = build_doc_review_messages(doc)
    prompt = msgs[0]["content"][-1]["text"]
    assert "800-word" in prompt


# ---------------------------------------------------------------------------
# build_doc_review_messages — with related docs
# ---------------------------------------------------------------------------

def _related_section(title: str) -> str:
    return f"\n## Related\n\n| *{title}*\n"


def test_doc_review_with_related_uses_review_text(tmp_path):
    prior = _doc(tmp_path, "2024-01-01-prior.md", "Prior Doc", "prior body")
    body = "target body" + _related_section("Prior Doc")
    doc = _doc(tmp_path, "2024-06-01-target.md", "Target Doc", body)
    reviews = {prior.name: "This is the review of Prior Doc."}
    msgs = build_doc_review_messages(doc, title_to_path={"Prior Doc": prior}, reviews=reviews)
    content = msgs[0]["content"]
    assert len(content) == 3


def test_doc_review_related_block_is_cached(tmp_path):
    prior = _doc(tmp_path, "2024-01-01-prior.md", "Prior Doc")
    body = "target body" + _related_section("Prior Doc")
    doc = _doc(tmp_path, "2024-06-01-target.md", "Target Doc", body)
    reviews = {prior.name: "Review of Prior Doc."}
    msgs = build_doc_review_messages(
        doc, title_to_path={"Prior Doc": prior}, reviews=reviews
    )
    related_block = msgs[0]["content"][0]
    assert "cache_control" in related_block
    assert "Review of Prior Doc." in related_block["text"]


def test_doc_review_related_block_contains_review_not_full_text(tmp_path):
    prior = _doc(tmp_path, "2024-01-01-prior.md", "Prior Doc", "SECRET PRIOR BODY")
    body = "target body" + _related_section("Prior Doc")
    doc = _doc(tmp_path, "2024-06-01-target.md", "Target Doc", body)
    reviews = {prior.name: "Cached review text."}
    msgs = build_doc_review_messages(
        doc, title_to_path={"Prior Doc": prior}, reviews=reviews
    )
    full_text = " ".join(_content_texts(msgs))
    assert "Cached review text." in full_text
    assert "SECRET PRIOR BODY" not in full_text


def test_doc_review_prompt_mentions_related_context(tmp_path):
    prior = _doc(tmp_path, "2024-01-01-prior.md", "Prior Doc")
    body = "body" + _related_section("Prior Doc")
    doc = _doc(tmp_path, "2024-06-01-target.md", "Target Doc", body)
    reviews = {prior.name: "review text"}
    msgs = build_doc_review_messages(
        doc, title_to_path={"Prior Doc": prior}, reviews=reviews
    )
    prompt = msgs[0]["content"][-1]["text"]
    assert "related" in prompt.lower()


def test_doc_review_skips_related_without_review(tmp_path):
    prior = _doc(tmp_path, "2024-01-01-prior.md", "Prior Doc")
    body = "body" + _related_section("Prior Doc")
    doc = _doc(tmp_path, "2024-06-01-target.md", "Target Doc", body)
    msgs = build_doc_review_messages(
        doc, title_to_path={"Prior Doc": prior}, reviews={}
    )
    content = msgs[0]["content"]
    assert len(content) == 2


# ---------------------------------------------------------------------------
# build_final_messages
# ---------------------------------------------------------------------------

def test_final_messages_structure():
    msgs = build_final_messages([("Theme A", "assessment text")], "Final prompt.")
    assert len(msgs) == 1
    assert msgs[0]["role"] == "user"
    content = msgs[0]["content"]
    assert len(content) == 2


def test_final_messages_assessments_are_cached():
    msgs = build_final_messages([("Theme A", "text a"), ("Theme B", "text b")], "prompt")
    cached = _cache_controlled(msgs)
    assert len(cached) == 1
    assert "text a" in cached[0]
    assert "text b" in cached[0]


def test_final_messages_prompt_is_not_cached():
    msgs = build_final_messages([("Theme A", "text")], "My final prompt.")
    prompt_block = msgs[0]["content"][-1]
    assert prompt_block["text"] == "My final prompt."
    assert "cache_control" not in prompt_block


def test_final_messages_assessment_block_contains_theme_names():
    msgs = build_final_messages([("Epistemology", "ep text"), ("Ethics", "eth text")], "prompt")
    block_text = _cache_controlled(msgs)[0]
    assert "Epistemology" in block_text
    assert "Ethics" in block_text
