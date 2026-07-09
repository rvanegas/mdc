from __future__ import annotations

import re
import sys
from pathlib import Path


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


def _print_argument(args_dict: dict) -> None:
    argument = args_dict.get("argument", [])
    print("Argument:")
    for s in argument:
        j = f" (from: {', '.join(s['justifiers'])})" if s.get("justifiers") else ""
        print(f"  {s['symbol']}{j}: {s['proposition']}")


def _append_evaluation(path: Path, results: dict, verbose: bool) -> None:
    """Inject formalizations inline; append content/improvement sections."""
    from typing import cast
    from mdc.argue import extract_core_sections, inject_formalizations, markdown_to_argument
    from mdc.dianoia_results import (
        ContentEvalResult,
        FormalEvalResult,
        FormalizerResult,
        ImproverResult,
    )

    text = extract_core_sections(path.read_text(encoding="utf-8"))

    text = text.rstrip("\n") + "\n"

    results_by_agent = results.get("results_by_agent", {})

    # Collect formalizer output
    formalizer_results = results_by_agent.get("formalizer", [])
    new_formalizations: dict = {}
    all_definitions: dict = {"predicates": [], "constants": []}
    for r in formalizer_results:
        rc = cast(FormalizerResult, r.get("result_content", {}))
        for f in rc.get("formalizations", []):
            sym = f.get("symbol")
            if sym:
                new_formalizations[sym] = f.get("ascii", "")
        defs = rc.get("definitions", {})
        all_definitions["predicates"].extend(defs.get("predicates", []))
        all_definitions["constants"].extend(defs.get("constants", []))

    # Merge: existing endorsed sub-bullets win over newly generated
    try:
        existing = markdown_to_argument(text)
    except ValueError:
        existing = {"argument": []}
    endorsed: dict = {}
    for step in existing.get("argument", []):
        form = step.get("formalization") or {}
        if form.get("endorsed") and form.get("ascii"):
            endorsed[step["symbol"]] = form["ascii"]
    by_symbol = {**new_formalizations, **endorsed}

    if by_symbol:
        text = inject_formalizations(text, by_symbol)

    # When formalizer ran and produced definitions, replace ## Definitions content in-place
    # (un-endorsed predicates are naturally absent from formalizer output, implementing
    # un-endorsement by omission). Skip when formalizer returned early with no new definitions
    # (all steps already endorsed) so user-supplied definitions are preserved.
    if formalizer_results and (all_definitions["predicates"] or all_definitions["constants"]):
        def_content_lines = []
        for c in all_definitions["constants"]:
            def_content_lines.append(f"- {c.get('symbol', '?')} = {c.get('value', '')}")
        for p in all_definitions["predicates"]:
            sym = p.get('symbol', '?')
            arity = p.get('arity', 0)
            label = f"{sym}/{arity}" if arity else sym
            def_content_lines.append(f"- {label} = {p.get('value', '')}")
        new_def_content = "\n".join(def_content_lines)
        # Match the content block between ## Definitions header and next ## section (or end)
        def_content_match = re.search(
            r"(## Definitions[^\n]*\n)((?:(?!## ).*\n)*)", text
        )
        if def_content_match:
            replacement = (new_def_content + "\n") if new_def_content else ""
            text = (
                text[: def_content_match.start(2)]
                + replacement
                + text[def_content_match.end(2):]
            )
        else:
            # No existing section — insert before ## Argument
            section_block = "\n## Definitions\n" + (new_def_content + "\n" if new_def_content else "")
            insert_match = re.search(r"\n## Argument\b", text)
            if insert_match:
                text = text[: insert_match.start()] + section_block + text[insert_match.start():]
            else:
                text = text + section_block

    text = text.rstrip("\n") + "\n"
    lines = []

    form_eval_results = results_by_agent.get("form_evaluator", [])
    for r in form_eval_results:
        rc = cast(FormalEvalResult, r.get("result_content", {}))
        prop_evals = rc.get("proposition_evaluations", [])
        arg_validity = rc.get("argument_validity")
        issues = rc.get("logical_issues", [])
        recommendations = rc.get("recommendations", [])
        if prop_evals or issues or arg_validity is not None:
            lines.append("\n## Formal evaluation\n")
        for item in sorted(prop_evals, key=lambda x: x.get("symbol", "")):
            sym = item.get("symbol", "?")
            val = item.get("validity", "?")
            reasoning = item.get("reasoning", "")
            lines.append(f"- {sym} validity: {val} — {reasoning}")
        if prop_evals:
            lines.append("")
        if arg_validity is not None:
            lines.append(f"- argument validity: {arg_validity}")
            lines.append("")
        for issue in issues:
            lines.append(f"- {issue}")
        if issues:
            lines.append("")
        for rec in recommendations:
            lines.append(f"- {rec}")
        if recommendations:
            lines.append("")

    content_results = results_by_agent.get("content_evaluator", [])
    for r in content_results:
        rc = cast(ContentEvalResult, r.get("result_content", {}))
        truth = rc.get("truth_evaluations", [])
        validity = rc.get("validity_evaluations", [])
        incoherent = rc.get("incoherent_sets", [])
        if truth or validity or incoherent:
            lines.append("\n## Content evaluation\n")
        for item in sorted(truth, key=lambda x: x.get("symbol", "")):
            sym = item.get("symbol", "?")
            val = item.get("truth_value", "?")
            reasoning = item.get("reasoning", "")
            lines.append(f"- {sym} truth: {val} — {reasoning}")
        if truth:
            lines.append("")
        for item in sorted(validity, key=lambda x: x.get("symbol", "")):
            sym = item.get("symbol", "?")
            val = item.get("validity_value", "?")
            reasoning = item.get("reasoning", "")
            lines.append(f"- {sym} validity: {val} — {reasoning}")
        if validity:
            lines.append("")
        for item in incoherent:
            syms = ", ".join(item.get("symbols", []))
            val = item.get("incoherence_value", "?")
            lines.append(f"- incoherent ({val}): {syms}")
        if incoherent:
            lines.append("")

    improvement_results = results_by_agent.get("improver", [])
    for r in improvement_results:
        recs = cast(ImproverResult, r.get("result_content", {})).get("recommendations", [])
        if recs:
            lines.append("\n## Improvement recommendations\n")
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

    path.write_text(text + "\n".join(lines), encoding="utf-8")


