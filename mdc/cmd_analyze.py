from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import cast


_SECTION_RE = re.compile(
    r"^## Argument (\S+) \(proposition (\d+)\) — Analysis\s*$"
)


def _primary(companion: Path) -> Path:
    return companion.with_suffix("").with_suffix(".md")


def _render_truth(rc: dict) -> list[str]:
    lines = ["### Truth", ""]
    for item in sorted(rc.get("truth_evaluations", []), key=lambda x: x.get("symbol", "")):
        sym = item.get("symbol", "?")
        val = item.get("truth_value", "?")
        reasoning = item.get("reasoning", "")
        lines.append(f"- {sym} truth: {val} — {reasoning}")
    lines.append("")
    return lines


def _render_content_validity(rc: dict) -> list[str]:
    lines = ["### Content validity", ""]
    for item in sorted(rc.get("validity_evaluations", []), key=lambda x: x.get("symbol", "")):
        sym = item.get("symbol", "?")
        val = item.get("validity_value", "?")
        reasoning = item.get("reasoning", "")
        lines.append(f"- {sym} validity: {val} — {reasoning}")
    for item in rc.get("incoherent_sets", []):
        syms = ", ".join(item.get("symbols", []))
        val = item.get("incoherence_value", "?")
        lines.append(f"- incoherent ({val}): {syms}")
    lines.append("")
    return lines


def _render_phrasing(rc: dict) -> list[str]:
    evaluations = rc.get("phrasing_evaluations", [])
    if not evaluations:
        return []
    lines = ["### Phrasing", ""]
    for item in sorted(evaluations, key=lambda x: x.get("symbol", "")):
        sym = item.get("symbol", "?")
        issues = "; ".join(item.get("issues", []))
        recommendation = item.get("recommendation", "")
        lines.append(f"- {sym}: {issues} — {recommendation}")
    lines.append("")
    return lines


def _render_formalizations(rc: dict) -> list[str]:
    lines = ["### Formalizations", ""]
    for f in rc.get("formalizations", []):
        lines.append(f"- {f.get('symbol', '?')}: {f.get('ascii', '')}")
    defs = rc.get("definitions", {})
    constants = defs.get("constants", [])
    predicates = defs.get("predicates", [])
    if constants or predicates:
        lines.append("")
        for c in constants:
            lines.append(f"- {c.get('symbol', '?')} = {c.get('value', '')}")
        for p in predicates:
            sym = p.get("symbol", "?")
            arity = p.get("arity", 0)
            label = f"{sym}/{arity}" if arity else sym
            lines.append(f"- {label} = {p.get('value', '')}")
    lines.append("")
    return lines


def _render_formal_validity(rc: dict) -> list[str]:
    lines = ["### Formal validity", ""]
    for item in sorted(rc.get("proposition_evaluations", []), key=lambda x: x.get("symbol", "")):
        sym = item.get("symbol", "?")
        val = item.get("validity", "?")
        reasoning = item.get("reasoning", "")
        lines.append(f"- {sym} validity: {val} — {reasoning}")
    arg_validity = rc.get("argument_validity")
    if arg_validity is not None:
        lines.append(f"- argument validity: {arg_validity}")
    lines.append("")
    return lines


def _render_logical_issues(form_rc: dict, content_rc: dict) -> list[str]:
    lines = ["### Logical issues", ""]
    for issue in form_rc.get("logical_issues", []) + content_rc.get("logical_issues", []):
        lines.append(f"- {issue}")
    lines.append("")
    return lines


def _render_recommendations(form_rc: dict, content_rc: dict, improver_results: list) -> list[str]:
    lines = ["### Recommendations", ""]
    for rec in form_rc.get("recommendations", []) + content_rc.get("recommendations", []):
        lines.append(f"- {rec}")
    for r in improver_results:
        recs = r.get("result_content", {}).get("recommendations", [])
        for rec in recs:
            impact = rec.get("impact", "")
            reasoning = rec.get("reasoning", "")
            lines.append(f"**{impact.capitalize()} impact**: {reasoning}\n")
            for prop in rec.get("propositions", []):
                ptype = prop.get("type", "")
                sym = prop.get("symbol") or "new"
                text_p = prop.get("proposition", "")
                lines.append(f"- {sym} ({ptype}): {text_p}")
            lines.append("")
    lines.append("")
    return lines


def _render_analysis_body(results: dict) -> str:
    from mdc.dianoia_results import ContentEvalResult, FormalEvalResult, FormalizerResult, PhrasingEvalResult

    results_by_agent = results.get("results_by_agent", {})

    formalizer_rc = cast(FormalizerResult, next(iter(results_by_agent.get("formalizer", [])), {}).get("result_content", {}))
    form_rc = cast(FormalEvalResult, next(iter(results_by_agent.get("form_evaluator", [])), {}).get("result_content", {}))
    # truth_evaluator and content_validity_evaluator carry disjoint fields
    # (truth_evaluations/incoherent_sets vs validity_evaluations/logical_issues/
    # recommendations); merge them into the shape ContentEvalResult describes
    truth_rc = next(iter(results_by_agent.get("truth_evaluator", [])), {}).get("result_content", {})
    cv_rc = next(iter(results_by_agent.get("content_validity_evaluator", [])), {}).get("result_content", {})
    content_rc = cast(ContentEvalResult, {**truth_rc, **cv_rc})
    phrasing_rc = cast(PhrasingEvalResult, next(iter(results_by_agent.get("phrasing_evaluator", [])), {}).get("result_content", {}))
    improver_results = results_by_agent.get("improver", [])

    lines: list[str] = []
    lines += _render_truth(content_rc)
    lines += _render_content_validity(content_rc)
    lines += _render_phrasing(phrasing_rc)
    lines += _render_formalizations(formalizer_rc)
    lines += _render_formal_validity(form_rc)
    lines += _render_logical_issues(form_rc, content_rc)
    lines += _render_recommendations(form_rc, content_rc, improver_results)
    return "\n".join(lines).rstrip("\n") + "\n"


def _parse_analysis_blocks(text: str) -> dict[int, str]:
    """Return {proposition_number: body_text} for existing '## Argument ... (proposition N) ...' blocks."""
    blocks: dict[int, str] = {}
    parts = re.split(r"(?=^## Argument )", text, flags=re.MULTILINE)
    for part in parts:
        first_line = part.splitlines()[0] if part else ""
        m = _SECTION_RE.match(first_line.strip())
        if m:
            prop_num = int(m.group(2))
            body = "\n".join(part.splitlines()[1:]).strip("\n")
            blocks[prop_num] = body
    return blocks


def _write_analysis(analysis_path: Path, title: str, date_str: str, argument: list[dict], blocks: dict[int, str]) -> None:
    from mdc.argue import assign_argument_labels

    labels = assign_argument_labels(argument)
    lines = ["", f"# {title}", date_str, ""]
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

    analysis_path = companion.with_suffix("").with_suffix(".analysis.md")
    blocks = _parse_analysis_blocks(analysis_path.read_text(encoding="utf-8")) if analysis_path.exists() else {}
    blocks[proposition] = _render_analysis_body(results)

    _write_analysis(analysis_path, title, date_str, argument, blocks)
    print(f"Analysis written to {analysis_path.name}")
    return 0
