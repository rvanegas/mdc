import hashlib
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mdc import server

REPO = Path(__file__).resolve().parent.parent
CHAT = "\n# Old Roman\n2025-04-25\n\n## Prompt\n\nHello.\n\n## Claude\n\nHi.\n"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("MDC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MDC_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("MDC_CACHE_DIR", str(tmp_path / "cache"))
    # The run's subprocess imports this checkout's mdc.
    monkeypatch.setenv("PYTHONPATH", str(REPO))
    (tmp_path / "users.toml").write_text(
        f'[[tokens]]\nuser = "ann"\nsha256 = "{_sha(b"ann-token")}"\n'
        f'[[tokens]]\nuser = "bob"\nsha256 = "{_sha(b"bob-token")}"\n'
        f'[[tokens]]\nuser = "*"\nsha256 = "{_sha(b"service-token")}"\n'
    )
    lib = tmp_path / "libraries" / "ann" / "philosophy"
    (lib / "2025").mkdir(parents=True)
    (lib / "2025" / "2025-04-25-old-roman.chat.md").write_text(CHAT)
    (lib / ".mdc").mkdir()
    (lib / ".mdc" / "manifest.json").write_text("{}")
    (tmp_path / "libraries" / "bob" / "notes").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def client(data):
    return TestClient(server.app)


ANN = {"Authorization": "Bearer ann-token"}
BASE = "/libraries/ann/philosophy"


def _events(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        events.append((lines["event"], json.loads(lines["data"])))
    return events


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
    assert body["commit"] == "abc1234"


def test_a_token_is_required(client):
    assert client.get("/libraries").status_code == 401
    assert client.get("/libraries", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get(f"{BASE}/files").status_code == 401


def test_each_user_sees_their_own_libraries(client):
    assert client.get("/libraries", headers=ANN).json() == {"libraries": ["ann/philosophy"]}
    service = {"Authorization": "Bearer service-token"}
    assert client.get("/libraries", headers=service).json() == {"libraries": ["ann/philosophy", "bob/notes"]}
    assert client.get(f"{BASE}/files", headers={"Authorization": "Bearer bob-token"}).status_code == 403
    assert client.get(f"{BASE}/files", headers=service).status_code == 200


def test_listing_hides_dotfiles(client):
    files = client.get(f"{BASE}/files", headers=ANN).json()["files"]
    assert files == {"2025/2025-04-25-old-roman.chat.md": _sha(CHAT.encode())}


@pytest.mark.parametrize("rel", ["../../bob/notes/x.md", ".mdc/manifest.json", "2025/.hidden", "%2e%2e/x"])
def test_paths_stay_inside_and_visible(client, rel):
    assert client.get(f"{BASE}/files/{rel}", headers=ANN).status_code in (400, 404)
    assert client.put(f"{BASE}/files/{rel}", headers={**ANN, "If-None-Match": "*"}, content=b"x").status_code in (400, 404)


def test_symlinks_are_refused(client, data):
    (data / "libraries/ann/philosophy/link").symlink_to(data / "users.toml")
    assert client.get(f"{BASE}/files/link", headers=ANN).status_code == 400


def test_read_write_delete_with_preconditions(client):
    rel = f"{BASE}/files/2026/2026-01-02-new.document.md"
    assert client.put(rel, headers=ANN, content=b"one").status_code == 428
    created = client.put(rel, headers={**ANN, "If-None-Match": "*"}, content=b"one")
    assert created.status_code == 200 and created.headers["ETag"] == f'"{_sha(b"one")}"'
    assert client.put(rel, headers={**ANN, "If-None-Match": "*"}, content=b"again").status_code == 412
    got = client.get(rel, headers=ANN)
    assert got.content == b"one" and got.headers["ETag"] == f'"{_sha(b"one")}"'
    assert client.put(rel, headers={**ANN, "If-Match": '"stale"'}, content=b"two").status_code == 412
    assert client.put(rel, headers={**ANN, "If-Match": f'"{_sha(b"one")}"'}, content=b"two").status_code == 200
    assert client.delete(rel, headers=ANN).status_code == 428
    assert client.delete(rel, headers={**ANN, "If-Match": f'"{_sha(b"one")}"'}).status_code == 412
    assert client.delete(rel, headers={**ANN, "If-Match": f'"{_sha(b"two")}"'}).status_code == 204
    assert client.get(rel, headers=ANN).status_code == 404


def test_run_streams_and_reports_changes(client, data):
    response = client.post(f"{BASE}/run", headers=ANN, json={"argv": ["new", "Fresh", "Start"], "user_names": ["Ann"]})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _events(response.text)
    assert events[-1][0] == "done"
    done = events[-1][1]
    assert done["exit_code"] == 0
    assert [rel.split("/")[-1].split("-", 3)[-1] for rel in done["changed"]] == ["fresh-start.chat.md"]
    assert "fresh-start.chat.md" in "".join(e[1]["text"] for e in events if e[0] == "output")


def test_run_resolves_names_across_the_library(client):
    events = _events(client.post(f"{BASE}/run", headers=ANN, json={"argv": ["check", "old-roman"]}).text)
    assert events[-1][1]["exit_code"] == 0
    assert "OK: transcript is valid" in "".join(e[1]["text"] for e in events if e[0] == "output")


@pytest.mark.parametrize("argv", [["edit", "x"], ["config"], ["serve"], ["--lib", "/", "check", "x"],
                                  ["check", "--lib=/", "x"], ["reply", "-W", "x"], []])
def test_run_refuses(client, argv):
    assert client.post(f"{BASE}/run", headers=ANN, json={"argv": argv}).status_code in (400, 422)


def test_one_run_at_a_time_and_no_writes_meanwhile(client, data):
    import asyncio
    from mdc import service
    lock = service.lock_for(data / "libraries/ann/philosophy")
    asyncio.run(lock.acquire())
    try:
        assert client.post(f"{BASE}/run", headers=ANN, json={"argv": ["check", "old-roman"]}).status_code == 409
        assert client.put(f"{BASE}/files/2026/x.document.md", headers={**ANN, "If-None-Match": "*"}, content=b"x").status_code == 409
    finally:
        lock.release()
        service.locks.clear()


def test_add_token(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("add_token", REPO / "scripts" / "add_token.py")
    add_token = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(add_token)
    users = tmp_path / "users.toml"
    import contextlib, io
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        add_token.main(["ann", "--note", "Mac CLI", "--users", str(users)])
    token = out.getvalue().strip()
    import tomllib
    entry = tomllib.loads(users.read_text())["tokens"][0]
    assert entry == {"user": "ann", "sha256": _sha(token.encode()), "note": "Mac CLI"}
    assert users.stat().st_mode & 0o777 == 0o600
