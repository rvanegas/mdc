from __future__ import annotations

import sys
from pathlib import Path


def _primary(companion: Path) -> Path:
    return companion.with_suffix("").with_suffix(".md")


_CONDITION_TITLES = {
    "connectivity": "Connectivity",
    "order_independence": "Order independence",
    "conclusion": "Conclusion",
    "integrity": "Integrity",
}


def _print_findings(findings: list[dict]) -> None:
    for f in findings:
        title = _CONDITION_TITLES.get(f.get("condition", ""), f.get("condition", "?"))
        symbols = ", ".join(f.get("step_symbols", []))
        print(f"{title} — {symbols}")
        print(f"  issue: {f.get('issue', '')}")
        print(f"  fix:   {f.get('pointer', '')}")
        print()


def run_audit(path: Path) -> int:
    from mdc.argue import markdown_to_argument
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

    print(f"Auditing {companion.name}…")
    try:
        result = dianoia_client.audit(args_dict)
    except (FileNotFoundError, RuntimeError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    _save_audit(companion, argument_text, args_dict["argument"], result)

    findings = result.get("findings", [])
    if result.get("satisfied"):
        print("Argument satisfies all structural conditions.")
        return 0

    print(f"{len(findings)} finding{'s' if len(findings) != 1 else ''}:\n")
    _print_findings(findings)
    return 1


def _save_audit(companion: Path, argument_text: str, argument: list[dict], result: dict) -> None:
    """Persist the audit as the ## Audit section of the .analysis.md companion."""
    from mdc.analysis import render_audit_body
    from mdc.argue import _read_title_date
    from mdc.cmd_analyze import _parse_analysis_blocks, _write_analysis

    try:
        title, date_str = _read_title_date(argument_text)
    except ValueError as e:
        print(f"Warning: audit not saved: {e}", file=sys.stderr)
        return

    analysis_path = companion.with_suffix("").with_suffix(".analysis.md")
    existing = analysis_path.read_text(encoding="utf-8") if analysis_path.exists() else ""
    blocks = _parse_analysis_blocks(existing) if existing else {}

    _write_analysis(analysis_path, title, date_str, argument, blocks, render_audit_body(result))
    print(f"Audit written to {analysis_path.name}")
