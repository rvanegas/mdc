# Plan: Lazy Library Loading in `mdc reply`

## Context

Currently `_reply_anthropic` eagerly loads `render_manifest(entries)` — full summaries for every document — into the system prompt, plus exposes `search_library` and `read_document` tools. For a 500-document library this is 50–100 KB of context per call, most of it irrelevant.

The new design mirrors how a human uses a reference library: look up a term in the index, read summaries of matching documents, then read a document in full only if needed. Relations (from `mdc relate`) make term lookup self-navigating — a near-miss still surfaces related terms.

---

## New tools: `lookup_term` and `get_summary`

### `lookup_term(library_path, term) -> str` — `mdc/library.py`

Reads `_TERMS_STATE_PATH` (terms) and `_RELATIONS_STATE_PATH` (relations). Returns a formatted block:

```
internalism
  Documents:
    philosophy/moral-realism.md — Moral Realism
    philosophy/reasons.md — Reasons and Motivation
  Related: externalism; internalism and externalism
```

If term not found: returns `"Term not found: 'moral psychology'"`.

Case-insensitive match against term keys.

### `get_summary(library_path, rel_path) -> str` — `mdc/library.py`

Reads `_STATE_PATH` (manifest entries). Returns:

```
philosophy/moral-realism.md — Moral Realism — 4200w
  realism; metaethics; internalism; response-dependence
  A discussion of how moral realism relates to...
```

If not found: returns `"No summary found for 'path/to/doc.md'"`.

### Updated `LIBRARY_TOOLS` — `mdc/library.py`

Replace `search_library` with `lookup_term` and `get_summary`:

```python
LIBRARY_TOOLS = [
    {
        "name": "lookup_term",
        "description": (
            "Look up an index term and get the list of library documents tagged with it, "
            "plus semantically related terms to follow up on."
        ),
        "input_schema": {"type": "object",
                         "properties": {"term": {"type": "string"}},
                         "required": ["term"]},
    },
    {
        "name": "get_summary",
        "description": "Get the title, word count, summary, and index terms for a specific library document.",
        "input_schema": {"type": "object",
                         "properties": {"path": {"type": "string"}},
                         "required": ["path"]},
    },
    {
        "name": "read_document",
        "description": "Read the full content of a library document.",
        "input_schema": {"type": "object",
                         "properties": {"path": {"type": "string"}},
                         "required": ["path"]},
    },
]
```

---

## `-l`/`--library` and `-t`/`--term` flags — `mdc/cli.py`

Library access is opt-in. Add to the `reply` subparser:

```python
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
```

If `-t` is given without `-l`, fail immediately:

```python
if args.terms and not args.library:
    print("Error: -t/--term requires -l/--library.")
    return 1
```

Pass `library=args.library, terms=args.terms` through `run_reply` → `_reply_anthropic`.

---

## Injecting pre-specified terms as initial context — `mdc/cli.py`

"Injecting as initial context" means: before calling the model, look up each `-t` term using the same `lookup_term` function the tool calls, concatenate the results, and pass as the `library_context` system block via `build_anthropic_input`. The model sees the pre-looked-up results as a system-level given — not as something it called, but as context already provided.

Format of the injected block:

```
Pre-looked-up library terms:

internalism
  Documents:
    philosophy/moral-realism.md — Moral Realism
  Related: externalism; internalism and externalism

externalism
  Documents:
    philosophy/reasons.md — Reasons and Motivation
  Related: internalism; internalism and externalism
```

If a specified term is not found, it is omitted from the block and a warning is printed to stdout immediately (before the model is called):

```
! library term not found: "moral psychology"
```

---

## Miss warnings during tool calls — `mdc/cli.py`

When the model calls `lookup_term` with a term not in the index, the `tool_executor` returns the not-found string to the model AND calls `status` to print a warning — `status` is already in scope from `_reply_anthropic`:

