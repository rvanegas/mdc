from __future__ import annotations

import argparse
import datetime
import difflib
import os
import re
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path

# Pricing in USD per million tokens: {model_prefix: (input, output)}.
# Cache creation tokens cost ~25% more than input; cache read tokens ~10%.
_ANTHROPIC_PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-4":     (15.00, 75.00),
    "claude-sonnet-4":   ( 3.00, 15.00),
    "claude-haiku-4":    ( 0.80,  4.00),
    "claude-3-opus":     (15.00, 75.00),
    "claude-3-5-sonnet": ( 3.00, 15.00),
    "claude-3-5-haiku":  ( 0.80,  4.00),
    "claude-3-sonnet":   ( 3.00, 15.00),
    "claude-3-haiku":    ( 0.25,  1.25),
}

_OPENAI_PRICING: dict[str, tuple[float, float]] = {
    "gpt-4o-mini": (0.15,  0.60),
    "gpt-4o":      (2.50, 10.00),
    "o3":          (10.0, 40.00),
    "o4-mini":     (1.10,  4.40),
}

class _LibraryTermNotFoundError(Exception):
    def __init__(self, terms: list[str]) -> None:
        self.terms = terms

from mdc.assets import build_anthropic_input, build_chat_input, build_response_input, collect_local_assets
from mdc.review import sanitize_for_pandoc
from mdc.config import _default_assistant_name, load_config
from mdc.form import check_file, check_global_issues, fix_object_replacement, fix_rtl_spans, fix_section_spacing, fix_title_section, slugify
from mdc.transcript import (
    TranscriptError,
    append_assistant_reply,
    extract_references,
    extract_related,
    insert_references,
    parse_transcript,
    update_references_section,
    update_related_section,
)


def _read_file(path: Path) -> str:
    """Read a file as UTF-8, converting from latin-1 in place if needed."""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        text = path.read_text(encoding="latin-1")
        path.with_suffix(path.suffix + ".bak").write_bytes(path.read_bytes())
        path.write_text(text, encoding="utf-8")
        print(f"Warning: {path.name} was not UTF-8; converted in place (backup: {path.name}.bak).", file=sys.stderr)
        return text


def _require_md(path: Path) -> int:
    """Return 1 and print an error if path doesn't have a .md suffix, else 0."""
    if path.suffix.lower() != ".md":
        print(f"Error: '{path}' does not have a .md extension.")
        return 1
    return 0


def _require_bare(s: str) -> int:
    """Return 1 and print an error if s contains a directory separator, else 0."""
    if "/" in s:
        print(f"Error: '{s}' — pass a bare filename, not a path (mdc works in the current directory)")
        return 1
    return 0


_DATED_SLUG_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-.+\.md$")


def _primary(companion: Path) -> Path:
    """Return the primary document path for a companion file (*.chat.md, *.document.md, *.argument.md)."""
    return companion.with_suffix("").with_suffix(".md")


def _resolve_path_abbrev(s: str, cwd: Path, *, secondary_priority: tuple[str, ...] = ()) -> Path | None:
    """Resolve a file path argument, expanding abbreviations.

    If `s` names an existing file (absolute or relative to cwd), return it.
    Otherwise treat `s` as a case-insensitive substring to match against the
    stem of every date-slug .md file (YYYY-MM-DD-*.md) in cwd.  Returns the
    resolved Path on an unambiguous match, or None after printing an error.

    `secondary_priority` lists companion suffixes to include (e.g. ``("chat",)``).
    When multiple companions share a primary stem, the one whose suffix appears
    earliest in the tuple wins; bare ``.md`` files are always lowest priority.
    """
    candidate = Path(s)
    if not candidate.is_absolute():
        candidate = cwd / s
    if candidate.exists():
        return candidate

    abbrev = s.lower()
    raw_matches = sorted(
        p.name
        for p in cwd.iterdir()
        if _DATED_SLUG_RE.match(p.name)
        and (len(p.suffixes) == 1 or any(p.name.endswith(f".{sec}.md") for sec in secondary_priority))
        and abbrev in p.stem.lower()
    )

    if secondary_priority:
        def _sec_rank(name: str) -> int:
            for i, sec in enumerate(secondary_priority):
                if name.endswith(f".{sec}.md"):
                    return i
            return len(secondary_priority)

        def _primary_stem(name: str) -> str:
            for sec in secondary_priority:
                if name.endswith(f".{sec}.md"):
                    return name[: -len(f".{sec}.md")]
            return name[:-3]

        by_stem: dict[str, str] = {}
        for name in raw_matches:
            stem = _primary_stem(name)
            if stem not in by_stem or _sec_rank(name) < _sec_rank(by_stem[stem]):
                by_stem[stem] = name
        matches = sorted(by_stem.values())
    else:
        matches = raw_matches

    if not matches:
        print(f"Error: '{s}' not found.")
        return None
    if len(matches) == 1:
        return cwd / matches[0]
    print(f"Ambiguous abbreviation '{s}' matches multiple files:")
    for name in matches:
        print(f"  {name}")
    return None


_COMPANION_SUFFIXES = ("document", "chat", "argument")


def _resolve_edit_paths(s: str, cwd: Path) -> list[Path]:
    """Return all companion files sharing the same primary stem as `s`.

    If `s` names an existing file directly, return just that file.  Otherwise
    find the primary stem via abbreviation match and return all companions
    (.document.md, .chat.md, .argument.md) plus the bare .md if present.
    """
    candidate = Path(s) if Path(s).is_absolute() else cwd / s
    if candidate.exists():
        return [candidate]

    primary = _resolve_path_abbrev(s, cwd, secondary_priority=_COMPANION_SUFFIXES)
    if primary is None:
        return []

    stem = primary.name
    for sec in _COMPANION_SUFFIXES:
        if stem.endswith(f".{sec}.md"):
            stem = stem[: -len(f".{sec}.md")]
            break
    else:
        stem = stem[:-3]  # strip .md

    results = []
    for sec in _COMPANION_SUFFIXES:
        p = cwd / f"{stem}.{sec}.md"
        if p.exists():
            results.append(p)
    bare = cwd / f"{stem}.md"
    if bare.exists():
        results.append(bare)
    return results or [primary]


_SPECIAL_LINE_RE = re.compile(
    r"^(?:#|[-*+] |\d+\. |\| |    |\t|[-*_]{3,}\s*$)"
)


def wrap_paragraphs(text: str, width: int = 100) -> str:
    """Wrap prose paragraphs at `width` columns; leave code fences, headings, lists, refs untouched."""
    lines = text.split("\n")
    result: list[str] = []
    in_code = False
    para: list[str] = []

    def flush() -> None:
        if para:
            if all(l.startswith("> ") for l in para):
                inner = " ".join(l[2:].rstrip() for l in para)
                wrapped = textwrap.fill(inner, width=max(1, width - 2), break_long_words=False, break_on_hyphens=False)
                result.extend("> " + l for l in wrapped.split("\n"))
            else:
                joined = " ".join(l.rstrip() for l in para)
                result.extend(textwrap.fill(joined, width=width, break_long_words=False, break_on_hyphens=False).split("\n"))
            para.clear()

    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            flush()
            in_code = not in_code
            result.append(line)
            continue
        if in_code or not line.strip():
            flush()
            result.append(line)
            continue
        if _SPECIAL_LINE_RE.match(line):
            flush()
            result.append(line)
            continue
        para.append(line)

    flush()
    return "\n".join(result)


