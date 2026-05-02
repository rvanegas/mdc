"""
Validate markdown conversation files against the mdc format.
With fix_title_section(), corrects title-section infractions that can be repaired automatically.

Format rules:
  1. Blank first line
  2. First-level header: # Title
  3. Date line immediately after title: yyyy-mm-dd
  4. Filename derived from title and date
  5. Blank line after date
  6. Arbitrary sections with ## headers (one or more words)
  7. '## ChatGPT' is disallowed — use '## GPT' instead
  8. Each section is preceded and followed by a blank line
  9. References, if present, must be the final section; Related (if present) must come just before References; Notes (if present) must come just before Related (or References if no Related, or be last if neither)
 10. Each reference: | Last, First (year) *Title*
 11. Multi-author: | Last1, First1, First2 Last2, ... (year) *Title*
 12. Each note line: | [n] Text, with n consecutive starting from 1

Note: Rule 11's "| " prefix is enforced during `mdc validate` but is not required
by `mdc reply`'s reference parser, which follows the AI output format.
"""

import re
import sys
from pathlib import Path


KNOWN_LLMS = {"Claude", "GPT"}

# ── RTL span encoding ─────────────────────────────────────────────────────────

# Matches pandoc-style [char]{dir="rtl"} spans that some converters emit.
_RTL_SPAN_RE = re.compile(r'\[([^\]]*)\]\{dir="rtl"\}')

# Typographic Unicode → plain ASCII, for chars that may appear inside spans.
_UNICODE_TO_ASCII: dict[str, str] = {
    "\u2018": "'",    # LEFT SINGLE QUOTATION MARK
    "\u2019": "'",    # RIGHT SINGLE QUOTATION MARK
    "\u201A": ",",    # SINGLE LOW-9 QUOTATION MARK
    "\u201B": "'",    # SINGLE HIGH-REVERSED-9 QUOTATION MARK
    "\u201C": '"',    # LEFT DOUBLE QUOTATION MARK
    "\u201D": '"',    # RIGHT DOUBLE QUOTATION MARK
    "\u201E": '"',    # DOUBLE LOW-9 QUOTATION MARK
    "\u2013": "-",    # EN DASH
    "\u2014": "--",   # EM DASH
    "\u2026": "...",  # HORIZONTAL ELLIPSIS
    "\u00AB": '"',    # LEFT-POINTING DOUBLE ANGLE QUOTATION MARK
    "\u00BB": '"',    # RIGHT-POINTING DOUBLE ANGLE QUOTATION MARK
    "\u2039": "'",    # SINGLE LEFT-POINTING ANGLE QUOTATION MARK
    "\u203A": "'",    # SINGLE RIGHT-POINTING ANGLE QUOTATION MARK
}


def _ascii_equivalent(text: str) -> str:
    """Normalise typographic Unicode to plain ASCII."""
    return "".join(_UNICODE_TO_ASCII.get(ch, ch) for ch in text)


def fix_object_replacement(lines: list[str]) -> tuple[list[str], list[str]]:
    """
    Strip U+FFFC (OBJECT REPLACEMENT CHARACTER) from all lines.

    Returns (new_lines, fixes).
    """
    new_lines: list[str] = []
    fixes: list[str] = []
    for i, line in enumerate(lines):
        if "\ufffc" in line:
            new_lines.append(line.replace("\ufffc", ""))
            fixes.append(f"removed OBJECT REPLACEMENT CHARACTER (U+FFFC) on line {i + 1}")
        else:
            new_lines.append(line)
    return new_lines, fixes


def fix_rtl_spans(lines: list[str]) -> tuple[list[str], list[str]]:
    """
    Replace [char]{dir="rtl"} spans with plain ASCII equivalents.

    Returns (new_lines, fixes) where fixes is a list of human-readable
    descriptions of what was changed.
    """
    new_lines: list[str] = []
    fixes: list[str] = []
    for i, line in enumerate(lines):
        if _RTL_SPAN_RE.search(line):
            new_line = _RTL_SPAN_RE.sub(lambda m: _ascii_equivalent(m.group(1)), line)
            new_lines.append(new_line)
            fixes.append(f"replaced RTL span(s) on line {i + 1}")
        else:
            new_lines.append(line)
    return new_lines, fixes


