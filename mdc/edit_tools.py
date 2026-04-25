from __future__ import annotations

import difflib
import re
import sys
from collections.abc import Callable
from pathlib import Path

from mdc.transcript import Preamble, TranscriptError


EDIT_TOOL: dict[str, object] = {
    "name": "edit_file",
    "description": (
        "Replace old_str with new_str in a file that was declared for editing. "
        "Apply the change immediately. old_str must match exactly (including whitespace)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path":    {"type": "string", "description": "Relative path as declared in [Edit: ...]"},
            "old_str": {"type": "string", "description": "Exact text to replace"},
            "new_str": {"type": "string", "description": "Replacement text"},
        },
        "required": ["path", "old_str", "new_str"],
    },
}

_EDIT_INSTRUCTIONS = """\
You are acting as a writing assistant with file-editing capabilities.
Call edit_file to apply changes immediately — no confirmation needed.
Use old_str/new_str that are specific enough to be unambiguous in the file.
After editing, briefly describe what you changed (one or two sentences).\
"""

_BACKUP_RE = re.compile(r"^(.+)--(\d+)(\.[^.]+)$")


def resolve_edit_targets(preamble: Preamble) -> list[Path]:
    """Resolve [Edit: path] strings from the preamble against cwd."""
    targets = []
    for raw in preamble.edit_targets:
        p = Path(raw)
        if p.is_absolute():
            raise TranscriptError(f"Edit target must be a relative path: {raw}")
        resolved = p.resolve()
        if not resolved.is_file():
            raise TranscriptError(f"Edit target not found: {raw}")
        targets.append(resolved)
    return targets


def build_edit_context(targets: list[Path], wrap_width: int = 100) -> str:
    """Build the file-content block injected into the system prompt.

    Wraps each target before sending to the model. If wrapping changes the
    content, saves a revision and writes the wrapped version to disk first so
    that subsequent edit diffs are against the already-wrapped text.
    """
    from mdc.cli import wrap_paragraphs
    parts = [_EDIT_INSTRUCTIONS, ""]
    for t in targets:
        raw = t.read_text(encoding="utf-8")
        content = wrap_paragraphs(raw, width=wrap_width)
        if content != raw:
            _write_version(t, content)
        parts.append(f"--- {t.name} ---")
        parts.append(content.rstrip())
        parts.append(f"--- end {t.name} ---")
    return "\n".join(parts)


def _write_version(path: Path, content: str) -> None:
    """Write content to path and to a new numbered revision file.

    The numbered file is only created if path currently matches the latest
    revision (i.e. no manual edits since the last automated write). If path
    has diverged, only the current file is updated.
    """
    stem = path.stem
    suffix = path.suffix
    parent = path.parent

    highest = 0
    latest_revision: Path | None = None
    for sibling in parent.iterdir():
        m = _BACKUP_RE.match(sibling.name)
        if m and m.group(1) == stem and m.group(3) == suffix:
            n = int(m.group(2))
            if n > highest:
                highest = n
                latest_revision = sibling

    current = path.read_text(encoding="utf-8") if path.exists() else None
    latest_content = latest_revision.read_text(encoding="utf-8") if latest_revision else None

    if current is None or latest_content is None or current == latest_content:
        new_revision = parent / f"{stem}--{highest + 1}{suffix}"
        new_revision.write_text(content, encoding="utf-8")

    path.write_text(content, encoding="utf-8")


def _make_diff(old_text: str, new_text: str, name: str) -> str:
    lines = list(
        difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"{name} (before)",
            tofile=f"{name} (after)",
            n=3,
        )
    )
    return "".join(lines) if lines else "(no changes)"


def make_edit_executor(targets: list[Path], wrap_width: int = 100) -> Callable[[str, dict[str, object]], str]:
    """Return a tool_executor that handles edit_file calls for the given targets."""
    allowed = {t.resolve(): t for t in targets}
    by_name = {t.name: t for t in targets}

    def executor(tool_name: str, tool_input: dict[str, object]) -> str:
        if tool_name != "edit_file":
            return f"Unknown tool: {tool_name}"

        raw_path = str(tool_input.get("path", ""))
        old_str = str(tool_input.get("old_str", ""))
        new_str = str(tool_input.get("new_str", ""))

        candidate = Path(raw_path)
        resolved = candidate.resolve()

        target = allowed.get(resolved) or by_name.get(candidate.name)
        if target is None:
            return f"Error: '{raw_path}' is not an editable file in this transcript."

        current = target.read_text(encoding="utf-8")
        if old_str not in current:
            return f"Error: old_str not found in {target.name}. No changes made."

        if new_str and any(len(line) > wrap_width for line in new_str.splitlines()):
            from mdc.cli import wrap_paragraphs
            new_str = wrap_paragraphs(new_str, width=wrap_width)
        new_content = current.replace(old_str, new_str, 1)
        _write_version(target, new_content)
        diff = _make_diff(current, new_content, target.name)
        added = sum(1 for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++"))
        removed = sum(1 for l in diff.splitlines() if l.startswith("-") and not l.startswith("---"))
        sys.stdout.write(f"edit_file: +{added} / -{removed}\n")
        sys.stdout.flush()
        return diff

    return executor
