from __future__ import annotations

import difflib
import re
import sys
from collections.abc import Callable
from pathlib import Path

from mdc.transcript import TranscriptError

EDIT_TOOL: dict[str, object] = {
    "name": "edit_file",
    "description": (
        "Replace old_str with new_str in a companion file. "
        "Apply the change immediately. old_str must match exactly (including whitespace)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Filename of the companion file. An argument file's only "
                    "content is its ## Argument proposition list."
                ),
            },
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

_ARGUMENT_FORMAT = """\
## Argument file format

The user writes the propositions. Your role is to help keep them correctly \
structured and consistently numbered — not to supply logical content \
independently.

The file contains exactly one section:

**## Argument**  (one line per proposition; later propositions may cite \
earlier ones as justifiers)
- 1: premise text
- 2 (from: 1): proposition text, justified by proposition 1
- 3 (from: 1, 2): conclusion text, justified by propositions 1 and 2

Nothing else belongs in this file — no definitions, no formalizations, no \
evaluation content. Analysis is generated separately by `mdc analyze` into \
a companion `.analysis.md` file, which you never edit.

A proposition with justifiers is an "argument" in the sense used by `mdc \
analyze`; a bare proposition with no justifiers is just a premise.

**Proposition numbering rules**
- Never renumber existing propositions. Their numbers are stable identifiers \
referenced by justifier lists and external notes.
- When adding a new proposition, assign the next integer after the current \
highest number in the file, regardless of where in the argument it appears.
- Proposition numbers must form a contiguous sequence starting at 1, with no \
gaps.
- Proposition numbers are plain integers only. No subscripts, primes, \
apostrophes, asterisks, or other decorations (e.g. use `4`, not `3a`, `3'`, \
or `3*`).\
"""

_TRIAD_NOTE = """\
The document file contains the prose argument; the argument file captures its \
logical structure. These files are interdependent: edits to one should remain \
consistent with the other. When editing the argument file, treat the document \
as authoritative context for the intended meaning of each proposition. When \
editing the document, treat the argument structure as a constraint on logical \
coherence.\
"""

_BACKUP_RE = re.compile(r"^(.+)--(\d+)(\.[^.]+)$")


def create_document_file(path: Path, title: str, date: str) -> None:
    """Write a fresh .document.md containing only the preamble (title + date)."""
    path.write_text(f"\n# {title}\n{date}\n\n", encoding="utf-8")


def resolve_edit_targets(chat_path: Path) -> list[Path]:
    """Return companion .document.md and .argument.md files for a .chat.md transcript.

    Both files are optional; only those that exist on disk are returned.
    Returns an empty list for non-.chat.md files.
    """
    if not chat_path.name.endswith(".chat.md"):
        return []
    stem = chat_path.name[: -len(".chat.md")]
    targets = []
    for suffix in ("document", "argument"):
        p = chat_path.parent / f"{stem}.{suffix}.md"
        if p.is_file():
            targets.append(p)
    return targets


def build_edit_context(targets: list[Path], wrap_width: int = 100, revisions_dir: Path | None = None) -> str:
    """Build the file-content block injected into the system prompt.

    Wraps each target before sending to the model. If wrapping changes the
    content, saves a revision and writes the wrapped version to disk first so
    that subsequent edit diffs are against the already-wrapped text.
    """
    from mdc.cli import wrap_paragraphs

    has_argument = any(t.name.endswith(".argument.md") for t in targets)
    has_document = any(t.name.endswith(".document.md") for t in targets)

    parts: list[str] = []
    if has_argument and has_document:
        parts += [_TRIAD_NOTE, ""]
    parts += [_EDIT_INSTRUCTIONS, ""]
    if has_argument:
        parts += [_ARGUMENT_FORMAT, ""]

    for t in targets:
        raw = t.read_text(encoding="utf-8")
        content = wrap_paragraphs(raw, width=wrap_width)
        if content != raw:
            _write_version(t, content, revisions_dir=revisions_dir)
        parts.append(f"--- {t.name} ---")
        parts.append(content.rstrip())
        parts.append(f"--- end {t.name} ---")
    return "\n".join(parts)


def _write_version(path: Path, content: str, revisions_dir: Path | None = None) -> None:
    """Write content to path and to a new numbered revision file.

    The numbered file is only created if path currently matches the latest
    revision (i.e. no manual edits since the last automated write). If path
    has diverged, only the current file is updated.
    """
    stem = path.stem
    suffix = path.suffix
    rev_dir = revisions_dir if revisions_dir is not None else path.parent

    highest = 0
    latest_revision: Path | None = None
    if rev_dir.is_dir():
        for entry in rev_dir.iterdir():
            m = _BACKUP_RE.match(entry.name)
            if m and m.group(1) == stem and m.group(3) == suffix:
                n = int(m.group(2))
                if n > highest:
                    highest = n
                    latest_revision = entry

    current = path.read_text(encoding="utf-8") if path.exists() else None
    latest_content = latest_revision.read_text(encoding="utf-8") if latest_revision else None

    if current is None or latest_content is None or current == latest_content:
        rev_dir.mkdir(parents=True, exist_ok=True)
        (rev_dir / f"{stem}--{highest + 1}{suffix}").write_text(content, encoding="utf-8")

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


def make_edit_executor(targets: list[Path], wrap_width: int = 100, revisions_dir: Path | None = None) -> Callable[[str, dict[str, object]], str]:
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

        if target.name.endswith(".argument.md"):
            from mdc.argue import validate_proposition_numbering
            err = validate_proposition_numbering(current, new_content)
            if err:
                return f"Error: {err} No changes made."

        _write_version(target, new_content, revisions_dir=revisions_dir)
        diff = _make_diff(current, new_content, target.name)
        added = sum(1 for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++"))
        removed = sum(1 for l in diff.splitlines() if l.startswith("-") and not l.startswith("---"))
        sys.stdout.write(f"edit_file: +{added} / -{removed}\n")
        sys.stdout.flush()
        return diff

    return executor
