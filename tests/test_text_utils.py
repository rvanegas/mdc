"""Tests for pure text-transformation helpers."""

import pytest

from mdc.text_utils import _parse_index_reply, _upgrade_reply_headings, wrap_paragraphs


# ---------------------------------------------------------------------------
# wrap_paragraphs
# ---------------------------------------------------------------------------

def test_wrap_short_line_unchanged():
    text = "Short line."
    assert wrap_paragraphs(text, width=80) == text


def test_wrap_long_prose_wrapped():
    words = " ".join(["word"] * 30)
    result = wrap_paragraphs(words, width=40)
    for line in result.split("\n"):
        assert len(line) <= 40


def test_wrap_code_fence_preserved():
    text = "Before.\n\n```python\nx = 1 + 2 + 3 + 4 + 5 + 6 + 7 + 8 + 9 + 10 + 11 + 12 + 13 + 14\n```\n\nAfter."
    result = wrap_paragraphs(text, width=40)
    assert "x = 1 + 2 + 3 + 4 + 5 + 6 + 7 + 8 + 9 + 10 + 11 + 12 + 13 + 14" in result


def test_wrap_list_items_preserved():
    text = "- This is a list item that is quite long and would normally be wrapped by the algorithm"
    result = wrap_paragraphs(text, width=40)
    assert result == text


def test_wrap_heading_preserved():
    text = "# This is a heading that is quite long and would normally be wrapped by the algorithm"
    result = wrap_paragraphs(text, width=40)
    assert result == text


def test_wrap_pipe_lines_preserved():
    text = "| Smith, John (2020) *A Very Long Book Title That Would Otherwise Wrap At Forty Characters*"
    result = wrap_paragraphs(text, width=40)
    assert result == text


def test_wrap_blockquote_wrapped():
    long_quote = "> " + " ".join(["word"] * 30)
    result = wrap_paragraphs(long_quote, width=60)
    lines = result.split("\n")
    assert all(line.startswith("> ") for line in lines if line)
    assert any(len(line) < len(long_quote) for line in lines)


def test_wrap_tilde_code_fence_preserved():
    text = "Text.\n\n~~~\nlong line " + "x" * 100 + "\n~~~\n"
    result = wrap_paragraphs(text, width=40)
    assert "x" * 100 in result


def test_wrap_multiple_paragraphs():
    text = "First paragraph.\n\nSecond paragraph."
    result = wrap_paragraphs(text, width=80)
    assert "First paragraph." in result
    assert "Second paragraph." in result


# ---------------------------------------------------------------------------
# _upgrade_reply_headings
# ---------------------------------------------------------------------------

def test_upgrade_h1_to_h3():
    result = _upgrade_reply_headings("# Section Title")
    assert result == "### Section Title"


def test_upgrade_h2_to_h3():
    result = _upgrade_reply_headings("## Section Title")
    assert result == "### Section Title"


def test_upgrade_h3_unchanged():
    result = _upgrade_reply_headings("### Already Deep")
    assert result == "### Already Deep"


def test_upgrade_h4_unchanged():
    result = _upgrade_reply_headings("#### Very Deep")
    assert result == "#### Very Deep"


def test_upgrade_references_exempt():
    result = _upgrade_reply_headings("## References")
    assert result == "## References"


def test_upgrade_related_exempt():
    result = _upgrade_reply_headings("## Related")
    assert result == "## Related"


def test_upgrade_mixed_content():
    text = "## Intro\n\nSome prose.\n\n# Big Heading\n\n## References\n"
    result = _upgrade_reply_headings(text)
    assert "### Intro" in result
    assert "### Big Heading" in result
    assert "## References" in result


def test_upgrade_only_at_line_start():
    result = _upgrade_reply_headings("Text with ## inline hash")
    assert "## inline hash" in result


# ---------------------------------------------------------------------------
# _parse_index_reply
# ---------------------------------------------------------------------------

def test_parse_index_reply_basic():
    text = "SUMMARY: A document about things.\nTERMS: foo; bar; baz"
    summary, terms = _parse_index_reply(text)
    assert summary == "A document about things."
    assert terms == ["foo", "bar", "baz"]


def test_parse_index_reply_strips_whitespace():
    text = "SUMMARY:  Leading space.  \nTERMS:  foo ; bar "
    summary, terms = _parse_index_reply(text)
    assert summary == "Leading space."
    assert terms == ["foo", "bar"]


def test_parse_index_reply_multiline_summary():
    text = "SUMMARY: First line\ncontinued here\nTERMS: foo; bar"
    summary, terms = _parse_index_reply(text)
    assert "First line" in summary
    assert "continued here" in summary


def test_parse_index_reply_empty_terms():
    text = "SUMMARY: A summary.\nTERMS:"
    summary, terms = _parse_index_reply(text)
    assert summary == "A summary."
    assert terms == []


def test_parse_index_reply_many_terms():
    term_list = "; ".join([f"term{i}" for i in range(10)])
    text = f"SUMMARY: Summary.\nTERMS: {term_list}"
    _, terms = _parse_index_reply(text)
    assert len(terms) == 10


def test_parse_index_reply_no_summary_line():
    text = "TERMS: foo; bar"
    summary, terms = _parse_index_reply(text)
    assert summary == ""
    assert terms == ["foo", "bar"]
