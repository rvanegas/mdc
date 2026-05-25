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
                theme=args.theme,
                selection=args.selection,
                doc_start=args.doc_start,
                docs=args.docs,
                evaluate=args.evaluate,
                action=args.action,
                action_themes=args.action_themes,
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
        "--theme",
        default=None,
        metavar="NAME",
        help="Theme code or name (required for --docs and --assess).",
    )
    review_parser.add_argument(
        "--selection",
        action="store_true",
        default=False,
        help="Interactively classify documents into themes; updates THEMES.md.",
    )
    review_parser.add_argument(
        "--start",
        default=None,
        metavar="N",
        dest="doc_start",
        type=int,
        help="Begin --selection at document N (1-based).",
    )
    review_parser.add_argument(
        "--docs",
        action="store_true",
        default=False,
        help="Run individual doc reviews for all theme-assigned documents that lack one.",
    )
    review_parser.add_argument(
        "--evaluate",
        action="store_true",
        default=False,
        help="Synthesize individual document reviews into a thematic assessment.",
    )
    review_parser.add_argument(
        "action",
        nargs="?",
        default=None,
        metavar="ACTION",
        help="Subcommand: 'tokens' to show review token counts by theme.",
    )
    review_parser.add_argument(
        "action_themes",
        nargs="*",
        metavar="THEME",
        help="Theme codes or names for the action subcommand.",
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


def get_editor() -> str | None:
    return os.environ.get("MDC_EDITOR") or os.environ.get("EDITOR")


def run_edit(paths: list[Path]) -> int:
    editor_cmd = get_editor()
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
        paths = [chat_filename, document_filename]
    else:
        filename = f"{today}-{slug}.md"
        path = Path(filename)
        if path.exists():
            print(f"Error: '{filename}' already exists.")
            return 1
        path.write_text(f"\n# {title}\n{today}\n\n## Prompt\n\n", encoding="utf-8")
        print(filename)
        paths = [filename]
    editor_cmd = get_editor()
    if editor_cmd:
        subprocess.run([editor_cmd, *paths])
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


def run_review(library_path: str | None, reset: bool, theme: str | None = None, selection: bool = False, doc_start: int | None = None, docs: bool = False, evaluate: bool = False, action: str | None = None, action_themes: list[str] | None = None) -> int:
    import hashlib
    from mdc.config import _state_dir
    from mdc.anthropic_client import AnthropicChatClient
    from mdc.review import (
        _DEFAULT_DOC_REVIEW_SYSTEM_PROMPT,
        _DEFAULT_FINAL_PROMPT,
        _REVIEW_PROMPTS_DIR,
        build_doc_review_messages,
        build_final_messages,
        build_reviews_md,
        _resolve_related_docs,
        extract_doc_heading,
        load_prompt,
        load_review_state,
        build_themed_synthesis_messages,
        _DEFAULT_THEMED_SYNTHESIS_PROMPT,
        _demote_headings,
        parse_combinations,
        parse_themes_md,
        save_review_state,
        THEMES_FILENAME,
        write_themes_md,
    )
    from mdc.library import (
        ASSESSMENT_FILENAME,
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

    # --start: override resume logic, begin at this 1-based document index.
    forced_start: int | None = (doc_start - 1) if doc_start is not None else None

    themes_path = lib_path / THEMES_FILENAME

    if action == "tokens":
        if not themes_path.exists():
            print(f"Error: {THEMES_FILENAME} not found.")
            return 1
        all_themes_tok, doc_assignments_tok = parse_themes_md(themes_path)
        if not all_themes_tok:
            print(f"No themes defined in {THEMES_FILENAME}.")
            return 1

        global_state_tok = load_review_state(_state_dir / f"review-{path_hash}.json")
        review_by_filename_tok = {r["filename"]: r for r in global_state_tok.doc_reviews}

        from mdc.library import load_entries as _le_tok
        _entries_tok = _le_tok(lib_path)
        title_to_filename_tok = {e.title: Path(e.rel_path).name for e in _entries_tok}
        title_to_path_tok = {e.title: lib_path / e.rel_path for e in _entries_tok}

        def _review_is_current(title: str, r: dict) -> bool:
            reviewed_at = r.get("reviewed_at")
            if not reviewed_at:
                return True
            doc_path = title_to_path_tok.get(title)
            if not doc_path or not doc_path.exists():
                return True
            mtime = datetime.datetime.fromtimestamp(doc_path.stat().st_mtime, tz=datetime.timezone.utc)
            return mtime <= datetime.datetime.fromisoformat(reviewed_at)

        name_to_code_tok = {v.lower(): k for k, v in all_themes_tok.items()}

        def _theme_tokens(*codes: str) -> tuple[int, int, float]:
            seen: set[str] = set()
            reviewed = 0
            tokens = 0.0
            for code in codes:
                for t, t_codes in doc_assignments_tok.items():
                    if code not in t_codes or t in seen:
                        continue
                    seen.add(t)
                    fn = title_to_filename_tok.get(t)
                    r = review_by_filename_tok.get(fn) if fn else None
                    if r and _review_is_current(t, r):
                        reviewed += 1
                        tokens += len(r["text"]) / 4
            return reviewed, len(seen), tokens

        # Resolve requested theme codes.
        requested: list[str] = []
        for t in (action_themes or []):
            if t in all_themes_tok:
                requested.append(t)
            elif t.lower() in name_to_code_tok:
                requested.append(name_to_code_tok[t.lower()])
            else:
                print(f"Error: theme '{t}' not found in {THEMES_FILENAME}.")
                return 1

        def _print_table(rows: list, sum_row: tuple | None = None) -> None:
            all_rows = list(rows) + ([sum_row] if sum_row else [])
            name_w = max(len(r[0]) for r in all_rows)
            frac_w = max(len(f"{r[1]}/{r[2]}") for r in all_rows)
            tok_w  = max(len(f"~{r[3] / 1000:.0f}k") for r in all_rows)
            name_w = max(name_w, len("Theme"))
            frac_w = max(frac_w, len("Rev/Total"))
            tok_w  = max(tok_w, len("Tokens"))
            hdr = f"  {'Theme':<{name_w}}  {'Rev/Total':>{frac_w}}  {'Tokens':>{tok_w}}"
            sep = "  " + "-" * (len(hdr) - 2)
            print(hdr)
            print(sep)
            for name, reviewed, total, tokens in rows:
                frac = f"{reviewed}/{total}"
                tok  = f"~{tokens / 1000:.0f}k"
                print(f"  {name:<{name_w}}  {frac:>{frac_w}}  {tok:>{tok_w}}")
            if sum_row:
                name, reviewed, total, tokens = sum_row
                frac = f"{reviewed}/{total}"
                tok  = f"~{tokens / 1000:.0f}k"
                print(sep)
                print(f"  {name:<{name_w}}  {frac:>{frac_w}}  {tok:>{tok_w}}")

        if requested:
            total_reviewed = 0
            total_docs = 0
            total_tokens = 0.0
            names = []
            for code in requested:
                reviewed, total, tokens = _theme_tokens(code)
                total_reviewed += reviewed
                total_docs += total
                total_tokens += tokens
                names.append(all_themes_tok[code])
            rows = [(n, *_theme_tokens(c)) for n, c in zip(names, requested)]
            _print_table(rows, sum_row=("Sum", total_reviewed, total_docs, total_tokens))
        else:
            codes = sorted(all_themes_tok)
            rows = [(all_themes_tok[c], *_theme_tokens(c)) for c in codes]
            _print_table(rows)

            combos = parse_combinations(themes_path) if themes_path.exists() else []
            if combos:
                print()
                combo_rows = [
                    (f"{i}  {', '.join(ns)}", *_theme_tokens(*[name_to_code_tok[n.lower()] for n in ns if n.lower() in name_to_code_tok]))
                    for i, ns in enumerate(combos, 1)
                ]
                _print_table(combo_rows)

        return 0

    if selection:
        if not themes_path.exists():
            print(f"Error: {THEMES_FILENAME} not found. Define themes first.")
            return 1
        all_themes_ns, doc_assignments_ns = parse_themes_md(themes_path)
        if not all_themes_ns:
            print(f"No themes defined in {THEMES_FILENAME}.")
            return 1

        from mdc.library import load_entries as _le_ns
        entries_ns = _le_ns(lib_path)

        # Sync: add any library docs not yet in THEMES.md.
        _library_titles = {e.title for e in entries_ns}
        _added_sync = 0
        _removed_sync = 0
        for _e_sync in entries_ns:
            if _e_sync.title not in doc_assignments_ns:
                doc_assignments_ns[_e_sync.title] = set()
                _added_sync += 1
        for _t_sync in [t for t in doc_assignments_ns if t not in _library_titles]:
            del doc_assignments_ns[_t_sync]
            _removed_sync += 1
        if _added_sync or _removed_sync:
            write_themes_md(themes_path, doc_assignments_ns, list(doc_assignments_ns.keys()))
            if _added_sync:
                print(f"Added {_added_sync} new document(s) to {THEMES_FILENAME}.")
            if _removed_sync:
                print(f"Removed {_removed_sync} deleted document(s) from {THEMES_FILENAME}.")

        summary_by_title_ns = {e.title: e.summary for e in entries_ns}
        terms_by_title_ns = {e.title: e.terms for e in entries_ns}
        title_to_path_ns = {e.title: lib_path / Path(e.rel_path) for e in entries_ns}
        path_to_title_ns = {p: t for t, p in title_to_path_ns.items()}

        title_order_ns = list(doc_assignments_ns.keys())
        all_lib_paths_ns = [title_to_path_ns[t] for t in title_order_ns if t in title_to_path_ns]
        total_ns = len(all_lib_paths_ns)

        if forced_start is not None:
            start_idx_ns = forced_start
            all_lib_paths_ns = all_lib_paths_ns[start_idx_ns:]
        else:
            # Resume after the last doc with any assignment.
            start_idx_ns = 0
            for _j, _p in enumerate(all_lib_paths_ns):
                _t = path_to_title_ns.get(_p)
                if _t and doc_assignments_ns.get(_t):
                    start_idx_ns = _j + 1
            all_lib_paths_ns = all_lib_paths_ns[start_idx_ns:]

        def _read_char() -> str:
            if not sys.stdin.isatty():
                try:
                    return input().strip().lower()[:1]
                except EOFError:
                    return "\x00"
            import tty, termios
            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            try:
                tty.setraw(fd)
                ch = sys.stdin.read(1).lower()
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
            if ch in ("\x03", "\x04", "\x1b"):
                return "\x00"
            return ch

        def _render_prompt(codes: set) -> int:
            tokens = [f"{c}:{all_themes_ns[c]}" for c in sorted(all_themes_ns)]
            sep = "  "
            indent = "  "
            groups: list[list[str]] = []
            current: list[str] = []
            current_len = len(indent)
            for token in tokens:
                added = len(token) if not current else len(sep) + len(token)
                if current and current_len + added > 76:
                    groups.append(current)
                    current = [token]
                    current_len = len(indent) + len(token)
                else:
                    current.append(token)
                    current_len += added
            if current:
                groups.append(current)
            output = []
            for group in groups:
                parts = []
                for token in group:
                    c = token.split(":")[0]
                    parts.append(f"\033[7m{token}\033[m" if c in codes else token)
                output.append(indent + sep.join(parts))
            output.append(indent + "space: next  q: quit")
            for line in output:
                print(line)
            return len(output)

        if start_idx_ns:
            print(f"Resuming at {start_idx_ns + 1}/{total_ns}.\n")
        print(f"{total_ns} documents  —  a-z toggle  space/↵ next\n")

        changed = 0
        stop = False
        for i, doc_path in enumerate(all_lib_paths_ns, start_idx_ns + 1):
            title = path_to_title_ns.get(doc_path) or extract_doc_heading(doc_path)
            date_str = doc_path.name[:10] if len(doc_path.name) > 10 and doc_path.name[4] == "-" else ""
            header = f'[{i}/{total_ns}] "{title}"'
            if date_str:
                header += f" ({date_str})"
            print(header)

            summary = summary_by_title_ns.get(title, "")
            if summary:
                print(f"  {summary}")

            terms = terms_by_title_ns.get(title, ())
            if terms:
                for line in textwrap.wrap("; ".join(terms), width=76, initial_indent="  Terms: ", subsequent_indent="         "):
                    print(line)

            print()
            current = doc_assignments_ns.get(title, set()).copy()
            n_prompt = _render_prompt(current)

            while True:
                ch = _read_char()
                if ch == "\x00":
                    stop = True
                    break
                if ch in (" ", "\r", "\n", ""):
                    break
                if ch in all_themes_ns:
                    if ch in current:
                        current.discard(ch)
                    else:
                        current.add(ch)
                    print(f"\033[{n_prompt}A\033[J", end="", flush=True)
                    n_prompt = _render_prompt(current)

            old_codes = doc_assignments_ns.get(title, set())
            if current != old_codes:
                doc_assignments_ns[title] = current
                changed += 1

            print()
            if stop:
                break

        write_themes_md(themes_path, doc_assignments_ns, title_order_ns)
        print(f"{changed} change(s) written to {THEMES_FILENAME}.")
        return 0

    def _resolve_theme(
        theme_arg: str | None,
        all_themes: dict[str, str],
        combinations: list[list[str]],
    ) -> tuple[str, str, set[str]] | None:
        """Resolve --theme to (display_name, slug, set_of_theme_codes).

        Accepts: a single theme code, a single theme name, or a 1-based combination number.
        Returns None and prints an error if not found.
        """
        if not theme_arg:
            return None
        name_to_code = {v.lower(): k for k, v in all_themes.items()}
        if theme_arg in all_themes:
            name = all_themes[theme_arg]
            return name, name.replace(" ", "-").lower(), {theme_arg}
        if theme_arg.lower() in name_to_code:
            code = name_to_code[theme_arg.lower()]
            name = all_themes[code]
            return name, name.replace(" ", "-").lower(), {code}
        if theme_arg.isdigit():
            idx = int(theme_arg) - 1
            if 0 <= idx < len(combinations):
                names = combinations[idx]
                display = ", ".join(names)
                slug = f"combination-{theme_arg}"
                codes: set[str] = set()
                for n in names:
                    c = name_to_code.get(n.lower())
                    if c:
                        codes.add(c)
                return display, slug, codes
            print(f"Error: combination {theme_arg} out of range (1–{len(combinations)}).")
            return None
        print(f"Error: '{theme_arg}' not found as a theme or combination number in {THEMES_FILENAME}.")
        return None

    if docs:
        if not themes_path.exists():
            print(f"Error: {THEMES_FILENAME} not found. Run 'mdc review --selection' first.")
            return 1
        all_themes_d, doc_assignments_docs = parse_themes_md(themes_path)
        combinations_d = parse_combinations(themes_path)
        if theme:
            resolved_d = _resolve_theme(theme, all_themes_d, combinations_d)
            if resolved_d is None:
                return 1
            theme_name_d, _, theme_codes_d = resolved_d
            all_doc_titles = [t for t, codes in doc_assignments_docs.items() if codes & theme_codes_d]
            if not all_doc_titles:
                print(f"No documents assigned to {theme_name_d}. Run 'mdc review --selection' first.")
                return 1
        else:
            all_doc_titles = [t for t, codes in doc_assignments_docs.items() if codes]
            if not all_doc_titles:
                print("No documents assigned to any theme. Run 'mdc review --selection' first.")
                return 1

        global_state_path = _state_dir / f"review-{path_hash}.json"
        global_state = load_review_state(global_state_path)
        review_by_filename = {r["filename"]: r for r in global_state.doc_reviews}

        from mdc.library import load_entries as _le2
        _entries2 = _le2(lib_path)
        _title_to_path2 = {e.title: lib_path / Path(e.rel_path) for e in _entries2}
        unmatched = [t for t in all_doc_titles if t not in _title_to_path2]
        if unmatched:
            print(f"Warning: {len(unmatched)} title(s) not found in library — skipping.")

        def _themed_needs_review(doc_path: Path) -> bool:
            entry = review_by_filename.get(doc_path.name)
            if entry is None:
                return True
            reviewed_at = entry.get("reviewed_at")
            if not reviewed_at:
                return False
            mtime = datetime.datetime.fromtimestamp(doc_path.stat().st_mtime, tz=datetime.timezone.utc)
            return mtime > datetime.datetime.fromisoformat(reviewed_at)

        pending = [
            _title_to_path2[t] for t in all_doc_titles
            if t in _title_to_path2 and _themed_needs_review(_title_to_path2[t])
        ]
        if not pending:
            print(f"All {len(all_doc_titles)} selected documents already have up-to-date reviews.")
            print("  Next: mdc review --evaluate")
            return 0
        stale = sum(1 for t in all_doc_titles if t in _title_to_path2 and review_by_filename.get(_title_to_path2[t].name) and _themed_needs_review(_title_to_path2[t]))
        new_count = len(pending) - stale
        parts = []
        if new_count:
            parts.append(f"{new_count} new")
        if stale:
            parts.append(f"{stale} updated since last review")
        print(f"{len(pending)} of {len(all_doc_titles)} document(s) need reviews ({', '.join(parts)}).")
        doc_review_system_prompt = load_prompt(_REVIEW_PROMPTS_DIR / "doc-system.md", _DEFAULT_DOC_REVIEW_SYSTEM_PROMPT)
        client = AnthropicChatClient(model=effective_model, api_key=config.anthropic_api_key)
        rates = _lookup_price(effective_model, _ANTHROPIC_PRICING)
        cumulative_cost = 0.0
        title_to_path_all = {e.title: lib_path / Path(e.rel_path) for e in _entries2}
        cached = dict(review_by_filename)
        for i, doc_path in enumerate(pending, 1):
            title = extract_doc_heading(doc_path)
            date = doc_path.name[:10] if len(doc_path.name) > 10 and doc_path.name[4] == "-" else ""
            label = f'"{title}" ({date})' if date else f'"{title}"'
            print(f"\n[{i}/{len(pending)}] {label}")
            review_reply = client.generate_reply(
                [{"type": "text", "text": doc_review_system_prompt, "cache_control": {"type": "ephemeral"}}],
                build_doc_review_messages(doc_path, title_to_path_all,
                                          reviews={k: v["text"] for k, v in cached.items()}),
                on_delta=_print_reply_delta,
                reasoning_effort="none",
            )
            print()
            if rates:
                in_rate, out_rate = rates
                cost = (
                    review_reply.input_tokens * in_rate / 1_000_000
                    + review_reply.output_tokens * out_rate / 1_000_000
                    + review_reply.cache_creation_tokens * in_rate * 1.25 / 1_000_000
                    + review_reply.cache_read_tokens * in_rate * 0.10 / 1_000_000
                )
                cumulative_cost += cost
                print(f"  {_format_cost(cost)}  (total {_format_total(cumulative_cost)})")
            entry = {
                "filename": doc_path.name,
                "label": label,
                "text": review_reply.text,
                "reviewed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            }
            existing_idx = next((j for j, r in enumerate(global_state.doc_reviews) if r["filename"] == doc_path.name), None)
            if existing_idx is not None:
                global_state.doc_reviews[existing_idx] = entry
            else:
                global_state.doc_reviews.append(entry)
            cached[doc_path.name] = entry
            save_review_state(global_state, global_state_path)
        print("\nReviews complete. Run:")
        print("  mdc review --evaluate")
        return 0

    if evaluate:
        if not theme:
            print("Error: --assess requires --theme.")
            return 1
        if not themes_path.exists():
            print(f"Error: {THEMES_FILENAME} not found. Run 'mdc review --selection' first.")
            return 1
        all_themes_ev, doc_assignments_ev = parse_themes_md(themes_path)
        combinations_ev = parse_combinations(themes_path)
        resolved_ev = _resolve_theme(theme, all_themes_ev, combinations_ev)
        if resolved_ev is None:
            return 1
        theme_name_ev, theme_slug_ev, theme_codes_ev = resolved_ev
        all_evaluate_titles = [t for t, codes in doc_assignments_ev.items() if codes & theme_codes_ev]
        if not all_evaluate_titles:
            print(f"No documents assigned to {theme_name_ev}. Run 'mdc review --selection' first.")
            return 1

        global_state_path = _state_dir / f"review-{path_hash}.json"
        global_state = load_review_state(global_state_path)
        review_by_filename = {r["filename"]: r for r in global_state.doc_reviews}

        from mdc.library import load_entries as _le
        _entries = _le(lib_path)
        _title_to_path = {e.title: lib_path / Path(e.rel_path) for e in _entries}

        selected_reviews: list[dict] = []
        missing_reviews: list[str] = []
        for title in all_evaluate_titles:
            p = _title_to_path.get(title)
            if not p:
                print(f"Warning: '{title}' not found in library — skipping.")
                continue
            review = review_by_filename.get(p.name)
            if not review:
                missing_reviews.append(title)
                continue
            selected_reviews.append(review)

        if missing_reviews:
            print(f"Note: {len(missing_reviews)} document(s) not yet reviewed (will be omitted from synthesis):")
            for t in missing_reviews[:5]:
                print(f"  - {t}")
            if len(missing_reviews) > 5:
                print(f"  ... and {len(missing_reviews) - 5} more")
            print("  Run 'mdc review --docs' to generate missing reviews.")

        if not selected_reviews:
            print("No reviews available yet. Run 'mdc review --docs' first.")
            return 1

        total_review_tokens = sum(len(r["text"]) / 4 for r in selected_reviews)
        rates = _lookup_price(effective_model, _ANTHROPIC_PRICING)
        print(f"\nReviews: {len(selected_reviews)} documents  •  ~{total_review_tokens / 1000:.0f}k tokens")
        if rates:
            in_rate, out_rate = rates
            cost_est = total_review_tokens * in_rate / 1_000_000 + 1500 * out_rate / 1_000_000
            print(f"Cost estimate: ~${cost_est:.2f}")

        answer = input("\nProceed? [y/N] ").strip().lower()
        if answer != "y":
            return 0

        synthesis_prompt = load_prompt(_REVIEW_PROMPTS_DIR / "synthesis-theme.md", _DEFAULT_THEMED_SYNTHESIS_PROMPT)
        client = AnthropicChatClient(model=effective_model, api_key=config.anthropic_api_key)

        # Build collection map so the model can situate itself within the whole.
        combos_ev = parse_combinations(themes_path)
        name_to_code_ev2 = {v.lower(): k for k, v in all_themes_ev.items()}

        theme_names = sorted(all_themes_ev.values())
        lines = ["This collection is organized into the following themes:"]
        lines.append("  " + ", ".join(theme_names))
        if combos_ev:
            lines.append("\nAnd the following combinations:")
            for i, ns in enumerate(combos_ev, 1):
                lines.append(f"  {i}. {', '.join(ns)}")

        if theme and theme.isdigit():
            scope_label = f"combination {theme} ({theme_name_ev})"
        else:
            scope_label = theme_name_ev

        lines.append(
            f"\nYou are assessing {scope_label}. "
            "Where you observe a gap or absence, note it — but also consider whether it may be "
            "addressed in another theme, and say so explicitly if plausible."
        )
        collection_context = "\n".join(lines)

        print(f"\n>>> Thematic synthesis: {theme_name_ev} ({len(selected_reviews)} reviews) <<<\n")
        messages = build_themed_synthesis_messages(selected_reviews, synthesis_prompt, collection_context)
        reply = client.generate_reply([], messages, on_delta=_print_reply_delta, reasoning_effort="medium")
        print()

        if rates:
            in_rate, out_rate = rates
            cost = (
                reply.input_tokens * in_rate / 1_000_000
                + reply.output_tokens * out_rate / 1_000_000
                + reply.cache_creation_tokens * in_rate * 1.25 / 1_000_000
                + reply.cache_read_tokens * in_rate * 0.10 / 1_000_000
            )
            print(f"  {_format_cost(cost)}")

        assessment_filename = f"ASSESSMENT-{theme_slug_ev}.md"
        assessment_path = lib_path / assessment_filename
        assessment_path.write_text(
            sanitize_for_pandoc(f"# Assessment: {theme_name_ev}\n\n{_demote_headings(reply.text)}\n"),
            encoding="utf-8",
        )
        print(f"Wrote {assessment_filename}.")
        _render_review_pdfs(assessment_path)
        return 0

    # Final cross-theme assessment.
    final_prompt_text = load_prompt(_REVIEW_PROMPTS_DIR / "final.md", _DEFAULT_FINAL_PROMPT)

    assessment_files = sorted(lib_path.glob("ASSESSMENT-*.md"))
    if not assessment_files:
        print("No theme assessments found. Run 'mdc review --evaluate' for each theme first.")
        return 1

    assessments: list[tuple[str, str]] = []
    for af in assessment_files:
        text = af.read_text(encoding="utf-8")
        lines = text.splitlines()
        name = lines[0].lstrip("# ").strip() if lines and lines[0].startswith("#") else af.stem
        body = "\n".join(lines[1:]).strip()
        assessments.append((name, body))

    print(f"\n>>> Final assessment ({len(assessments)} theme assessments) <<<\n")
    client = AnthropicChatClient(model=effective_model, api_key=config.anthropic_api_key)
    messages = build_final_messages(assessments, final_prompt_text)
    reply = client.generate_reply(
        [], messages, on_delta=_print_reply_delta, reasoning_effort="medium"
    )
    print()

    rates = _lookup_price(effective_model, _ANTHROPIC_PRICING)
    if rates:
        in_rate, out_rate = rates
        cost = (
            reply.input_tokens * in_rate / 1_000_000
            + reply.output_tokens * out_rate / 1_000_000
            + reply.cache_creation_tokens * in_rate * 1.25 / 1_000_000
            + reply.cache_read_tokens * in_rate * 0.10 / 1_000_000
        )
        print(f"  {_format_cost(cost)}")

    assessment_path = lib_path / ASSESSMENT_FILENAME
    assessment_path.write_text(
        sanitize_for_pandoc(f"# Final Assessment\n\n{_demote_headings(reply.text)}\n"),
        encoding="utf-8",
    )
    print(f"Wrote {ASSESSMENT_FILENAME}.")
    _render_review_pdfs(assessment_path)
    return 0

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
