from pathlib import Path

import pytest

import mdc.config
from mdc.config import load_config


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    monkeypatch.setattr(mdc.config, "DEFAULT_CONFIG_PATH", path)
    for var in ("MDC_LIBRARY", "MDC_USER_NAMES"):
        monkeypatch.delenv(var, raising=False)
    return path


def test_user_names_always_include_prompt(config_file):
    config_file.write_text('user_names = ["Rodrigo"]\n')
    assert load_config().user_names == ("Prompt", "Rodrigo")


def test_user_names_from_the_run_replace_the_config(config_file, monkeypatch):
    config_file.write_text('user_names = ["Prompt", "Rodrigo"]\n')
    monkeypatch.setenv("MDC_USER_NAMES", "Ana, Prompt,Bea")
    assert load_config().user_names == ("Prompt", "Ana", "Bea")


def test_run_library_overrides_config(config_file, tmp_path, monkeypatch):
    configured = tmp_path / "configured"
    chosen = tmp_path / "chosen"
    configured.mkdir()
    chosen.mkdir()
    config_file.write_text(f'library_path = "{configured}"\n')
    assert load_config().library_path == configured.resolve()
    monkeypatch.setenv("MDC_LIBRARY", str(chosen))
    assert load_config().library_path == chosen.resolve()


def test_system_prompt_ends_with_the_librarys(config_file, tmp_path, monkeypatch):
    library = tmp_path / "lib"
    library.mkdir()
    (library / "SYSTEM_PROMPT.md").write_text("Write in continuous prose.\n")
    monkeypatch.setenv("MDC_LIBRARY", str(library))
    prompt = load_config().system_prompt
    assert prompt.startswith("You are continuing a hand-edited markdown chat transcript.")
    assert prompt.endswith("Write in continuous prose.")


def test_no_library_no_library_prompt(config_file):
    config_file.write_text("")
    config = load_config()
    assert config.library_path is None
    assert "Last, First (year)" in config.system_prompt
