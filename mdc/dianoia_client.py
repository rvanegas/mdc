"""HTTP client for dianoia's argument API (bearer-token auth)."""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

_POLL_SECONDS = 2
_EVALUATE_TIMEOUT_SECONDS = 300


def _endpoint() -> tuple[str, str]:
    from mdc.config import load_config
    config = load_config()
    if not config.dianoia_api_url or not config.dianoia_token:
        raise RuntimeError("set dianoia_api_url and dianoia_token in ~/.config/mdc/config.toml.")
    return config.dianoia_api_url.rstrip("/"), config.dianoia_token


def _request(base: str, token: str, method: str, path: str,
             body: dict | None = None, query: dict | None = None) -> dict:
    url = base + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method=method,
    )
    try:
        with urllib.request.urlopen(request) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"dianoia {path} failed: HTTP {e.code} — {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"dianoia unreachable: {e.reason}") from e
    except json.JSONDecodeError as e:
        raise RuntimeError(f"dianoia {path} returned invalid JSON: {e}") from e


def _restrict_to_step(args: dict, step: str) -> dict:
    """Keep only the step and its justifiers — the one argument to evaluate."""
    target = next((s for s in args["argument"] if s["symbol"] == step), None)
    if target is None:
        raise RuntimeError(f"step '{step}' not found in argument.")
    keep = {step} | set(target.get("justifiers") or [])
    return {**args, "argument": [s for s in args["argument"] if s["symbol"] in keep]}


def extract(text: str, max_props: int | None = None) -> dict:
    """Extract an argument from text, return Arguments dict."""
    base, token = _endpoint()
    return _request(base, token, "POST", "/argument/extract",
                    body={"text": text, "max_props": max_props})


def audit(args: dict) -> dict:
    """Audit an Arguments dict, return an AuditResult dict."""
    base, token = _endpoint()
    return _request(base, token, "POST", "/argument/audit", body={"argument": args["argument"]})


def evaluate(args: dict, step: str | None = None) -> dict:
    """Evaluate an Arguments dict with dianoia's agents, return results dict.

    Starts the agents, then polls their results until all tasks complete,
    reporting each agent to stderr as its first result arrives.
    """
    base, token = _endpoint()
    if step is not None:
        args = _restrict_to_step(args, step)
    started = _request(base, token, "POST", "/argument/evaluate", body={"argument": args["argument"]})
    query = {"conversation_id": started["conversation_id"], "snapshot_id": started["snapshot_id"]}

    seen_agents: set[str] = set()
    deadline = time.monotonic() + _EVALUATE_TIMEOUT_SECONDS
    while True:
        time.sleep(_POLL_SECONDS)
        results = _request(base, token, "GET", "/agents/results", query=query)
        for agent_type in results.get("agent_types", []):
            if agent_type not in seen_agents:
                seen_agents.add(agent_type)
                print(f"{agent_type} done", file=sys.stderr)
        if results.get("tasks_complete"):
            return results
        if time.monotonic() > deadline:
            print("Warning: timed out waiting for agents", file=sys.stderr)
            return results
