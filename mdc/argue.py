"""Conversion between mdc's argument markdown format and dianoia's Arguments JSON."""

from __future__ import annotations

import re
from datetime import date


_ASSUMPTION_RE = re.compile(
    r"^- ([A-Z]):\s+(.+)$"
)
_ARGUMENT_RE = re.compile(
    r"^- ([A-Z])(?:\s+\(from:\s*([A-Z,\s]+)\))?\s*:\s+(.+)$"
)


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

    lines = [
        "",
        f"# {title}",
        date_str,
        "",
    ]

    if assumptions:
        lines.append("## Assumptions")
        for step in assumptions:
            symbol = step.get("symbol", "")
            prop = step.get("proposition", "")
            if not symbol or not prop:
                raise ValueError(f"assumption step missing symbol or proposition: {step!r}")
            lines.append(f"- {symbol}: {prop}")
        lines.append("")

    lines.append("## Argument")
    for step in argument:
        symbol = step.get("symbol", "")
        prop = step.get("proposition", "")
        justifiers = step.get("justifiers", [])
        if not symbol or not prop:
            raise ValueError(f"argument step missing symbol or proposition: {step!r}")
        if justifiers:
            from_clause = f" (from: {', '.join(justifiers)})"
        else:
            from_clause = ""
        lines.append(f"- {symbol}{from_clause}: {prop}")
    lines.append("")

    return "\n".join(lines)


def markdown_to_argument(text: str) -> dict:
    """Parse companion argument markdown back to a dianoia Arguments dict.

    Strips any ## Evaluation section before parsing.
    Raises ValueError on parse failure.
    """
    # Strip ## Evaluation section
    eval_match = re.search(r"\n## Evaluation\b.*", text, re.DOTALL)
    if eval_match:
        text = text[: eval_match.start()]

    assumptions: list[dict] = []
    argument: list[dict] = []
    current_section: str | None = None

    for lineno, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()

        if line == "## Assumptions":
            current_section = "assumptions"
            continue
        if line == "## Argument":
            current_section = "argument"
            continue
        if line.startswith("## "):
            current_section = None
            continue

        if not line or line.startswith("#"):
            continue

        if current_section == "assumptions" and line.startswith("- "):
            m = _ASSUMPTION_RE.match(line)
            if not m:
                raise ValueError(
                    f"line {lineno}: cannot parse assumption: {raw_line!r}"
                )
            assumptions.append({
                "symbol": m.group(1),
                "proposition": m.group(2).strip(),
                "justifiers": [],
                "truth_score": "1.0",
            })

        elif current_section == "argument" and line.startswith("- "):
            m = _ARGUMENT_RE.match(line)
            if not m:
                raise ValueError(
                    f"line {lineno}: cannot parse argument step: {raw_line!r}"
                )
            symbol = m.group(1)
            justifiers_raw = m.group(2)
            proposition = m.group(3).strip()
            justifiers = (
                [j.strip() for j in justifiers_raw.split(",") if j.strip()]
                if justifiers_raw
                else []
            )
            argument.append({
                "symbol": symbol,
                "proposition": proposition,
                "justifiers": justifiers,
                "truth_score": "",
            })

    if not argument:
        raise ValueError("no argument steps found in file")

    return {"assumptions": assumptions, "argument": argument}
