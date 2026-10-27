"""The CLI as a client: sync and remote runs against a live in-process server."""

import hashlib
import io
import socket
import threading
import time
from contextlib import redirect_stdout
from pathlib import Path

import pytest
import uvicorn

from mdc import server
from mdc.config import AppConfig
from mdc.remote import CONFLICT_SUFFIX, Client, remote_argv, run_remote, sync

REPO = Path(__file__).resolve().parent.parent
CHAT = "\n# Old Roman\n2025-04-25\n\n## Prompt\n\nHello.\n\n## Claude\n\nHi.\n\n## References\n\n| Ando, Clifford (2000) *Imperial Ideology*\n"
LIB = "ann/philosophy"


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    data = tmp_path_factory.mktemp("data")
    mp = pytest.MonkeyPatch()
    mp.setenv("MDC_DATA_DIR", str(data))
    mp.setenv("MDC_CONFIG_DIR", str(data / "config"))
    mp.setenv("MDC_CACHE_DIR", str(data / "cache"))
    mp.setenv("PYTHONPATH", str(REPO))
    (data / "users.toml").write_text(f'[[tokens]]\nuser = "ann"\nsha256 = "{hashlib.sha256(b"t").hexdigest()}"\n')
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    uv = uvicorn.Server(uvicorn.Config(server.app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=uv.run, daemon=True)
    thread.start()
    while not uv.started:
        time.sleep(0.05)
    yield data, f"http://127.0.0.1:{port}"
    uv.should_exit = True
    thread.join(5)
    mp.undo()


@pytest.fixture
def setup(live, tmp_path):
    data, url = live
    remote_root = data / "libraries" / "ann" / "philosophy"
    import shutil
    shutil.rmtree(remote_root, ignore_errors=True)
    remote_root.mkdir(parents=True)
    local = tmp_path / "Chats"
    local.mkdir()
    config = AppConfig(server_url=url, server_token="t", user_names=("Prompt", "Ann"), mirrors=((LIB, local),))
    return Client(url, "t", LIB), local, remote_root, config


def _quiet(fn, *args):
    out = io.StringIO()
    with redirect_stdout(out):
        result = fn(*args)
    return result, out.getvalue()


def test_first_sync_makes_the_server_follow(setup):
    client, local, remote, _ = setup
    (local / "2025").mkdir()
    (local / "2025" / "a.chat.md").write_text("mine")
    (local / "b.md").write_text("mine b")
    (remote / "b.md").write_text("server b")
    (remote / "c.md").write_text("server only")
    (local / ".mdc").mkdir()
    (local / ".mdc" / "manifest.json").write_text("{}")
    assert _quiet(sync, client, local, LIB)[0] == []
    assert client.list_files() == {
        "2025/a.chat.md": hashlib.sha256(b"mine").hexdigest(),
        "b.md": hashlib.sha256(b"mine b").hexdigest(),
    }
    assert not (remote / ".mdc").exists()


def test_push_and_pull_and_deletions_both_ways(setup):
    client, local, remote, _ = setup
    (local / "x.md").write_text("one")
    (local / "y.md").write_text("why")
    _quiet(sync, client, local, LIB)
    (local / "x.md").write_text("two")          # changed here
    (remote / "y.md").write_text("server why")  # changed there
    (local / "z.md").write_text("new here")
    _quiet(sync, client, local, LIB)
    assert (remote / "x.md").read_text() == "two"
    assert (local / "y.md").read_text() == "server why"
    assert (remote / "z.md").read_text() == "new here"
    (local / "x.md").unlink()                   # deleted here
    (remote / "z.md").unlink()                  # deleted there
    _quiet(sync, client, local, LIB)
    assert not (remote / "x.md").exists()
    assert not (local / "z.md").exists()


def test_a_conflict_keeps_both_and_stops_runs(setup):
    client, local, remote, config = setup
    (local / "x.md").write_text("base")
    _quiet(sync, client, local, LIB)
    (local / "x.md").write_text("mine")
    (remote / "x.md").write_text("theirs")
    conflicts, out = _quiet(sync, client, local, LIB)
    assert conflicts == ["x.md"]
    assert (local / "x.md").read_text() == "mine"
    assert (local / f"x.md{CONFLICT_SUFFIX}").read_text() == "theirs"
    assert (remote / "x.md").read_text() == "theirs"
    code, out = _quiet(run_remote, config, LIB, local, ["index", "--refs-only-all"])
    assert code == 1 and "resolve the conflicts" in out
    # Deleting the server's copy keeps mine.
    (local / f"x.md{CONFLICT_SUFFIX}").unlink()
    assert _quiet(sync, client, local, LIB)[0] == []
    assert (remote / "x.md").read_text() == "mine"


def test_a_run_streams_and_brings_its_changes_back(setup):
    client, local, remote, config = setup
    (local / "2025").mkdir()
    (local / "2025" / "2025-04-25-old-roman.chat.md").write_text(CHAT)
    code, out = _quiet(run_remote, config, LIB, local, ["index", "--refs-only-all"])
    assert code == 0, out
    assert "document(s)" in out or "References" in out or out
    assert (local / "REFERENCES.md").exists()
    assert "Ando" in (local / "REFERENCES.md").read_text()
    assert (local / "REFERENCES.md").read_bytes() == (remote / "REFERENCES.md").read_bytes()


def test_a_local_edit_during_a_run_is_a_conflict_not_lost(setup, monkeypatch):
    client, local, remote, config = setup
    (local / "2025").mkdir()
    (local / "2025" / "2025-04-25-old-roman.chat.md").write_text(CHAT)
    (local / "REFERENCES.md").write_text("old")
    _quiet(sync, client, local, LIB)
    original = Client.run

    def run_while_editing(self, argv, names, on_text):
        done = original(self, argv, names, on_text)
        (local / "REFERENCES.md").write_text("edited during the run")
        return done

    monkeypatch.setattr(Client, "run", run_while_editing)
    _quiet(run_remote, config, LIB, local, ["index", "--refs-only-all"])
    assert (local / "REFERENCES.md").read_text() == "edited during the run"
    assert "Ando" in (local / f"REFERENCES.md{CONFLICT_SUFFIX}").read_text()


def test_remote_argv_names_files_and_drops_lib(tmp_path):
    (tmp_path / "2026").mkdir()
    (tmp_path / "2026" / "x.chat.md").write_text("")
    assert remote_argv(["--lib", "/l", "reply", "-l", "2026/x.chat.md"], tmp_path) == ["reply", "-l", "x.chat.md"]
    assert remote_argv(["reply", "eleatic", "--lib=/l"], tmp_path) == ["reply", "eleatic"]
