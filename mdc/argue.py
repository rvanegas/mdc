"""Conversion between mdc's argument markdown format and dianoia's Arguments JSON."""

from __future__ import annotations

import re


_ARGUMENT_RE = re.compile(r"^- (\d+)(?:\s+\(from:\s*([\d,\s]+)\))?\s*:\s+(.+)$")
_PROP_SYMBOL_RE = re.compile(r"^- (\d+)[\s:(]")
_DECORATED_PROP_RE = re.compile(r"^- (\d[\w'*]*)[\s:(]")


def _read_title_date(text: str) -> tuple[str, str]:
    """Extract title and date from the first few lines of an mdc preamble."""
    lines = text.splitlines()
    title: str | None = None
    date_str: str | None = None
    for line in lines[:6]:
        line = line.strip()
        if line.startswith("# ") and title is None:
            title = line[2:].strip()
        elif re.match(r"^\d{4}-\d{2}-\d{2}$", line) and date_str is None:
            date_str = line
        if title and date_str:
            break
    if not title:
        raise ValueError("could not find '# Title' in preamble")
    if not date_str:
        raise ValueError("could not find date line in preamble")
    return title, date_str


def _to_alpha_index(n: int) -> str:
    """Convert a 0-based index to a base-26 letter label: 0->A, 25->Z, 26->AA, ...

    Direct port of Roxana's toAlphaIndex (~/src/roxana/src/app/util.tsx).
    """
    base = ord("A")
    letters: list[str] = []
    while n >= 0:
        n, remainder = divmod(n, 26)
        letters.append(chr(base + remainder))
        n -= 1
    return "".join(reversed(letters))


def assign_argument_labels(argument: list[dict]) -> dict[str, str]:
    """Map each justified proposition's symbol to a Roxana-style letter label.

    Only propositions with at least one justifier are "arguments" in Roxana's
    sense (premises -> conclusion); labels are assigned in ascending order of
    proposition number, matching the "ordered according to the proposition
    being argued for" rule. Labels are computed on demand, never persisted.
    """
    justified = sorted(int(s["symbol"]) for s in argument if s.get("justifiers"))
    return {str(sym): _to_alpha_index(i) for i, sym in enumerate(justified)}


def argument_to_markdown(args_dict: dict, title: str, date_str: str) -> str:
    """Convert a dianoia Arguments dict to companion argument markdown.

    Raises ValueError on malformed input.
    """
    argument = args_dict.get("argument")

    if argument is None:
        raise ValueError("argument dict must have an 'argument' key")
    if not isinstance(argument, list):
        raise ValueError("'argument' must be a list")
    if not argument:
        raise ValueError("argument must have at least one step")

    lines = ["", f"# {title}", date_str, "", "## Argument"]
    symbols: list[int] = []
    for step in argument:
        symbol = step.get("symbol", "")
        prop = step.get("proposition", "")
        justifiers = step.get("justifiers", [])
        if not symbol or not prop:
            raise ValueError(f"argument step missing symbol or proposition: {step!r}")
        from_clause = f" (from: {', '.join(justifiers)})" if justifiers else ""
        lines.append(f"- {symbol}{from_clause}: {prop}")
        symbols.append(int(symbol))
    lines.append("")

    err = _check_full_sequence(set(symbols))
    if err:
        raise ValueError(err)

    return "\n".join(lines)


_CORE_SECTIONS = {"Argument"}


def extract_core_sections(text: str) -> str:
    """Return only the preamble and the core ## Argument section.

    Raises ValueError if any other section (e.g. a legacy ## Definitions or
    evaluation section) is present — argument files must contain nothing but
    the proposition list.
    """
    # Split on ## headings, keeping the delimiters
    parts = re.split(r"(?=^## )", text, flags=re.MULTILINE)
    kept: list[str] = []
    for part in parts:
        m = re.match(r"^## (\S+)", part)
        if m is None:
            # Preamble (before the first ## heading)
            kept.append(part)
        elif m.group(1) in _CORE_SECTIONS:
            kept.append(part)
        else:
            raise ValueError(
                f"Unexpected section '## {m.group(1)}' in argument file. "
                "Argument files must contain only a ## Argument section."
            )
    return "".join(kept)


def markdown_to_argument(text: str) -> dict:
    """Parse companion argument markdown back to a dianoia Arguments dict.

    Retains only the preamble and core sections before parsing.
    Raises ValueError on parse failure.
    """
    text = extract_core_sections(text)

    argument: list[dict] = []
    current_section: str | None = None

    for lineno, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()

        if line == "## Argument":
            current_section = "argument"
            continue
        if line.startswith("## "):
            current_section = None
            continue

        if not line or line.startswith("#"):
            continue

        if current_section == "argument" and line.startswith("- "):
            m = _ARGUMENT_RE.match(line)
            if not m:
                raise ValueError(f"line {lineno}: cannot parse argument step: {raw_line!r}")
            justifiers_raw = m.group(2)
            argument.append({
                "symbol": m.group(1),
                "proposition": m.group(3).strip(),
                "justifiers": (
                    [j.strip() for j in justifiers_raw.split(",") if j.strip()]
                    if justifiers_raw else []
                ),
                # required by dianoia's Step wire schema; not part of mdc's own
                # format and never round-tripped through argument_to_markdown
                "truth_score": "",
            })
            continue

    if not argument:
        raise ValueError("no argument steps found in file")

    return {"argument": argument}


def _prop_numbers_in_sections(text: str) -> tuple[set[int], list[str]]:
    """Return (integer proposition numbers, decorated symbols) found in the Argument section."""
    numbers: set[int] = set()
    decorated: list[str] = []
    in_target = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "## Argument":
            in_target = True
            continue
        if stripped.startswith("## "):
            in_target = False
            continue
        if in_target and stripped.startswith("- "):
            if _PROP_SYMBOL_RE.match(stripped):
                numbers.add(int(_PROP_SYMBOL_RE.match(stripped).group(1)))
            elif _DECORATED_PROP_RE.match(stripped):
                decorated.append(_DECORATED_PROP_RE.match(stripped).group(1))
    return numbers, decorated


def _check_full_sequence(nums: set[int]) -> str | None:
    """Return an error message if nums isn't a contiguous 1..N sequence, else None."""
    expected = set(range(1, len(nums) + 1))
    if nums != expected:
        missing = sorted(expected - nums)
        return (
            f"Proposition numbers must be a contiguous sequence starting at 1 "
            f"with no gaps (missing: {missing})."
        )
    return None


def validate_proposition_numbering(old_text: str, new_text: str) -> str | None:
    """Return an error message if the edit violates proposition numbering rules, else None."""
    old_nums, _ = _prop_numbers_in_sections(old_text)
    new_nums, decorated = _prop_numbers_in_sections(new_text)

    if decorated:
        return (
            f"Invalid proposition symbol(s) {decorated}: use plain integers only "
            "(no subscripts, primes, apostrophes, or asterisks)."
        )

    missing = old_nums - new_nums
    if missing:
        return (
            f"Proposition(s) {sorted(missing)} were renumbered or removed. "
            "Existing proposition numbers must not change."
        )

    added = new_nums - old_nums
    if added and old_nums:
        max_old = max(old_nums)
        conflicts = sorted(n for n in added if n <= max_old)
        if conflicts:
            return (
                f"New proposition number(s) {conflicts} fall within the existing range "
                f"(current max: {max_old}). New propositions must be numbered after the maximum."
            )

    return _check_full_sequence(new_nums)
