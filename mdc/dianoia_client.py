"""Subprocess client for the dianoia CLI."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path


def _dianoia_exe() -> str:
    exe = shutil.which("dianoia")
    if exe is None:
        raise FileNotFoundError(
            "dianoia not found on PATH. Install with: pip install -e ~/src/dianoia"
        )
    return exe


def _run(args: list[str], input_path: Path) -> dict:
    result = subprocess.run(
        [_dianoia_exe()] + args + [str(input_path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"dianoia {args[0]} failed:\n{result.stderr.strip()}"
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"dianoia {args[0]} returned invalid JSON: {e}") from e


def extract(text: str, max_props: int | None = None) -> dict:
    """Call `dianoia extract` on text, return Arguments dict."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", encoding="utf-8", delete=False
    ) as f:
        f.write(text)
        tmp = Path(f.name)
    cmd = ["extract"]
    if max_props is not None:
        cmd += ["-m", str(max_props)]
    try:
        return _run(cmd, tmp)
    finally:
        tmp.unlink(missing_ok=True)


def audit(args: dict) -> dict:
    """Call `dianoia audit` on an Arguments dict, return an AuditResult dict."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", encoding="utf-8", delete=False
    ) as f:
        json.dump(args, f)
        tmp = Path(f.name)
    try:
        return _run(["audit"], tmp)
    finally:
        tmp.unlink(missing_ok=True)


def evaluate(args: dict, step: str | None = None) -> dict:
    """Call `dianoia evaluate` on an Arguments dict, return results dict.

    Progress messages from the agent run are forwarded to stderr in real time.
    """
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", encoding="utf-8", delete=False
    ) as f:
        json.dump(args, f)
        tmp = Path(f.name)
    try:
        exe = _dianoia_exe()
        step_args = ["--step", step] if step is not None else []
        proc = subprocess.Popen(
            [exe, "evaluate"] + step_args + [str(tmp)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stdout, stderr = proc.communicate()
        # Forward dianoia's progress output (stderr) to our stderr in real time
        import sys
        if stderr:
            print(stderr, end="", file=sys.stderr)
        if proc.returncode != 0:
            raise RuntimeError(f"dianoia evaluate failed:\n{stderr.strip()}")
        try:
            return json.loads(stdout)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"dianoia evaluate returned invalid JSON: {e}") from e
    finally:
        tmp.unlink(missing_ok=True)
