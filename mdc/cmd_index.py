from __future__ import annotations

import random
from pathlib import Path

from mdc.config import load_config
from mdc.text_utils import (
    _format_cost,
    _format_total,
    _lookup_price,
    _parse_index_reply,
    _parse_relate_reply,
)


def _annotate_voice(
    content: str,
    role: str,
    user_names: tuple[str, ...],
    llm_names: tuple[str, ...],
) -> str:
    """Return content annotated with [VOICE: ...] labels, by its role.

    A document is the user's own writing and a source someone else's, whole;
    a chat is labelled section by section from its speaker headings.
    """
    from mdc.library import _STRUCTURAL_HEADINGS
    from mdc.transcript import HEADING_RE

    if role == "document":
        return "[VOICE: this entire document is the user's own writing]\n\n" + content
    if role == "source":
        return "[VOICE: third-party — this entire document is source material, not the user's writing]\n\n" + content

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


def run_index(refs_only: bool = False, reprocess_all: bool = False, verbose: bool = False) -> int:
    from mdc.library import MANIFEST_FILENAME, build_index
    from mdc.review import sanitize_for_pandoc

    config = load_config()

    lib_path = config.library_path
    if not lib_path:
        print("Error: no library. Pass --lib or set 'library_path' in config.")
        return 1
    if not lib_path.is_dir():
        print(f"Error: '{lib_path}' is not a directory.")
        return 1

    # Indexing goes by role; a library with unnamed roles would lose those
    # documents from its state and summarize them again once named.
    from mdc.library import lacks_role
    unnamed = sorted(p.relative_to(lib_path).as_posix() for p in lib_path.rglob("*")
                     if p.is_file() and lacks_role(p.name)
                     and not {"REVISIONS", ".mdc"} & set(p.relative_to(lib_path).parts))
    if unnamed:
        print(f"Error: {len(unnamed)} dated file(s) without a role (.chat, .document, .source, …), e.g. {unnamed[0]}.")
        print("Name their roles first (scripts/migrate_layout.py does a whole library).")
        return 1

    summarize = None
    total_cost = 0.0
    last_cost: list[float] = [0.0]

    # Import pricing table lazily to avoid circular import
    from mdc.cmd_reply import _ANTHROPIC_PRICING

    if not refs_only:
        effective_model = config.index_model
        if not effective_model.startswith("claude-"):
            print(f"Error: index_model must be an Anthropic model (claude-*), not '{effective_model}'.")
            return 1
        from mdc.anthropic_client import AnthropicChatClient
        client = AnthropicChatClient(model=effective_model, api_key=config.anthropic_api_key)
        rates = _lookup_price(effective_model, _ANTHROPIC_PRICING)

        def summarize(content: str, word_count: int, role: str) -> tuple[str, list[str]]:
            nonlocal total_cost
            system = "You are a library indexing assistant."
            annotated = _annotate_voice(content, role, config.user_names, config.llm_names)
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

    # Need _slug_map from cli — import lazily to avoid circular
    from mdc.cli import _slug_map

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
