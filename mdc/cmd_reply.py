from __future__ import annotations

import re
import sys
import time
from pathlib import Path

from mdc.assets import build_anthropic_input, build_chat_input, build_response_input, collect_local_assets
from mdc.config import _default_assistant_name, load_config
from mdc.form import fix_section_spacing
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
from mdc.text_utils import (
    _format_cost,
    _format_total,
    _lookup_price,
    _print_reply_delta,
    _upgrade_reply_headings,
    wrap_paragraphs,
)

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


def _is_retriable_anthropic_asset_error(exc: Exception) -> bool:
    combined = str(exc).lower()
    return "file" in combined and any(
        phrase in combined
        for phrase in ("not found", "does not exist", "no such file", "invalid file", "unknown file", "expired")
    )


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
    edit: bool = False,
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
        included_rel_paths: set[str] = set()
        for raw_title in transcript.related:
            rel_path = resolve_title(lib, raw_title)
            if rel_path is None:
                status(f"! related document not found: \"{raw_title}\"")
            else:
                related_summaries.append(_get_summary(lib, rel_path, exclude=exclude))
                included_rel_paths.add(rel_path)

        _ITALIC_RE = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
        all_turn_text = "\n".join(
            t.content for t in (*transcript.turns, *(
                [transcript.pending_turn] if transcript.pending_turn else []
            ))
        )
        for m in _ITALIC_RE.finditer(all_turn_text):
            candidate = m.group(1)
            rel_path = resolve_title(lib, candidate)
            if rel_path and rel_path not in included_rel_paths and rel_path != exclude:
                related_summaries.append(_get_summary(lib, rel_path, exclude=exclude))
                included_rel_paths.add(rel_path)
                status(f"inline title matched library document: \"{candidate}\" → {rel_path}")

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

    edit_targets = resolve_edit_targets(path) if edit else []
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
    edit: bool = False,
) -> int:
    if path.suffix.lower() != ".md":
        print(f"Error: '{path}' does not have a .md extension.")
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
                edit=edit,
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
