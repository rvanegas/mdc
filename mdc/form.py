"""
Validate markdown conversation files against the mdform format.
With fix_title_section(), corrects title-section infractions that can be repaired automatically.

Format rules:
  1. Blank first line
  2. First-level header: # Title
  3. Date line immediately after title: yyyy-mm-dd
  4. Filename derived from title and date
  5. Blank line after date
  6. Arbitrary sections with ## headers
  7. Each section header is exactly one word
  8. The word is a personal name, "Prompt", "Claude", or "GPT"
  9. Each section is preceded and followed by a blank line
 10. References, if present, must be the final section
 11. Each reference: | Last, First (year) *Title*
 12. Multi-author: | Last1, First1, First2 Last2, ... (year) *Title*

Note: Rule 11's "| " prefix is enforced during `mdc validate` but is not required
by `mdc reply`'s reference parser, which follows the AI output format.
"""

import re
from pathlib import Path


KNOWN_LLMS = {"Claude", "GPT"}


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
_NAME = rf"(?:{_NAME_TOKEN})(?:\s+{_NAME_TOKEN})*"


def _validate_reference(line: str):
    """
    Return an error string if the reference line is malformed, else None.

    Expected shapes:
      Last, First (year) *Title*
      Last1, First1, First2 Last2[, FirstN LastN]* (year) *Title*
    """
    # ── year ──────────────────────────────────────────────────────────
    # Accepts: (1989)  (c. 350 BCE)  (c. 350)  (350 BCE)
    year_m = re.search(r"\(c\.?\s*\d+(?:\s*BCE)?\)", line) or \
             re.search(r"\(\d+(?:\s*BCE)?\)", line)
    if not year_m:
        return "missing year in parentheses — expected e.g. (1989) or (c. 350 BCE)"

    # ── italic title ──────────────────────────────────────────────────
    after_year = line[year_m.end():].strip()
    if not re.match(r"^\*[^*]+\*[.,;]?\s*$", after_year):
        return "title must be in italics: *Title*"

    # ── author block ──────────────────────────────────────────────────
    author_block = line[: year_m.start()].strip()
    if not author_block:
        return "missing author"

    comma_idx = author_block.find(",")
    if comma_idx == -1:
        # Allow mononyms (Aristotle, Plato, Avicenna, …)
        if not re.match(rf"^{_NAME_TOKEN}$", author_block):
            return "first author must be in 'Last, First' format — no comma found"
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

    return lines, fixes


# ── checker ───────────────────────────────────────────────────────────────────

def check_file(path: Path) -> list[str]:
    errors: list[str] = []

    raw = path.read_text(encoding="utf-8")

    # Split on newlines; a file ending with \n gives a trailing empty string,
    # which is fine — joining with \n reconstructs the original faithfully.
    lines = raw.split("\n")
    n = len(lines)

    def err(lineno: int, msg: str) -> None:  # lineno is 1-based
        errors.append(f"line {lineno}: {msg}")

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

    # ── 6/7. each header has exactly one word ─────────────────────────
    for idx, header in sections:
        content = header[3:].strip()
        words = content.split()
        if len(words) == 0:
            err(idx + 1, "section header has no word after '##'")
            return errors
        elif len(words) > 1:
            err(
                idx + 1,
                f"section header '## {content}' has {len(words)} words; expected 1",
            )
            return errors

    # ── 8. each word is a valid section label ─────────────────────────
    for idx, header in sections:
        word = header[3:].strip()
        if word == "References":
            continue  # validated separately
        if word in KNOWN_LLMS or word == "Prompt":
            continue
        # treat as personal name: must be a single capitalised word
        if not re.match(r"^[A-Z][A-Za-z'\-]*$", word):
            err(
                idx + 1,
                f"'## {word}': section label must be a personal name, "
                f"'Prompt', 'Claude', or 'GPT'",
            )
            return errors

    # ── 9. blank line immediately after each ## header ────────────────
    for idx, header in sections:
        next_idx = idx + 1
        if next_idx >= n or lines[next_idx].strip() != "":
            got = repr(lines[next_idx]) if next_idx < n else "'<EOF>'"
            err(
                idx + 2,
                f"expected blank line after '## ...' header, got {got}",
            )
            return errors

    # ── 9b. blank line before each section (except the first) ─────────
    for idx, header in sections[1:]:
        prev_idx = idx - 1
        if prev_idx < 0 or lines[prev_idx].strip() != "":
            got = repr(lines[prev_idx]) if prev_idx >= 0 else "'<BOF>'"
            err(
                idx,  # 1-based: the line before the header
                f"expected blank line before '## {header[3:].strip()}', got {got}",
            )
            return errors

    # ── 10. References, if present, must be the final section ─────────
    last_idx, last_header = sections[-1]
    last_word = last_header[3:].strip()
    has_references = last_word == "References"

    for idx, header in sections[:-1]:
        if header[3:].strip() == "References":
            err(idx + 1, "'## References' must be the final section but appears before other sections")
            return errors

    # ── 11/12. validate reference lines ──────────────────────────────
    if has_references:
        ref_start = last_idx + 2   # skip header + blank line
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
                return errors
            ref_err = _validate_reference(stripped[2:])
            if ref_err:
                err(i + 1, f"reference — {ref_err}: {stripped!r}")
                return errors

    return errors