def slugify(title: str) -> str:
    """Derive the filename slug from a title."""
    s = title.lower()
    s = re.sub(r"[^a-z0-9\s-]", "", s)   # strip punctuation (keep spaces and hyphens)
    s = re.sub(r"\s+", "-", s)             # spaces → hyphens
    s = re.sub(r"-{2,}", "-", s)           # collapse runs of hyphens
    s = s.strip("-")
    return s


# A name token: a capitalised word (letters, hyphens, apostrophes) or
# an initial such as "W." or "M. G. F."
# Covers ASCII letters plus the common Latin-1 Supplement block (U+00C0–U+00FF),
# which includes accented letters used in French, German, Spanish, etc.
_NAME_TOKEN = r"[A-ZÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ'\-]*\.?"
_NAME_ARTICLE = r"(?:van|von|de|del|den|der|di|du|la|le|ten|ter)\s+"
_NAME = rf"(?:{_NAME_ARTICLE})?(?:{_NAME_TOKEN})(?:\s+{_NAME_TOKEN})*"


def _validate_reference(line: str) -> str | None:
    """
    Return an error string or None.

    Expected shapes:
      Last, First (year) *Title*
      Last1, First1, First2 Last2[, FirstN LastN]* (year) *Title*
    """
    # ── year ──────────────────────────────────────────────────────────
    # Accepts: (1989)  (c. 350 BCE)  (c. 350)  (350 BCE)  (c. 53-55)  (1781/1787)
    _yr = r"\d+(?:[/-]\d+)?"
    year_m = re.search(rf"\(c\.?\s*{_yr}(?:\s*BCE)?\)", line) or \
             re.search(rf"\({_yr}(?:\s*BCE)?\)", line)
    if not year_m:
        return "missing year in parentheses — expected e.g. (1989), (c. 350 BCE), (c. 53-55), or (1781/1787)"

    # ── italic title ──────────────────────────────────────────────────
    after_year = line[year_m.end():].strip()
    if not re.match(r"^\*[^*]+\*", after_year):
        return "title is not in italics — expected *Title*"

    # ── author block ──────────────────────────────────────────────────
    author_block = line[: year_m.start()].strip()
    if not author_block:
        return "missing author"

    comma_idx = author_block.find(",")
    if comma_idx == -1:
        # Allow mononyms (Aristotle, Plato, Avicenna, …)
        if not re.match(rf"^{_NAME_TOKEN}$", author_block):
            return "author name not in 'Last, First' format — may not be surname-first"
    else:
        last_name = author_block[:comma_idx].strip()
        if not re.match(rf"^{_NAME}$", last_name):
            return f"last name '{last_name}' is not a properly capitalised name"

        rest = author_block[comma_idx + 1:].strip()
        if not rest:
            return "missing first name for first author"
        if not re.match(r"^[A-Z]", rest):
            return f"first name should start with a capital letter, got: '{rest[:30]}'"

    return None


# ── title-section fixer ───────────────────────────────────────────────────────

_BARE_DATE = r"\d{4}-\d{2}-\d{2}"


