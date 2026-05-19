# Plan: Rethink Review Context and State

## Context

The review system currently uses a sliding window for state storage: only the 40 most recent
document responses are kept in JSON. This means (a) the MD files can't be reconstructed from
state alone, (b) related documents older than 40 can't be surfaced as context, and (c) the
context rules for per-doc vs. interim vs. final calls are inconsistent with the desired model.

The desired model:
- **Per-doc**: 40 most recent reviews + reviews of explicitly Related docs + most recent interim
- **Interim assessment**: 40 most recent reviews + most recent interim only (not all interims)
- **Final assessment**: all interims (no rolling window of docs)
- **State**: all reviews and assessments stored; MD files are fully reconstructable from JSON

---

## Files to Modify

- `mdc/review.py` -- state dataclass, helpers, message builder
- `mdc/cli.py` -- `run_review()`, `run_validate()`, and supporting functions

---

## State Changes (`review.py`)

### `ReviewState` dataclass

Change `interims` from `list[str]` to `list[dict]` so headers are preserved for reconstruction:

```python
# interims entry format:
{"header": "Interim 1 (after doc 40)", "text": "...", "after_doc": 40}
```

Add `final_text: str | None = None` to store the final assessment text.

`responses` already has the right shape -- just stop truncating it. The list grows
unboundedly and all entries are available for related-doc lookup and reconstruction.

### Migration in `load_review_state`

If a loaded `interims` entry is a plain string (old format), convert it to a dict with a
synthetic header and `after_doc` derived from its index x `DEFAULT_WINDOW`.

### Updated fields summary

```python
@dataclass
class ReviewState:
    doc_index: int = 0
    interims: list[dict] = field(default_factory=list)  # {"header", "text", "after_doc"}
    responses: list[dict] = field(default_factory=list)  # ALL responses; each: {"filename", "label", "text"}
    cumulative_cost: float = 0.0
    final_interim_done: bool = False
    final_done: bool = False
    final_text: str | None = None
```

---

## Validation Changes (`cli.py` -- `run_validate`)

`run_validate` already checks that Related titles resolve to known library documents
(lines 1299-1302 via `resolve_title`). Add a second check: every resolved related document
must precede the current document in chronological order.

To do this, build an ordered position map once per `run_validate` call when a library is
configured:

```python
from mdc.review import list_review_docs
doc_order = {p.name: i for i, p in enumerate(list_review_docs(lib))} if lib else {}
```

Then after the existing resolve check, for each related entry that does resolve, look up both
documents in `doc_order` and error if the related doc's position is >= the current doc's
position:

```python
rel_path = resolve_title(lib, entry)
if rel_path is None:
    errs.append(f"Related title not found in library: {entry!r}")
elif doc_order.get(Path(rel_path).name, -1) >= doc_order.get(path.name, -1):
    errs.append(f"Related title does not precede this document: {entry!r}")
```

`mdc review` calls `run_validate` (or the same shared helper) on all library docs before the
confirm prompt and aborts if any errors are found. Since `list_review_docs` is already called
in `run_review`, reuse that list for the position map rather than calling it twice.

---

## New Helpers (`review.py`)

### `extract_related_titles(doc_path: Path) -> list[str]`

Reads the `## Related` section from a document file and returns the titles as strings
(stripping leading `- ` bullet markers). Reuses the `_extract_section` / `_RELATED_SECTION_RE`
logic already in `library.py` -- import or replicate the regex.

### `build_review_md(state: ReviewState, include_toc: bool = False) -> str`

Reconstructs the full REVIEW.md content from state:
1. Optional TOC block prefix
2. For each response in `state.responses`, enumerate from 1:
   - Emit `\n# {label}\n\n{text}\n\n---\n`
   - If any interim has `after_doc == N` (where N is the count of responses emitted so far),
     emit it immediately after
3. Emit final assessment from `state.final_text` if present

### `build_assessments_md(state: ReviewState, include_toc: bool = False) -> str`

Same but only emits interim and final entries (no per-doc responses). Mirrors how
`_append_output(..., assessment=True)` currently works.

### Updated `build_review_messages`

Add `related_responses: list[dict] | None = None` parameter. If provided, inject
these as user/assistant pairs AFTER the rolling 40 (but before the current doc):

```
[most recent interim user+assistant]
[last 40 rolling window user+assistant pairs]
[related doc user+assistant pairs, deduped against rolling window]
[current doc user]
```

Change `interims: list[str]` -> `list[dict]` and use `entry["text"]` internally.

---

## `cli.py` Changes (`run_review`)

### Pre-run validation

