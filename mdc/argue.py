"""Conversion between mdc's argument markdown format and dianoia's Arguments JSON."""

from __future__ import annotations

import re


_ASSUMPTION_RE = re.compile(r"^- (\d+)\s*:\s+(.+)$")
_ARGUMENT_RE = re.compile(r"^- (\d+)(?:\s+\(from:\s*([\d,\s]+)\))?\s*:\s+(.+)$")
_DEFINITION_RE = re.compile(r"^- ([A-Za-z][A-Za-z0-9]*(?:/\d+)?)\s*=\s*(.+)$")
_PROP_SYMBOL_RE = re.compile(r"^- (\d+)[\s:(]")


def argument_to_markdown(args_dict: dict, title: str, date_str: str) -> str:
    """Convert a dianoia Arguments dict to companion argument markdown.

    Raises ValueError on malformed input.
    """
    assumptions = args_dict.get("assumptions")
    argument = args_dict.get("argument")

    if assumptions is None or argument is None:
        raise ValueError("argument dict must have 'assumptions' and 'argument' keys")
    if not isinstance(assumptions, list) or not isinstance(argument, list):
        raise ValueError("'assumptions' and 'argument' must be lists")
    if not argument:
        raise ValueError("argument must have at least one step")

    lines = ["", f"# {title}", date_str, ""]

    definitions = args_dict.get("definitions")
    if definitions:
        predicates = definitions.get("predicates", [])
        constants = definitions.get("constants", [])
        if predicates or constants:
            lines.append("## Definitions")
            for c in constants:
                lines.append(f"- {c.get('symbol', '?')} = {c.get('value', '')}")
            for p in predicates:
                sym = p.get('symbol', '?')
                arity = p.get('arity', 0)
                label = f"{sym}/{arity}" if arity else sym
                lines.append(f"- {label} = {p.get('value', '')}")
            lines.append("")

    if assumptions:
        lines.append("## Assumptions")
        for step in assumptions:
            symbol = step.get("symbol", "")
            prop = step.get("proposition", "")
            if not symbol or not prop:
                raise ValueError(f"assumption step missing symbol or proposition: {step!r}")
            lines.append(f"- {symbol}: {prop}")
            _append_formalization_bullet(lines, step)
        lines.append("")

    lines.append("## Argument")
    for step in argument:
        symbol = step.get("symbol", "")
        prop = step.get("proposition", "")
        justifiers = step.get("justifiers", [])
        if not symbol or not prop:
            raise ValueError(f"argument step missing symbol or proposition: {step!r}")
        from_clause = f" (from: {', '.join(justifiers)})" if justifiers else ""
        lines.append(f"- {symbol}{from_clause}: {prop}")
        _append_formalization_bullet(lines, step)
    lines.append("")

    return "\n".join(lines)


def _append_formalization_bullet(lines: list, step: dict) -> None:
    form = step.get("formalization") or {}
    if form.get("endorsed") and form.get("ascii"):
        lines.append(f"  - {form['ascii']}")


def inject_formalizations(text: str, by_symbol: dict) -> str:
    """Add formalization sub-bullets in ## Assumptions and ## Argument.

    Existing sub-bullets are dropped and re-emitted from by_symbol. The
    caller is responsible for ensuring by_symbol already reflects any endorsed
    formalizations, so nothing is lost.
    """
    lines = text.splitlines()
    result = []
    in_target = False
    last_symbol: str | None = None

    def flush() -> None:
        if in_target and last_symbol is not None and last_symbol in by_symbol:
            result.append(f"  - {by_symbol[last_symbol]}")

    for line in lines:
        stripped = line.strip()

        if stripped in ("## Assumptions", "## Argument"):
            flush()
            in_target = True
            last_symbol = None
            result.append(line)
            continue

        if stripped.startswith("## "):
            flush()
            in_target = False
            last_symbol = None
            result.append(line)
            continue

        if in_target:
            if line.startswith("  - "):
                # Drop existing sub-bullet; flush() will re-emit it
                continue

            m = _PROP_SYMBOL_RE.match(stripped)
            if m:
                flush()
                last_symbol = m.group(1)
                result.append(line)
                continue

            # Any other line (blank, non-prop) — flush pending sub-bullet first
            flush()
            last_symbol = None

        result.append(line)

    flush()
    return "\n".join(result)


