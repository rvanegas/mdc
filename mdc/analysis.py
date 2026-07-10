"""Canonical .analysis.md block format: rendering and parsing.

Roxana's DianoiaResultData is the canonical shape. Each analysis block
renders dianoia agent results into markdown sections whose line grammar
parses back, losslessly, into that shape (parse_analysis_result). Sections
outside the canonical set — Definitions, Phrasing, Improvements — are
display-only and skipped by the parser.

Canonical grammar (sections omitted when empty):

    ### Truth
    - <symbol> truth: <value> — <reasoning>

    ### Content validity
    - <symbol> validity: <value> — <reasoning>

    ### Incoherent sets
    - <sym>, <sym> incoherence: <value> — <reasoning>

    ### Logical issues (content) / ### Recommendations (content)
    - <text>

    ### Formalizations
    - <symbol>: <ascii>

    ### Formal validity
    - <symbol> validity: <value> — <reasoning>
    - argument validity: <value>

    ### Logical issues (formal) / ### Recommendations (formal)
    - <text>

Reasoning is optional; the first " — " on a line delimits it. Reasoning
text is flattened to one line at render time so the grammar holds.
"""

from __future__ import annotations

import re


_EM_SEP = " — "

_SCORED_RE = re.compile(
    r"^- (?P<syms>.+?) (?P<kind>truth|validity|incoherence): (?P<val>[-\d.]+)"
    r"(?: — (?P<reason>.*))?$"
)
_ARG_VALIDITY_RE = re.compile(r"^- argument validity: (?P<val>[-\d.]+)$")
_FORMALIZATION_RE = re.compile(r"^- (?P<sym>\S+): (?P<ascii>.*)$")
_PLAIN_RE = re.compile(r"^- (?P<text>.*)$")


def _one_line(text: str) -> str:
    return " ".join(str(text).split())


def _scored_line(symbols: str, kind: str, value, reasoning: str) -> str:
    line = f"- {symbols} {kind}: {value}"
    if reasoning:
        line += _EM_SEP + _one_line(reasoning)
    return line


# --- Rendering (dianoia snake_case results → markdown) ---

def _render_scored_section(title: str, items: list, kind: str, value_key: str) -> list[str]:
    if not items:
        return []
    lines = [f"### {title}", ""]
    for item in sorted(items, key=lambda x: x.get("symbol", "")):
        lines.append(_scored_line(
            item.get("symbol", "?"), kind, item.get(value_key, "?"),
            item.get("reasoning", "")))
    lines.append("")
    return lines


def _render_plain_section(title: str, items: list[str]) -> list[str]:
    if not items:
        return []
    lines = [f"### {title}", ""]
    for text in items:
        lines.append(f"- {_one_line(text)}")
    lines.append("")
    return lines


def _render_incoherent_sets(items: list) -> list[str]:
    if not items:
        return []
    lines = ["### Incoherent sets", ""]
    for item in items:
        syms = ", ".join(item.get("symbols", []))
        lines.append(_scored_line(
            syms, "incoherence", item.get("incoherence_value", "?"),
            item.get("reasoning", "")))
    lines.append("")
    return lines


def _render_formalizations(rc: dict) -> list[str]:
    items = rc.get("formalizations", [])
    if not items:
        return []
    lines = ["### Formalizations", ""]
    for f in items:
        lines.append(f"- {f.get('symbol', '?')}: {_one_line(f.get('ascii', ''))}")
    lines.append("")
    return lines


def _render_definitions(rc: dict) -> list[str]:
    defs = rc.get("definitions", {})
    constants = defs.get("constants", [])
    predicates = defs.get("predicates", [])
    if not constants and not predicates:
        return []
    lines = ["### Definitions", ""]
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
    items = rc.get("proposition_evaluations", [])
    arg_validity = rc.get("argument_validity")
    if not items and arg_validity is None:
        return []
    lines = ["### Formal validity", ""]
    for item in sorted(items, key=lambda x: x.get("symbol", "")):
        lines.append(_scored_line(
            item.get("symbol", "?"), "validity", item.get("validity", "?"),
            item.get("reasoning", "")))
    if arg_validity is not None:
        lines.append(f"- argument validity: {arg_validity}")
    lines.append("")
    return lines


def _render_phrasing(rc: dict) -> list[str]:
    evaluations = rc.get("phrasing_evaluations", [])
    if not evaluations:
        return []
    lines = ["### Phrasing", ""]
    for item in sorted(evaluations, key=lambda x: x.get("symbol", "")):
        issues = "; ".join(_one_line(i) for i in item.get("issues", []))
        recommendation = _one_line(item.get("recommendation", ""))
        lines.append(f"- {item.get('symbol', '?')}: {issues}{_EM_SEP}{recommendation}")
    lines.append("")
    return lines


def _render_improvements(improver_results: list) -> list[str]:
    lines: list[str] = []
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
    if not lines:
        return []
    return ["### Improvements", ""] + lines


