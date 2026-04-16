from __future__ import annotations

import argparse
import datetime
import difflib
import re
import shutil
import subprocess
import sys
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

from mdc.assets import build_anthropic_input, build_chat_input, build_response_input, collect_local_assets
from mdc.config import _default_assistant_name, load_config
from mdc.form import check_file, fix_rtl_spans, fix_title_section, slugify
from mdc.transcript import (
    TranscriptError,
    append_assistant_reply,
    extract_references,
    insert_references,
    parse_transcript,
    update_references_section,
)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "new":
            return run_new(args.title)
        if args.command == "check":
            return run_check(Path(args.path))
        if args.command == "validate":
            return run_validate([Path(p) for p in args.paths])
        if args.command == "fix":
            return run_fix([Path(p) for p in args.paths])
        if args.command == "reply":
            return run_reply(
                Path(args.path),
                model=args.model,
                reasoning_effort=args.reasoning_effort,
                text_verbosity=args.text_verbosity,
                verbose=args.verbose,
            )
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
        "-t", "--text-verbosity",
        choices=("low", "medium", "high"),
        default="medium",
        help="Set the model's output verbosity (default: medium).",
    )
    reply_parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        default=False,
        help="Print progress messages while fetching a reply.",
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

    return parser


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
    transcript = parse_transcript(path.read_text(encoding="utf-8"))
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


def run_validate(paths: list[Path]) -> int:
    any_errors = False
    for path in paths:
        errs = check_file(path)
        if errs:
            print(f"{path}:")
            for e in errs:
                print(f"  error: {e}")
            any_errors = True
        else:
            print(f"{path}: OK")
    return 1 if any_errors else 0


def run_fix(paths: list[Path]) -> int:
    any_errors = False
    for path in paths:
        raw = path.read_text(encoding="utf-8")
        rtl_lines, rtl_applied = fix_rtl_spans(raw.split("\n"))
        new_lines, title_applied = fix_title_section(rtl_lines)
        applied = rtl_applied + title_applied

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

        errs = check_file(path)

        if not applied and not errs:
            print(f"{path}: OK")
        elif errs:
            if not applied:
                print(f"{path}:")
            for e in errs:
                print(f"  error: {e}")

        if errs:
            any_errors = True

    return 1 if any_errors else 0


def run_pdf(path: Path, quiet: bool = False) -> int:
    if not path.exists():
        print(f"Error: '{path}' does not exist.")
        return 1
    if path.suffix.lower() != ".md":
        print(f"Error: '{path}' does not have a .md extension.")
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


def run_reply(
    path: Path,
    model: str | None = None,
    reasoning_effort: str | None = None,
    text_verbosity: str | None = None,
    verbose: bool = False,
) -> int:
    def status(msg: str) -> None:
        if verbose:
            print(msg, flush=True)

    status(f"Reading transcript from {path}...")
    text = path.read_text(encoding="utf-8")
    config = load_config()
    effective_model = model or config.model
    if not effective_model:
        print("Error: no model specified. Pass --model or set 'model' in ~/.config/mdc/config.toml.")
        return 1
    assistant_name = _default_assistant_name(effective_model)

    status("Validating transcript...")
    transcript = parse_transcript(text, assistant_name=assistant_name)
    if not transcript.pending:
        print("No pending human turn found. Nothing to do.")
        return 1

    if effective_model.startswith("claude-"):
        reply_text = _reply_anthropic(
            transcript, config, path, effective_model,
            reasoning_effort=reasoning_effort,
            verbose=verbose,
            status=status,
        )
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
            text_verbosity=text_verbosity,
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
) -> str:
    from mdc.anthropic_client import AnthropicChatClient

    client = AnthropicChatClient(model=model, api_key=config.anthropic_api_key)
    system, messages = build_anthropic_input(transcript, config.system_prompt, path)
    status(f"Requesting reply from Anthropic model '{model}'...")
    status("Streaming reply:")
    reply = client.generate_reply(system, messages, on_delta=_print_reply_delta, reasoning_effort=reasoning_effort)
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
    text_verbosity: str | None,
    verbose: bool,
    status,
) -> str:
    from mdc.openai_client import OpenAIChatClient

    client = OpenAIChatClient(
        model=model,
        api_key=config.openai_api_key,
        reasoning_effort=reasoning_effort,
        text_verbosity=text_verbosity,
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
    if dollars < 0.01:
        return f"${dollars * 100:.3f}¢"
    return f"${dollars:.4f}"


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

    print(f"\n[{' | '.join(parts)}]", file=sys.stderr)


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
    print(f"\n[{' | '.join(parts)}]", file=sys.stderr)
