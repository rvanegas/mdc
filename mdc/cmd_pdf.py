from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from mdc.review import sanitize_for_pandoc


def run_pdf(path: Path, quiet: bool = False) -> int:
    if not path.exists():
        print(f"Error: '{path}' does not exist.")
        return 1
    if path.suffix.lower() != ".md":
        print(f"Error: '{path}' does not have a .md extension.")
        return 1

    import tempfile
    output = path.with_suffix(".pdf")
    sanitized = sanitize_for_pandoc(path.read_text(encoding="utf-8"))
    with tempfile.NamedTemporaryFile(suffix=".md", mode="w", encoding="utf-8", delete=False) as tmp:
        tmp.write(sanitized)
        tmp_path = Path(tmp.name)
    base_cmd = ["pandoc", str(tmp_path), "-o", str(output),
                "-V", "geometry:margin=1in", "-V", "fontsize=11pt"]
    for engine in ("xelatex", None):
        cmd = base_cmd + ([f"--pdf-engine={engine}"] if engine else [])
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            break
        if engine is None:
            tmp_path.unlink(missing_ok=True)
            print(f"pandoc error:\n{result.stderr}", file=sys.stderr)
            return result.returncode
    tmp_path.unlink(missing_ok=True)
    if not quiet:
        if shutil.which("open"):
            subprocess.run(["open", str(output)])
        elif shutil.which("start"):
            subprocess.run(["start", str(output)], shell=True)
    return 0
