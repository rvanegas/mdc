from pathlib import Path

import pytest

import mdc.config
from mdc.assets import collect_local_assets
from mdc.transcript import TranscriptError, parse_transcript


def _transcript(path: Path, link: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"\n# T\n2027-01-02\n\n## Prompt\n\nRead [it]({link}).\n", encoding="utf-8")
    return path


def _assets(path: Path) -> list[Path]:
    transcript = parse_transcript(path.read_text(encoding="utf-8"), assistant_name="Claude")
    return [a.path for assets in collect_local_assets(transcript, path).values() for a in assets]


@pytest.fixture
def library(tmp_path, monkeypatch):
    lib = tmp_path / "lib"
    (lib / "2026").mkdir(parents=True)
    (lib / "2026" / "2026-10-19-parfit2011.pdf").write_bytes(b"%PDF-1.4 parfit")
    monkeypatch.setattr(mdc.config, "DEFAULT_CONFIG_PATH", tmp_path / "config.toml")
    monkeypatch.setenv("MDC_LIBRARY", str(lib))
    return lib


def test_bare_name_found_in_another_year(library):
    chat = _transcript(library / "2027" / "2027-01-02-t.md", "2026-10-19-parfit2011.pdf")
    assert _assets(chat) == [(library / "2026" / "2026-10-19-parfit2011.pdf").resolve()]


def test_link_with_a_directory_resolves_from_the_transcript(library):
    chat = _transcript(library / "2027" / "2027-01-02-t.md", "../2026/2026-10-19-parfit2011.pdf")
    assert _assets(chat) == [(library / "2026" / "2026-10-19-parfit2011.pdf").resolve()]


def test_nothing_outside_the_library(library, tmp_path):
    (tmp_path / "secret.txt").write_text("no")
    chat = _transcript(library / "2027" / "2027-01-02-t.md", "../../secret.txt")
    with pytest.raises(TranscriptError, match="must stay within"):
        _assets(chat)


def test_ambiguous_name_is_reported(library):
    (library / "2027").mkdir()
    (library / "2025").mkdir()
    (library / "2025" / "dup.txt").write_text("a")
    (library / "2024").mkdir()
    (library / "2024" / "dup.txt").write_text("b")
    chat = _transcript(library / "2027" / "2027-01-02-t.md", "dup.txt")
    with pytest.raises(TranscriptError, match="ambiguous"):
        _assets(chat)


def test_revisions_do_not_count(library):
    (library / "REVISIONS").mkdir()
    (library / "REVISIONS" / "2026-10-19-parfit2011.pdf").write_bytes(b"old")
    chat = _transcript(library / "2027" / "2027-01-02-t.md", "2026-10-19-parfit2011.pdf")
    assert _assets(chat) == [(library / "2026" / "2026-10-19-parfit2011.pdf").resolve()]


def test_outside_a_library_only_its_own_directory(library, tmp_path):
    chat = _transcript(tmp_path / "elsewhere" / "2027-01-02-t.md", "2026-10-19-parfit2011.pdf")
    with pytest.raises(TranscriptError, match="does not exist"):
        _assets(chat)
