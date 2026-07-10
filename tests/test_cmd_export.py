import json
from pathlib import Path
from unittest.mock import patch

import pytest

from mdc.cmd_export import _argument_contents, _layout_entry, run_export
from mdc.roxana_client import _ID_ALPHABET, generate_discussion_id


ARGUMENT_TEXT = """
# Sample Argument
2026-07-08

## Argument
- 1: All humans are mortal.
- 2: Socrates is a human.
- 3 (from: 1, 2): Socrates is mortal.
- 4 (from: 3): Socrates will die.
"""


@pytest.fixture
def argument_file(tmp_path: Path) -> Path:
    companion = tmp_path / "2026-07-08-sample-argument.argument.md"
    companion.write_text(ARGUMENT_TEXT, encoding="utf-8")
    return companion


def test_argument_contents_order():
    argument = [
        {"symbol": "4", "proposition": "d", "justifiers": ["3"]},
        {"symbol": "1", "proposition": "a", "justifiers": []},
        {"symbol": "3", "proposition": "c", "justifiers": ["1", "2"]},
        {"symbol": "2", "proposition": "b", "justifiers": []},
    ]
    assert _argument_contents(argument) == ["1 2 3", "3 4"]


def test_layout_entry_shape():
    entry = _layout_entry(2, "abc-123")
    assert entry == {
        "index": 2,
        "id": "abc-123",
        "status": "committed",
        "accepted": [],
        "rejected": [],
        "cleared": [],
        "goal": [],
        "hidden": False,
    }


def test_generate_discussion_id():
    for _ in range(50):
        discussion_id = generate_discussion_id()
        assert len(discussion_id) == 4
        assert all(c in _ID_ALPHABET for c in discussion_id)


def test_dry_run_makes_no_network_calls(argument_file, capsys):
    with patch("mdc.roxana_client._graphql") as graphql:
        assert run_export(argument_file, dry_run=True) == 0
        graphql.assert_not_called()
    out = capsys.readouterr().out
    assert "Socrates is mortal." in out
    assert "1 2 3" in out
    assert "Dry run" in out


def test_missing_companion(tmp_path, capsys):
    doc = tmp_path / "2026-07-08-sample.document.md"
    doc.write_text("\n# Sample\n2026-07-08\n\nText.\n", encoding="utf-8")
    assert run_export(doc) == 1
    assert "mdc argue" in capsys.readouterr().out


def test_bad_extension(tmp_path, capsys):
    path = tmp_path / "notes.txt"
    assert run_export(path) == 1
    assert ".md extension" in capsys.readouterr().out


class FakeRoxana:
    def __init__(self, fail_on_call: int | None = None):
        self.calls: list[tuple] = []
        self.sentence_count = 0
        self.fail_on_call = fail_on_call

    def generate_discussion_id(self):
        return "k2rw"

    def discussion_exists(self, url, api_key, discussion_id):
        self.calls.append(("exists", discussion_id))
        return False

    def create_sentence(self, url, api_key, content, discussion_id):
        self.sentence_count += 1
        if self.fail_on_call == self.sentence_count:
            raise RuntimeError("boom")
        sentence_id = f"s{self.sentence_count}"
        self.calls.append(("sentence", content, discussion_id, sentence_id))
        return sentence_id

    def create_discussion(self, url, api_key, discussion_id, layout, analysis_results=None):
        self.calls.append(("discussion", discussion_id, layout))

    def delete_sentence(self, url, api_key, sentence_id):
        self.calls.append(("delete", sentence_id))


@pytest.fixture
def configured():
    with patch("mdc.config.load_config") as load_config:
        config = load_config.return_value
        config.roxana_api_url = "https://example/graphql"
        config.roxana_api_key = "da2-test"
        config.roxana_web_url = "https://roxana.example"
        yield config


def _run_with(fake: FakeRoxana, path: Path) -> int:
    with patch.multiple(
        "mdc.roxana_client",
        generate_discussion_id=fake.generate_discussion_id,
        discussion_exists=fake.discussion_exists,
        create_sentence=fake.create_sentence,
        create_discussion=fake.create_discussion,
        delete_sentence=fake.delete_sentence,
    ):
        return run_export(path)


def test_export_flow(argument_file, configured, capsys):
    fake = FakeRoxana()
    assert _run_with(fake, argument_file) == 0

    kinds = [c[0] for c in fake.calls]
    assert kinds == ["exists"] + ["sentence"] * 6 + ["discussion"]

    sentence_contents = [c[1] for c in fake.calls if c[0] == "sentence"]
    assert sentence_contents == [
        "All humans are mortal.",
        "Socrates is a human.",
        "Socrates is mortal.",
        "Socrates will die.",
        "1 2 3",
        "3 4",
    ]

    layout = json.loads(fake.calls[-1][2])
    assert [e["index"] for e in layout["propositions"]] == [1, 2, 3, 4]
    assert [e["id"] for e in layout["propositions"]] == ["s1", "s2", "s3", "s4"]
    assert [e["id"] for e in layout["arguments"]] == ["s5", "s6"]

    out = capsys.readouterr().out
    assert "4 propositions, 2 arguments" in out
    assert "https://roxana.example/discussions/k2rw" in out


def test_cleanup_on_midflight_failure(argument_file, configured, capsys):
    fake = FakeRoxana(fail_on_call=3)
    assert _run_with(fake, argument_file) == 1

    deleted = [c[1] for c in fake.calls if c[0] == "delete"]
    assert deleted == ["s1", "s2"]
    assert not any(c[0] == "discussion" for c in fake.calls)
    assert "boom" in capsys.readouterr().err


def test_missing_config(argument_file, configured, capsys):
    configured.roxana_api_url = None
    with patch("mdc.roxana_client._graphql") as graphql:
        assert run_export(argument_file) == 1
        graphql.assert_not_called()
    assert "roxana_api_url" in capsys.readouterr().out