_CORE_SECTIONS = {"Definitions", "Assumptions", "Argument"}


def extract_core_sections(text: str) -> str:
    """Return only the preamble and the three core sections of an argument file.

    Discards ## Formal evaluation, ## Content evaluation,
    ## Improvement recommendations, and any other unknown sections.
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
    return "".join(kept)


def markdown_to_argument(text: str) -> dict:
    """Parse companion argument markdown back to a dianoia Arguments dict.

    Retains only the preamble and core sections before parsing.
    Raises ValueError on parse failure.
    """
    text = extract_core_sections(text)

    assumptions: list[dict] = []
    argument: list[dict] = []
    definitions: dict = {"predicates": [], "constants": []}
    current_section: str | None = None
    pending_step: dict | None = None
    pending_list: list | None = None

    def finalize() -> None:
        nonlocal pending_step
        if pending_step is not None and pending_list is not None:
            pending_list.append(pending_step)
            pending_step = None

    for lineno, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()

        if line == "## Assumptions":
            finalize()
            current_section = "assumptions"
            pending_list = assumptions
            continue
        if line == "## Argument":
            finalize()
            current_section = "argument"
            pending_list = argument
            continue
        if line == "## Definitions":
            finalize()
            current_section = "definitions"
            pending_list = None
            continue
        if line.startswith("## "):
            finalize()
            current_section = None
            pending_list = None
            continue

        if not line or line.startswith("#"):
            continue

        # Formalization sub-bullet under a pending proposition
        if current_section in ("assumptions", "argument") and raw_line.startswith("  - "):
            if pending_step is not None:
                ascii_form = raw_line[4:].strip()
                if ascii_form:
                    pending_step["formalization"] = {
                        "ascii": ascii_form,
                        "json_structure": None,
                        "endorsed": True,
                    }
            continue

        if current_section == "assumptions" and line.startswith("- "):
            finalize()
            m = _ASSUMPTION_RE.match(line)
            if not m:
                raise ValueError(f"line {lineno}: cannot parse assumption: {raw_line!r}")
            pending_step = {
                "symbol": m.group(1),
                "proposition": m.group(2).strip(),
                "justifiers": [],
                "truth_score": "1.0",
            }
            continue

        if current_section == "argument" and line.startswith("- "):
            finalize()
            m = _ARGUMENT_RE.match(line)
            if not m:
                raise ValueError(f"line {lineno}: cannot parse argument step: {raw_line!r}")
            justifiers_raw = m.group(2)
            pending_step = {
                "symbol": m.group(1),
                "proposition": m.group(3).strip(),
                "justifiers": (
                    [j.strip() for j in justifiers_raw.split(",") if j.strip()]
                    if justifiers_raw else []
                ),
                "truth_score": "",
            }
            continue

        if current_section == "definitions" and line.startswith("- "):
            m = _DEFINITION_RE.match(line)
            if m:
                raw_sym, value = m.group(1), m.group(2).strip()
                if "/" in raw_sym:
                    sym, arity_str = raw_sym.split("/", 1)
                    arity = int(arity_str)
                else:
                    sym, arity = raw_sym, 0
                if sym[0].isupper():
                    definitions["predicates"].append({"symbol": sym, "value": value, "arity": arity})
                else:
                    definitions["constants"].append({"symbol": sym, "value": value})

    finalize()

    if not argument:
        raise ValueError("no argument steps found in file")

    result: dict = {"assumptions": assumptions, "argument": argument}
    if definitions["predicates"] or definitions["constants"]:
        result["definitions"] = definitions
    return result