def run_argue(path: Path, verbose: bool = False, max_props: int | None = None, step: str | None = None) -> int:
    from mdc.argue import argument_to_markdown, markdown_to_argument
    from mdc import dianoia_client
    from mdc.config import load_config
    from mdc.form import check_global_issues

    def _primary(companion: Path) -> Path:
        return companion.with_suffix("").with_suffix(".md")

    def _read_file(p: Path) -> str:
        try:
            return p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = p.read_text(encoding="latin-1")
            p.with_suffix(p.suffix + ".bak").write_bytes(p.read_bytes())
            p.write_text(text, encoding="utf-8")
            print(f"Warning: {p.name} was not UTF-8; converted in place (backup: {p.name}.bak).", file=sys.stderr)
            return text

    if path.name.endswith(".argument.md"):
        companion = path
        path = _primary(path)
    elif path.name.endswith(".document.md"):
        if not path.exists():
            print(f"Error: '{path}' does not exist.")
            return 1
        companion = path.with_suffix("").with_suffix(".argument.md")
    else:
        if not path.exists():
            print(f"Error: '{path}' does not exist.")
            return 1
        if path.suffix.lower() != ".md":
            print(f"Error: '{path}' does not have a .md extension.")
            return 1
        # Adopt bare document into the companion model by renaming it.
        document = path.with_suffix(".document.md")
        path.rename(document)
        path = document
        companion = path.with_suffix("").with_suffix(".argument.md")

    if companion.exists():
        # Evaluate: companion exists, submit it to dianoia
        try:
            args_dict = markdown_to_argument(companion.read_text(encoding="utf-8"))
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        print("Submitting to dianoia for evaluation…")
        try:
            results = dianoia_client.evaluate(args_dict, step=step)
        except (FileNotFoundError, RuntimeError) as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        _append_evaluation(companion, results, verbose)
        print(f"Evaluation written to {companion.name}")
        return 0

    # Extract: no companion yet — validate and extract from the primary document
    if step:
        print("Warning: --step ignored during extraction", file=sys.stderr)
    text = _read_file(path)
    from mdc.library import is_library_transcript
    config = load_config()
    if is_library_transcript(text, config.user_names, config.llm_names):
        print("Error: mdc argue requires a plain document, not a transcript.")
        return 1
    errs = check_global_issues(path)
    if errs:
        for e in errs:
            print(f"  error: {e}")
        return 1

    print("Extracting argument…")
    try:
        args_dict = dianoia_client.extract(text, max_props=max_props)
    except (FileNotFoundError, RuntimeError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    try:
        title, date_str = _read_title_date(text)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    try:
        companion_text = argument_to_markdown(args_dict, title, date_str)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    companion.write_text(companion_text, encoding="utf-8")
    _print_argument(args_dict)
    print(f"\nWritten to {companion.name}. Edit it, then run: mdc argue {path.name}")
    return 0
