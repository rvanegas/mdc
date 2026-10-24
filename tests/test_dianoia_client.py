import io
import json
import urllib.error
from unittest.mock import patch

import pytest

from mdc import dianoia_client
from mdc.config import AppConfig


BASE = "https://dianoia.example/api"
TOKEN = "secret-token"

ARGS = {"argument": [
    {"symbol": "1", "proposition": "All humans are mortal.", "justifiers": [], "truth_score": "1.0"},
    {"symbol": "2", "proposition": "Socrates is a human.", "justifiers": [], "truth_score": "1.0"},
    {"symbol": "3", "proposition": "Socrates is mortal.", "justifiers": ["1", "2"], "truth_score": "1.0"},
    {"symbol": "4", "proposition": "Socrates will die.", "justifiers": ["3"], "truth_score": "1.0"},
]}

# Shaped after dianoia's AuditResult and AgentResultManager.get_formatted_results.
AUDIT_RESULT = {"satisfied": True, "findings": []}
STARTED = {"conversation_id": "abc:mdc", "snapshot_id": "1"}
RESULTS_PARTIAL = {
    "conversation_id": "abc:mdc", "snapshot_id": "1",
    "results_by_agent": {"formalizer": [{"result_content": "{}"}]},
    "total_count": 1, "agent_types": ["formalizer"],
    "tasks_complete": False, "active_tasks": [],
}
RESULTS_DONE = {
    **RESULTS_PARTIAL,
    "results_by_agent": {"formalizer": [{"result_content": "{}"}], "truth_evaluator": [{"result_content": "{}"}]},
    "total_count": 2, "agent_types": ["formalizer", "truth_evaluator"],
    "tasks_complete": True,
}


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeDianoia:
    """Replays canned responses and records each request."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request):
        body = json.loads(request.data.decode("utf-8")) if request.data else None
        self.requests.append({
            "method": request.get_method(),
            "url": request.full_url,
            "auth": request.get_header("Authorization"),
            "body": body,
        })
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return _Response(json.dumps(response).encode("utf-8"))


@pytest.fixture
def configured():
    config = AppConfig(dianoia_api_url=BASE + "/", dianoia_token=TOKEN)
    with patch("mdc.config.load_config", return_value=config):
        yield


def _serve(*responses):
    fake = FakeDianoia(*responses)
    return fake, patch("urllib.request.urlopen", fake)


def test_audit_posts_argument_with_token(configured):
    fake, served = _serve(AUDIT_RESULT)
    with served:
        result = dianoia_client.audit({**ARGS, "explanation": "ignored"})
    assert result == AUDIT_RESULT
    [request] = fake.requests
    assert request["method"] == "POST"
    assert request["url"] == f"{BASE}/argument/audit"
    assert request["auth"] == f"Bearer {TOKEN}"
    assert request["body"] == {"argument": ARGS["argument"]}


def test_extract_sends_text_and_limit(configured):
    fake, served = _serve(ARGS)
    with served:
        result = dianoia_client.extract("Socrates is mortal because…", max_props=5)
    assert result == ARGS
    [request] = fake.requests
    assert request["url"] == f"{BASE}/argument/extract"
    assert request["body"] == {"text": "Socrates is mortal because…", "max_props": 5}


def test_evaluate_polls_until_complete(configured, capsys):
    fake, served = _serve(STARTED, RESULTS_PARTIAL, RESULTS_DONE)
    with served, patch("time.sleep"):
        result = dianoia_client.evaluate(ARGS)
    assert result == RESULTS_DONE
    start, first_poll, second_poll = fake.requests
    assert start["url"] == f"{BASE}/argument/evaluate"
    assert [s["symbol"] for s in start["body"]["argument"]] == ["1", "2", "3", "4"]
    assert first_poll["method"] == "GET"
    assert first_poll["url"] == f"{BASE}/agents/results?conversation_id=abc%3Amdc&snapshot_id=1"
    assert second_poll["url"] == first_poll["url"]
    assert capsys.readouterr().err == "formalizer done\ntruth_evaluator done\n"


def test_evaluate_step_sends_only_that_argument(configured):
    fake, served = _serve(STARTED, RESULTS_DONE)
    with served, patch("time.sleep"):
        dianoia_client.evaluate(ARGS, step="3")
    sent = fake.requests[0]["body"]["argument"]
    assert [s["symbol"] for s in sent] == ["1", "2", "3"]


def test_evaluate_unknown_step_fails_before_any_request(configured):
    fake, served = _serve()
    with served, pytest.raises(RuntimeError, match="step '9' not found"):
        dianoia_client.evaluate(ARGS, step="9")
    assert fake.requests == []


def test_evaluate_times_out_with_partial_results(configured, capsys):
    fake, served = _serve(STARTED, RESULTS_PARTIAL)
    clock = iter([0.0, dianoia_client._EVALUATE_TIMEOUT_SECONDS + 1])
    with served, patch("time.sleep"), patch("time.monotonic", lambda: next(clock)):
        result = dianoia_client.evaluate(ARGS)
    assert result == RESULTS_PARTIAL
    assert "timed out" in capsys.readouterr().err


def test_http_error_becomes_runtime_error(configured):
    error = urllib.error.HTTPError(f"{BASE}/argument/extract", 401, "Unauthorized", {},
                                   io.BytesIO(b'{"detail":"Invalid or missing bearer token"}'))
    _, served = _serve(error)
    with served, pytest.raises(RuntimeError, match="HTTP 401.*bearer token"):
        dianoia_client.extract("text")


def test_unreachable_becomes_runtime_error(configured):
    _, served = _serve(urllib.error.URLError("connection refused"))
    with served, pytest.raises(RuntimeError, match="unreachable: connection refused"):
        dianoia_client.audit(ARGS)


def test_missing_config_is_reported():
    with patch("mdc.config.load_config", return_value=AppConfig()), \
         pytest.raises(RuntimeError, match="dianoia_api_url and dianoia_token"):
        dianoia_client.audit(ARGS)
