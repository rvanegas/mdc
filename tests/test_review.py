"""Tests for mdc.review message-building functions.

Each test verifies the exact structure sent to the model for a given step
of the review process: segment interim, individual doc review, final assessment.
"""

from pathlib import Path

import pytest

from mdc.review import (
    _DEFAULT_INTERIM_PROMPT,
    build_doc_review_messages,
    build_final_messages,
    build_interim_messages,
    build_segment_content,
    extract_doc_heading,
    parse_theme_file,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _doc(tmp_path: Path, name: str, title: str, body: str = "") -> Path:
    """Write a minimal markdown document and return its path."""
    text = f"\n# {title}\n2024-01-01\n\n{body}"
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def _content_texts(messages: list[dict]) -> list[str]:
    """Return the text values of all content blocks in the first message."""
    return [block["text"] for block in messages[0]["content"]]


def _cache_controlled(messages: list[dict]) -> list[str]:
    """Return text values of content blocks that carry cache_control."""
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
# build_segment_content
# ---------------------------------------------------------------------------

def test_build_segment_content_labels_each_doc(tmp_path):
    a = _doc(tmp_path, "2024-01-01-alpha.md", "Alpha Doc", "body of alpha")
    b = _doc(tmp_path, "2024-02-01-beta.md", "Beta Doc", "body of beta")
    text = build_segment_content([a, b])
    assert '"Alpha Doc" (2024-01-01)' in text
    assert '"Beta Doc" (2024-02-01)' in text
    assert "body of alpha" in text
    assert "body of beta" in text
    assert "---" in text  # separator between docs


def test_build_segment_content_single_doc_no_separator(tmp_path):
    a = _doc(tmp_path, "2024-01-01-alpha.md", "Alpha Doc", "body")
    text = build_segment_content([a])
    assert "---" not in text


# ---------------------------------------------------------------------------
# build_interim_messages
# ---------------------------------------------------------------------------

def test_interim_messages_structure(tmp_path):
    a = _doc(tmp_path, "2024-01-01-alpha.md", "Alpha Doc")
    msgs = build_interim_messages([a], _DEFAULT_INTERIM_PROMPT)

    assert len(msgs) == 1
    assert msgs[0]["role"] == "user"
    content = msgs[0]["content"]
    assert len(content) == 2


def test_interim_messages_segment_text_is_cached(tmp_path):
    a = _doc(tmp_path, "2024-01-01-alpha.md", "Alpha Doc", "body of alpha")
    msgs = build_interim_messages([a], _DEFAULT_INTERIM_PROMPT)
    cached = _cache_controlled(msgs)
    assert len(cached) == 1
    assert "body of alpha" in cached[0]


def test_interim_messages_prompt_is_not_cached(tmp_path):
    a = _doc(tmp_path, "2024-01-01-alpha.md", "Alpha Doc")
    prompt = "Assess this segment."
    msgs = build_interim_messages([a], prompt)
    content = msgs[0]["content"]
    prompt_block = content[-1]
    assert prompt_block["text"] == prompt
    assert "cache_control" not in prompt_block


def test_interim_messages_contains_doc_label(tmp_path):
    a = _doc(tmp_path, "2024-03-15-my-essay.md", "My Essay")
    msgs = build_interim_messages([a], "prompt")
    segment_text = msgs[0]["content"][0]["text"]
    assert '"My Essay" (2024-03-15)' in segment_text


# ---------------------------------------------------------------------------
# build_doc_review_messages — no related docs
# ---------------------------------------------------------------------------

def test_doc_review_messages_structure_no_related(tmp_path):
    doc = _doc(tmp_path, "2024-01-01-target.md", "Target Doc", "some content")
    msgs = build_doc_review_messages(doc)

    assert len(msgs) == 1
    assert msgs[0]["role"] == "user"
    content = msgs[0]["content"]
    # Without related docs: document block + prompt block only.
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
    assert "this document" not in prompt


def test_doc_review_word_limit_short_doc(tmp_path):
    body = " ".join(["word"] * 500)  # < 1000 words → 300-word limit
    doc = _doc(tmp_path, "2024-01-01-short.md", "Short Doc", body)
    msgs = build_doc_review_messages(doc)
    prompt = msgs[0]["content"][-1]["text"]
    assert "300-word" in prompt


def test_doc_review_word_limit_medium_doc(tmp_path):
    body = " ".join(["word"] * 2000)  # 1000–3000 words → 500-word limit
    doc = _doc(tmp_path, "2024-01-01-medium.md", "Medium Doc", body)
    msgs = build_doc_review_messages(doc)
    prompt = msgs[0]["content"][-1]["text"]
    assert "500-word" in prompt


def test_doc_review_word_limit_long_doc(tmp_path):
    body = " ".join(["word"] * 4000)  # > 3000 words → 800-word limit
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
    title_to_path = {"Prior Doc": prior}
    reviews = {prior.name: "This is the review of Prior Doc."}

    msgs = build_doc_review_messages(doc, title_to_path=title_to_path, reviews=reviews)
    content = msgs[0]["content"]

    # With related docs: related reviews block + document block + prompt block.
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

    # Related doc exists in title_to_path but has no cached review — silently omitted.
    msgs = build_doc_review_messages(
        doc, title_to_path={"Prior Doc": prior}, reviews={}
    )
    content = msgs[0]["content"]
    # No related block — just document block + prompt block.
    assert len(content) == 2


# ---------------------------------------------------------------------------
# build_final_messages
# ---------------------------------------------------------------------------

def _interim(n: int, text: str = "interim text") -> dict:
    return {"header": f"Segment {n} (docs 1–40)", "text": text, "after_doc": n * 40}


def test_final_messages_structure_minimal(tmp_path):
    msgs = build_final_messages([_interim(1)], "Final prompt.")
    assert len(msgs) == 1
    assert msgs[0]["role"] == "user"
    content = msgs[0]["content"]
    # Interims block (cached) + final prompt.
    assert len(content) == 2


def test_final_messages_interims_are_cached():
    msgs = build_final_messages([_interim(1), _interim(2)], "prompt")
    cached = _cache_controlled(msgs)
    assert len(cached) == 1
    assert "interim text" in cached[0]


def test_final_messages_prompt_is_not_cached():
    msgs = build_final_messages([_interim(1)], "My final prompt.")
    prompt_block = msgs[0]["content"][-1]
    assert prompt_block["text"] == "My final prompt."
    assert "cache_control" not in prompt_block


def test_final_messages_with_manifest_summaries():
    msgs = build_final_messages([_interim(1)], "prompt", manifest_summaries="summary text")
    content = msgs[0]["content"]
    # Interims (cached) + manifest summaries (cached) + prompt.
    assert len(content) == 3
    cached = _cache_controlled(msgs)
    assert any("summary text" in t for t in cached)


def test_final_messages_with_selected_reviews():
    msgs = build_final_messages([_interim(1)], "prompt", selected_reviews="review text")
    content = msgs[0]["content"]
    # Interims (cached) + reviews (not cached) + prompt.
    assert len(content) == 3
    texts = _content_texts(msgs)
    assert any("review text" in t for t in texts)
    # Reviews block should not be cache-controlled.
    review_block = content[1]
    assert "cache_control" not in review_block


def test_final_messages_interims_stripped_of_doc_counts():
    """Model sees 'Segment 3' not 'Segment 3 (docs 81–120)' in the interims block."""
    interim = {"header": "Segment 3 (docs 81–120)", "text": "content", "after_doc": 120}
    msgs = build_final_messages([interim], "prompt")
    interims_text = _cache_controlled(msgs)[0]
    assert "Segment 3:" in interims_text
    assert "(docs 81" not in interims_text


def test_final_messages_all_optional_blocks():
    msgs = build_final_messages(
        [_interim(1)],
        "prompt",
        selected_reviews="reviews",
        manifest_summaries="summaries",
    )
    content = msgs[0]["content"]
    # Interims (cached) + summaries (cached) + reviews + prompt.
    assert len(content) == 4


# ---------------------------------------------------------------------------
# parse_theme_file
# ---------------------------------------------------------------------------

def _theme_doc(tmp_path: Path, slug: str, terms: list[str], includes: list[str] = (), excludes: list[str] = ()) -> Path:
    lines = [f"# Theme: {slug.title()}", "", "## Terms"]
    lines += [f"- {t}" for t in terms]
    if includes:
        lines += ["", "## Include"] + [f"- {t}" for t in includes]
    if excludes:
        lines += ["", "## Exclude"] + [f"- {t}" for t in excludes]
    p = tmp_path / f"THEME-{slug}.md"
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def test_parse_theme_file_terms_only(tmp_path):
    p = _theme_doc(tmp_path, "epistemology", ["epistemology", "knowledge"])
    terms, includes, excludes, selection = parse_theme_file(p)
    assert terms == ["epistemology", "knowledge"]
    assert includes == []
    assert excludes == []
    assert selection == []


def test_parse_theme_file_with_include_and_exclude(tmp_path):
    p = _theme_doc(tmp_path, "ethics", ["ethics"], includes=["Paper A"], excludes=["Paper B"])
    terms, includes, excludes, selection = parse_theme_file(p)
    assert terms == ["ethics"]
    assert includes == ["Paper A"]
    assert excludes == ["Paper B"]


def test_parse_theme_file_empty_sections(tmp_path):
    content = "# Theme: Empty\n\n## Terms\n\n## Include\n\n## Exclude\n"
    p = tmp_path / "THEME-empty.md"
    p.write_text(content, encoding="utf-8")
    terms, includes, excludes, selection = parse_theme_file(p)
    assert terms == []
    assert includes == []
    assert excludes == []
    assert selection == []


def test_parse_theme_file_ignores_unknown_sections(tmp_path):
    content = "# Theme: Test\n\n## Terms\n- logic\n\n## Notes\n- ignored\n"
    p = tmp_path / "THEME-test.md"
    p.write_text(content, encoding="utf-8")
    terms, includes, excludes, selection = parse_theme_file(p)
    assert terms == ["logic"]
    assert includes == []


def test_parse_theme_file_selection(tmp_path):
    content = "# Theme: Test\n\n## Terms\n- logic\n\n## Auto-Included\n<!-- 2 documents, ~50k tokens estimated -->\n- Paper One\n- Paper Two\n"
    p = tmp_path / "THEME-test.md"
    p.write_text(content, encoding="utf-8")
    terms, _, _, selection = parse_theme_file(p)
    assert terms == ["logic"]
    assert selection == ["Paper One", "Paper Two"]
