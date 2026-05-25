from __future__ import annotations

import datetime
import textwrap
from pathlib import Path

from mdc.config import load_config
from mdc.pdf_utils import _render_review_pdfs
from mdc.review import sanitize_for_pandoc
from mdc.text_utils import _format_cost, _format_total, _lookup_price, _print_reply_delta


def run_review(library_path: str | None, reset: bool, theme: str | None = None, selection: bool = False, doc_start: int | None = None, docs: bool = False, evaluate: bool = False, action: str | None = None, action_themes: list[str] | None = None) -> int:
    import hashlib
    import sys
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
    from mdc.cmd_reply import _ANTHROPIC_PRICING

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
