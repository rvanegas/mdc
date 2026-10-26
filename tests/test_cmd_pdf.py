import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from mdc import cmd_pdf


@pytest.fixture
def doc(tmp_path: Path) -> Path:
    path = tmp_path / "doc.md"
    path.write_text("\n# Doc\n2026-10-26\n\nText.\n", encoding="utf-8")
    return path


def _run_pdf(doc: Path, platform: str, quiet: bool = False) -> list[list[str]]:
    calls = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with patch.object(cmd_pdf.sys, "platform", platform), patch.object(cmd_pdf.subprocess, "run", side_effect=fake_run):
        assert cmd_pdf.run_pdf(doc, quiet=quiet) == 0
    return calls


def test_server_converts_without_opening(doc):
    calls = _run_pdf(doc, "linux")
    assert [c[0] for c in calls] == ["pandoc"]


def test_mac_opens_the_pdf(doc):
    calls = _run_pdf(doc, "darwin")
    assert calls[-1] == ["open", str(doc.with_suffix(".pdf"))]


def test_quiet_never_opens(doc):
    calls = _run_pdf(doc, "darwin", quiet=True)
    assert [c[0] for c in calls] == ["pandoc"]