After `all_docs` and the title map are built, validate every document using the same Related
checks added to `run_validate`. Build `doc_order` from `all_docs`, then for each doc extract
related titles, resolve them, and check both existence and ordering. Print all errors and
return 1 if any are found -- before the confirm prompt.

### Per-doc loop

1. **Don't pop**: remove `if len(state.responses) > _REVIEW_WINDOW: state.responses.pop(0)`
2. **Related doc context**: before calling the API, extract related titles from the doc, map
   them to filenames via `{e.title: Path(e.rel_path).name for e in load_entries(lib_path)}`
   (build this map once before the loop), find matching entries in `state.responses`, exclude
   any already in `state.responses[-_REVIEW_WINDOW:]`, pass remainder as `related_responses`
3. **Most recent interim**: pass `state.interims[-1:]` (unchanged from current behavior)

### Interim assessments

Change context from ALL interims to only the most recent one. This prevents context rot
(accumulated interim prose reinforcing the same patterns) while preserving thread continuity
via the prior interim. The rolling 40 supplies the newly out-of-window segment directly.

```python
# Was:
messages = build_review_messages(state.interims, state.responses[-_REVIEW_WINDOW:], interim_user)
# Now:
messages = build_review_messages(state.interims[-1:], state.responses[-_REVIEW_WINDOW:], interim_user)
```

Store interim as dict:
```python
state.interims.append({"header": f"Interim {n} (after doc {state.doc_index})", "text": reply.text, "after_doc": state.doc_index})
```

### Final interim assessment

Same context change (only most recent interim). Store as dict with `after_doc = total`.

### Final assessment

Keep ALL interims in context (no change to semantics). Store result:
```python
state.final_text = reply.text
```

### Numbering mode

Remove the `--no-numbers` flag entirely. Unnumbered mode (title + date headers) becomes the
only mode. Remove `state.numbered`, `_DEFAULT_SYSTEM_PROMPT` (the numbered variant), and
`_SYSTEM_PROMPT_HEADING_NUMBERED`. Update the system prompt to use unnumbered headings
unconditionally. Remove the numbering-mode conflict check on resume.

### `--rebuild` flag

Add `--rebuild` to `mdc review`. When set:
- Load state, call `build_review_md` and `build_assessments_md`, write the files, exit.
- Also re-apply the TOC block if `state.final_done`.

### `_truncate_review_since` and `--since`

The `--since` path filters `state.responses` to those in `reviewed_names`. With all responses
stored this is a simple list comprehension (already present). No structural change needed --
just ensure `since` resets `state.final_text = None` alongside the existing flags.

### `_get_active_reviewed_docs`

Reads from the MD file -- no change needed. (The MD file is the display artifact; state is
the source of truth, but the tombstoning/detection logic still uses the MD file.)

### Tombstone editing

When a document is removed, its review entry is tombstoned as before. Additionally, any other
response in `state.responses` and any interim or final text in `state.interims` /
`state.final_text` that references the removed document must be lightly edited to remove
those references.

This is done via an API call per affected entry: send the existing text along with a prompt
instructing the model to remove or rephrase only the references to the removed document title,
making the minimum changes necessary. The edited text replaces the original in state, and
`--rebuild` is called automatically afterward to regenerate the MD files from the updated state.

`_tombstone_entry_in_file` and `_annotate_removed_entry_in_assessments` are removed entirely.
Detection of which entries are affected can still search for the filename stem and title.
After state is updated, call `--rebuild` to regenerate the MD files.

---

## Reconstruction Contract

`build_review_md(state)` must produce byte-for-byte identical output to the incrementally
written REVIEW.md (for a fresh run with no tombstoning). This is enforced by:
- Same entry format: `\n# {header}\n\n{body}\n\n---\n`
- Same ordering: docs in `doc_num` order, interims inserted at their `after_doc` position
- Same TOC block prepended when `include_toc=True`

Removed entries are deleted from `state.responses`. Interims and the final assessment have
their texts updated in state after the editing API calls. `--rebuild` produces output that
reflects all removals correctly, with no tombstone markers needed.

---

## Verification

1. Run `mdc review --dry-run` on a test library -- should show no regressions.
2. Run a short review (5-10 docs) from scratch; confirm responses accumulate without truncation.
3. Add a Related section to a doc, re-run `mdc review --since` from before it; confirm the
   related doc's review appears in context.
4. After a complete run, invoke `mdc review --rebuild`; diff the rebuilt files against the
   originals -- should be identical (modulo tombstones if any were ever created).
5. Confirm `load_review_state` migrates old state (interims as strings) without error.
