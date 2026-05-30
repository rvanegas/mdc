from __future__ import annotations

import argparse
import datetime
import difflib
import os
import re
import subprocess
import sys
from pathlib import Path

from mdc.config import load_config
from mdc.form import check_file, check_global_issues, fix_object_replacement, fix_rtl_spans, fix_section_spacing, fix_title_section, slugify
from mdc.transcript import TranscriptError, parse_transcript
from mdc.assets import collect_local_assets

from mdc.cmd_diff import run_diff, run_files_ls
from mdc.cmd_argue import run_argue
from mdc.cmd_pdf import run_pdf
from mdc.cmd_index import run_index
from mdc.cmd_reply import run_reply, _LibraryTermNotFoundError
from mdc.cmd_review import run_review


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

# Import wrap_paragraphs for backward compatibility (used by tests and external code)
from mdc.text_utils import wrap_paragraphs, _upgrade_reply_headings, _parse_index_reply  # noqa: F401


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
                pass2=args.pass2,
                final=args.final,
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
        "--pass2",
        action="store_true",
        default=False,
        help="With --evaluate: generate a second-pass assessment using sibling pass1 assessments as context.",
    )
    review_parser.add_argument(
        "--final",
        action="store_true",
        default=False,
        help="Generate the final cross-theme assessment from all pass2 assessments.",
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