def fix_title_section(lines: list[str]) -> tuple[list[str], list[str]]:
    """
    Apply all correctable title-section infractions.

    Fixable:
      • Missing blank first line       → insert one
      • Date '*' delimiter(s) present  → remove the extra asterisk(s)
      • Missing blank line after date  → insert one
      • '## ChatGPT' header            → replace with '## GPT'

    Not fixable here:
      • Malformed or absent title (# …) — can't reconstruct content
      • Wrong filename                  — would require renaming the file

    Returns (new_lines, fixes) where fixes is a list of human-readable
    descriptions of what was changed.
    """
    lines = list(lines)
    fixes: list[str] = []

    # ── fix 1: blank first line ───────────────────────────────────────
    if not lines or lines[0].strip() != "":
        lines.insert(0, "")
        fixes.append("inserted blank first line")

    # Stop here if line[1] doesn't look like a title; further fixes would
    # touch the wrong positions.
    if len(lines) < 2 or not re.match(r"^# .+", lines[1]):
        return lines, fixes

    # ── fix 2: date delimiter(s) ──────────────────────────────────────
    if len(lines) >= 3:
        d = lines[2]
        if not re.match(rf"^{_BARE_DATE}$", d):
            if re.match(rf"^\*({_BARE_DATE})\*$", d):   # both * present
                lines[2] = re.sub(r"^\*(.+)\*$", r"\1", d)
                fixes.append("removed '*' delimiters from date line")
            elif re.match(rf"^\*{_BARE_DATE}$", d):     # leading * only
                lines[2] = d[1:]
                fixes.append("removed leading '*' from date line")
            elif re.match(rf"^{_BARE_DATE}\*$", d):     # trailing * only
                lines[2] = d[:-1]
                fixes.append("removed trailing '*' from date line")

    # ── fix 3: blank line after date ──────────────────────────────────
    if len(lines) < 4 or lines[3].strip() != "":
        lines.insert(3, "")
        fixes.append("inserted blank line after date")

    # ── fix 4: '## ChatGPT' → '## GPT' ───────────────────────────────
    for i, line in enumerate(lines):
        if line.strip() == "## ChatGPT":
            lines[i] = "## GPT"
            fixes.append(f"replaced '## ChatGPT' with '## GPT' on line {i + 1}")

    # ── fix 5: bare labels → ## headers ──────────────────────────────
    _BARE_HEADERS = {"Prompt:": "## Prompt", "ChatGPT:": "## GPT", "References:": "## References"}
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped in _BARE_HEADERS:
            lines[i] = _BARE_HEADERS[stripped]
            fixes.append(f"replaced '{stripped}' with '{_BARE_HEADERS[stripped]}' on line {i + 1}")

    return lines, fixes


def fix_section_spacing(text: str) -> tuple[str, list[str]]:
    """Ensure exactly one blank line before and after every ## header, and exactly one trailing newline."""
    fixes: list[str] = []

    new = re.sub(r'\n{3,}(## )', r'\n\n\1', text)
    if new != text:
        fixes.append("collapsed extra blank lines before section headers")
        text = new

    new = re.sub(r'([^\n])\n(## )', r'\1\n\n\2', text)
    if new != text:
        fixes.append("added missing blank line before section headers")
        text = new

    new = re.sub(r'(^## [^\n]*\n)\n+', r'\1\n', text, flags=re.MULTILINE)
    if new != text:
        fixes.append("collapsed extra blank lines after section headers")
        text = new

    new = re.sub(r'(^## [^\n]*\n)([^\n])', r'\1\n\2', text, flags=re.MULTILINE)
    if new != text:
        fixes.append("added missing blank line after section headers")
        text = new

    stripped = text.rstrip('\n')
    normalized = stripped + '\n'
    if text != normalized:
        fixes.append("removed extra trailing newlines" if text.endswith('\n') else "added missing trailing newline")
        text = normalized

    return text, fixes


# ── checker ───────────────────────────────────────────────────────────────────

def check_global_issues(path: Path) -> list[str]:
    """Check encoding and bare-label issues that apply to any document type."""
    errors: list[str] = []

    try:
        raw = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raw = path.read_text(encoding="latin-1")
        path.with_suffix(path.suffix + ".bak").write_bytes(path.read_bytes())
        path.write_text(raw, encoding="utf-8")
        print(f"Warning: {path.name} was not UTF-8; converted in place (backup: {path.name}.bak).", file=sys.stderr)

    lines = raw.split("\n")

    def err(lineno: int, msg: str) -> None:
        errors.append(f"line {lineno}: {msg}")

    for i, line in enumerate(lines):
        if "\ufffc" in line:
            err(i + 1, "contains U+FFFC OBJECT REPLACEMENT CHARACTER — run 'mdc fix' to remove it")

    for i, line in enumerate(lines):
        if _RTL_SPAN_RE.search(line):
            err(i + 1, 'contains [char]{dir="rtl"} encoding — run \'mdc fix\' to replace with plain ASCII')

    _BARE_LABELS = {"Prompt:", "ChatGPT:", "References:"}
    for i, line in enumerate(lines):
        if line.strip() in _BARE_LABELS:
            err(i + 1, f"bare label '{line.strip()}' — use '## Prompt' / '## GPT' / '## References' instead")

    return errors