def main(argv: list[str] | None = None) -> int:
    from mdc.config import DEFAULT_CONFIG_PATH, DEFAULT_SYSTEM_PROMPT_PATH, _write_default_config, _write_default_system_prompt
    if not DEFAULT_CONFIG_PATH.exists():
        _write_default_config(DEFAULT_CONFIG_PATH)
    if not DEFAULT_SYSTEM_PROMPT_PATH.exists():
        _write_default_system_prompt(DEFAULT_SYSTEM_PROMPT_PATH)

    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "review":
            return run_review(
                library_path=args.lib,
                reset=args.reset,
                dry_run=args.dry_run,
                since=args.since,
                no_write=args.no_assessment,
                rebuild=args.rebuild,
            )
        if args.command == "index":
            return run_index(
                library_path=args.lib,
                refs_only=args.refs_only_all,
                reprocess_all=args.all,
                verbose=args.verbose,
            )
        if args.command == "new":
            config = load_config()
            lib = Path(args.lib).expanduser().resolve() if args.lib else config.library_path
            return run_new(" ".join(args.title_words) or None, edit=args.edit, library_path=lib)
        if args.command == "check":
            if _require_bare(args.path):
                return 1
            path = _resolve_path_abbrev(args.path, Path.cwd())
            if path is None:
                return 1
            return run_check(path)
        if args.command == "validate":
            paths = []
            for p in args.paths:
                if _require_bare(p):
                    return 1
                resolved = _resolve_path_abbrev(p, Path.cwd())
                if resolved is None:
                    return 1
                paths.append(resolved)
            return run_validate(paths, force_transcript=args.transcript, library_path=args.lib)
        if args.command == "fix":
            paths = []
            for p in args.paths:
                if _require_bare(p):
                    return 1
                resolved = _resolve_path_abbrev(p, Path.cwd())
                if resolved is None:
                    return 1
                paths.append(resolved)
            return run_fix(paths)
        if args.command == "reply":
            if args.terms and not args.library:
                print("Error: -t/--term requires -l/--library.")
                return 1
            if _require_bare(args.path):
                return 1
            path = _resolve_path_abbrev(args.path, Path.cwd(), secondary_priority=("chat",))
            if path is None:
                return 1
            return run_reply(
                path,
                model=args.model,
                reasoning_effort=args.reasoning_effort,
                verbose=args.verbose,
                watch=args.watch,
                library=args.library,
                terms=args.terms,
                strict=args.strict,
                web_search=args.web_search,
                library_path=args.lib,
            )
        if args.command == "diff":
            extra = args.diff_args or []
            if extra and extra[0] == "--":
                extra = extra[1:]
            config = load_config()
            lib_path = Path(args.lib).expanduser().resolve() if args.lib else config.library_path
            _rev_dir = (lib_path / "REVISIONS") if lib_path else None
            if _require_bare(args.path):
                return 1
            path = _resolve_path_abbrev(args.path, Path.cwd(), secondary_priority=("document", "chat", "argument"))
            if path is None:
                return 1
            return run_diff(
                path,
                revision=args.revision,
                delta=args.delta,
                diff_args=extra or None,
                revisions_dir=_rev_dir,
            )
        if args.command == "config":
            return run_config()
        if args.command == "argue":
            if _require_bare(args.path):
                return 1
            path = _resolve_path_abbrev(args.path, Path.cwd(), secondary_priority=("argument", "document"))
            if path is None:
                return 1
            return run_argue(path, verbose=args.verbose, max_props=args.max_props, step=args.step)
        if args.command == "edit":
            if _require_bare(args.path):
                return 1
            paths = _resolve_edit_paths(args.path, Path.cwd())
            if not paths:
                return 1
            return run_edit(paths)
        if args.command == "files":
            if getattr(args, "files_command", None) == "ls":
                return run_files_ls()
            args._files_parser.print_help()
            return 1
        if args.command == "pdf":
            if _require_bare(args.path):
                return 1
            path = _resolve_path_abbrev(args.path, Path.cwd())
            if path is None:
                return 1
            return run_pdf(path, quiet=args.quiet)
    except TranscriptError as exc:
        print(f"Error: {exc}")
        return 1
    except FileNotFoundError as exc:
        print(f"Error: {exc}")
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 1
    except Exception as exc:
        print(f"Error: {exc}")
        return 1

    parser.print_help()
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mdc",
        description="Work with mdc-format markdown conversation files.",
    )
    parser.add_argument(
        "--lib",
        default=None,
        metavar="PATH",
        help="Override the library_path from config for this invocation.",
    )
    subparsers = parser.add_subparsers(dest="command")

    # review
    review_parser = subparsers.add_parser(
        "review",
        help="Run a staged AI review over an indexed document collection.",
    )
    review_parser.add_argument(
        "--reset",
        action="store_true",
        default=False,
        help="Discard saved state and start the review from scratch.",
    )
    review_parser.add_argument(
        "--since",
        default=None,
        metavar="DOC",
        help="Re-review from this document onward, overriding saved state from that point.",
    )
    review_parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Show what would be reviewed without making API calls.",
    )
    review_parser.add_argument(
        "--no-assessment",
        action="store_true",
        default=False,
        help="Print doc reviews and save state to JSON but skip interim/final assessments and do not write REVIEW files.",
    )
    review_parser.add_argument(
        "--rebuild",
        action="store_true",
        default=False,
        help="Rebuild REVIEW.md from saved state without making API calls.",
    )


    # relate
    # index
    index_parser = subparsers.add_parser(
        "index",
        help="Build or update the library document index using an AI model for summaries.",
    )
    index_parser.add_argument(
        "--all",
        action="store_true",
        default=False,
        help="Reindex all documents and rebuild all relations from scratch.",
    )
    index_parser.add_argument(
        "--refs-only-all",
        action="store_true",
        default=False,
        help="Extract references from all documents without calling any AI model.",
    )
    index_parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        default=False,
        help="Print warnings about unused KEYS.md entries.",
    )

    # new
    new_parser = subparsers.add_parser(
        "new",
        help="Create a new mdc conversation file in the current directory.",
    )
    new_parser.add_argument(
        "title_words",
        nargs="*",
        metavar="WORD",
        help="Title of the conversation.",
    )
    new_parser.add_argument(
        "-e", "--edit",
        action="store_true",
        help="Also create an editor file with '(Editor)' appended to the title.",
    )

    # check
    check_parser = subparsers.add_parser(
        "check",
        help="Validate transcript structure and report reply status.",
    )
    check_parser.add_argument("path", help="Path to the markdown transcript.")

    # validate
    validate_parser = subparsers.add_parser(
        "validate",
        help="Run mdc format rules on one or more files.",
    )
    validate_parser.add_argument("paths", nargs="+", metavar="file.md")
    validate_parser.add_argument(
        "-t", "--transcript",
        action="store_true",
        default=False,
        help="Force transcript validation even for plain documents.",
    )

    # fix
    fix_parser = subparsers.add_parser(
        "fix",
        help="Auto-fix correctable mdc format violations (modifies files in place).",
    )
    fix_parser.add_argument("paths", nargs="+", metavar="file.md")

    # reply
    reply_parser = subparsers.add_parser(
        "reply",
        help="Append one AI assistant reply to a transcript.",
    )
    reply_parser.add_argument(
        "-m", "--model",
        default=None,
        help="Model to use (e.g. claude-sonnet-4-6, gpt-4o). Overrides config file.",
    )
    reply_parser.add_argument(
        "-r", "--reasoning-effort",
        choices=("none", "low", "medium", "high", "xhigh"),
        default="low",
        help="Set the model's reasoning effort (default: low).",
    )
    reply_parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        default=False,
        help="Print progress messages while fetching a reply.",
    )
    reply_parser.add_argument(
        "-W", "--watch",
        action="store_true",
        default=False,
        help="Poll the transcript every second and reply whenever a pending turn is found.",
    )
    reply_parser.add_argument(
        "-l", "--library",
        action="store_true",
        default=False,
        help="Enable library tool access (requires library_path in config).",
    )
    reply_parser.add_argument(
        "-t", "--term",
        action="append",
        dest="terms",
        default=[],
        metavar="TERM",
        help="Pre-look up a library index term and inject results into context. Requires -l. May be repeated.",
    )
    reply_parser.add_argument(
        "--strict",
        action="store_true",
        default=False,
        help="Abort if any library term lookup fails (default: warn and proceed).",
    )
    reply_parser.add_argument(
        "-w", "--web-search",
        action="store_true",
        default=False,
        help="Enable Anthropic web search (server-side tool).",
    )
    reply_parser.add_argument("path", help="Path to the markdown transcript.")

    # pdf
    pdf_parser = subparsers.add_parser(
        "pdf",
        help="Convert a markdown file to PDF via pandoc.",
    )
    pdf_parser.add_argument(
        "-q", "--quiet",
        action="store_true",
        default=False,
        help="Do not open the PDF after conversion.",
    )
    pdf_parser.add_argument("path", help="Path to the markdown file.")

    # diff
    diff_parser = subparsers.add_parser(
        "diff",
        help="Show changes made to a file by the last mdc reply edit.",
    )
    diff_parser.add_argument("path", help="Path to the edited file.")
    diff_parser.add_argument(
        "-r", "--revision",
        type=int,
        default=None,
        metavar="N",
        help="Diff revision N against the current file.",
    )
    diff_parser.add_argument(
        "-d", "--delta",
        type=int,
        default=None,
        metavar="N",
        help="Show the Nth most recent change (default: 1).",
    )
    diff_parser.add_argument(
        "diff_args",
        nargs=argparse.REMAINDER,
        help=argparse.SUPPRESS,
    )

    # edit
    edit_parser = subparsers.add_parser(
        "edit",
        help="Open a file in $EDITOR.",
    )
    edit_parser.add_argument("path", help="Path to the markdown file.")

    # config
    subparsers.add_parser(
        "config",
        help="Show configuration and data file locations.",
    )

    # files
    files_parser = subparsers.add_parser(
        "files",
        help="Manage files uploaded to the Anthropic Files API.",
    )
    files_sub = files_parser.add_subparsers(dest="files_command")
    files_sub.add_parser(
        "ls",
        help="List locally cached file uploads.",
    )
    files_parser.set_defaults(_files_parser=files_parser)

    # argue
    argue_parser = subparsers.add_parser(
        "argue",
        help="Extract a structured argument from a plain document, or submit a companion argument file to dianoia for evaluation.",
    )
    argue_parser.add_argument("path", help="Plain document (.md). Extracts argument to <stem>.argument.md if absent, evaluates it if present.")
    argue_parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Show extra detail.",
    )
    argue_parser.add_argument(
        "-m", "--max-props",
        metavar="N",
        type=int,
        default=None,
        help="Maximum total number of propositions passed to dianoia extract.",
    )
    argue_parser.add_argument(
        "step",
        nargs="?",
        default=None,
        metavar="STEP",
        help="Evaluate only this step and its direct justifiers (requires companion .argument.md).",
    )

    return parser


def run_config() -> int:
    from mdc.config import DEFAULT_CONFIG_PATH, DEFAULT_SYSTEM_PROMPT_PATH, _cache_dir, _state_dir
    print(f"Config file:   {DEFAULT_CONFIG_PATH}")
    print(f"System prompt: {DEFAULT_SYSTEM_PROMPT_PATH}")
    print(f"State dir:     {_state_dir}")
    print(f"Cache dir:     {_cache_dir}")
    return 0


def run_edit(paths: list[Path]) -> int:
    editor_cmd = os.environ.get("EDITOR")
    if not editor_cmd:
        for p in paths:
            print(str(p))
        return 0
    subprocess.run([editor_cmd, *[str(p) for p in paths]])
    return 0


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


def _annotate_voice(
    content: str,
    user_names: tuple[str, ...],
    llm_names: tuple[str, ...],
) -> str:
    """Return content annotated with [VOICE: ...] labels per section.

    Non-transcripts (including sectionless documents) are prefixed with a
    label indicating the whole document is the user's own writing.
    """
    from mdc.library import _STRUCTURAL_HEADINGS, is_library_transcript
    from mdc.transcript import HEADING_RE

    if not is_library_transcript(content, user_names, llm_names):
        return "[VOICE: this entire document is the user's own writing]\n\n" + content

    user_set = frozenset(user_names)
    llm_set = frozenset(llm_names)

    def _label(speaker: str) -> str:
        if speaker in user_set:
            return "[VOICE: user]"
        if speaker in llm_set:
            return "[VOICE: llm — collaborative elaboration, implicitly endorsed unless contradicted]"
        if speaker in _STRUCTURAL_HEADINGS:
            return ""
        return "[VOICE: third-party]"

    parts: list[str] = []
    matches = list(HEADING_RE.finditer(content))
    prev_end = 0
    for m in matches:
        parts.append(content[prev_end:m.end()])
        label = _label(m.group(2).strip())
        if label:
            parts.append(f"\n{label}")
        prev_end = m.end()
    parts.append(content[prev_end:])
    return "".join(parts)


