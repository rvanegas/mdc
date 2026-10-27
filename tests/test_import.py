import json
import shutil
import zipfile
from pathlib import Path

import pytest

import mdc.config
from mdc.cli import main
from mdc.form import check_file

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def library(tmp_path, monkeypatch):
    lib = tmp_path / "lib"
    lib.mkdir()
    monkeypatch.setattr(mdc.config, "DEFAULT_CONFIG_PATH", tmp_path / "config.toml")
    monkeypatch.setenv("MDC_LIBRARY", str(lib))
    monkeypatch.delenv("MDC_SERVER_URL", raising=False)
    monkeypatch.chdir(lib)
    return lib


def test_claude_export_becomes_exact_chats(library, capsys):
    assert main(["import", str(FIXTURES / "claude_export.json")]) == 0
    chat = library / "2026" / "2026-09-14-being-and-seeming.chat.md"
    text = chat.read_text()
    assert text.startswith("\n# Being and Seeming\n2026-09-14\n\n## Prompt\n\nIs seeming a kind of being?")
    assert "| [attachment: notes.txt]" in text
    assert "\n### Two readings" in text and "\n## Two readings" not in text   # promoted, as reply does
    assert "hidden" not in text                                          # thinking is not a turn
    assert "Take the phenomenal reading." in text
    assert "edited away" not in text                                      # the active branch only
    assert "| [tool: web_search]" in text and "Meinong" in text
    assert check_file(chat) == []
    untitled = library / "2026" / "2026-10-01-untitled.chat.md"
    assert "A quick answer." in untitled.read_text()
    out = capsys.readouterr().out
    assert "2 imported, 0 skipped, 0 failed." in out                      # the empty one is nothing


def test_saveai_export_becomes_a_chat(library):
    src = library.parent / "2026-08-03-verbal-disputes.md"
    shutil.copy(FIXTURES / "saveai_export.md", src)
    assert main(["import", str(src)]) == 0
    chat = library / "2026" / "2026-08-03-verbal-disputes.chat.md"
    text = chat.read_text()
    assert "## Prompt\n\nWhat is a verbal dispute?" in text
    assert "### Short answer" in text
    assert "message time" not in text and "From:" not in text
    assert check_file(chat) == []


def test_anything_else_is_a_document(library):
    src = library.parent / "notes-on-quine.md"
    src.write_text("# Notes on Quine\n\nTwo dogmas, one paragraph.\n")
    assert main(["import", str(src)]) == 0
    docs = list((library).rglob("*-notes-on-quine.document.md"))
    assert len(docs) == 1
    assert docs[0].read_text().startswith("\n# Notes on Quine\n")
    assert "Two dogmas, one paragraph." in docs[0].read_text()


def test_zip_only_skip_and_replace(library, capsys):
    archive = library.parent / "data-2026-10-27.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.write(FIXTURES / "claude_export.json", "data/conversations.json")
    assert main(["import", str(archive), "--only", "Being and Seeming"]) == 0
    assert [p.name for p in (library / "2026").iterdir()] == ["2026-09-14-being-and-seeming.chat.md"]
    chat = library / "2026" / "2026-09-14-being-and-seeming.chat.md"
    chat.write_text(chat.read_text() + "\n## Prompt\n\nMy own addition.\n")
    capsys.readouterr()
    main(["import", str(archive), "--only", "c-1"])
    assert "skipped" in capsys.readouterr().out
    assert "My own addition." in chat.read_text()
    main(["import", str(archive), "--only", "c-1", "--replace"])
    assert "My own addition." not in chat.read_text()


def test_a_file_that_fails_its_check_is_not_written(library, monkeypatch, capsys):
    import mdc.cmd_import as imp
    monkeypatch.setattr(imp, "_problems", lambda path, role: ["broken"])
    existing = library / "2026" / "2026-09-14-being-and-seeming.chat.md"
    existing.parent.mkdir()
    existing.write_text("keep me")
    main(["import", str(FIXTURES / "claude_export.json"), "--only", "c-1", "--replace"])
    assert existing.read_text() == "keep me"
    assert "failed" in capsys.readouterr().out
