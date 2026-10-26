import json

from fastapi.testclient import TestClient

from mdc import server


def test_healthz_in_a_checkout_says_dev(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_DEPLOYED", tmp_path / "deployed.json")
    response = TestClient(server.app).get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "commit": "dev"}


def test_healthz_reports_the_deployed_commit(tmp_path, monkeypatch):
    stamp = tmp_path / "deployed.json"
    stamp.write_text(json.dumps({"commit": "abc1234", "branch": "master", "deployedAt": "2026-10-25T00:00:00Z"}))
    monkeypatch.setattr(server, "_DEPLOYED", stamp)
    body = TestClient(server.app).get("/healthz").json()
    assert body["ok"] is True
    assert body["commit"] == "abc1234"
    assert body["branch"] == "master"