def render_analysis_body(results: dict) -> str:
    """Render dianoia formatted results into a canonical analysis block body."""
    results_by_agent = results.get("results_by_agent", {})

    def rc(agent_type: str) -> dict:
        return next(iter(results_by_agent.get(agent_type, [])), {}).get("result_content", {})

    truth_rc = rc("truth_evaluator")
    cv_rc = rc("content_validity_evaluator")
    phrasing_rc = rc("phrasing_evaluator")
    formalizer_rc = rc("formalizer")
    form_rc = rc("form_evaluator")
    improver_results = results_by_agent.get("improver", [])

    lines: list[str] = []
    lines += _render_scored_section("Truth", truth_rc.get("truth_evaluations", []),
                                    "truth", "truth_value")
    lines += _render_scored_section("Content validity", cv_rc.get("validity_evaluations", []),
                                    "validity", "validity_value")
    lines += _render_incoherent_sets(truth_rc.get("incoherent_sets", []))
    lines += _render_plain_section("Logical issues (content)", cv_rc.get("logical_issues", []))
    lines += _render_plain_section("Recommendations (content)", cv_rc.get("recommendations", []))
    lines += _render_formalizations(formalizer_rc)
    lines += _render_definitions(formalizer_rc)
    lines += _render_formal_validity(form_rc)
    lines += _render_plain_section("Logical issues (formal)", form_rc.get("logical_issues", []))
    lines += _render_plain_section("Recommendations (formal)", form_rc.get("recommendations", []))
    lines += _render_phrasing(phrasing_rc)
    lines += _render_improvements(improver_results)
    return "\n".join(lines).rstrip("\n") + "\n"


# --- Parsing (markdown → Roxana DianoiaResultData shape) ---

def _empty_result() -> dict:
    return {
        "truthEvaluations": [],
        "validityEvaluations": [],
        "incoherentSets": [],
        "contentLogicalIssues": [],
        "contentRecommendations": [],
        "formalizations": [],
        "propositionEvaluations": [],
        "argumentValidity": None,
        "formalLogicalIssues": [],
        "formalRecommendations": [],
    }


def _number(s: str):
    value = float(s)
    return int(value) if value.is_integer() else value


def parse_analysis_result(body: str) -> dict:
    """Parse a canonical analysis block body into Roxana's DianoiaResultData shape.

    Non-canonical sections (Definitions, Phrasing, Improvements) are skipped.
    """
    result = _empty_result()
    section: str | None = None

    for raw_line in body.splitlines():
        line = raw_line.rstrip()
        if line.startswith("### "):
            section = line[4:].strip()
            continue
        if not line.startswith("- "):
            continue

        if section == "Truth":
            m = _SCORED_RE.match(line)
            if m:
                result["truthEvaluations"].append({
                    "symbol": m["syms"], "truth_value": _number(m["val"]),
                    "reasoning": m["reason"] or "",
                })
        elif section == "Content validity":
            m = _SCORED_RE.match(line)
            if m:
                result["validityEvaluations"].append({
                    "symbol": m["syms"], "validity_value": _number(m["val"]),
                    "reasoning": m["reason"] or "",
                })
        elif section == "Incoherent sets":
            m = _SCORED_RE.match(line)
            if m:
                result["incoherentSets"].append({
                    "symbols": [s.strip() for s in m["syms"].split(",")],
                    "incoherence_value": _number(m["val"]),
                    "reasoning": m["reason"] or "",
                })
        elif section == "Logical issues (content)":
            result["contentLogicalIssues"].append(_PLAIN_RE.match(line)["text"])
        elif section == "Recommendations (content)":
            result["contentRecommendations"].append(_PLAIN_RE.match(line)["text"])
        elif section == "Formalizations":
            m = _FORMALIZATION_RE.match(line)
            if m:
                result["formalizations"].append({
                    "symbol": m["sym"], "ascii": m["ascii"], "json_structure": "",
                })
        elif section == "Formal validity":
            m = _ARG_VALIDITY_RE.match(line)
            if m:
                result["argumentValidity"] = _number(m["val"])
                continue
            m = _SCORED_RE.match(line)
            if m:
                result["propositionEvaluations"].append({
                    "symbol": m["syms"], "validity": _number(m["val"]),
                    "reasoning": m["reason"] or "",
                })
        elif section == "Logical issues (formal)":
            result["formalLogicalIssues"].append(_PLAIN_RE.match(line)["text"])
        elif section == "Recommendations (formal)":
            result["formalRecommendations"].append(_PLAIN_RE.match(line)["text"])
        # Definitions, Phrasing, Improvements, unknown sections: display-only

    return result


# --- Audit section (file-level, precedes the argument blocks) ---

def render_audit_body(audit: dict) -> str:
    """Render an AuditResult dict as the body of the file-level ## Audit section."""
    if audit.get("satisfied"):
        return "- satisfied"
    lines = []
    for f in audit.get("findings", []):
        syms = ", ".join(f.get("step_symbols", []))
        issue = _one_line(f.get("issue", ""))
        pointer = _one_line(f.get("pointer", ""))
        lines.append(f"- {f.get('condition', '?')} [{syms}]: {issue}{_EM_SEP}{pointer}")
    return "\n".join(lines)
