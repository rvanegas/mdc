import json
from pathlib import Path
from unittest.mock import patch

import pytest

import mdc.config
from mdc.cli import main
from mdc.library import load_entries, lookup_term, misplaced, move_documents

DOC = "\n# Old Roman\n2025-04-25\n\n## Prompt\n\nHello.\n\n## Claude\n\nHi.\n"


@pytest.fixture
def library(tmp_path, monkeypatch):
    lib = tmp_path / "lib"
    lib.mkdir()
    monkeypatch.setattr(mdc.config, "DEFAULT_CONFIG_PATH", tmp_path / "config.toml")
    monkeypatch.setenv("MDC_LIBRARY", str(lib))
    return lib


def test_misplaced(library):
    assert misplaced(library, library / "2025" / "_private" / "2025-04-25-x.md") == library.resolve() / "2025" / "2025-04-25-x.md"
    assert misplaced(library, library / "2025-04-25-x.md") == library.resolve() / "2025" / "2025-04-25-x.md"
    assert misplaced(library, library / "2025" / "2025-04-25-x.md") is None
    assert misplaced(library, library / "KEYS.md") is None
    assert misplaced(library, library / "REVISIONS" / "2025-04-25-x--1.md") is None
    assert misplaced(library, library / "ASSESSMENTS" / "ASSESSMENT-2026-06-17.md") is None


def test_move_carries_index_state(library):
    old = library / "2025" / "mmaa" / "2025-04-25-x.md"
    old.parent.mkdir(parents=True)
    old.write_text(DOC)
    state = library / ".mdc"
    state.mkdir()
    entry = {"rel_path": "2025/mmaa/2025-04-25-x.md", "title": "Old Roman", "word_count": 2, "summary": "S.", "terms": ["Rome"]}
    (state / "manifest.json").write_text(json.dumps({"entries": [entry]}))
    (state / "index.json").write_text(json.dumps({"terms": {"Rome": [{"rel_path": entry["rel_path"], "title": "Old Roman"}]}}))
    new = library / "2025" / "2025-04-25-x.md"
    move_documents(library, {old: new})
    assert new.exists() and not old.exists()
    assert [e.rel_path for e in load_entries(library.resolve())] == ["2025/2025-04-25-x.md"]
    assert "2025/2025-04-25-x.md" in lookup_term(library.resolve(), "Rome")


def test_check_reports_a_misplaced_file(library, monkeypatch, capsys):
    path = library / "2025" / "_private" / "2025-04-25-x.chat.md"
    path.parent.mkdir(parents=True)
    path.write_text(DOC)
    monkeypatch.chdir(path.parent)
    assert main(["check", path.name]) == 1
    assert "belongs in 2025/" in capsys.readouterr().out


def test_fix_moves_the_file_and_its_companions(library, monkeypatch):
    here = library / "2025" / "_private"
    here.mkdir(parents=True)
    (here / "2025-04-25-x.chat.md").write_text(DOC)
    (here / "2025-04-25-x.document.md").write_text("\n# Old Roman\n2025-04-25\n\nText.\n")
    (here / "2025-04-25-x.pdf").write_bytes(b"%PDF")
    (here / "2025-04-25-other.md").write_text(DOC)
    monkeypatch.chdir(here)
    with patch("builtins.input", return_value="y"):
        main(["fix", "2025-04-25-x.chat.md"])
    assert sorted(p.name for p in (library / "2025").iterdir() if p.is_file()) == [
        "2025-04-25-x.chat.md", "2025-04-25-x.document.md", "2025-04-25-x.pdf",
    ]
    assert [p.name for p in here.iterdir()] == ["2025-04-25-other.md"]


def test_validate_runs_inside_a_library(library, monkeypatch, capsys):
    path = library / "2025" / "2025-04-25-old-roman.chat.md"
    path.parent.mkdir(parents=True)
    path.write_text(DOC)
    monkeypatch.chdir(path.parent)
    assert main(["validate", path.name]) == 0
    assert "Error" not in capsys.readouterr().out


def test_role_of():
    from mdc.library import lacks_role, role_of
    assert role_of("2026-10-19-parfit2011.source.pdf") == "source"
    assert role_of("2026-06-29-the-eleatic-critique.chat.md") == "chat"
    assert role_of("2026-06-29-the-eleatic-critique.chat.pdf") == "chat"
    assert role_of("2026-06-29-the-eleatic-critique.md") is None
    assert lacks_role("2026-06-29-the-eleatic-critique.md")
    assert not lacks_role("KEYS.md")
    assert not lacks_role("2026-06-29-x.review.md")


def test_check_wants_a_role(library, monkeypatch, capsys):
    path = library / "2025" / "2025-04-25-old-roman.md"
    path.parent.mkdir(parents=True)
    path.write_text(DOC)
    monkeypatch.chdir(path.parent)
    assert main(["check", path.name]) == 1
    assert "does not name its role" in capsys.readouterr().out


def test_index_refuses_a_library_with_unnamed_roles(library, capsys):
    (library / "2025").mkdir()
    (library / "2025" / "2025-04-25-old-roman.md").write_text(DOC)
    assert main(["index", "--refs-only-all"]) == 1
    assert "without a role" in capsys.readouterr().out


def test_voice_by_role():
    from mdc.cmd_index import _annotate_voice
    assert "user's own writing" in _annotate_voice("text", "document", ("Prompt",), ("Claude",))
    assert "third-party" in _annotate_voice("text", "source", ("Prompt",), ("Claude",))
    chat = _annotate_voice("\n# T\n\n## Prompt\n\nQ\n\n## Claude\n\nA\n", "chat", ("Prompt",), ("Claude",))
    assert "[VOICE: user]" in chat and "[VOICE: llm" in chat
