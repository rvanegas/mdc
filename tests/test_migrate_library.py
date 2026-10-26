import importlib.util
import json
from pathlib import Path

import pytest

from mdc.library import load_entries, load_relations, load_terms

_SPEC = importlib.util.spec_from_file_location(
    "migrate_library", Path(__file__).resolve().parent.parent / "scripts" / "migrate_library.py"
)
migrate_library = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(migrate_library)

ENTRY = {"rel_path": "a.md", "title": "A", "word_count": 3, "summary": "S.", "terms": ["x"]}


@pytest.fixture
def mac(tmp_path):
    library = tmp_path / "Chats"
    library.mkdir()
    (library / "SYSTEM_PROMPT.md").write_text("Do not assume physicalism.\n")
    state = tmp_path / "state"
    state.mkdir()
    stamp = str(library.resolve())
    (state / "library-manifest.json").write_text(json.dumps({"library_path": stamp, "entries": [ENTRY]}))
    (state / "library-index.json").write_text(json.dumps({"library_path": stamp, "terms": {"x": [{"rel_path": "a.md", "title": "A"}]}}))
    (state / "library-relations.json").write_text(json.dumps({"library_path": stamp, "relations": {"x": ["y"]}}))
    (state / "review-b58728d9.json~").write_text("{}")
    config = tmp_path / "config"
    config.mkdir()
    (config / "system.md").write_text("// a comment\nWrite in continuous prose.\n")
    (config / "system.md~").write_text("old\n")
    (config / "config.toml").write_text('model = "claude-sonnet-5"\nsystem_prompt_file = "~/.config/mdc/system.md"\nuser_names = ["Prompt", "Rodrigo"]\n')
    return library, state, config


def _run(library, state, config):
    assert migrate_library.main([str(library), "--state-dir", str(state), "--config-dir", str(config)]) == 0


def test_state_moves_into_the_library_and_still_loads(mac):
    library, state, config = mac
    _run(*mac)
    assert sorted(p.name for p in (library / ".mdc").iterdir()) == ["index.json", "manifest.json", "relations.json"]
    assert "library_path" not in json.loads((library / ".mdc/manifest.json").read_text())
    lib = library.resolve()
    assert [e.rel_path for e in load_entries(lib)] == ["a.md"]
    assert load_terms(lib) == {"x"}
    assert load_relations(lib) == {"x": ["y"]}
    assert [p.name for p in state.iterdir()] == ["review-b58728d9.json~"]


def test_state_of_another_library_stays(mac, tmp_path):
    library, state, config = mac
    other = tmp_path / "Other"
    other.mkdir()
    migrate_library.main([str(other), "--state-dir", str(state), "--config-dir", str(config)])
    assert (state / "library-manifest.json").exists()
    assert not (other / ".mdc").exists()


def test_system_md_heads_the_library_prompt(mac):
    library, state, config = mac
    _run(*mac)
    assert (library / "SYSTEM_PROMPT.md").read_text() == "Write in continuous prose.\n\nDo not assume physicalism.\n"
    assert sorted(p.name for p in config.iterdir()) == ["config.toml"]
    assert "system_prompt_file" not in (config / "config.toml").read_text()
    assert 'user_names = ["Prompt", "Rodrigo"]' in (config / "config.toml").read_text()


def test_idempotent(mac):
    library, state, config = mac
    _run(*mac)
    first = {p: p.read_text() for p in library.rglob("*") if p.is_file()}
    _run(*mac)
    assert {p: p.read_text() for p in library.rglob("*") if p.is_file()} == first
