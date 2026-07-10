from __future__ import annotations

import re
import sys
from pathlib import Path


_SECTION_RE = re.compile(
    r"^## Argument (\S+) \(proposition (\d+)\) — Analysis\s*$"
)


def _primary(companion: Path) -> Path:
    return companion.with_suffix("").with_suffix(".md")


def _parse_analysis_blocks(text: str) -> dict[int, str]:
    """Return {proposition_number: body_text} for existing '## Argument ... (proposition N) ...' blocks."""
    blocks: dict[int, str] = {}
    parts = re.split(r"(?=^## )", text, flags=re.MULTILINE)
    for part in parts:
        first_line = part.splitlines()[0] if part else ""
        m = _SECTION_RE.match(first_line.strip())
        if m:
            prop_num = int(m.group(2))
            body = "\n".join(part.splitlines()[1:]).strip("\n")
            blocks[prop_num] = body
    return blocks


def _parse_audit_body(text: str) -> str | None:
    """Return the body of the file-level '## Audit' section, or None if absent."""
    parts = re.split(r"(?=^## )", text, flags=re.MULTILINE)
    for part in parts:
        first_line = part.splitlines()[0] if part else ""
        if first_line.strip() == "## Audit":
            return "\n".join(part.splitlines()[1:]).strip("\n")
    return None


def _write_analysis(analysis_path: Path, title: str, date_str: str, argument: list[dict],
                    blocks: dict[int, str], audit_body: str | None = None) -> None:
    from mdc.argue import assign_argument_labels

    labels = assign_argument_labels(argument)
    lines = ["", f"# {title}", date_str, ""]
    if audit_body is not None:
        lines.append("## Audit")
        lines.append("")
        lines.append(audit_body)
        lines.append("")
    for prop_num in sorted(blocks):
        letter = labels.get(str(prop_num), "?")
        lines.append(f"## Argument {letter} (proposition {prop_num}) — Analysis")
        lines.append("")
        lines.append(blocks[prop_num])
        lines.append("")
    analysis_path.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")


def run_analyze(path: Path, proposition: int, verbose: bool = False) -> int:
    from mdc.argue import _read_title_date, markdown_to_argument
    from mdc import dianoia_client

    if path.name.endswith(".argument.md"):
        companion = path
    elif path.name.endswith(".document.md") or path.suffix.lower() == ".md":
        companion = path.with_suffix("").with_suffix(".argument.md")
    else:
        print(f"Error: '{path}' does not have a .md extension.")
        return 1

    if not companion.exists():
        print(f"Error: '{companion.name}' does not exist. Run 'mdc argue {_primary(companion).name}' first.")
        return 1

    argument_text = companion.read_text(encoding="utf-8")
    try:
        args_dict = markdown_to_argument(argument_text)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    argument = args_dict["argument"]
    step = next((s for s in argument if s["symbol"] == str(proposition)), None)
    if step is None:
        print(f"Error: proposition {proposition} not found in {companion.name}.")
        return 1
    if not step.get("justifiers"):
        print(f"Error: proposition {proposition} has no justifiers — it's a bare premise, not an argument to analyze.")
        return 1

    print(f"Submitting proposition {proposition} to dianoia for analysis…")
    try:
        results = dianoia_client.evaluate(args_dict, step=str(proposition))
    except (FileNotFoundError, RuntimeError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    try:
        title, date_str = _read_title_date(argument_text)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    from mdc.analysis import render_analysis_body

    analysis_path = companion.with_suffix("").with_suffix(".analysis.md")
    existing = analysis_path.read_text(encoding="utf-8") if analysis_path.exists() else ""
    blocks = _parse_analysis_blocks(existing) if existing else {}
    audit_body = _parse_audit_body(existing) if existing else None
    blocks[proposition] = render_analysis_body(results)

    _write_analysis(analysis_path, title, date_str, argument, blocks, audit_body)
    print(f"Analysis written to {analysis_path.name}")
    return 0
