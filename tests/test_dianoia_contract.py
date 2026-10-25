"""mdc's reading of dianoia's GET /api/agents/results, against a real response.

fixtures/dianoia_agents_results.json is the response dianoia's own contract test
validates (dianoia's src/tests/fixtures/agents_results.json, recorded
2026-10-25 through `dianoia_client.evaluate` for a three-proposition argument).
When dianoia's agents change, it re-records that file against its published
schema; copy it here, and a shape mdc no longer reads fails this test instead
of rendering an analysis with sections silently missing.
"""

import json
from pathlib import Path

from mdc.analysis import parse_analysis_result, render_analysis_body

RECORDED = json.loads((Path(__file__).parent / "fixtures" / "dianoia_agents_results.json").read_text())

# The agents render_analysis_body reads, by the names dianoia publishes.
READ_BY_MDC = {
    "truth_evaluator",
    "content_validity_evaluator",
    "phrasing_evaluator",
    "formalizer",
    "form_evaluator",
    "improver",
}


def content(agent_type: str) -> dict:
    return RECORDED["results_by_agent"][agent_type][0]["result_content"]


def by_symbol(items: list, key: str) -> dict:
    return {item["symbol"]: item[key] for item in items}


def test_every_agent_mdc_reads_is_published():
    assert RECORDED["tasks_complete"] is True
    assert READ_BY_MDC <= set(RECORDED["results_by_agent"])


def test_scores_survive_rendering_and_parsing():
    parsed = parse_analysis_result(render_analysis_body(RECORDED))

    assert by_symbol(parsed["truthEvaluations"], "truth_value") == \
        by_symbol(content("truth_evaluator")["truth_evaluations"], "truth_value")
    assert by_symbol(parsed["validityEvaluations"], "validity_value") == \
        by_symbol(content("content_validity_evaluator")["validity_evaluations"], "validity_value")
    assert by_symbol(parsed["propositionEvaluations"], "validity") == \
        by_symbol(content("form_evaluator")["proposition_evaluations"], "validity")
    assert parsed["argumentValidity"] == content("form_evaluator")["argument_validity"]
    assert by_symbol(parsed["formalizations"], "ascii") == \
        by_symbol(content("formalizer")["formalizations"], "ascii")


def test_every_rendered_section_has_content():
    body = render_analysis_body(RECORDED)
    for heading in ("Truth", "Content validity", "Formalization", "Formal validity", "Improvements"):
        assert heading in body, heading