```python
def tool_executor(tool_name, tool_input):
    if tool_name == "lookup_term":
        term = str(tool_input.get("term", ""))
        result = lookup_term(lib, term)
        if result.startswith("Term not found"):
            status(f"! library term not found: \"{term}\"")
        return result
    if tool_name == "get_summary":
        return get_summary(lib, str(tool_input.get("path", "")))
    if tool_name == "read_document":
        return read_document(lib, str(tool_input.get("path", "")))
    return f"Unknown tool: {tool_name}"
```

---

## System prompt addition — `mdc/cli.py`

When library tools are active, `library_context` is passed to `build_anthropic_input` as a single string. It contains the tools description followed (if `-t` terms were given) by the pre-looked-up results:

```
You have access to a library of documents. To find relevant material:
1. Call lookup_term with a relevant index term. It returns matching documents and related terms.
2. Call get_summary for documents that look relevant.
3. Call read_document only when a summary confirms the document is worth reading in full.
Start by looking up index terms relevant to the user's question.

Pre-looked-up library terms:

internalism
  ...
```

If no `-t` terms were given, `library_context` contains only the tools description.

---

## Updated `_reply_anthropic` logic — `mdc/cli.py`

```python
def _reply_anthropic(transcript, config, path, model, reasoning_effort, verbose, status,
                     include_manifest=False, library=False, terms=None) -> str:

    if include_manifest:
        # unchanged: load MANIFEST.md as static block, no tools
        ...

    elif library:
        if not config.library_path or not config.library_path.is_dir():
            print("Error: -l/--library requires library_path to be set in config.")
            # caller handles non-zero return
            ...
        lib = config.library_path
        tools = LIBRARY_TOOLS

        # Build pre-looked-up block from -t terms
        preloaded_lines = []
        for term in (terms or []):
            result = lookup_term(lib, term)
            if result.startswith("Term not found"):
                status(f"! library term not found: \"{term}\"")
            else:
                preloaded_lines.append(result)

        library_tools_prompt = (
            "You have access to a library of documents. To find relevant material:\n"
            "1. Call lookup_term with a relevant index term.\n"
            "2. Call get_summary for documents that look relevant.\n"
            "3. Call read_document only when a summary confirms it is worth reading.\n"
            "Start by looking up index terms relevant to the user's question."
        )
        if preloaded_lines:
            library_context = library_tools_prompt + "\n\nPre-looked-up library terms:\n\n" + "\n\n".join(preloaded_lines)
        else:
            library_context = library_tools_prompt

        def tool_executor(tool_name, tool_input):
            ...  # as above

        status("Library tools active.")
```

---

## Files to Modify

- `mdc/library.py` — add `lookup_term`, `get_summary`; update `LIBRARY_TOOLS`
- `mdc/cli.py` — add `-t` to reply subparser; update `run_reply`, `_reply_anthropic`
- `mdc/assets.py` — rename `library_manifest` → `library_context` parameter in `build_anthropic_input`

## Files Unchanged

- `mdc/anthropic_client.py` — tool loop unchanged

---

## Verification

1. Run `mdc reply file.md` with library configured — confirm no library tools active, behavior unchanged.
2. Run `mdc reply -t "internalism" file.md` (without `-l`) — confirm error: `-t requires -l`.
3. Run `mdc reply -l file.md` — confirm tools are active, model calls `lookup_term` first.
4. Run `mdc reply -l -t "internalism" file.md` — confirm pre-looked-up block in system prompt before model is called.
5. Run `mdc reply -l -t "nonexistent term" file.md` — confirm `! library term not found` printed before model call.
6. During a reply with `-l`, observe model call `lookup_term` with a nonexistent term — confirm warning printed to stdout.
7. Run `mdc reply -i file.md` — confirm unchanged: MANIFEST.md loaded, no tools.
8. Run `mdc reply -l file.md` with no `library_path` in config — confirm error message.
