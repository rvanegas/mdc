from __future__ import annotations

import argparse
import datetime
import difflib
import re
import shutil
import subprocess
import sys
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
from mdc.config import _default_assistant_name, load_config
from mdc.form import check_file, fix_object_replacement, fix_rtl_spans, fix_title_section, slugify
from mdc.transcript import (
    TranscriptError,
    append_assistant_reply,
    extract_references,
    insert_references,
    parse_transcript,
    update_references_section,
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


def main(argv: list[str] | None = None) -> int:
    from mdc.config import DEFAULT_CONFIG_PATH, DEFAULT_SYSTEM_PROMPT_PATH, _write_default_config, _write_default_system_prompt
    if not DEFAULT_CONFIG_PATH.exists():
        _write_default_config(DEFAULT_CONFIG_PATH)
    if not DEFAULT_SYSTEM_PROMPT_PATH.exists():
        _write_default_system_prompt(DEFAULT_SYSTEM_PROMPT_PATH)

    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "index":
            return run_index(
                library_path=args.library_path,
                refs_only=args.refs_only_all,
            )
        if args.command == "relate":
            return run_relate(
                library_path=args.library_path,
                model=args.model,
                reprocess_all=args.all,
            )
        if args.command == "new":
            return run_new(args.title)
        if args.command == "check":
            return run_check(Path(args.path))
        if args.command == "validate":
            return run_validate([Path(p) for p in args.paths], verbose=args.verbose)
        if args.command == "fix":
            return run_fix([Path(p) for p in args.paths])
        if args.command == "reply":
            if args.terms and not args.library:
                print("Error: -t/--term requires -l/--library.")
                return 1
            return run_reply(
                Path(args.path),
                model=args.model,
                reasoning_effort=args.reasoning_effort,
                verbose=args.verbose,
                watch=args.watch,
                library=args.library,
                terms=args.terms,
            )
        if args.command == "config":
            return run_config()
        if args.command == "pdf":
            return run_pdf(Path(args.path), quiet=args.quiet)
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
        description="Work with mdform-format markdown conversation files.",
    )
    subparsers = parser.add_subparsers(dest="command")

    # relate
    relate_parser = subparsers.add_parser(
        "relate",
        help="Build or update semantic relations between library index terms using an AI model.",
    )
    relate_parser.add_argument(
        "library_path",
        nargs="?",
        default=None,
        help="Path to library directory (overrides config file).",
    )
    relate_parser.add_argument(
        "-m", "--model",
        default=None,
        help="Model to use (e.g. claude-haiku-4-5-20251001). Overrides config file.",
    )
    relate_parser.add_argument(
        "--all",
        action="store_true",
        default=False,
        help="Reprocess all terms, not just new or unprocessed ones.",
    )

    # index
    index_parser = subparsers.add_parser(
        "index",
        help="Build or update the library document index using an AI model for summaries.",
    )
    index_parser.add_argument(
        "library_path",
        nargs="?",
        default=None,
        help="Path to library directory (overrides config file).",
    )
    index_parser.add_argument(
        "--refs-only-all",
        action="store_true",
        default=False,
        help="Extract references from all documents without calling any AI model.",
    )

    # new
    new_parser = subparsers.add_parser(
        "new",
        help="Create a new mdform conversation file in the current directory.",
    )
    new_parser.add_argument("title", help="Title of the conversation.")

    # check
    check_parser = subparsers.add_parser(
        "check",
        help="Validate transcript structure and report reply status.",
    )
    check_parser.add_argument("path", help="Path to the markdown transcript.")

    # validate
    validate_parser = subparsers.add_parser(
        "validate",
        help="Run all 12 mdform format rules on one or more files.",
    )
    validate_parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        default=False,
        help="Show warnings in addition to errors.",
    )
    validate_parser.add_argument("paths", nargs="+", metavar="file.md")

    # fix
    fix_parser = subparsers.add_parser(
        "fix",
        help="Auto-fix correctable mdform violations (modifies files in place).",
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
        "-w", "--watch",
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

    # config
    subparsers.add_parser(
        "config",
        help="Show configuration and data file locations.",
    )

    return parser


def run_config() -> int:
    from mdc.config import DEFAULT_CONFIG_PATH, DEFAULT_SYSTEM_PROMPT_PATH, _cache_dir, _state_dir
    print(f"Config file:   {DEFAULT_CONFIG_PATH}")
    print(f"System prompt: {DEFAULT_SYSTEM_PROMPT_PATH}")
    print(f"State dir:     {_state_dir}")
    print(f"Cache dir:     {_cache_dir}")
    return 0


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
        f"- SUMMARY must be {s_target} describing the document's actual subject matter.\n"
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


def run_index(library_path: str | None, refs_only: bool = False) -> int:
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
                messages = [{"role": "user", "content": _index_prompt(content, word_count)}]
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
                messages = [{"role": "user", "content": _index_prompt(content, word_count)}]
                reply = client.generate_reply(messages)
                return _parse_index_reply(reply.text)

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

    from mdc.library import load_terms, prune_relations
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
        "- Are frequently discussed together in the literature\n\n"
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


def run_relate(library_path: str | None, model: str | None, reprocess_all: bool = False) -> int:
    import random
    from mdc.library import load_relations, load_terms, prune_relations, save_relations

    config = load_config()

    raw_path = library_path or (str(config.library_path) if config.library_path else None)
    if not raw_path:
        print("Error: no library path specified. Pass a path or set 'library_path' in config.")
        return 1

    lib_path = Path(raw_path).expanduser().resolve()
    if not lib_path.is_dir():
        print(f"Error: '{lib_path}' is not a directory.")
        return 1

    all_terms = sorted(load_terms(lib_path))
    if not all_terms:
        print("No indexed terms found. Run 'mdc index' first.")
        return 1

    effective_model = model or config.index_model
    total_cost = 0.0
    last_cost: list[float] = [0.0]

    if effective_model.startswith("claude-"):
        from mdc.anthropic_client import AnthropicChatClient
        client = AnthropicChatClient(model=effective_model, api_key=config.anthropic_api_key)
        rates = _lookup_price(effective_model, _ANTHROPIC_PRICING)

        def call_model(prompt: str) -> str:
            nonlocal total_cost
            reply = client.generate_reply("You are a library indexing assistant.", [{"role": "user", "content": prompt}])
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
            return reply.text

    else:
        from mdc.ollama_client import OllamaChatClient
        ollama_model = effective_model.removeprefix("ollama/")
        client = OllamaChatClient(model=ollama_model, base_url=config.ollama_base_url)

        def call_model(prompt: str) -> str:
            reply = client.generate_reply([{"role": "user", "content": prompt}])
            return reply.text

    relations = load_relations(lib_path)

    # Prune stale terms before doing anything else.
    all_terms_set = set(all_terms)
    stale = {t for t in relations if t not in all_terms_set}
    if stale:
        prune_relations(lib_path, stale)
        relations = load_relations(lib_path)
        print(f"Pruned {len(stale)} removed term(s) from relations.")

    # Assign stable numeric IDs sorted case-insensitively.
    id_to_term: dict[int, str] = {i + 1: t for i, t in enumerate(all_terms)}

    # Select terms to process.
    if reprocess_all:
        to_process = all_terms_set
    else:
        new_terms = [t for t in all_terms if t not in relations]
        to_process = set(new_terms)
        for t in new_terms:
            to_process.update(relations.get(t, []))
        to_process &= all_terms_set  # guard against stale entries in relations values

    if not to_process:
        print("Nothing to do — all terms already related. Use --all to reprocess.")
        return 0

    term_to_id: dict[str, int] = {t: i for i, t in id_to_term.items()}
    process_ids = [term_to_id[t] for t in to_process]
    random.shuffle(process_ids)
    batch_size = 20
    batches = [process_ids[i:i + batch_size] for i in range(0, len(process_ids), batch_size)]
    total_batches = len(batches)

    print(f"Building relations for {len(to_process)} of {len(all_terms)} terms "
          f"in {total_batches} batches using model '{effective_model}'...")

    for i, batch_ids in enumerate(batches, 1):
        cost_str = f"  (total {_format_total(total_cost)})" if total_cost else ""
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

    total_str = f"  Total cost: {_format_total(total_cost)}." if total_cost else ""
    print(f"\nRelations written for {len(relations)} terms.{total_str}")
    return 0


def run_new(title: str) -> int:
    today = datetime.date.today().isoformat()
    filename = f"{today}-{slugify(title)}.md"
    path = Path(filename)
    if path.exists():
        print(f"Error: '{filename}' already exists.")
        return 1
    path.write_text(f"\n# {title}\n{today}\n\n## Prompt\n\n", encoding="utf-8")
    print(filename)
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


def run_validate(paths: list[Path], verbose: bool = False) -> int:
    any_errors = False
    for path in paths:
        if _require_md(path):
            any_errors = True
            continue
        errs, warns = check_file(path)
        visible_warns = warns if verbose else []
        if errs or visible_warns:
            print(f"{path}:")
            for e in errs:
                print(f"  error: {e}")
            for w in visible_warns:
                print(f"  warning: {w}")
            if errs:
                any_errors = True
        else:
            print(f"{path}: OK")
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
        applied = orc_applied + rtl_applied + title_applied

        if applied:
            new_text = "\n".join(new_lines)
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

        errs, warns = check_file(path)

        if not applied and not errs and not warns:
            print(f"{path}: OK")
        elif errs or warns:
            if not applied:
                print(f"{path}:")
            for e in errs:
                print(f"  error: {e}")
            for w in warns:
                print(f"  warning: {w}")

        if errs:
            any_errors = True

    return 1 if any_errors else 0


def run_pdf(path: Path, quiet: bool = False) -> int:
    if not path.exists():
        print(f"Error: '{path}' does not exist.")
        return 1
    if _require_md(path):
        return 1

    output = path.with_suffix(".pdf")
    result = subprocess.run(
        ["pandoc", str(path), "-o", str(output),
         "-V", "geometry:margin=1in", "-V", "fontsize=11pt"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"pandoc error:\n{result.stderr}", file=sys.stderr)
        return result.returncode

    if not quiet:
        if shutil.which("open"):
            subprocess.run(["open", str(output)])
        elif shutil.which("start"):
            subprocess.run(["start", str(output)], shell=True)
    return 0


def _run_reply_watch(
    path: Path,
    model: str | None = None,
    reasoning_effort: str | None = None,
    verbose: bool = False,
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

        try:
            if effective_model.startswith("claude-"):
                reply_text = _reply_anthropic(transcript, config, path, effective_model,
                                              reasoning_effort=reasoning_effort,
                                              verbose=verbose, status=noop)
            elif effective_model.startswith("ollama/"):
                reply_text = _reply_ollama(transcript, config, path, effective_model,
                                           verbose=verbose, status=noop)
            else:
                reply_text = _reply_openai(transcript, config, path, effective_model,
                                           reasoning_effort=reasoning_effort,
                                           verbose=verbose, status=noop)

            reply_text = _upgrade_reply_headings(reply_text)
            if not reply_text.endswith("\n"):
                sys.stdout.write("\n")
            sys.stdout.flush()

            body, new_refs = extract_references(reply_text)
            updated = append_assistant_reply(text, body, assistant_name=assistant_name,
                                             heading=transcript.pending_turn.heading)
            existing_refs = list(parse_transcript(updated, assistant_name=assistant_name).references)
            merged = insert_references(existing_refs, new_refs)
            if merged != existing_refs:
                updated = update_references_section(updated, merged)
            path.write_text(updated, encoding="utf-8")
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
) -> int:
    if _require_md(path):
        return 1
    if watch:
        return _run_reply_watch(path, model=model, reasoning_effort=reasoning_effort, verbose=verbose)

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

    reply_text = _upgrade_reply_headings(reply_text)
    if not reply_text.endswith("\n"):
        sys.stdout.write("\n")
    sys.stdout.flush()
    status("Appending to transcript...")
    body, new_refs = extract_references(reply_text)
    updated = append_assistant_reply(text, body, assistant_name=assistant_name, heading=transcript.pending_turn.heading)

    existing_refs = list(parse_transcript(updated, assistant_name=assistant_name).references)
    merged = insert_references(existing_refs, new_refs)
    if merged != existing_refs:
        updated = update_references_section(updated, merged)

    path.write_text(updated, encoding="utf-8")
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
) -> str:
    from mdc.anthropic_client import AnthropicChatClient
    from mdc.library import LIBRARY_TOOLS, get_summary, lookup_term, read_document

    tools = None
    tool_executor = None
    library_context = None

    if library:
        if not config.library_path or not config.library_path.is_dir():
            raise ValueError("--library requires library_path to be set in config.")
        lib = config.library_path
        tools = LIBRARY_TOOLS

        try:
            exclude = str(path.resolve().relative_to(lib.resolve()))
        except ValueError:
            exclude = None

        preloaded: list[str] = []
        for term in (terms or []):
            result = lookup_term(lib, term, exclude=exclude)
            if result.startswith("Term not found"):
                status(f"! library term not found: \"{term}\"")
            else:
                preloaded.append(result)

        library_tools_prompt = (
            "You have access to a library of the user's own writings. To find relevant material:\n"
            "1. Call lookup_term with a relevant index term. It returns matching documents and related terms.\n"
            "2. Call get_summary for documents that look relevant.\n"
            "3. Call read_document only when a summary confirms it is worth reading in full.\n"
            "Start by looking up index terms relevant to the user's question."
        )
        if preloaded:
            library_context = library_tools_prompt + "\n\nPre-looked-up library terms:\n\n" + "\n\n".join(preloaded)
        else:
            library_context = library_tools_prompt

        missing_terms: list[str] = []

        def tool_executor(tool_name: str, tool_input: dict[str, object]) -> str:
            if tool_name == "lookup_term":
                term = str(tool_input.get("term", ""))
                result = lookup_term(lib, term, exclude=exclude)
                if result.startswith("Term not found"):
                    status(f"! library term not found: \"{term}\"")
                    missing_terms.append(term)
                return result
            if tool_name == "get_summary":
                return get_summary(lib, str(tool_input.get("path", "")), exclude=exclude)
            if tool_name == "read_document":
                return read_document(lib, str(tool_input.get("path", "")), exclude=exclude)
            return f"Unknown tool: {tool_name}"

        def post_batch() -> None:
            if missing_terms:
                raise _LibraryTermNotFoundError(list(missing_terms))

        status("Library tools active.")

    client = AnthropicChatClient(model=model, api_key=config.anthropic_api_key)
    system, messages = build_anthropic_input(transcript, config.system_prompt, path, library_context=library_context)
    status(f"Requesting reply from Anthropic model '{model}'...")
    status("Streaming reply:")
    reply = client.generate_reply(
        system, messages,
        on_delta=_print_reply_delta,
        reasoning_effort=reasoning_effort,
        tools=tools,
        tool_executor=tool_executor,
        post_batch=post_batch if library else None,
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
            system_prompt,
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


def _upgrade_reply_headings(text: str) -> str:
    """Promote any ## headings in the reply to ### to avoid colliding with turn delimiters."""
    return re.sub(r"^##(?!#)", "###", text, flags=re.MULTILINE)


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

    print(f"  {' | '.join(parts)}")


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
    print(f"  {' | '.join(parts)}")
