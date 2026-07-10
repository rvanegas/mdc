from mdc.analysis import parse_analysis_result, render_analysis_body, render_audit_body


RESULTS = {
    "results_by_agent": {
        "truth_evaluator": [{"result_content": {
            "truth_evaluations": [
                {"symbol": "1", "truth_value": 0.9,
                 "reasoning": "Widely\naccepted   biological fact."},
                {"symbol": "2", "truth_value": 1, "reasoning": "Historical record."},
            ],
            "incoherent_sets": [
                {"symbols": ["1", "2"], "incoherence_value": 0.2,
                 "reasoning": "Mild tension."},
            ],
        }}],
        "content_validity_evaluator": [{"result_content": {
            "validity_evaluations": [
                {"symbol": "3", "validity_value": 0.95, "reasoning": "Follows directly."},
            ],
            "logical_issues": ["Premise 1 is a bare universal."],
            "recommendations": ["Restrict the universal to relevant cases."],
        }}],
        "phrasing_evaluator": [{"result_content": {
            "phrasing_evaluations": [
                {"symbol": "2", "issues": ["Begins with 'Therefore'"],
                 "recommendation": "Drop the transition."},
            ],
        }}],
        "formalizer": [{"result_content": {
            "formalizations": [
                {"symbol": "1", "ascii": "Ax(Px -> Qx)", "json_structure": "{...}"},
                {"symbol": "3", "ascii": "Qa", "json_structure": "{...}"},
            ],
            "definitions": {
                "constants": [{"symbol": "a", "value": "Socrates"}],
                "predicates": [{"symbol": "P", "arity": 1, "value": "is human"}],
            },
        }}],
        "form_evaluator": [{"result_content": {
            "proposition_evaluations": [
                {"symbol": "3", "validity": 1, "reasoning": "Modus ponens."},
            ],
            "argument_validity": 1,
            "logical_issues": ["Quantifier scope unclear in 1."],
            "recommendations": ["Make the scope explicit."],
        }}],
        "improver": [{"result_content": {
            "recommendations": [{
                "impact": "high", "reasoning": "Strengthens the link.",
                "propositions": [
                    {"type": "add", "symbol": None, "proposition": "A bridging premise."},
                ],
            }],
        }}],
    },
}


def test_round_trip_to_roxana_shape():
    body = render_analysis_body(RESULTS)
    parsed = parse_analysis_result(body)
    assert parsed == {
        "truthEvaluations": [
            {"symbol": "1", "truth_value": 0.9,
             "reasoning": "Widely accepted biological fact."},
            {"symbol": "2", "truth_value": 1, "reasoning": "Historical record."},
        ],
        "validityEvaluations": [
            {"symbol": "3", "validity_value": 0.95, "reasoning": "Follows directly."},
        ],
        "incoherentSets": [
            {"symbols": ["1", "2"], "incoherence_value": 0.2, "reasoning": "Mild tension."},
        ],
        "contentLogicalIssues": ["Premise 1 is a bare universal."],
        "contentRecommendations": ["Restrict the universal to relevant cases."],
        "formalizations": [
            {"symbol": "1", "ascii": "Ax(Px -> Qx)", "json_structure": ""},
            {"symbol": "3", "ascii": "Qa", "json_structure": ""},
        ],
        "propositionEvaluations": [
            {"symbol": "3", "validity": 1, "reasoning": "Modus ponens."},
        ],
        "argumentValidity": 1,
        "formalLogicalIssues": ["Quantifier scope unclear in 1."],
        "formalRecommendations": ["Make the scope explicit."],
    }


def test_render_includes_display_only_sections():
    body = render_analysis_body(RESULTS)
    assert "### Definitions" in body
    assert "- a = Socrates" in body
    assert "- P/1 = is human" in body
    assert "### Phrasing" in body
    assert "- 2: Begins with 'Therefore' — Drop the transition." in body
    assert "### Improvements" in body
    assert "**High impact**: Strengthens the link." in body
    assert "- new (add): A bridging premise." in body


def test_parse_skips_display_only_sections():
    parsed = parse_analysis_result(render_analysis_body(RESULTS))
    flattened = str(parsed)
    assert "Socrates" not in flattened
    assert "Drop the transition" not in flattened
    assert "bridging premise" not in flattened


def test_render_omits_empty_sections():
    body = render_analysis_body({"results_by_agent": {}})
    assert body.strip() == ""

    truth_only = {"results_by_agent": {"truth_evaluator": [{"result_content": {
        "truth_evaluations": [{"symbol": "1", "truth_value": 0.5, "reasoning": ""}],
    }}]}}
    body = render_analysis_body(truth_only)
    assert "### Truth" in body
    assert "### Content validity" not in body
    assert "### Formal validity" not in body


def test_parse_empty_body():
    parsed = parse_analysis_result("")
    assert parsed["truthEvaluations"] == []
    assert parsed["argumentValidity"] is None


def test_scored_line_without_reasoning():
    parsed = parse_analysis_result("### Truth\n\n- 1 truth: 0.5\n")
    assert parsed["truthEvaluations"] == [
        {"symbol": "1", "truth_value": 0.5, "reasoning": ""},
    ]


def test_reasoning_containing_em_dash_round_trips():
    results = {"results_by_agent": {"truth_evaluator": [{"result_content": {
        "truth_evaluations": [
            {"symbol": "1", "truth_value": 0.5, "reasoning": "Contested — some disagree."},
        ],
    }}]}}
    parsed = parse_analysis_result(render_analysis_body(results))
    assert parsed["truthEvaluations"][0]["reasoning"] == "Contested — some disagree."


def test_render_audit_body_satisfied():
    assert render_audit_body({"satisfied": True, "findings": []}) == "- satisfied"


def test_render_audit_body_findings():
    audit = {
        "satisfied": False,
        "findings": [
            {"condition": "connectivity", "step_symbols": ["1"],
             "issue": "Step supports nothing.",
             "pointer": "Cite step 1 in a later\nstep or remove it."},
            {"condition": "conclusion", "step_symbols": ["16", "20"],
             "issue": "Two sinks.", "pointer": "Merge or connect them."},
        ],
    }
    body = render_audit_body(audit)
    assert body.splitlines() == [
        "- connectivity [1]: Step supports nothing. — Cite step 1 in a later step or remove it.",
        "- conclusion [16, 20]: Two sinks. — Merge or connect them.",
    ]