def _index_prompt(content: str, word_count: int) -> str:
    from mdc.library import summary_target, terms_target
    s_target = summary_target(word_count)
    t_target = terms_target(word_count)
    return (
        "You are a library indexing assistant. Respond in English only. "
        "Your only job is to output a SUMMARY and a TERMS list in the exact format "
        "shown below. Do not add any other text, commentary, greetings, or explanation.\n\n"
        "OUTPUT FORMAT (use exactly these two lines):\n"
        "SUMMARY: <summary text here>\n"
        "TERMS: <term1>; <term2>; <term3>; ...\n\n"
        "RULES:\n"
        f"- SUMMARY must be {s_target} describing the document's actual subject matter. "
        "When [VOICE: ...] labels are present: [VOICE: user] marks the user's direct words; "
        "[VOICE: llm — collaborative elaboration, implicitly endorsed unless contradicted] marks AI replies that "
        "expand on the user's thinking and should be treated as representing the user's intellectual "
        "context unless a later [VOICE: user] section explicitly contradicts them; "
        "[VOICE: third-party] marks external sources or quoted voices; "
        "[VOICE: this entire document is the user's own writing] means all content is the user's voice.\n"
        f"- TERMS must be {t_target} index terms separated by semicolons: key topics, "
        "concepts, and names as found in a book index. Write people's names in "
        "inverted form suitable for sorting (e.g. 'Twain, Mark'). "
        "Lowercase all terms except proper names and acronyms.\n"
        "- Output only the two lines. Nothing before SUMMARY, nothing after TERMS.\n"
        "- If the document is a conversation transcript, index the topics discussed, "
        "not the fact that it is a conversation and not the conversational style.\n"
        "- Use singular forms for terms unless the plural is the standard form (e.g. 'belief', not 'beliefs').\n\n"
        "EXAMPLE OUTPUT:\n"
        "SUMMARY: A discussion of how satire functions as social criticism, examining "
        "the use of irony and vernacular voice to expose hypocrisy and moral failure.\n"
        "TERMS: Twain, Mark; satire; social criticism; irony; vernacular; hypocrisy; "
        "moral philosophy; American literature\n\n"
        "---\n"
        f"{content}"
    )


def _parse_index_reply(text: str) -> tuple[str, list[str]]:
    summary_lines: list[str] = []
    terms_lines: list[str] = []
    mode = ""
    for line in text.splitlines():
        if line.startswith("SUMMARY:"):
            mode = "summary"
            rest = line[len("SUMMARY:"):].strip()
            if rest:
                summary_lines.append(rest)
        elif line.startswith("TERMS:"):
            mode = "terms"
            rest = line[len("TERMS:"):].strip()
            if rest:
                terms_lines.append(rest)
        elif mode == "summary" and line.strip():
            summary_lines.append(line.strip())
        elif mode == "terms" and line.strip():
            terms_lines.append(line.strip())
    summary = " ".join(summary_lines).strip()
    raw_terms = " ".join(terms_lines)
    terms = [t.strip() for t in raw_terms.split(";") if t.strip()]
    return summary, terms


def run_index(library_path: str | None, refs_only: bool = False, reprocess_all: bool = False, verbose: bool = False) -> int:
    import random
    from mdc.library import MANIFEST_FILENAME, build_index

    config = load_config()

    raw_path = library_path or (str(config.library_path) if config.library_path else None)
    if not raw_path:
        print("Error: no library path specified. Pass a path or set 'library_path' in config.")
        return 1

    lib_path = Path(raw_path).expanduser().resolve()
    if not lib_path.is_dir():
        print(f"Error: '{lib_path}' is not a directory.")
        return 1

    summarize = None
    total_cost = 0.0
    last_cost: list[float] = [0.0]

    if not refs_only:
        effective_model = config.index_model
        if effective_model.startswith("claude-"):
            from mdc.anthropic_client import AnthropicChatClient
            client = AnthropicChatClient(model=effective_model, api_key=config.anthropic_api_key)
            rates = _lookup_price(effective_model, _ANTHROPIC_PRICING)

            def summarize(content: str, word_count: int) -> tuple[str, list[str]]:
                nonlocal total_cost
                system = "You are a library indexing assistant."
                annotated = _annotate_voice(content, config.user_names, config.llm_names)
                messages = [{"role": "user", "content": _index_prompt(annotated, word_count)}]
                reply = client.generate_reply(system, messages)
                if rates:
                    in_rate, out_rate = rates
                    cost = (
                        reply.input_tokens * in_rate / 1_000_000
                        + reply.output_tokens * out_rate / 1_000_000
                        + reply.cache_creation_tokens * in_rate * 1.25 / 1_000_000
                        + reply.cache_read_tokens * in_rate * 0.10 / 1_000_000
                    )
                    total_cost += cost
                    last_cost[0] = cost
                return _parse_index_reply(reply.text)

        else:
            from mdc.ollama_client import OllamaChatClient
            ollama_model = effective_model.removeprefix("ollama/")
            client = OllamaChatClient(model=ollama_model, base_url=config.ollama_base_url)

            def summarize(content: str, word_count: int) -> tuple[str, list[str]]:
                annotated = _annotate_voice(content, config.user_names, config.llm_names)
                messages = [{"role": "user", "content": _index_prompt(annotated, word_count)}]
                reply = client.generate_reply(messages)
                return _parse_index_reply(reply.text)

    duplicates = {slug: paths for slug, paths in _slug_map(lib_path).items() if len(paths) > 1}
    if duplicates:
        print("Warning: duplicate slugs detected in library:")
        for slug, paths in sorted(duplicates.items()):
            for p in paths:
                print(f"  {p.relative_to(lib_path)}")
        print()

    counts: dict[str, int] = {}
    last_status: list[str] = [""]
    sanitize_warnings: list[str] = []

    def on_progress(rel_path: str, status: str) -> None:
        counts[status] = counts.get(status, 0) + 1
        if status == "indexed":
            if last_status[0] in ("cached", "skipped"):
                print()
            cost_str = f"  {_format_cost(last_cost[0])}  (total {_format_total(total_cost)})" if total_cost else ""
            print(f"  indexed  {rel_path}{cost_str}")
        elif status in ("cached", "skipped"):
            n = counts[status]
            print(f"\r  {status} {n} files   ", end="", flush=True)
        last_status[0] = status

    def on_warning(msg: str) -> None:
        sanitize_warnings.append(msg)

    from mdc.library import load_terms
    old_terms = load_terms(lib_path)

    if refs_only:
        print(f"Extracting references from {lib_path} (no model)...")
    else:
        print(f"Indexing {lib_path} with model '{effective_model}'...")
    entries, keys_warnings = build_index(lib_path, summarize=summarize, on_progress=on_progress, on_warning=on_warning)

    if counts.get("cached") or counts.get("skipped"):
        print()

    parts = [f"{len(entries)} document(s) indexed"]
    if counts.get("indexed"):
        parts.append(f"{counts['indexed']} new/updated")
    if counts.get("cached"):
        parts.append(f"{counts['cached']} cached")
    if counts.get("skipped"):
        parts.append(f"{counts['skipped']} skipped (too large)")
    if total_cost:
        parts.append(f"total cost {_format_total(total_cost)}")
    print(f"\n{', '.join(parts)}.")
    print(f"Written to {lib_path / MANIFEST_FILENAME}.")

    from mdc.library import cooccurrence_relations, load_relations, load_terms, prune_relations, save_relations
    new_terms = load_terms(lib_path)
    added_terms = sorted(new_terms - old_terms)
    removed_terms = sorted(old_terms - new_terms)
    if added_terms or removed_terms:
        print()
        for t in added_terms:
            print(f"  + {t}")
        for t in removed_terms:
            print(f"  - {t}")

    if removed_terms:
        prune_relations(lib_path, set(removed_terms))

    # ── semantic relations ────────────────────────────────────────────
    all_terms = sorted(new_terms)
    relations = load_relations(lib_path)

    if all_terms and not refs_only:
        all_terms_set = set(all_terms)
        stale = {t for t in relations if t not in all_terms_set}
        if stale:
            prune_relations(lib_path, stale)
            relations = load_relations(lib_path)

        if reprocess_all:
            to_process = all_terms_set
        else:
            unrelated = [t for t in all_terms if t not in relations]
            to_process = set(unrelated)
            for t in unrelated:
                to_process.update(relations.get(t, []))
            to_process &= all_terms_set

        if to_process:
            id_to_term: dict[int, str] = {i + 1: t for i, t in enumerate(all_terms)}
            term_to_id: dict[str, int] = {t: i for i, t in id_to_term.items()}
            process_ids = [term_to_id[t] for t in to_process]
            random.shuffle(process_ids)
            batch_size = 20
            batches = [process_ids[i:i + batch_size] for i in range(0, len(process_ids), batch_size)]
            total_batches = len(batches)
            relate_cost = 0.0

            if effective_model.startswith("claude-"):
                from mdc.anthropic_client import AnthropicChatClient
                rclient = AnthropicChatClient(model=effective_model, api_key=config.anthropic_api_key)
                rrates = _lookup_price(effective_model, _ANTHROPIC_PRICING)

                def call_model(prompt: str) -> str:
                    nonlocal relate_cost
                    reply = rclient.generate_reply("You are a library indexing assistant.", [{"role": "user", "content": prompt}])
                    if rrates:
                        in_rate, out_rate = rrates
                        relate_cost += (
                            reply.input_tokens * in_rate / 1_000_000
                            + reply.output_tokens * out_rate / 1_000_000
                            + reply.cache_creation_tokens * in_rate * 1.25 / 1_000_000
                            + reply.cache_read_tokens * in_rate * 0.10 / 1_000_000
                        )
                    return reply.text
            else:
                from mdc.ollama_client import OllamaChatClient
                rollama_model = effective_model.removeprefix("ollama/")
                rclient = OllamaChatClient(model=rollama_model, base_url=config.ollama_base_url)

                def call_model(prompt: str) -> str:
                    reply = rclient.generate_reply([{"role": "user", "content": prompt}])
                    return reply.text

            print(f"\nBuilding relations for {len(to_process)} of {len(all_terms)} terms "
                  f"in {total_batches} batches...")
            for i, batch_ids in enumerate(batches, 1):
                cost_str = f"  (total {_format_total(relate_cost)})" if relate_cost else ""
                print(f"  batch {i}/{total_batches}{cost_str}")
                prompt = _relate_prompt(id_to_term, batch_ids)
                text = call_model(prompt)
                parsed = _parse_relate_reply(text, batch_ids, id_to_term)
                for term, related in parsed.items():
                    clean = list(dict.fromkeys(r for r in related if r != term))
                    relations[term] = clean
                    for r in clean:
                        existing = relations.setdefault(r, [])
                        if term not in existing and term != r:
                            existing.append(term)
                save_relations(lib_path, relations)

            relate_cost_str = f"  Total relations cost: {_format_total(relate_cost)}." if relate_cost else ""
            print(f"\nRelations written for {len(relations)} terms.{relate_cost_str}")

    # ── co-occurrence supplementation ────────────────────────────────
    if relations:
        cooc = cooccurrence_relations(lib_path, min_count=6)
        new_pairs: list[tuple[str, str]] = []
        all_pairs: list[tuple[str, str]] = []
        for term, co_related in cooc.items():
            if term not in relations:
                continue
            existing = relations[term]
            for r in co_related:
                if r != term:
                    pair = (min(term, r), max(term, r))
                    if pair not in [p for p in all_pairs]:
                        all_pairs.append(pair)
                    if r not in existing:
                        existing.append(r)
                        new_pairs.append(pair)
        if new_pairs:
            save_relations(lib_path, relations)
        if all_pairs:
            print(f"\n  Co-occurrence relations: {len(all_pairs)} found, {len(new_pairs)} new.")

    if verbose:
        all_warnings = sanitize_warnings + keys_warnings
        if all_warnings:
            print("\nWarnings:")
            for w in all_warnings:
                print(f"  ! {w}")
    return 0


