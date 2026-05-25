"""Tests for mdc.transcript — parsing, appending, reference management."""

import pytest

from mdc.transcript import (
    TranscriptError,
    append_assistant_reply,
    extract_references,
    extract_related,
    insert_references,
    parse_transcript,
    update_references_section,
    update_related_section,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make(turns: str, *, title: str = "My Doc", date: str = "2024-01-01", tail: str = "") -> str:
    """Build a minimal valid transcript string."""
    return f"\n# {title}\n{date}\n\n{turns}{tail}"


def _human_turn(text: str = "Hello there.") -> str:
    return f"## Human\n\n{text}\n\n"


def _assistant_turn(text: str = "Hello back.") -> str:
    return f"## Claude\n\n{text}\n\n"


# ---------------------------------------------------------------------------
# parse_transcript — happy paths
# ---------------------------------------------------------------------------

def test_parse_single_human_turn():
    text = _make(_human_turn())
    t = parse_transcript(text)
    assert len(t.turns) == 1
    assert t.turns[0].speaker == "Human"
    assert not t.turns[0].is_assistant


def test_parse_exchange_no_pending():
    text = _make(_human_turn() + _assistant_turn())
    t = parse_transcript(text)
    assert len(t.turns) == 2
    assert t.pending_turn is None
    assert not t.pending


def test_parse_pending_turn_detected():
    text = _make(_human_turn() + _assistant_turn() + _human_turn("Follow-up."))
    t = parse_transcript(text)
    assert t.pending
    assert t.pending_turn is not None
    assert t.pending_turn.speaker == "Human"


def test_parse_preamble_title_and_date():
    text = _make(_human_turn(), title="My Essay", date="2025-06-15")
    t = parse_transcript(text)
    assert t.preamble.title == "My Essay"
    assert t.preamble.date == "2025-06-15"


def test_parse_custom_assistant_name():
    text = _make("## GPT\n\nReply text.\n\n", title="Chat", date="2024-03-01")
    t = parse_transcript(text, assistant_name="GPT")
    assert t.turns[0].is_assistant


# ---------------------------------------------------------------------------
# parse_transcript — references, notes, related
# ---------------------------------------------------------------------------

def test_parse_references_extracted():
    tail = "## References\n\n| Smith, John (2020) *A Book*\n"
    text = _make(_human_turn() + _assistant_turn(), tail=tail)
    t = parse_transcript(text)
    assert len(t.references) == 1
    assert "Smith" in t.references[0]


def test_parse_references_not_a_turn():
    tail = "## References\n\n| Smith, John (2020) *A Book*\n"
    text = _make(_human_turn() + _assistant_turn(), tail=tail)
    t = parse_transcript(text)
    speakers = [turn.speaker for turn in t.turns]
    assert "References" not in speakers


def test_parse_notes_extracted():
    tail = "## Notes\n\n| [1] First note.\n"
    text = _make(_human_turn() + _assistant_turn(), tail=tail)
    t = parse_transcript(text)
    assert "| [1] First note." in t.notes


def test_parse_related_extracted():
    tail = "## Related\n\n| *Some Title*\n"
    text = _make(_human_turn() + _assistant_turn(), tail=tail)
    t = parse_transcript(text)
    assert "| *Some Title*" in t.related


def test_parse_notes_after_references_raises():
    tail = "## References\n\n| Smith, J. (2020) *Title*\n\n## Notes\n\n| [1] note\n"
    text = _make(_human_turn(), tail=tail)
    with pytest.raises(TranscriptError, match="Notes"):
        parse_transcript(text)


def test_parse_related_after_references_raises():
    tail = "## References\n\n| Smith, J. (2020) *Title*\n\n## Related\n\n| *Doc*\n"
    text = _make(_human_turn(), tail=tail)
    with pytest.raises(TranscriptError, match="Related"):
        parse_transcript(text)


# ---------------------------------------------------------------------------
# parse_transcript — errors
# ---------------------------------------------------------------------------

def test_parse_no_sections_raises():
    with pytest.raises(TranscriptError, match="section heading"):
        parse_transcript("\n# Title\n2024-01-01\n\nNo sections here.\n")


def test_parse_yaml_frontmatter_raises():
    with pytest.raises(TranscriptError, match="frontmatter"):
        parse_transcript("---\ntitle: test\n---\n")


def test_parse_missing_preamble_raises():
    with pytest.raises(TranscriptError):
        parse_transcript("## Human\n\nHello.\n")


def test_parse_blank_first_line_required():
    with pytest.raises(TranscriptError):
        parse_transcript("# Title\n2024-01-01\n\n## Human\n\nHello.\n")


def test_parse_bad_date_format_raises():
    with pytest.raises(TranscriptError, match="yyyy-mm-dd"):
        parse_transcript("\n# Title\n01-01-2024\n\n## Human\n\nHello.\n")


def test_parse_two_assistant_turns_raises():
    text = _make(_human_turn() + _assistant_turn() + _assistant_turn())
    with pytest.raises(TranscriptError, match="two"):
        parse_transcript(text)


def test_parse_empty_human_turn_raises():
    text = _make("## Human\n\n\n\n")
    with pytest.raises(TranscriptError, match="empty"):
        parse_transcript(text)


def test_parse_empty_assistant_turn_raises():
    text = _make(_human_turn() + "## Claude\n\n\n\n")
    with pytest.raises(TranscriptError, match="empty"):
        parse_transcript(text)


# ---------------------------------------------------------------------------
# append_assistant_reply
# ---------------------------------------------------------------------------

def test_append_basic():
    text = _make(_human_turn("Question."))
    result = append_assistant_reply(text, "Answer.")
    assert "## Claude\n\nAnswer." in result


def test_append_inserts_before_references():
    tail = "## References\n\n| Smith, J. (2020) *Book*\n"
    text = _make(_human_turn(), tail=tail)
    result = append_assistant_reply(text, "Reply.")
    assert result.index("## Claude") < result.index("## References")


def test_append_inserts_before_related():
    tail = "## Related\n\n| *Some Title*\n"
    text = _make(_human_turn(), tail=tail)
    result = append_assistant_reply(text, "Reply.")
    assert result.index("## Claude") < result.index("## Related")


def test_append_inserts_before_notes():
    tail = "## Notes\n\n| [1] A note.\n"
    text = _make(_human_turn(), tail=tail)
    result = append_assistant_reply(text, "Reply.")
    assert result.index("## Claude") < result.index("## Notes")


def test_append_inserts_before_earliest_special_section():
    tail = "## Related\n\n| *Title*\n\n## References\n\n| A, B (2020) *X*\n"
    text = _make(_human_turn(), tail=tail)
    result = append_assistant_reply(text, "Reply.")
    assert result.index("## Claude") < result.index("## Related")


def test_append_empty_reply_raises():
    text = _make(_human_turn())
    with pytest.raises(TranscriptError, match="empty"):
        append_assistant_reply(text, "   ")


def test_append_strips_whitespace_from_reply():
    text = _make(_human_turn())
    result = append_assistant_reply(text, "\n\n  Trimmed.  \n\n")
    assert "## Claude\n\nTrimmed." in result


def test_append_custom_assistant_name():
    text = _make(_human_turn())
    result = append_assistant_reply(text, "Reply.", assistant_name="GPT")
    assert "## GPT\n\nReply." in result


# ---------------------------------------------------------------------------
# extract_references
# ---------------------------------------------------------------------------

def test_extract_refs_no_refs():
    body, refs = extract_references("Just some prose.")
    assert body == "Just some prose."
    assert refs == []


def test_extract_refs_trailing_refs():
    reply = "Body text.\n\n| Smith, J. (2020) *Title*\n| Jones, A. (2021) *Other*"
    body, refs = extract_references(reply)
    assert "Body text." in body
    assert len(refs) == 2
    assert all("*" in r for r in refs)


def test_extract_refs_strips_references_heading():
    reply = "Body.\n\nReferences\n| Smith, J. (2020) *Title*"
    body, refs = extract_references(reply)
    assert "References" not in body
    assert len(refs) == 1


def test_extract_refs_strips_markdown_references_heading():
    reply = "Body.\n\n## References\n| Smith, J. (2020) *Title*"
    body, refs = extract_references(reply)
    assert "## References" not in body
    assert len(refs) == 1


def test_extract_refs_non_ref_trailing_line_stops_extraction():
    reply = "Body.\n\nNot a reference.\n| Smith, J. (2020) *Title*"
    body, refs = extract_references(reply)
    assert len(refs) == 1
    assert "Not a reference." in body


# ---------------------------------------------------------------------------
# insert_references
# ---------------------------------------------------------------------------

def test_insert_refs_deduplicates():
    existing = ["| Smith, J. (2020) *Title*"]
    new = ["| Smith, J. (2020) *Title*"]
    result = insert_references(existing, new)
    assert result == existing


def test_insert_refs_sorted():
    existing = ["| Zhao, X. (2020) *Title*"]
    new = ["| Adams, A. (2019) *Earlier*"]
    result = insert_references(existing, new)
    assert result[0].startswith("| Adams")
    assert result[1].startswith("| Zhao")


def test_insert_refs_bce_sorts_before_ad():
    existing = ["| Plato (c. 380 BCE) *Republic*"]
    new = ["| Smith, J. (2020) *Modern*"]
    result = insert_references(existing, new)
    assert result[0].startswith("| Plato")
    assert result[1].startswith("| Smith")


def test_insert_refs_empty_existing():
    result = insert_references([], ["| Smith, J. (2020) *Title*"])
    assert len(result) == 1


# ---------------------------------------------------------------------------
# extract_related
# ---------------------------------------------------------------------------

def test_extract_related_none():
    body, titles = extract_related("Just prose.")
    assert body == "Just prose."
    assert titles == []


def test_extract_related_section_removed_from_body():
    reply = "Body text.\n\n## Related\n\n| *Some Title*\n"
    body, titles = extract_related(reply)
    assert "## Related" not in body
    assert "| *Some Title*" in titles


def test_extract_related_titles_only_pipe_lines():
    reply = "Body.\n\n## Related\n\nNot a pipe line.\n| *Actual Title*\n"
    _, titles = extract_related(reply)
    assert titles == ["| *Actual Title*"]


# ---------------------------------------------------------------------------
# update_references_section
# ---------------------------------------------------------------------------

def test_update_refs_section_appended_when_absent():
    text = _make(_human_turn() + _assistant_turn())
    refs = ["| Smith, J. (2020) *Title*"]
    result = update_references_section(text, refs)
    assert "## References" in result
    assert "| Smith, J." in result


def test_update_refs_section_replaces_existing():
    tail = "## References\n\n| Old, A. (2000) *Old Book*\n"
    text = _make(_human_turn() + _assistant_turn(), tail=tail)
    refs = ["| New, B. (2024) *New Book*"]
    result = update_references_section(text, refs)
    assert "| New, B." in result
    assert "| Old, A." not in result
    assert result.count("## References") == 1


# ---------------------------------------------------------------------------
# update_related_section
# ---------------------------------------------------------------------------

def test_update_related_inserted_before_references():
    tail = "## References\n\n| Smith, J. (2020) *Title*\n"
    text = _make(_human_turn() + _assistant_turn(), tail=tail)
    titles = ["| *A Related Title*"]
    result = update_related_section(text, titles)
    assert result.index("## Related") < result.index("## References")


def test_update_related_replaces_existing():
    tail = "## Related\n\n| *Old Title*\n"
    text = _make(_human_turn() + _assistant_turn(), tail=tail)
    result = update_related_section(text, ["| *New Title*"])
    assert "| *New Title*" in result
    assert "| *Old Title*" not in result
    assert result.count("## Related") == 1


def test_update_related_appended_when_no_references():
    text = _make(_human_turn() + _assistant_turn())
    result = update_related_section(text, ["| *Title*"])
    assert "## Related" in result
    assert "| *Title*" in result
