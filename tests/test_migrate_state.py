import importlib.util
import json
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "migrate_state", Path(__file__).resolve().parent.parent / "scripts" / "migrate_state.py"
)
migrate_state = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(migrate_state)

OLD = "/Users/someone/Documents/Writing/Chats"
NEW = "/data/workspaces/someone/files"

CONFIG = """\
anthropic_api_key = "sk-ant-secret"
openai_api_key = "sk-secret"
model = "claude-sonnet-4-6"
library_path = "~/Documents/Writing/Chats"
index_model = "ollama/qwen2.5:14b"
ollama_base_url = "http://localhost:11434/v1"
roxana_api_key = "da2-secret"
dianoia_api_url = "https://dianoia.rvanegas.co/api"
dianoia_token = "secret"
user_names = ["Prompt", "Rodrigo"]
"""


@pytest.fixture
def home(tmp_path: Path) -> Path:
    config = tmp_path / ".config/mdc"
    state = tmp_path / ".local/state/mdc"
    cache = tmp_path / ".cache/mdc"
    for d in (config, state, cache):
        d.mkdir(parents=True)
    (config / "config.toml").write_text(CONFIG)
    (config / "config.toml~").write_text(CONFIG)
    (config / "config.toml.bak-2026-10-25").write_text(CONFIG)
    (config / "system.md").write_text("Be brief.\n")
    (state / "library-manifest.json").write_text(json.dumps({"library_path": OLD, "entries": [{"path": "a.md"}]}))
    (state / "library-index.json").write_text(json.dumps({"library_path": OLD, "terms": ["x"]}))
    (state / "library-relations.json").write_text(json.dumps({"library_path": OLD, "relations": {}}))
    (state / "review-b58728d9.json~").write_text("{}")
    (state / "review-b58728d9.json.bak").write_text("{}")
    (cache / "anthropic-asset-cache.json").write_text(json.dumps({
        f"{OLD}/2026/a.pdf": {"file_id": "file_1"},
        "/elsewhere/b.pdf": {"file_id": "file_2"},
    }))
    return tmp_path


def _run(home: Path) -> None:
    assert migrate_state.main([str(home), "--old-root", OLD, "--new-root", NEW]) == 0


def test_state_points_at_the_new_root(home):
    _run(home)
    state = home / ".local/state/mdc"
    for name in ("library-manifest.json", "library-index.json", "library-relations.json"):
        assert json.loads((state / name).read_text())["library_path"] == NEW
    assert json.loads((state / "library-manifest.json").read_text())["entries"] == [{"path": "a.md"}]
    assert sorted(p.name for p in state.iterdir()) == [
        "library-index.json", "library-manifest.json", "library-relations.json",
    ]


def test_asset_cache_keys_are_rebased(home):
    _run(home)
    cache = json.loads((home / ".cache/mdc/anthropic-asset-cache.json").read_text())
    assert cache == {f"{NEW}/2026/a.pdf": {"file_id": "file_1"}, "/elsewhere/b.pdf": {"file_id": "file_2"}}


def test_config_loses_its_secrets(home):
    _run(home)
    config_dir = home / ".config/mdc"
    assert sorted(p.name for p in config_dir.iterdir()) == ["config.toml", "system.md"]
    text = (config_dir / "config.toml").read_text()
    assert "secret" not in text
    assert "ollama_base_url" not in text
    assert f'library_path = "{NEW}"' in text
    assert 'index_model = "claude-haiku-4-5"' in text
    assert 'dianoia_api_url = "https://dianoia.rvanegas.co/api"' in text
    assert (config_dir / "config.toml").stat().st_mode & 0o777 == 0o600


def test_idempotent(home):
    _run(home)
    first = {p: p.read_text() for p in home.rglob("*") if p.is_file()}
    _run(home)
    assert {p: p.read_text() for p in home.rglob("*") if p.is_file()} == first