def _relate_prompt(id_to_term: dict[int, str], batch_ids: list[int]) -> str:
    terms_block = "\n".join(f"{i}: {id_to_term[i]}" for i in sorted(id_to_term))
    batch_block = "\n".join(f"{i}: {id_to_term[i]}" for i in batch_ids)
    return (
        "You are building a semantic index for a philosophy library.\n\n"
        "For each term in the BATCH below, identify all related terms from the "
        "TERM LIST — meaning a reader looking up that term would likely also "
        "want to consult them. Only use IDs from the TERM LIST. "
        "Do not invent IDs or terms.\n\n"
        "Include terms that:\n"
        "- Cover the same concept from a different angle\n"
        "- Are the broader or narrower form of the concept\n"
        "- Are frequently discussed together in the literature\n"
        "- Are morphological variants or derivatives of the same root\n\n"
        "Exclude terms that are only loosely or incidentally related.\n\n"
        "TERM LIST:\n"
        f"{terms_block}\n\n"
        "BATCH:\n"
        f"{batch_block}\n\n"
        "OUTPUT FORMAT — one line per batch term: its ID, a colon, then the IDs "
        "of related terms separated by semicolons. If none, write 'none'.\n"
        "Example:\n"
        "42: 103; 217; 445\n"
        "87: none"
    )


def _parse_relate_reply(
    text: str, batch_ids: list[int], id_to_term: dict[int, str]
) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    batch_id_set = set(batch_ids)
    for line in text.splitlines():
        if ":" not in line:
            continue
        left, _, right = line.partition(":")
        try:
            line_id = int(left.strip())
        except ValueError:
            continue
        if line_id not in batch_id_set:
            continue
        term = id_to_term[line_id]
        if right.strip().lower() == "none":
            result[term] = []
        else:
            related: list[str] = []
            for part in right.split(";"):
                try:
                    related_id = int(part.strip())
                except ValueError:
                    continue
                if related_id in id_to_term:
                    related.append(id_to_term[related_id])
            result[term] = related
    return result


def _slug_map(*roots: Path) -> dict[str, list[Path]]:
    seen: dict[str, list[Path]] = {}
    visited: set[Path] = set()
    for root in roots:
        if not root.is_dir():
            continue
        resolved = root.resolve()
        if resolved in visited:
            continue
        visited.add(resolved)
        for p in root.rglob("*.md"):
            if (root / "REVISIONS") in p.parents:
                continue
            seen.setdefault(p.stem, []).append(p)
    return seen


def _collect_existing_slugs(extra_root: Path | None) -> set[str]:
    slugs = {p.stem for p in Path.cwd().glob("*.md")}
    if extra_root and extra_root.is_dir():
        slugs |= set(_slug_map(extra_root).keys())
    return slugs


def run_new(title: str | None, edit: bool = False, library_path: Path | None = None) -> int:
    today = datetime.date.today().isoformat()
    existing = _collect_existing_slugs(library_path)
    if title is None:
        base = "Untitled"
        candidate = base
        n = 2
        while f"{today}-{slugify(candidate)}" in existing:
            candidate = f"{base} {n}"
            n += 1
        title = candidate
    slug = slugify(title)
    if edit:
        document_filename = f"{today}-{slug}.document.md"
        chat_filename = f"{today}-{slug}.chat.md"
        document_path = Path(document_filename)
        chat_path = Path(chat_filename)
        if document_path.exists():
            print(f"Error: '{document_filename}' already exists.")
            return 1
        if chat_path.exists():
            print(f"Error: '{chat_filename}' already exists.")
            return 1
        document_path.write_text(f"\n# {title}\n{today}\n\n", encoding="utf-8")
        chat_path.write_text(
            f"\n# {title}\n{today}\n\n## Prompt\n\n",
            encoding="utf-8",
        )
        print(document_filename)
        print(chat_filename)
        editor_cmd = os.environ.get("EDITOR")
        if editor_cmd:
            subprocess.run([editor_cmd, chat_filename, document_filename])
    else:
        filename = f"{today}-{slug}.md"
        path = Path(filename)
        if path.exists():
            print(f"Error: '{filename}' already exists.")
            return 1
        path.write_text(f"\n# {title}\n{today}\n\n## Prompt\n\n", encoding="utf-8")
        print(filename)
        editor_cmd = os.environ.get("EDITOR")
        if editor_cmd:
            subprocess.run([editor_cmd, filename])
    return 0


def run_check(path: Path) -> int:
    if _require_md(path):
        return 1
    transcript = parse_transcript(_read_file(path))
    assets_by_turn = collect_local_assets(transcript, path)
    asset_count = sum(len(assets) for assets in assets_by_turn.values())
    if transcript.pending:
        print(
            "OK: transcript is valid, "
            f"{asset_count} local asset(s) resolved, "
            f"and a reply is pending for '{transcript.pending_turn.speaker}'."
        )
    else:
        print(f"OK: transcript is valid, {asset_count} local asset(s) resolved, and no reply is pending.")
    return 0


def run_validate(paths: list[Path], force_transcript: bool = False, library_path: str | None = None) -> int:
    from mdc.library import is_library_transcript, resolve_title
    from mdc.review import list_review_docs

    config = load_config()
    raw_lib = Path(library_path).expanduser().resolve() if library_path else config.library_path
    lib = raw_lib if raw_lib and raw_lib.is_dir() else None
    doc_order = {p.name: i for i, p in enumerate(list_review_docs(lib))} if lib else {}

    any_errors = False
    for path in paths:
        if _require_md(path):
            any_errors = True
            continue
        content = _read_file(path)
        is_transcript = is_library_transcript(content, config.user_names, config.llm_names)
        doc_label = "transcript" if is_transcript else "plain document"

        if is_transcript or force_transcript:
            errs = check_file(path)
            if lib and not errs:
                try:
                    transcript = parse_transcript(content)
                    for entry in transcript.related:
                        rel_path = resolve_title(lib, entry)
                        if rel_path is None:
                            errs.append(f"Related title not found in library: {entry!r}")
                        elif doc_order.get(Path(rel_path).name, -1) >= doc_order.get(path.name, -1):
                            errs.append(f"Related title does not precede this document: {entry!r}")
                except TranscriptError:
                    pass
        else:
            errs = check_global_issues(path)

        if errs:
            label = doc_label if not force_transcript or is_transcript else f"{doc_label}, validated as transcript"
            print(f"{path} [{label}]:")
            for e in errs:
                print(f"  error: {e}")
            any_errors = True
        else:
            print(f"{path}: OK [{doc_label}]")
    return 1 if any_errors else 0


def run_fix(paths: list[Path]) -> int:
    any_errors = False
    for path in paths:
        if _require_md(path):
            any_errors = True
            continue
        raw = _read_file(path)
        orc_lines, orc_applied = fix_object_replacement(raw.split("\n"))
        rtl_lines, rtl_applied = fix_rtl_spans(orc_lines)
        new_lines, title_applied = fix_title_section(rtl_lines)
        new_text, spacing_applied = fix_section_spacing("\n".join(new_lines))
        applied = orc_applied + rtl_applied + title_applied + spacing_applied

        if applied:
            diff = list(difflib.unified_diff(
                raw.splitlines(keepends=True),
                new_text.splitlines(keepends=True),
                fromfile=f"{path} (original)",
                tofile=f"{path} (fixed)",
            ))
            print(f"{path}:")
            for fix in applied:
                print(f"  would fix: {fix}")
            print()
            sys.stdout.writelines(diff)
            print()
            try:
                answer = input("Apply changes? [y/N] ").strip().lower()
            except EOFError:
                answer = ""
            if answer == "y":
                path.with_suffix(".md.bak").write_text(raw, encoding="utf-8")
                path.write_text(new_text, encoding="utf-8")
                print("  Applied.")
            else:
                print("  Skipped.")
                print()
                continue

        errs = check_file(path)

        if not applied and not errs:
            print(f"{path}: OK")
        elif errs:
            if not applied:
                print(f"{path}:")
            for e in errs:
                print(f"  error: {e}")
            any_errors = True

    return 1 if any_errors else 0


