# Plan: `mdc relate` + relation maintenance in `mdc index`

## Context

`mdc relate` builds a semantic relations map over the library's canonical index terms — bidirectional pairs like "internalism ↔ externalism ↔ internalism and externalism". This map will eventually be used by the `lookup_term` tool in `mdc reply` so the model can discover neighboring terms without guessing. This plan also adds lightweight housekeeping to `mdc index`: term sanitization and relation pruning when terms are removed.

---

## Part 1: Changes to `mdc index`

### Term sanitization

In `build_index` (`library.py`), after `summarize(content, wc)` returns terms, sanitize each before storing in `DocEntry`:
- Replace `;` → `,` and `:` → `,`, then strip whitespace
- If a term changed, emit a warning

Add to `library.py`:
```python
def _sanitize_term(term: str) -> tuple[str, str | None]:
    cleaned = term.strip().replace(";", ",").replace(":", ",").strip()
    if cleaned != term.strip():
        return cleaned, f"term sanitized: \"{term.strip()}\" → \"{cleaned}\""
    return cleaned, None
```

Add `on_warning: Callable[[str], None] | None = None` to `build_index` signature. Call `_sanitize_term` on each returned term, invoking `on_warning` for any changes.

In `run_index` (`cli.py`), pass an `on_warning` that collects warnings and prints them after the run alongside existing `keys_warnings`.

### Relation pruning

When terms are removed, prune `_TERMS_STATE_PATH`'s `relations` map.

Add to `library.py`:
```python
def prune_relations(library_path: Path, removed_terms: set[str]) -> None:
```

- Reads `_TERMS_STATE_PATH`, removes entries for deleted terms, removes deleted terms from all other terms' related lists, writes back.
- No-op if `relations` key is absent or file doesn't exist.

In `run_index` (`cli.py`), after computing `removed_terms`:
```python
if removed_terms:
    from mdc.library import prune_relations
    prune_relations(lib_path, set(removed_terms))
```

---

## Part 2: `mdc relate` command

### Storage

Add `relations: {term: [term, ...]}` to `_TERMS_STATE_PATH` alongside the existing `terms` and `groups` fields.

Add to `library.py`:
```python
def load_relations(library_path: Path) -> dict[str, list[str]]: ...
def save_relations(library_path: Path, relations: dict[str, list[str]]) -> None: ...
```

`save_relations` reads the existing JSON, updates only the `relations` key, writes back — preserving `terms`, `groups`, etc.

### Prompt — `mdc/cli.py`

```python
def _relate_prompt(all_terms: list[str], batch: list[str]) -> str:
```

```
You are building a semantic index for a philosophy library.

Below is the complete list of canonical index terms:
- aristotle
- compatibilism
- ...

For each of the following terms, list all terms from the above list
that are semantically related — meaning a reader looking up that term
would likely also want to consult them.

Include terms that:
- Cover the same concept from a different angle
- Are the broader or narrower form of the concept
- Are frequently discussed together in the literature

Exclude terms that are only loosely or incidentally related.

TERMS TO RELATE:
- internalism
- free will
- ...

OUTPUT FORMAT — one line per term, term exactly as given, then a colon,
then related terms separated by semicolons. If none, write 'none'.
internalism: externalism; internalism and externalism
free will: freedom and determinism; compatibilism
```

### Output parsing — `mdc/cli.py`

```python
def _parse_relate_reply(text: str, batch: list[str]) -> dict[str, list[str]]:
```

- Splits on newlines, partitions each line on the first `:`
- Matches left side case-insensitively against the batch
- Returns `{term: [related, ...]}` (empty list for "none")
- Skips unrecognized lines silently

### `run_relate` — `mdc/cli.py`

1. Resolve `lib_path`, load config (same pattern as `run_index`)
2. Load all canonical terms via `load_terms(lib_path)`; exit with message if empty
3. Load existing relations via `load_relations(lib_path)`
4. Shuffle terms randomly (`random.shuffle`), batch into groups of 20
5. For each batch:
   - Call model with `_relate_prompt(all_terms, batch)`
   - Parse reply; validate that returned related terms exist in the full term set — warn and skip unknown ones
   - Merge bidirectionally: `A → B` also adds `B → A`
   - Save relations after each batch (resilience on interrupt)
   - Print progress: `  batch 3/47`
6. Print total: `Relations written for N terms.`

Model support: same `claude-*` / `ollama/` branching as `run_index`, reusing `_lookup_price`, `_format_cost`, `_ANTHROPIC_PRICING`. Cost tracking same pattern.

### CLI wiring — `mdc/cli.py`

Add `relate` subparser alongside `index`:
```
mdc relate [library_path] [--model MODEL]
```
Same arguments as `mdc index`.

---

## Files to Modify

- `mdc/library.py` — `_sanitize_term`, `prune_relations`, `load_relations`, `save_relations`
- `mdc/cli.py` — `build_index` `on_warning` param, `run_index` pruning + warnings, `_relate_prompt`, `_parse_relate_reply`, `run_relate`, subparser

## Verification

1. **Sanitization**: Index a document whose AI-returned terms include `;` or `:`. Confirm warning printed and term stored cleanly in `library-manifest.json`.
2. **Pruning**: Delete a document, re-run `mdc index`. Confirm removed terms show with `-`. Confirm they are gone from `relations` in `library-index.json`.
3. **`mdc relate`**: Run on a small indexed library. Confirm `library-index.json` gains a `relations` field with bidirectional entries.
4. **Interrupt resilience**: Kill `mdc relate` mid-run. Confirm partial results saved. Re-run completes cleanly.
5. **Cost output**: Run with a Claude model; confirm per-batch and total cost printed.