def check_file(path: Path) -> list[str]:
    errors: list[str] = []

    try:
        raw = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raw = path.read_text(encoding="latin-1")
        path.with_suffix(path.suffix + ".bak").write_bytes(path.read_bytes())
        path.write_text(raw, encoding="utf-8")
        print(f"Warning: {path.name} was not UTF-8; converted in place (backup: {path.name}.bak).", file=sys.stderr)

    # Split on newlines; a file ending with \n gives a trailing empty string,
    # which is fine — joining with \n reconstructs the original faithfully.
    lines = raw.split("\n")
    n = len(lines)

    def err(lineno: int, msg: str) -> None:  # lineno is 1-based
        errors.append(f"line {lineno}: {msg}")


    # ── object replacement character (whole-file scan) ────────────────
    for i, line in enumerate(lines):
        if "\ufffc" in line:
            err(i + 1, "contains U+FFFC OBJECT REPLACEMENT CHARACTER — run 'mdc fix' to remove it")

    # ── RTL span encoding (whole-file scan) ───────────────────────────
    for i, line in enumerate(lines):
        if _RTL_SPAN_RE.search(line):
            err(i + 1, 'contains [char]{dir="rtl"} encoding — run \'mdc fix\' to replace with plain ASCII')

    # ── bare speaker labels (whole-file scan) ─────────────────────────
    _BARE_LABELS = {"Prompt:", "ChatGPT:", "References:"}
    for i, line in enumerate(lines):
        if line.strip() in _BARE_LABELS:
            err(i + 1, f"bare label '{line.strip()}' — use '## Prompt' / '## GPT' / '## References' instead")

    # ── 1. blank first line ───────────────────────────────────────────
    if n < 1 or lines[0].strip() != "":
        err(1, f"expected blank line, got {lines[0]!r}")
        return errors

    # ── 2. first-level header ─────────────────────────────────────────
    title: str | None = None
    if n < 2 or not re.match(r"^# .+", lines[1]):
        err(2, f"expected '# Title', got {lines[1]!r}")
        return errors
    title = lines[1][2:].strip()

    # ── 3. date line ──────────────────────────────────────────────────
    date_str: str | None = None
    if n < 3:
        err(3, "expected date line 'yyyy-mm-dd'")
        return errors
    m = re.match(r"^(\d{4}-\d{2}-\d{2})$", lines[2])
    if not m:
        err(3, f"expected 'yyyy-mm-dd', got {lines[2]!r}")
        return errors
    date_str = m.group(1)

    # ── 4. filename ───────────────────────────────────────────────────
    if len(path.suffixes) == 1:
        expected = f"{date_str}-{slugify(title)}.md"
        actual = path.name
        if actual != expected:
            errors.append(f"filename: expected '{expected}', got '{actual}'")
            return errors

    # ── 5. blank line after date ──────────────────────────────────────
    if n < 4 or lines[3].strip() != "":
        err(4, f"expected blank line after date, got {lines[3]!r}")
        return errors

    # ── locate section headers ────────────────────────────────────────
    sections: list[tuple[int, str]] = []   # (0-based index, raw line)
    for i, line in enumerate(lines):
        if re.match(r"^## ", line):
            sections.append((i, line))

    if not sections:
        errors.append("no sections found (expected '## ...' headers)")
        return errors

    # ── 6. each header has non-empty content ─────────────────────────
    for idx, header in sections:
        content = header[3:].strip()
        if not content:
            err(idx + 1, "section header has no text after '##'")
            return errors

    # ── 7. flag legacy '## ChatGPT' label ────────────────────────────
    for idx, header in sections:
        if header[3:].strip() == "ChatGPT":
            err(idx + 1, "'## ChatGPT': use '## GPT' instead")
            return errors

    # ── 9. section spacing (blank lines around headers, trailing newline) ──
    _, spacing_fixes = fix_section_spacing(raw)
    for fix in spacing_fixes:
        errors.append(fix + " — run 'mdc fix' to correct")

    # ── 10. Notes/Related/References ordering and position ───────────
    # Required tail order (all optional): Notes → Related → References
    # References must be last; Related just before it; Notes just before Related.
    notes_si = next((si for si, (_, h) in enumerate(sections) if h[3:].strip() == "Notes"), None)
    related_si = next((si for si, (_, h) in enumerate(sections) if h[3:].strip() == "Related"), None)
    refs_si = next((si for si, (_, h) in enumerate(sections) if h[3:].strip() == "References"), None)

    if refs_si is not None and refs_si != len(sections) - 1:
        idx, _ = sections[refs_si]
        err(idx + 1, "'## References' must be the final section but appears before other sections")
        return errors

    if related_si is not None:
        expected = len(sections) - 1 if refs_si is None else refs_si - 1
        if related_si != expected:
            idx, _ = sections[related_si]
            err(idx + 1, "'## Related' must appear just before '## References' (or at the end if no References section)")
            return errors

    if notes_si is not None:
        if related_si is not None:
            expected = related_si - 1
        elif refs_si is not None:
            expected = refs_si - 1
        else:
            expected = len(sections) - 1
        if notes_si != expected:
            idx, _ = sections[notes_si]
            err(idx + 1, "'## Notes' must appear just before '## Related' or '## References' (or at the end if neither is present)")
            return errors

    # ── 11/12. validate reference lines ──────────────────────────────
    if refs_si is not None:
        ref_idx, _ = sections[refs_si]
        ref_start = ref_idx + 2   # skip header + blank line
        ref_lines = []
        for i in range(ref_start, n):
            if re.match(r"^## ", lines[i]):
                break
            ref_lines.append((i, lines[i]))

        for i, ref_line in ref_lines:
            stripped = ref_line.strip()
            if stripped == "":
                continue
            if not stripped.startswith("| "):
                err(i + 1, f"reference must start with '| ': {stripped!r}")
                continue
            ref_err = _validate_reference(stripped[2:])
            if ref_err:
                err(i + 1, f"reference — {ref_err}: {stripped!r}")

    # ── validate Notes lines ──────────────────────────────────────────
    if notes_si is not None:
        notes_idx, _ = sections[notes_si]
        notes_start = notes_idx + 2   # skip header + blank line
        note_lines = []
        for i in range(notes_start, n):
            if re.match(r"^## ", lines[i]):
                break
            note_lines.append((i, lines[i]))

        expected_n = 1
        for i, note_line in note_lines:
            stripped = note_line.strip()
            if stripped == "":
                continue
            m = re.match(r"^\| \[(\d+)\] .+$", stripped)
            if not m:
                err(i + 1, f"note line must be '| [n] Text': {stripped!r}")
                return errors
            actual_n = int(m.group(1))
            if actual_n != expected_n:
                err(i + 1, f"note numbers must be consecutive starting from 1: expected [{expected_n}], got [{actual_n}]")
                return errors
            expected_n += 1

    # ── validate Related lines ────────────────────────────────────────
    if related_si is not None:
        related_idx, _ = sections[related_si]
        related_start = related_idx + 2   # skip header + blank line
        related_lines = []
        for i in range(related_start, n):
            if re.match(r"^## ", lines[i]):
                break
            related_lines.append((i, lines[i]))

        for i, related_line in related_lines:
            stripped = related_line.strip()
            if stripped == "":
                continue
            if not stripped.startswith("| "):
                err(i + 1, f"related line must start with '| ': {stripped!r}")
                return errors

    return errors