def run_argue(path: Path, verbose: bool = False, max_props: int | None = None, step: str | None = None) -> int:
    from mdc.argue import argument_to_markdown, markdown_to_argument
    from mdc import dianoia_client

    if path.name.endswith(".argument.md"):
        companion = path
        path = _primary(path)
    elif path.name.endswith(".document.md"):
        if not path.exists():
            print(f"Error: '{path}' does not exist.")
            return 1
        companion = path.with_suffix("").with_suffix(".argument.md")
    else:
        if not path.exists():
            print(f"Error: '{path}' does not exist.")
            return 1
        if _require_md(path):
            return 1
        # Adopt bare document into the companion model by renaming it.
        document = path.with_suffix(".document.md")
        path.rename(document)
        path = document
        companion = path.with_suffix("").with_suffix(".argument.md")

    if companion.exists():
        # Evaluate: companion exists, submit it to dianoia
        try:
            args_dict = markdown_to_argument(companion.read_text(encoding="utf-8"))
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        print("Submitting to dianoia for evaluation…")
        try:
            results = dianoia_client.evaluate(args_dict, step=step)
        except (FileNotFoundError, RuntimeError) as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        _append_evaluation(companion, results, verbose)
        print(f"Evaluation written to {companion.name}")
        return 0

    # Extract: no companion yet — validate and extract from the primary document
    if step:
        print("Warning: --step ignored during extraction", file=sys.stderr)
    text = _read_file(path)
    from mdc.library import is_library_transcript
    config = load_config()
    if is_library_transcript(text, config.user_names, config.llm_names):
        print("Error: mdc argue requires a plain document, not a transcript.")
        return 1
    errs = check_global_issues(path)
    if errs:
        for e in errs:
            print(f"  error: {e}")
        return 1

    print("Extracting argument…")
    try:
        args_dict = dianoia_client.extract(text, max_props=max_props)
    except (FileNotFoundError, RuntimeError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    try:
        title, date_str = _read_title_date(text)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    try:
        companion_text = argument_to_markdown(args_dict, title, date_str)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    companion.write_text(companion_text, encoding="utf-8")
    _print_argument(args_dict)
    print(f"\nWritten to {companion.name}. Edit it, then run: mdc argue {path.name}")
    return 0


def _read_title_date(text: str) -> tuple[str, str]:
    """Extract title and date from the first few lines of an mdc preamble."""
    lines = text.splitlines()
    title: str | None = None
    date_str: str | None = None
    for line in lines[:6]:
        line = line.strip()
        if line.startswith("# ") and title is None:
            title = line[2:].strip()
        elif re.match(r"^\d{4}-\d{2}-\d{2}$", line) and date_str is None:
            date_str = line
        if title and date_str:
            break
    if not title:
        raise ValueError("could not find '# Title' in preamble")
    if not date_str:
        raise ValueError("could not find date line in preamble")
    return title, date_str


def _print_argument(args_dict: dict) -> None:
    assumptions = args_dict.get("assumptions", [])
    argument = args_dict.get("argument", [])
    if assumptions:
        print("Assumptions:")
        for s in assumptions:
            print(f"  {s['symbol']}: {s['proposition']}")
    print("Argument:")
    for s in argument:
        j = f" (from: {', '.join(s['justifiers'])})" if s.get("justifiers") else ""
        print(f"  {s['symbol']}{j}: {s['proposition']}")


def _append_evaluation(path: Path, results: dict, verbose: bool) -> None:
    """Inject formalizations inline; append content/improvement sections."""
    from typing import cast
    from mdc.argue import extract_core_sections, inject_formalizations, markdown_to_argument
    from mdc.dianoia_results import (
        ContentEvalResult,
        FormalEvalResult,
        FormalizerResult,
        ImproverResult,
    )

    text = extract_core_sections(path.read_text(encoding="utf-8"))

    text = text.rstrip("\n") + "\n"

    results_by_agent = results.get("results_by_agent", {})

    # Collect formalizer output
    formalizer_results = results_by_agent.get("formalizer", [])
    new_formalizations: dict = {}
    all_definitions: dict = {"predicates": [], "constants": []}
    for r in formalizer_results:
        rc = cast(FormalizerResult, r.get("result_content", {}))
        for f in rc.get("formalizations", []):
            sym = f.get("symbol")
            if sym:
                new_formalizations[sym] = f.get("ascii", "")
        defs = rc.get("definitions", {})
        all_definitions["predicates"].extend(defs.get("predicates", []))
        all_definitions["constants"].extend(defs.get("constants", []))

    # Merge: existing endorsed sub-bullets win over newly generated
    try:
        existing = markdown_to_argument(text)
    except ValueError:
        existing = {"assumptions": [], "argument": []}
    endorsed: dict = {}
    for step in existing.get("assumptions", []) + existing.get("argument", []):
        form = step.get("formalization") or {}
        if form.get("endorsed") and form.get("ascii"):
            endorsed[step["symbol"]] = form["ascii"]
    by_symbol = {**new_formalizations, **endorsed}

    if by_symbol:
        text = inject_formalizations(text, by_symbol)

    # When formalizer ran and produced definitions, replace ## Definitions content in-place
    # (un-endorsed predicates are naturally absent from formalizer output, implementing
    # un-endorsement by omission). Skip when formalizer returned early with no new definitions
    # (all steps already endorsed) so user-supplied definitions are preserved.
    if formalizer_results and (all_definitions["predicates"] or all_definitions["constants"]):
        def_content_lines = []
        for c in all_definitions["constants"]:
            def_content_lines.append(f"- {c.get('symbol', '?')} = {c.get('value', '')}")
        for p in all_definitions["predicates"]:
            sym = p.get('symbol', '?')
            arity = p.get('arity', 0)
            label = f"{sym}/{arity}" if arity else sym
            def_content_lines.append(f"- {label} = {p.get('value', '')}")
        new_def_content = "\n".join(def_content_lines)
        # Match the content block between ## Definitions header and next ## section (or end)
        def_content_match = re.search(
            r"(## Definitions[^\n]*\n)((?:(?!## ).*\n)*)", text
        )
        if def_content_match:
            replacement = (new_def_content + "\n") if new_def_content else ""
            text = (
                text[: def_content_match.start(2)]
                + replacement
                + text[def_content_match.end(2):]
            )
        else:
            # No existing section — insert before ## Assumptions or ## Argument
            section_block = "\n## Definitions\n" + (new_def_content + "\n" if new_def_content else "")
            insert_match = re.search(r"\n## (?:Assumptions|Argument)\b", text)
            if insert_match:
                text = text[: insert_match.start()] + section_block + text[insert_match.start():]
            else:
                text = text + section_block

    text = text.rstrip("\n") + "\n"
    lines = []

    form_eval_results = results_by_agent.get("form_evaluator", [])
    for r in form_eval_results:
        rc = cast(FormalEvalResult, r.get("result_content", {}))
        prop_evals = rc.get("proposition_evaluations", [])
        arg_validity = rc.get("argument_validity")
        issues = rc.get("logical_issues", [])
        recommendations = rc.get("recommendations", [])
        if prop_evals or issues or arg_validity is not None:
            lines.append("\n## Formal evaluation\n")
        for item in sorted(prop_evals, key=lambda x: x.get("symbol", "")):
            sym = item.get("symbol", "?")
            val = item.get("validity", "?")
            reasoning = item.get("reasoning", "")
            lines.append(f"- {sym} validity: {val} — {reasoning}")
        if prop_evals:
            lines.append("")
        if arg_validity is not None:
            lines.append(f"- argument validity: {arg_validity}")
            lines.append("")
        for issue in issues:
            lines.append(f"- {issue}")
        if issues:
            lines.append("")
        for rec in recommendations:
            lines.append(f"- {rec}")
        if recommendations:
            lines.append("")

    content_results = results_by_agent.get("content_evaluator", [])
    for r in content_results:
        rc = cast(ContentEvalResult, r.get("result_content", {}))
        truth = rc.get("truth_evaluations", [])
        validity = rc.get("validity_evaluations", [])
        incoherent = rc.get("incoherent_sets", [])
        if truth or validity or incoherent:
            lines.append("\n## Content evaluation\n")
        for item in sorted(truth, key=lambda x: x.get("symbol", "")):
            sym = item.get("symbol", "?")
            val = item.get("truth_value", "?")
            reasoning = item.get("reasoning", "")
            lines.append(f"- {sym} truth: {val} — {reasoning}")
        if truth:
            lines.append("")
        for item in sorted(validity, key=lambda x: x.get("symbol", "")):
            sym = item.get("symbol", "?")
            val = item.get("validity_value", "?")
            reasoning = item.get("reasoning", "")
            lines.append(f"- {sym} validity: {val} — {reasoning}")
        if validity:
            lines.append("")
        for item in incoherent:
            syms = ", ".join(item.get("symbols", []))
            val = item.get("incoherence_value", "?")
            lines.append(f"- incoherent ({val}): {syms}")
        if incoherent:
            lines.append("")

    improvement_results = results_by_agent.get("improver", [])
    for r in improvement_results:
        recs = cast(ImproverResult, r.get("result_content", {})).get("recommendations", [])
        if recs:
            lines.append("\n## Improvement recommendations\n")
        for rec in recs:
            impact = rec.get("impact", "")
            reasoning = rec.get("reasoning", "")
            lines.append(f"**{impact.capitalize()} impact**: {reasoning}\n")
            for prop in rec.get("propositions", []):
                ptype = prop.get("type", "")
                sym = prop.get("symbol") or "new"
                text_p = prop.get("proposition", "")
                lines.append(f"- {sym} ({ptype}): {text_p}")
            lines.append("")

    path.write_text(text + "\n".join(lines), encoding="utf-8")


def run_pdf(path: Path, quiet: bool = False) -> int:
    if not path.exists():
        print(f"Error: '{path}' does not exist.")
        return 1
    if _require_md(path):
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


_REVIEW_WINDOW = 40


_TOC_BLOCK = """\
```{=latex}
\\tableofcontents
\\newpage
```

"""


def _prepend_toc(out_path: Path) -> None:
    content = out_path.read_text(encoding="utf-8")
    if not content.startswith(_TOC_BLOCK):
        out_path.write_text(_TOC_BLOCK + content, encoding="utf-8")


def _render_review_pdfs(*md_paths: Path) -> None:
    import subprocess
    for md_path in md_paths:
        pdf_path = md_path.with_suffix(".pdf")
        for engine in ("xelatex", None):
            cmd = ["pandoc", str(md_path), "-o", str(pdf_path)]
            if engine:
                cmd += [f"--pdf-engine={engine}"]
            try:
                subprocess.run(cmd, check=True, capture_output=True)
                print(f"Wrote {pdf_path.name}.")
                break
            except FileNotFoundError:
                print("pandoc not found; skipping PDF generation.")
                return
            except subprocess.CalledProcessError as e:
                if engine is None:
                    print(f"pandoc error on {md_path.name}: {e.stderr.decode()[:300]}")


def run_review(library_path: str | None, reset: bool, dry_run: bool = False, since: str | None = None, no_write: bool = False, rebuild: bool = False) -> int:
    import hashlib
    from mdc.config import _state_dir
    from mdc.anthropic_client import AnthropicChatClient
    from mdc.review import (
        _DEFAULT_FINAL_PROMPT,
        _DEFAULT_INTERIM_PROMPT,
        _DEFAULT_SYSTEM_PROMPT,
        _REVIEW_PROMPTS_DIR,
        DEFAULT_WINDOW,
        build_assessment_md,
        build_assessments_md,
        build_doc_review_messages,
        build_final_messages,
        build_interim_messages,
        build_manifest_summaries,
        build_reviews_md,
        extract_doc_heading,
        extract_mentioned_titles,
        generate_include_list,
        load_include_list,
        list_review_docs,
        load_prompt,
        load_review_state,
        save_review_state,
    )
    from mdc.library import (
        ASSESSMENT_FILENAME,
        REVIEW_FILENAME,
        REVIEW_INCLUDE_FILENAME,
        REVIEWS_FILENAME,
    )

    config = load_config()
    effective_model = config.model
    if not effective_model:
        print("Error: no model specified. Set 'model' in config.")
        return 1
    if not effective_model.startswith("claude-"):
        print("Error: mdc review only supports Anthropic (claude-*) models.")
        return 1

    raw_path = library_path or (str(config.library_path) if config.library_path else None)
    if not raw_path:
        print("Error: no library path. Pass a path or set 'library_path' in config.")
        return 1
    lib_path = Path(raw_path).expanduser().resolve()
    if not lib_path.is_dir():
        print(f"Error: '{lib_path}' is not a directory.")
        return 1

    path_hash = hashlib.sha256(str(lib_path).encode()).hexdigest()[:8]
    state_path = _state_dir / f"review-{path_hash}.json"
    out_path = lib_path / REVIEW_FILENAME

    if reset:
        state_path.unlink(missing_ok=True)
        out_path.unlink(missing_ok=True)
        print("State and output file reset.")

    state = load_review_state(state_path)

    if rebuild:
        include_toc = state.final_done
        content = build_assessments_md(state, include_toc=include_toc)
        out_path.write_text(content, encoding="utf-8")
        print(f"Rebuilt {out_path.name} ({len(state.interims)} segments, final={'yes' if state.final_done else 'no'}).")

        if state.final_done:
            from mdc.library import load_entries
            entries = load_entries(lib_path)

            assessment_path = lib_path / ASSESSMENT_FILENAME
            assessment_path.write_text(build_assessment_md(state), encoding="utf-8")
            print(f"Wrote {ASSESSMENT_FILENAME}.")

            reviews_path = lib_path / REVIEWS_FILENAME
            reviews_path.write_text(build_reviews_md(state, entries), encoding="utf-8")
            print(f"Wrote {REVIEWS_FILENAME} ({len(state.doc_reviews)} individual reviews).")

            _render_review_pdfs(assessment_path, reviews_path)

        return 0


    include_toc = True
    system_prompt = load_prompt(_REVIEW_PROMPTS_DIR / "system.md", _DEFAULT_SYSTEM_PROMPT)
    interim_prompt = load_prompt(_REVIEW_PROMPTS_DIR / "interim.md", _DEFAULT_INTERIM_PROMPT)
    final_prompt_text = load_prompt(_REVIEW_PROMPTS_DIR / "final.md", _DEFAULT_FINAL_PROMPT)

    all_docs = list_review_docs(lib_path)
    if not all_docs:
        print("No indexed documents found. Run 'mdc index' first.")
        return 1

    total = len(all_docs)

    if since:
        since_name = Path(since).name
        start_pos = next((i for i, d in enumerate(all_docs) if d.name == since_name), None)
        if start_pos is None:
            print(f"Error: '{since_name}' not found in indexed documents.")
            return 1
        # Rewind to the segment boundary at or before start_pos.
        segment_start = (start_pos // DEFAULT_WINDOW) * DEFAULT_WINDOW
        state.interims = [e for e in state.interims if e["after_doc"] <= segment_start]
        state.doc_index = segment_start
        state.final_done = False
        state.final_text = None
        if not dry_run:
            out_path.write_text(build_assessments_md(state), encoding="utf-8")
            save_review_state(state, state_path)
        print(f"State rewound to segment starting at doc {segment_start + 1}.")

    if state.final_done:
        if state.doc_index >= total:
            print("Review already complete. Use --reset to start over.")
            return 0
        state.final_done = False
        state.final_text = None

    # If new documents have arrived and the last interim was partial, drop it and reprocess.
    if state.interims and not since and total > state.doc_index:
        last = state.interims[-1]
        prev_end = state.interims[-2]["after_doc"] if len(state.interims) > 1 else 0
        if last["after_doc"] - prev_end < DEFAULT_WINDOW:
            state.interims.pop()
            state.doc_index = prev_end
            state.final_done = False
            state.final_text = None
            if not dry_run:
                save_review_state(state, state_path)
            print(f"Last segment was partial; rewinding to doc {prev_end + 1}.")

    remaining_docs = all_docs[state.doc_index:]
    rates = _lookup_price(effective_model, _ANTHROPIC_PRICING)

    segments_done = state.doc_index // DEFAULT_WINDOW
    segments_total = (total + DEFAULT_WINDOW - 1) // DEFAULT_WINDOW

    if state.doc_index == 0:
        print(f"{total} documents  •  {segments_total} segments  •  model: {effective_model}")
    else:
        print(f"Resuming at segment {segments_done + 1} of {segments_total} (doc {state.doc_index + 1} of {total})  •  model: {effective_model}")

    if dry_run:
        remaining_segments = (len(remaining_docs) + DEFAULT_WINDOW - 1) // DEFAULT_WINDOW
        num_calls = remaining_segments + (0 if state.final_done else 1)
        print(f"Remaining docs: {len(remaining_docs)}  •  segments: {remaining_segments}  •  API calls: {num_calls}")
        for i in range(0, len(remaining_docs), DEFAULT_WINDOW):
            seg = remaining_docs[i:i + DEFAULT_WINDOW]
            print(f"\n  Segment {segments_done + i // DEFAULT_WINDOW + 1} ({len(seg)} docs):")
            for doc in seg:
                print(f"    {doc.name}")
        return 0

    client = AnthropicChatClient(model=effective_model, api_key=config.anthropic_api_key)
    system_content = [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}]

    def _calc_cost(reply) -> float:
        if not rates:
            return 0.0
        in_rate, out_rate = rates
        return (
            reply.input_tokens * in_rate / 1_000_000
            + reply.output_tokens * out_rate / 1_000_000
            + reply.cache_creation_tokens * in_rate * 1.25 / 1_000_000
            + reply.cache_read_tokens * in_rate * 0.10 / 1_000_000
        )

    def _call_synthesis(messages: list[dict]) -> object:
        return client.generate_reply(
            system_content, messages,
            on_delta=_print_reply_delta,
            reasoning_effort="medium",
        )

    def _write_output(header: str, text: str) -> None:
        if no_write:
            return
        entry = f"\n# {header}\n\n{text}\n\n---\n"
        with out_path.open("a", encoding="utf-8") as fh:
            fh.write(entry)

    def _record_cost(reply) -> None:
        cost = _calc_cost(reply)
        state.cumulative_cost += cost
        print(f"  {_format_cost(cost)}  (total {_format_total(state.cumulative_cost)})")

    # Process next segment.
    processed_segment = False
    if state.doc_index < total:
        segment_docs = all_docs[state.doc_index:state.doc_index + DEFAULT_WINDOW]
        n = state.doc_index // DEFAULT_WINDOW + 1
        after_doc = state.doc_index + len(segment_docs)
        print(f"\n>>> Segment {n} (docs {state.doc_index + 1}–{after_doc} of {total}) <<<\n")

        messages = build_interim_messages(segment_docs, interim_prompt)
        reply = _call_synthesis(messages)
        print()
        _record_cost(reply)

        interim_header = f"Segment {n} (docs {state.doc_index + 1}–{after_doc})"
        _write_output(interim_header, reply.text)
        state.interims.append({"header": interim_header, "text": reply.text, "after_doc": after_doc})
        state.doc_index = after_doc
        processed_segment = True
        save_review_state(state, state_path)

    # Doc reviews + final assessment — only after all segments are complete.
    if not state.final_done and state.doc_index >= total and not processed_segment:
        from mdc.library import load_entries

        entries = load_entries(lib_path)
        title_to_path = {e.title: lib_path / Path(e.rel_path) for e in entries}
        known_titles = list(title_to_path.keys())

        include_path = lib_path / REVIEW_INCLUDE_FILENAME

        if not include_path.exists():
            # Generate starting list from interims and pause for user to edit.
            interim_texts = [e["text"] for e in state.interims]
            mentioned_titles = extract_mentioned_titles(interim_texts, known_titles)
            include_path.write_text(generate_include_list(mentioned_titles), encoding="utf-8")
            print(f"\nInclude list written to {include_path.name} ({len(mentioned_titles)} titles).")
            print("Edit it, then run 'mdc review' again to proceed with the final assessment.")
            return 0

        titles = load_include_list(include_path)
        unmatched = [t for t in titles if t not in title_to_path]
        if unmatched:
            print(f"\nError: {len(unmatched)} title(s) in {include_path.name} not found in library:")
            for t in unmatched:
                print(f"  - {t}")
            return 1

        mentioned_docs = [
            title_to_path[t] for t in titles
            if t in title_to_path and title_to_path[t].exists()
        ]
        included_filenames = {d.name for d in mentioned_docs}

        # Drop saved reviews no longer in the include list.
        dropped = [r for r in state.doc_reviews if r["filename"] not in included_filenames]
        if dropped:
            print(f"Dropping {len(dropped)} saved review(s) no longer in include list.")
            state.doc_reviews = [r for r in state.doc_reviews if r["filename"] in included_filenames]
            save_review_state(state, state_path)

        cached = {r["filename"]: r for r in state.doc_reviews}
        remaining_docs = [d for d in mentioned_docs if d.name not in cached]
        done = len(mentioned_docs) - len(remaining_docs)

        print(f"\n>>> Reviewing {len(mentioned_docs)} mentioned documents <<<\n")
        if done:
            print(f"  {done} already reviewed, resuming from doc {done + 1}.\n")

        for doc_path in remaining_docs:
            date = doc_path.name[:10] if len(doc_path.name) > 10 and doc_path.name[4] == "-" else ""
            title = extract_doc_heading(doc_path)
            label = f'"{title}" ({date})' if date else f'"{title}"'
            print(f"\n[{len(cached) + 1}/{len(mentioned_docs)}] {label}")
            review_reply = client.generate_reply(
                system_content,
                build_doc_review_messages(doc_path, title_to_path),
                on_delta=_print_reply_delta,
                reasoning_effort="none",
            )
            print()
            _record_cost(review_reply)
            entry = {"filename": doc_path.name, "label": label, "text": review_reply.text}
            state.doc_reviews.append(entry)
            cached[doc_path.name] = entry
            save_review_state(state, state_path)

        reviews_path = lib_path / REVIEWS_FILENAME
        reviews_path.write_text(build_reviews_md(state, entries), encoding="utf-8")
        print(f"Wrote {REVIEWS_FILENAME}.")

        all_reviews = [cached[d.name]["text"] for d in mentioned_docs if d.name in cached]
        selected_reviews = "\n\n---\n\n".join(
            f"{cached[d.name]['label']}:\n\n{cached[d.name]['text']}"
            for d in mentioned_docs if d.name in cached
        ) if all_reviews else None

        included_titles = set(titles)
        manifest_summaries = build_manifest_summaries(entries, included_titles)
        summary_count = len([e for e in entries if e.title not in included_titles and e.summary])
        print(f"  Including summaries for {summary_count} additional documents.\n")

        if not no_write:
            print(f"\n>>> Final assessment <<<\n")
            messages = build_final_messages(state.interims, final_prompt_text, selected_reviews, manifest_summaries)
            reply = _call_synthesis(messages)
            print()
            _record_cost(reply)
            state.final_text = reply.text
            state.final_done = True
            save_review_state(state, state_path)
            out_path.write_text(build_assessments_md(state, include_toc=include_toc), encoding="utf-8")

    if state.doc_index < total:
        remaining = total - state.doc_index
        remaining_segs = (remaining + DEFAULT_WINDOW - 1) // DEFAULT_WINDOW
        print(f"\nPaused. {remaining} docs remaining ({remaining_segs} segments). Run again to continue.")
        print(f"Cumulative cost: {_format_total(state.cumulative_cost)}.")
    else:
        print(f"\nReview complete. Total cost: {_format_total(state.cumulative_cost)}.")
    if not no_write:
        print(f"Output: {out_path}")
    return 0


def _run_reply_watch(
    path: Path,
    model: str | None = None,
    reasoning_effort: str | None = None,
    verbose: bool = False,
    web_search: bool = False,
    library_path: str | None = None,
) -> int:
    config = load_config()
    effective_model = model or config.model
    if not effective_model:
        print("Error: no model specified. Pass --model or set 'model' in config.toml.")
        return 1
    assistant_name = _default_assistant_name(effective_model)

    def noop(_msg: str) -> None:
        pass

    while True:
        try:
            text = _read_file(path)
            transcript = parse_transcript(text, assistant_name=assistant_name)
        except (TranscriptError, FileNotFoundError, OSError):
            time.sleep(1)
            continue

        if not transcript.pending:
            time.sleep(1)
            continue

        print("Change detected, replying...", flush=True)
        try:
            if effective_model.startswith("claude-"):
                reply_text = _reply_anthropic(transcript, config, path, effective_model,
                                              reasoning_effort=reasoning_effort,
                                              verbose=verbose, status=noop,
                                              web_search=web_search)
            elif effective_model.startswith("ollama/"):
                reply_text = _reply_ollama(transcript, config, path, effective_model,
                                           verbose=verbose, status=noop)
            else:
                reply_text = _reply_openai(transcript, config, path, effective_model,
                                           reasoning_effort=reasoning_effort,
                                           verbose=verbose, status=noop)

            if not reply_text.endswith("\n"):
                sys.stdout.write("\n")
            sys.stdout.flush()

            _lib = Path(library_path).expanduser().resolve() if library_path else config.library_path
            _rev_dir = (_lib / "REVISIONS") if _lib else None
            _save_reply(path, text, reply_text, assistant_name, transcript.pending_turn.heading, revisions_dir=_rev_dir)
            if _rev_dir:
                _prune_revisions(_rev_dir, config.revision_retention_days)
            print("OK: reply appended.", flush=True)
        except Exception as exc:
            print(f"Error: {exc}", flush=True)

        time.sleep(1)


def run_reply(
    path: Path,
    model: str | None = None,
    reasoning_effort: str | None = None,
    verbose: bool = False,
    watch: bool = False,
    library: bool = False,
    terms: list[str] | None = None,
    strict: bool = False,
    web_search: bool = False,
    library_path: str | None = None,
) -> int:
    if _require_md(path):
        return 1

    # If a chat companion exists, reply there instead of the primary document.
    chat = path.with_suffix(".chat.md")
    if chat.exists():
        path = chat

    if watch:
        return _run_reply_watch(path, model=model, reasoning_effort=reasoning_effort, verbose=verbose, web_search=web_search, library_path=library_path)

    def status(msg: str) -> None:
        if verbose:
            print(msg, flush=True)

    status(f"Reading transcript from {path}...")
    text = _read_file(path)
    config = load_config()
    effective_model = model or config.model
    if not effective_model:
        print("Error: no model specified. Pass --model or set 'model' in config.toml.")
        return 1
    assistant_name = _default_assistant_name(effective_model)

    from mdc.library import is_library_transcript
    if not is_library_transcript(text, config.user_names, config.llm_names):
        print(f"Error: '{path}' is not a recognized transcript. Use 'mdc validate -t' to check transcript conditions.")
        return 1

    status("Validating transcript...")
    transcript = parse_transcript(text, assistant_name=assistant_name)
    if not transcript.pending:
        print("No pending human turn found. Nothing to do.")
        return 1

    if effective_model.startswith("claude-"):
        try:
            reply_text = _reply_anthropic(
                transcript, config, path, effective_model,
                reasoning_effort=reasoning_effort,
                verbose=verbose,
                status=status,
                library=library,
                terms=terms or [],
                strict=strict,
                web_search=web_search,
                library_path=library_path,
            )
        except _LibraryTermNotFoundError as exc:
            missing = ", ".join(f'"{t}"' for t in exc.terms)
            print(f"\nAborted: library term(s) not found: {missing}. Update KEYS.md and re-run.")
            return 1
    elif effective_model.startswith("ollama/"):
        reply_text = _reply_ollama(
            transcript, config, path, effective_model,
            verbose=verbose,
            status=status,
        )
    else:
        reply_text = _reply_openai(
            transcript, config, path, effective_model,
            reasoning_effort=reasoning_effort,
            verbose=verbose,
            status=status,
        )

    if not reply_text.endswith("\n"):
        sys.stdout.write("\n")
    sys.stdout.flush()
    status("Appending to transcript...")
    _lib = Path(library_path).expanduser().resolve() if library_path else config.library_path
    _rev_dir = (_lib / "REVISIONS") if _lib else None
    _save_reply(path, text, reply_text, assistant_name, transcript.pending_turn.heading, revisions_dir=_rev_dir)
    if _rev_dir:
        _prune_revisions(_rev_dir, config.revision_retention_days)
    status(f"Appended one reply to {path}.")
    return 0



def _reply_anthropic(
    transcript,
    config,
    path: Path,
    model: str,
    reasoning_effort: str | None,
    verbose: bool,
    status,
    library: bool = False,
    terms: list[str] | None = None,
    strict: bool = False,
    web_search: bool = False,
    library_path: str | None = None,
) -> str:
    from mdc.anthropic_client import AnthropicChatClient
    from mdc.library import LIBRARY_TOOLS, _get_summary, lookup_term, read_document, resolve_title

    tools = None
    tool_executor = None
    library_context = None

    if library:
        _raw_lib = Path(library_path).expanduser().resolve() if library_path else config.library_path
        if not _raw_lib or not _raw_lib.is_dir():
            raise ValueError("--library requires library_path to be set in config or via --lib.")
        lib = _raw_lib
        tools = LIBRARY_TOOLS

        try:
            exclude = path.resolve().relative_to(lib.resolve()).as_posix()
        except ValueError:
            exclude = None

        preloaded: list[str] = []
        for term in (terms or []):
            result = lookup_term(lib, term, exclude=exclude)
            if result.startswith("Term not found"):
                status(f"! library term not found: \"{term}\"")
            else:
                preloaded.append(result)

        related_summaries: list[str] = []
        for raw_title in transcript.related:
            rel_path = resolve_title(lib, raw_title)
            if rel_path is None:
                status(f"! related document not found: \"{raw_title}\"")
            else:
                related_summaries.append(_get_summary(lib, rel_path, exclude=exclude))

        library_tools_prompt = (
            "You have access to the Personal Library — a collection of the user's own writings. "
            "To find relevant material:\n"
            "1. Call lookup_term with a relevant index term. It returns matching Personal Library documents with summaries and related terms.\n"
            "2. Call read_document when you need the full text of a specific Personal Library document.\n"
            "Follow the Related terms from each lookup to discover adjacent material, and look up all "
            "plausibly relevant terms before composing your reply. "
            "Documents have dates and views may evolve or be superseded; prefer more recent documents "
            "when there is tension between them, and flag apparent contradictions to the user.\n"
            "Only include a Personal Library document in your reply if you have called read_document on it. "
            "List Personal Library documents under '## Related' using the format '| *Exact Title*' "
            "(the section implies the source; no date, author, or other annotation). "
            "List any newly cited works under '## References' — additions only; do not repeat works already in the accumulated references. "
            "Do not insert a horizontal rule before these sections."
        )
        library_context = library_tools_prompt
        if preloaded:
            library_context += "\n\nPre-looked-up Personal Library terms:\n\n" + "\n\n".join(preloaded)
        if related_summaries:
            library_context += "\n\nThe following Personal Library documents are already known to be relevant to this transcript. If the conversation asks you to read them, call read_document on each before composing your reply. Each summary lists the document's own Related documents — these reflect the author's own judgment of relevance and are a reliable starting point for following a reference chain; call read_document on any that seem pertinent. Use lookup_term to cast a wider net via semantic indexing, which is more thorough but less targeted:\n\n" + "\n\n".join(related_summaries)

        missing_terms: list[str] = []

        def tool_executor(tool_name: str, tool_input: dict[str, object]) -> str:
            if tool_name == "lookup_term":
                term = str(tool_input.get("term", ""))
                result = lookup_term(lib, term, exclude=exclude)
                if "Term not found" in result:
                    status(f"! library term not found: \"{term}\"")
                    missing_terms.append(term)
                else:
                    status(f"lookup_term: {term}")
                return result
            if tool_name == "read_document":
                path = str(tool_input.get("path", ""))
                result = read_document(lib, path, exclude=exclude)
                status(f"read_document: {Path(path).name}")
                return result
            return f"Unknown tool: {tool_name}"

        def post_batch() -> None:
            if missing_terms and strict:
                raise _LibraryTermNotFoundError(list(missing_terms))
            if missing_terms:
                terms_list = ", ".join(f'"{t}"' for t in missing_terms)
                print(f"\nWarning: library term(s) not found: {terms_list}. Update KEYS.md to add aliases.")

        status("Library tools active.")

    from mdc.edit_tools import EDIT_TOOL, build_edit_context, make_edit_executor, resolve_edit_targets

    edit_targets = resolve_edit_targets(path)
    if edit_targets:
        _rev_dir = (config.library_path / "REVISIONS") if config.library_path else None
        edit_exec = make_edit_executor(edit_targets, wrap_width=config.wrap_width, revisions_dir=_rev_dir)
        edit_context = build_edit_context(edit_targets, wrap_width=config.wrap_width, revisions_dir=_rev_dir)
        tools = (tools or []) + [EDIT_TOOL]
        prev_exec = tool_executor
        def tool_executor(name: str, inp: dict[str, object]) -> str:  # noqa: E306
            if name == "edit_file":
                return edit_exec(name, inp)
            return prev_exec(name, inp) if prev_exec else f"Unknown tool: {name}"
        library_context = (library_context or "") + ("\n\n" if library_context else "") + edit_context
        for t in edit_targets:
            status(f"Edit target: {t.name}")

    if web_search:
        tools = (tools or []) + [{"type": "web_search_20260209", "name": "web_search"}]
        status("Web search enabled.")

    lib_titles: dict[str, str] = {}
    if library and config.library_path:
        from mdc.library import load_entries as _load_entries
        lib_titles = {e.rel_path: e.title for e in _load_entries(config.library_path)}

    def _format_tool_annotation(tool_name: str, tool_input: dict[str, object]) -> str:
        if tool_name == "read_document":
            rel_path = str(tool_input.get("path", ""))
            title = lib_titles.get(rel_path, Path(rel_path).stem)
            return f"[read_document: {title}]"
        if tool_name == "lookup_term":
            return f"[lookup_term: {tool_input.get('term', '')}]"
        if tool_name == "edit_file":
            return f"[edit_file: {tool_input.get('path', '')}]"
        return f"[{tool_name}]"

    client = AnthropicChatClient(model=model, api_key=config.anthropic_api_key)

    assets_by_turn = collect_local_assets(transcript, path)
    has_binary_assets = any(
        a.kind in ("image", "pdf")
        for assets in assets_by_turn.values()
        for a in assets
    )
    cache_hit_assets: dict[object, object] = {}

    def build_inputs() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        cache_hit_assets.clear()

        def resolve_file_id(asset) -> str:
            resolved = client.ensure_asset_file(asset)
            if resolved.cache_hit:
                cache_hit_assets[asset.path] = asset
                status(f"Asset cache hit: {asset.raw_target}")
            else:
                status(f"Asset uploaded: {asset.raw_target}")
            return resolved.file_id

        return build_anthropic_input(
            transcript, config.system_prompt, path,
            library_context=library_context,
            resolve_file_id=resolve_file_id if has_binary_assets else None,
        )

    for assets in assets_by_turn.values():
        for asset in assets:
            if asset.kind not in ("image", "pdf"):
                status(f"Sending asset: {asset.raw_target}")

    system, messages = build_inputs()
    status(f"Requesting reply from Anthropic model '{model}'...")
    status("Streaming reply:")
    try:
        reply = client.generate_reply(
            system, messages,
            on_delta=_print_reply_delta,
            reasoning_effort=reasoning_effort,
            tools=tools,
            tool_executor=tool_executor,
            post_batch=post_batch if library else None,
            format_tool_annotation=_format_tool_annotation,
            use_files_api=has_binary_assets,
        )
    except Exception as exc:
        if not cache_hit_assets or not _is_retriable_anthropic_asset_error(exc):
            raise
        status("Cached Anthropic asset expired or was deleted; retrying with fresh upload(s)...")
        for asset in cache_hit_assets.values():
            client.invalidate_asset_file(asset)
        system, messages = build_inputs()
        status("Streaming reply:")
        reply = client.generate_reply(
            system, messages,
            on_delta=_print_reply_delta,
            reasoning_effort=reasoning_effort,
            tools=tools,
            tool_executor=tool_executor,
            post_batch=post_batch if library else None,
            format_tool_annotation=_format_tool_annotation,
            use_files_api=has_binary_assets,
        )
    if verbose:
        _print_anthropic_usage(model, reply)
    return reply.text


def _reply_ollama(
    transcript,
    config,
    path: Path,
    model: str,
    verbose: bool,
    status,
) -> str:
    from mdc.ollama_client import OllamaChatClient

    ollama_model = model.removeprefix("ollama/")
    client = OllamaChatClient(model=ollama_model, base_url=config.ollama_base_url)
    messages = build_chat_input(transcript, config.system_prompt, path)
    status(f"Requesting reply from Ollama model '{ollama_model}' at {config.ollama_base_url}...")
    status("Streaming reply:")
    reply = client.generate_reply(messages, on_delta=_print_reply_delta)
    return reply.text


def _reply_openai(
    transcript,
    config,
    path: Path,
    model: str,
    reasoning_effort: str | None,
    verbose: bool,
    status,
) -> str:
    from mdc.openai_client import OpenAIChatClient

    client = OpenAIChatClient(
        model=model,
        api_key=config.openai_api_key,
        reasoning_effort=reasoning_effort,
    )
    status(f"Requesting reply from OpenAI model '{model}'...")
    cache_hit_assets: dict[Path, object] = {}

    def build_messages() -> tuple[list[dict[str, object]], int, int]:
        asset_cache_hits = 0
        asset_cache_misses = 0
        cache_hit_assets.clear()

        def resolve_asset_file_id(asset) -> str:
            nonlocal asset_cache_hits, asset_cache_misses
            resolved = client.ensure_asset_file(asset)
            if resolved.cache_hit:
                asset_cache_hits += 1
                cache_hit_assets[asset.path] = asset
                status(f"Asset cache hit: {asset.raw_target}")
            else:
                asset_cache_misses += 1
                status(f"Asset cache miss: {asset.raw_target}")
            return resolved.file_id

        messages = build_response_input(
            transcript,
            config.system_prompt,
            path,
            resolve_file_id=resolve_asset_file_id,
        )
        if asset_cache_hits or asset_cache_misses:
            status(f"Resolved asset uploads: {asset_cache_hits} cache hit(s), {asset_cache_misses} cache miss(es).")
        return messages, asset_cache_hits, asset_cache_misses

    messages, asset_cache_hits, _asset_cache_misses = build_messages()
    status("Streaming reply:")
    try:
        reply = client.generate_reply(messages, on_delta=_print_reply_delta)
    except Exception as exc:
        if not asset_cache_hits or not cache_hit_assets or not client.is_retriable_asset_error(exc):
            raise

        status("Cached OpenAI asset expired or was deleted; retrying with fresh upload(s)...")
        for asset in cache_hit_assets.values():
            client.invalidate_asset_file(asset)

        messages, _, _ = build_messages()
        status("Streaming reply:")
        reply = client.generate_reply(messages, on_delta=_print_reply_delta)

    if verbose:
        _print_openai_usage(model, reply)
    return reply.text


def _save_reply(path: Path, text: str, reply_text: str, assistant_name: str, heading: str, revisions_dir: Path | None = None) -> None:
    from mdc.edit_tools import _BACKUP_RE
    body_with_refs, new_related = extract_related(reply_text)
    body, new_refs = extract_references(body_with_refs)
    body = _upgrade_reply_headings(body)
    updated = append_assistant_reply(text, body, assistant_name=assistant_name, heading=heading)
    updated_t = parse_transcript(updated, assistant_name=assistant_name)
    merged = insert_references(list(updated_t.references), new_refs)
    if merged != list(updated_t.references):
        updated = update_references_section(updated, merged)
    if new_related:
        existing_related = list(parse_transcript(updated, assistant_name=assistant_name).related)
        merged_related = existing_related + [r for r in new_related if r not in set(existing_related)]
        updated = update_related_section(updated, merged_related)
    stem, suffix = path.stem, path.suffix
    rev_dir = revisions_dir if revisions_dir is not None else path.parent
    highest = max(
        (int(m.group(2)) for s in (rev_dir.iterdir() if rev_dir.is_dir() else ())
         if (m := _BACKUP_RE.match(s.name)) and m.group(1) == stem and m.group(3) == suffix),
        default=0,
    )
    rev_dir.mkdir(parents=True, exist_ok=True)
    (rev_dir / f"{stem}--{highest + 1}{suffix}").write_text(text, encoding="utf-8")
    updated, _ = fix_section_spacing(updated)
    path.write_text(updated, encoding="utf-8")


def _prune_revisions(rev_dir: Path, days: int) -> None:
    if not rev_dir.is_dir() or days <= 0:
        return
    import time as _time
    cutoff = _time.time() - days * 86400
    for entry in rev_dir.iterdir():
        if entry.is_file() and entry.stat().st_mtime < cutoff:
            entry.unlink()


def _is_retriable_anthropic_asset_error(exc: Exception) -> bool:
    combined = str(exc).lower()
    return "file" in combined and any(
        phrase in combined
        for phrase in ("not found", "does not exist", "no such file", "invalid file", "unknown file", "expired")
    )


def _upgrade_reply_headings(text: str) -> str:
    """Promote any # or ## headings in the reply to ### to avoid colliding with turn delimiters.

    ## References and ## Related are exempt — they are structural section headings that must
    remain at ## to be recognized and merged by the transcript parser.
    """
    def promote(m: re.Match) -> str:
        hashes, rest = m.group(1), m.group(2)
        if rest.strip() in ("References", "Related"):
            return m.group(0)
        return "###" + rest

    return re.sub(r"^(#{1,2})(?!#)(.*)", promote, text, flags=re.MULTILINE)


def _print_reply_delta(chunk: str) -> None:
    sys.stdout.write(chunk)
    sys.stdout.flush()


def _lookup_price(model: str, table: dict[str, tuple[float, float]]) -> tuple[float, float] | None:
    for prefix, rates in table.items():
        if model.startswith(prefix):
            return rates
    return None


def _format_cost(dollars: float) -> str:
    return f"${dollars:.5f}"


def _format_total(dollars: float) -> str:
    return f"${dollars:.2f}"


def _print_anthropic_usage(model: str, reply) -> None:
    rates = _lookup_price(model, _ANTHROPIC_PRICING)
    parts = [
        f"in={reply.input_tokens:,}",
        f"out={reply.output_tokens:,}",
    ]
    if reply.cache_creation_tokens:
        parts.append(f"cache_write={reply.cache_creation_tokens:,}")
    if reply.cache_read_tokens:
        parts.append(f"cache_read={reply.cache_read_tokens:,}")

    if rates:
        in_rate, out_rate = rates
        cost = (
            reply.input_tokens * in_rate / 1_000_000
            + reply.output_tokens * out_rate / 1_000_000
            + reply.cache_creation_tokens * in_rate * 1.25 / 1_000_000
            + reply.cache_read_tokens * in_rate * 0.10 / 1_000_000
        )
        parts.append(_format_cost(cost))
    else:
        parts.append("cost=unknown model")

    print(f"\n  {' | '.join(parts)}")


def _print_openai_usage(model: str, reply) -> None:
    if reply.input_tokens is None:
        return
    rates = _lookup_price(model, _OPENAI_PRICING)
    parts = [f"in={reply.input_tokens:,}", f"out={reply.output_tokens:,}"]
    if rates:
        in_rate, out_rate = rates
        cost = reply.input_tokens * in_rate / 1_000_000 + reply.output_tokens * out_rate / 1_000_000
        parts.append(_format_cost(cost))
    else:
        parts.append("cost=unknown model")
    print(f"\n  {' | '.join(parts)}")
