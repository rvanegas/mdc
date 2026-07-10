from pathlib import Path
from unittest.mock import patch

import pytest

from mdc.cmd_audit import run_audit


ARGUMENT_TEXT = """
# Sample Argument
2026-07-08

## Argument
- 1: All humans are mortal.
- 2: Socrates is a human.
- 3 (from: 1, 2): Socrates is mortal.
"""


@pytest.fixture
def argument_file(tmp_path: Path) -> Path:
    companion = tmp_path / "2026-07-08-sample-argument.argument.md"
    companion.write_text(ARGUMENT_TEXT, encoding="utf-8")
    return companion


def test_audit_satisfied(argument_file, capsys):
    with patch("mdc.dianoia_client.audit",
               return_value={"satisfied": True, "findings": []}) as mock_audit:
        rc = run_audit(argument_file)
    assert rc == 0
    sent = mock_audit.call_args.args[0]
    assert [s["symbol"] for s in sent["argument"]] == ["1", "2", "3"]
    assert "satisfies all structural conditions" in capsys.readouterr().out


def test_audit_findings_reported(argument_file, capsys):
    result = {
        "satisfied": False,
        "findings": [{
            "condition": "connectivity",
            "step_symbols": ["1"],
            "issue": "Step supports nothing.",
            "pointer": "Cite step 1 in a later step or remove it.",
        }],
    }
    with patch("mdc.dianoia_client.audit", return_value=result):
        rc = run_audit(argument_file)
    assert rc == 1
    out = capsys.readouterr().out
    assert "1 finding" in out
    assert "Connectivity — 1" in out
    assert "Cite step 1" in out


def test_audit_resolves_document_to_companion(argument_file, capsys):
    document = argument_file.with_suffix("").with_suffix(".document.md")
    document.write_text("irrelevant", encoding="utf-8")
    with patch("mdc.dianoia_client.audit",
               return_value={"satisfied": True, "findings": []}):
        rc = run_audit(document)
    assert rc == 0


def test_audit_missing_companion(tmp_path, capsys):
    rc = run_audit(tmp_path / "2026-07-08-nope.document.md")
    assert rc == 1
    assert "does not exist" in capsys.readouterr().out


def test_audit_dianoia_error(argument_file, capsys):
    with patch("mdc.dianoia_client.audit",
               side_effect=RuntimeError("dianoia audit failed")):
        rc = run_audit(argument_file)
    assert rc == 1
    assert "dianoia audit failed" in capsys.readouterr().err


FINDINGS_RESULT = {
    "satisfied": False,
    "findings": [{
        "condition": "connectivity",
        "step_symbols": ["1"],
        "issue": "Step supports nothing.",
        "pointer": "Cite step 1 in a later step or remove it.",
    }],
}


def _analysis_path(argument_file: Path) -> Path:
    return argument_file.with_suffix("").with_suffix(".analysis.md")


def test_audit_persisted_to_analysis_file(argument_file, capsys):
    with patch("mdc.dianoia_client.audit", return_value=FINDINGS_RESULT):
        run_audit(argument_file)
    text = _analysis_path(argument_file).read_text(encoding="utf-8")
    assert "# Sample Argument" in text
    assert "2026-07-08" in text
    assert "## Audit" in text
    assert ("- connectivity [1]: Step supports nothing. — "
            "Cite step 1 in a later step or remove it.") in text
    assert "written to" in capsys.readouterr().out


def test_audit_satisfied_persisted(argument_file):
    with patch("mdc.dianoia_client.audit",
               return_value={"satisfied": True, "findings": []}):
        run_audit(argument_file)
    text = _analysis_path(argument_file).read_text(encoding="utf-8")
    assert "## Audit" in text
    assert "- satisfied" in text


def test_audit_preserves_existing_analysis_blocks(argument_file):
    analysis = _analysis_path(argument_file)
    analysis.write_text(
        "\n# Sample Argument\n2026-07-08\n\n"
        "## Argument A (proposition 3) — Analysis\n\n"
        "### Truth\n\n- 1 truth: 0.9 — Solid.\n",
        encoding="utf-8",
    )
    with patch("mdc.dianoia_client.audit", return_value=FINDINGS_RESULT):
        run_audit(argument_file)
    text = analysis.read_text(encoding="utf-8")
    assert "## Audit" in text
    assert "## Argument A (proposition 3) — Analysis" in text
    assert "- 1 truth: 0.9 — Solid." in text
    # Audit section comes before argument blocks.
    assert text.index("## Audit") < text.index("## Argument A")


def test_rerun_replaces_audit_section(argument_file):
    with patch("mdc.dianoia_client.audit", return_value=FINDINGS_RESULT):
        run_audit(argument_file)
    with patch("mdc.dianoia_client.audit",
               return_value={"satisfied": True, "findings": []}):
        run_audit(argument_file)
    text = _analysis_path(argument_file).read_text(encoding="utf-8")
    assert text.count("## Audit") == 1
    assert "- satisfied" in text
    assert "connectivity" not in text


def test_analyze_preserves_audit_section(argument_file):
    from mdc.cmd_analyze import run_analyze

    with patch("mdc.dianoia_client.audit", return_value=FINDINGS_RESULT):
        run_audit(argument_file)

    results = {"results_by_agent": {"truth_evaluator": [{"result_content": {
        "truth_evaluations": [{"symbol": "1", "truth_value": 0.9, "reasoning": "Solid."}],
    }}]}}
    with patch("mdc.dianoia_client.evaluate", return_value=results):
        assert run_analyze(argument_file, 3) == 0

    text = _analysis_path(argument_file).read_text(encoding="utf-8")
    assert "## Audit" in text
    assert "connectivity [1]" in text
    assert "(proposition 3) — Analysis" in text
    assert "- 1 truth: 0.9 — Solid." in text
    assert text.index("## Audit") < text.index("(proposition 3)")
