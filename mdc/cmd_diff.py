from __future__ import annotations

import difflib
import os
import re
import subprocess
import sys
from pathlib import Path


def run_files_ls() -> int:
    import anthropic as _anthropic
    from mdc.config import load_config

    cfg = load_config()
    api_key = os.environ.get("ANTHROPIC_API_KEY") or cfg.anthropic_api_key
    client = _anthropic.Anthropic(api_key=api_key)

    files = list(client.beta.files.list(limit=1000))
    if not files:
        print("No files on server.")
        return 0

    def _fmt_size(n: int) -> str:
        if n < 1024:
            return f"{n} B"
        if n < 1024 * 1024:
            return f"{n / 1024:.1f} KB"
        return f"{n / 1024 / 1024:.1f} MB"

    rows = [(f.id, f.created_at.strftime("%Y-%m-%d %H:%M"), _fmt_size(f.size_bytes), f.filename) for f in files]
    id_w = max(len(r[0]) for r in rows)
    dt_w = max(len(r[1]) for r in rows)
    sz_w = max(len(r[2]) for r in rows)
    for file_id, created, size, filename in rows:
        print(f"{file_id:<{id_w}}  {created}  {size:>{sz_w}}  {filename}")
    return 0


def _colorize_diff(text: str) -> str:
    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    RED    = "\033[31m"
    GREEN  = "\033[32m"
    CYAN   = "\033[36m"
    out = []
    for line in text.splitlines(keepends=True):
        if line.startswith("---") or line.startswith("+++"):
            out.append(BOLD + line + RESET)
        elif line.startswith("-"):
            out.append(RED + line + RESET)
        elif line.startswith("+"):
            out.append(GREEN + line + RESET)
        elif line.startswith("@@"):
            out.append(CYAN + line + RESET)
        else:
            out.append(line)
    return "".join(out)


def run_diff(
    path: Path,
    revision: int | None = None,
    delta: int | None = None,
    diff_args: list[str] | None = None,
    revisions_dir: Path | None = None,
) -> int:
    import re as _re
    import subprocess
    import sys

    path = path.resolve()
    if not path.is_file():
        print(f"Error: file not found: {path}")
        return 1

    stem = path.stem
    suffix = path.suffix
    rev_dir = revisions_dir if revisions_dir is not None else path.parent

    backup_re = _re.compile(rf"^{_re.escape(stem)}--(\d+){_re.escape(suffix)}$")
    revisions: list[tuple[int, Path]] = []
    if rev_dir.is_dir():
        for entry in rev_dir.iterdir():
            m = backup_re.match(entry.name)
            if m:
                revisions.append((int(m.group(1)), entry))
    revisions.sort(reverse=True)

    if not revisions:
        print(f"No revisions found for {path.name}. Has 'mdc reply' edited this file yet?")
        return 1

    if revision is not None:
        vpath = rev_dir / f"{stem}--{revision}{suffix}"
        if not vpath.is_file():
            print(f"Error: revision {revision} not found.")
            return 1
        baseline, target = vpath, path
    else:
        # Build change chain: current file followed by revisions highest-first.
        # Find consecutive pairs whose content differs; --delta N selects the Nth.
        chain = [path] + [vpath for _, vpath in revisions]
        _cache: dict[Path, str] = {}

        def _content(p: Path) -> str:
            if p not in _cache:
                _cache[p] = p.read_text(encoding="utf-8")
            return _cache[p]

        pairs: list[tuple[Path, Path]] = []  # (older, newer)
        for i in range(len(chain) - 1):
            if _content(chain[i]) != _content(chain[i + 1]):
                pairs.append((chain[i + 1], chain[i]))

        if not pairs:
            print(f"No changes: {path.name} matches all revisions.")
            return 0

        n = delta if delta is not None else 1
        if n < 1 or n > len(pairs):
            print(f"Error: delta {n} out of range (1–{len(pairs)}).")
            return 1
        baseline, target = pairs[n - 1]

    cmd = ["diff", "-u"] + (diff_args or []) + [str(baseline), str(target)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        output = result.stdout
        if sys.stdout.isatty() and output:
            output = _colorize_diff(output)
        if output:
            sys.stdout.write(output)
        if result.stderr:
            sys.stderr.write(result.stderr)
        return 0 if result.returncode in (0, 1) else result.returncode
    except FileNotFoundError:
        # No system diff (e.g. Windows); fall back to difflib.
        old_lines = baseline.read_text(encoding="utf-8").splitlines(keepends=True)
        new_lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
        output = "".join(
            difflib.unified_diff(old_lines, new_lines, fromfile=str(baseline), tofile=str(target))
        )
        if sys.stdout.isatty() and output:
            output = _colorize_diff(output)
        if output:
            sys.stdout.write(output)
        return 0
