"""Tests for mdc.form — format validation and auto-fixing."""

import pytest
from pathlib import Path

from mdc.form import (
    check_file,
    fix_object_replacement,
    fix_rtl_spans,
    fix_section_spacing,
    fix_title_section,
    slugify,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


def _valid(tmp_path: Path, title: str = "My Doc", date: str = "2024-01-01", body: str = "") -> Path:
    slug = slugify(title)
    name = f"{date}-{slug}.md"
    content = f"\n# {title}\n{date}\n\n## Human\n\nHello.\n\n## Claude\n\nReply.\n"
    return _write(tmp_path, name, content)


# ---------------------------------------------------------------------------
# slugify
# ---------------------------------------------------------------------------

def test_slugify_basic():
    assert slugify("Hello World") == "hello-world"


def test_slugify_strips_punctuation():
    assert slugify("Hello, World!") == "hello-world"


def test_slugify_collapses_spaces():
    assert slugify("Too  Many   Spaces") == "too-many-spaces"


def test_slugify_collapses_hyphens():
    assert slugify("already--hyphenated") == "already-hyphenated"


def test_slugify_strips_leading_trailing_hyphens():
    assert slugify("!Leading and trailing!") == "leading-and-trailing"


def test_slugify_preserves_existing_hyphens():
    assert slugify("self-referential") == "self-referential"


# ---------------------------------------------------------------------------
# fix_title_section
# ---------------------------------------------------------------------------

def test_fix_title_section_inserts_blank_first_line():
    lines = ["# Title", "2024-01-01", "", "## Human"]
    new_lines, fixes = fix_title_section(lines)
    assert new_lines[0] == ""
    assert any("blank first line" in f for f in fixes)


def test_fix_title_section_removes_asterisk_delimiters():
    lines = ["", "# Title", "*2024-01-01*", ""]
    new_lines, fixes = fix_title_section(lines)
    assert new_lines[2] == "2024-01-01"
    assert any("*" in f for f in fixes)


def test_fix_title_section_removes_leading_asterisk():
    lines = ["", "# Title", "*2024-01-01", ""]
    new_lines, fixes = fix_title_section(lines)
    assert new_lines[2] == "2024-01-01"


def test_fix_title_section_removes_trailing_asterisk():
    lines = ["", "# Title", "2024-01-01*", ""]
    new_lines, fixes = fix_title_section(lines)
    assert new_lines[2] == "2024-01-01"


def test_fix_title_section_inserts_blank_after_date():
    lines = ["", "# Title", "2024-01-01", "## Human"]
    new_lines, fixes = fix_title_section(lines)
    assert new_lines[3] == ""
    assert any("blank line after date" in f for f in fixes)


def test_fix_title_section_replaces_chatgpt_header():
    lines = ["", "# Title", "2024-01-01", "", "## ChatGPT"]
    new_lines, fixes = fix_title_section(lines)
    assert "## GPT" in new_lines
    assert "## ChatGPT" not in new_lines
    assert any("ChatGPT" in f for f in fixes)


def test_fix_title_section_replaces_bare_prompt_label():
    lines = ["", "# Title", "2024-01-01", "", "Prompt:"]
    new_lines, fixes = fix_title_section(lines)
    assert "## Prompt" in new_lines
    assert "Prompt:" not in new_lines


def test_fix_title_section_no_changes_needed():
    lines = ["", "# Title", "2024-01-01", "", "## Human"]
    new_lines, fixes = fix_title_section(lines)
    assert fixes == []
    assert new_lines == lines


# ---------------------------------------------------------------------------
# fix_section_spacing
# ---------------------------------------------------------------------------

def test_fix_section_spacing_collapses_extra_blank_lines_before_header():
    text = "Some text.\n\n\n## Header\n\nContent.\n"
    result, fixes = fix_section_spacing(text)
    assert "\n\n\n## Header" not in result
    assert "\n\n## Header" in result
    assert fixes


def test_fix_section_spacing_adds_missing_blank_line_before_header():
    text = "Some text.\n## Header\n\nContent.\n"
    result, fixes = fix_section_spacing(text)
    assert "\n\n## Header" in result
    assert fixes


def test_fix_section_spacing_no_changes_needed():
    text = "Some text.\n\n## Header\n\nContent.\n"
    result, fixes = fix_section_spacing(text)
    assert fixes == []
    assert result == text


def test_fix_section_spacing_normalizes_trailing_newline():
    text = "Content.\n\n## Header\n\nBody.\n\n\n"
    result, _ = fix_section_spacing(text)
    assert result.endswith("\n")
    assert not result.endswith("\n\n")


# ---------------------------------------------------------------------------
# fix_rtl_spans
# ---------------------------------------------------------------------------

def test_fix_rtl_spans_replaces_span():
    lines = ['Normal text [word]{dir="rtl"} more text']
    new_lines, fixes = fix_rtl_spans(lines)
    assert '{dir="rtl"}' not in new_lines[0]
    assert fixes


def test_fix_rtl_spans_leaves_clean_lines():
    lines = ["No RTL here.", "Just normal text."]
    new_lines, fixes = fix_rtl_spans(lines)
    assert new_lines == lines
    assert fixes == []


def test_fix_rtl_spans_converts_typographic_chars():
    lines = ['[—]{dir="rtl"}']  # em dash inside RTL span
    new_lines, _ = fix_rtl_spans(lines)
    assert "--" in new_lines[0]


# ---------------------------------------------------------------------------
# fix_object_replacement
# ---------------------------------------------------------------------------

def test_fix_object_replacement_removes_char():
    lines = ["before￼ after"]
    new_lines, fixes = fix_object_replacement(lines)
    assert "￼" not in new_lines[0]
    assert "before after" == new_lines[0]
    assert fixes


def test_fix_object_replacement_clean_lines():
    lines = ["clean line", "another clean"]
    new_lines, fixes = fix_object_replacement(lines)
    assert new_lines == lines
    assert fixes == []


# ---------------------------------------------------------------------------
# check_file
# ---------------------------------------------------------------------------

def test_check_file_valid(tmp_path):
    p = _valid(tmp_path)
    assert check_file(p) == []


def test_check_file_missing_blank_first_line(tmp_path):
    p = _write(tmp_path, "2024-01-01-test.md", "# Test\n2024-01-01\n\n## Human\n\nHello.\n")
    errors = check_file(p)
    assert any("blank" in e for e in errors)


def test_check_file_bad_title_line(tmp_path):
    p = _write(tmp_path, "2024-01-01-test.md", "\nNot a title\n2024-01-01\n\n## Human\n\nHello.\n")
    errors = check_file(p)
    assert any("Title" in e or "title" in e for e in errors)


def test_check_file_bad_date_format(tmp_path):
    p = _write(tmp_path, "2024-01-01-test.md", "\n# Test\n01/01/2024\n\n## Human\n\nHello.\n")
    errors = check_file(p)
    assert any("yyyy-mm-dd" in e or "date" in e.lower() for e in errors)


def test_check_file_wrong_filename(tmp_path):
    p = _write(tmp_path, "wrong-name.md", "\n# My Title\n2024-01-01\n\n## Human\n\nHello.\n")
    errors = check_file(p)
    assert any("filename" in e for e in errors)


def test_check_file_references_not_last(tmp_path):
    content = (
        "\n# My Doc\n2024-01-01\n\n"
        "## Human\n\nHello.\n\n"
        "## References\n\n| Smith, J. (2020) *Title*\n\n"
        "## Claude\n\nReply.\n"
    )
    p = _write(tmp_path, "2024-01-01-my-doc.md", content)
    errors = check_file(p)
    assert any("References" in e for e in errors)


def test_check_file_chatgpt_header(tmp_path):
    content = "\n# My Doc\n2024-01-01\n\n## Human\n\nHello.\n\n## ChatGPT\n\nReply.\n"
    p = _write(tmp_path, "2024-01-01-my-doc.md", content)
    errors = check_file(p)
    assert any("ChatGPT" in e for e in errors)


def test_check_file_object_replacement_char(tmp_path):
    content = "\n# My Doc\n2024-01-01\n\n## Human\n\nHello￼.\n\n## Claude\n\nReply.\n"
    p = _write(tmp_path, "2024-01-01-my-doc.md", content)
    errors = check_file(p)
    assert any("OBJECT REPLACEMENT" in e or "U+FFFC" in e for e in errors)
