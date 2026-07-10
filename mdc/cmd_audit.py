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

    findings = result.get("findings", [])
    if result.get("satisfied"):
        print("Argument satisfies all structural conditions.")
        return 0

    print(f"{len(findings)} finding{'s' if len(findings) != 1 else ''}:\n")
    _print_findings(findings)
    return 1
