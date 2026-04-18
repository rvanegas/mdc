# Library Tool-Use for MDC

## Context

MDC currently supports embedding local files in a conversation via markdown links (`[label](./path/to/file.md)`), but this only works for files explicitly linked in a turn. The user wants a large, persistent library of markdown files (~hundreds of files, ~1M words) to be available as a knowledge/instruction source — similar to how Claude Code provides tools to read files from the filesystem on demand.

The core challenge: 1M words exceeds any model's context window, so selective retrieval is required.

## Recommended Approach: Manifest + Lazy Tool Access

Always include a compact manifest (titles + 1-line summaries, ~few KB) in the system prompt so the model knows what's available. Provide `read_document` and `search_library` tools so the model can fetch specific files on demand. Tool calls are ephemeral (not recorded in the transcript) — only the final reply is appended, exactly as today.

This is the most "Claude Code-like" approach: the model sees a directory listing and reads what it needs.

## Changes Required

### 1. `mdc/config.py`
- Add `library_path: Path | None = None` to `AppConfig` ✓
- Add `index_model: str = "ollama/llama3.2"` to `AppConfig` ✓
- Parse both from `config.toml` ✓

### 2. New file: `mdc/library.py` ✓
- `DocEntry` frozen dataclass: `rel_path`, `title`, `summary`
- `build_index(library_path, summarize, on_progress)` — walks `*.md` files (excluding `INDEX.md`), uses Ollama model for summaries, incremental via mtime
- `render_manifest(entries)` — compact text block for system prompt
- `read_document(library_path, rel_path)` — reads file; validates path stays within library_path
- `search_library(index, query)` — TF-style keyword match, returns top-5
- `LIBRARY_TOOLS` — Anthropic tool definitions for `read_document` and `search_library`

### 3. `mdc/cli.py` — `mdc index` subcommand ✓
- `run_index(library_path, model)` — walks library, calls Ollama per file, writes `INDEX.md`
- Excludes `INDEX.md` from the file walk
- Prints per-file progress: `cached` or `indexed`

### 4. `mdc/cli.py` — `mdc index` subcommand (remaining)
- Write `INDEX.md` into the library directory as the primary output
- On incomplete run, write partial progress to `~/.local/state/mdc/index-progress.json`
- On next run, resume from partial progress file
- Timestamp in `INDEX.md` used to detect which files need re-summarizing (any file modified after the index timestamp)

### 5. `mdc/anthropic_client.py`
- Extend `generate_reply` signature: add `tools: list[dict] | None = None` and `tool_executor: Callable[[str, dict], str] | None = None`
- When `tools` is provided, wrap the current single-stream call in a loop:
  - Stream call with `tools=tools`
  - If `stop_reason == "tool_use"`: execute tools via `tool_executor`, append assistant message + tool results as user message, continue loop
  - If `stop_reason == "end_turn"`: break
  - Max 10 iterations to prevent infinite loops
- Accumulate token counts across all loop iterations
- Call `on_delta` with status lines during tool execution (e.g. `[reading: path]\n`)
- When `tools=None`: behavior identical to today (zero risk of regression)

### 6. `mdc/assets.py`
- Add `library_manifest: str | None = None` to `build_anthropic_input` signature
- When provided, append as a system block before applying `cache_control` to the last block
- The manifest gets cached automatically (it's the last system block)

### 7. `mdc/cli.py` — wire library into `mdc reply`
- In `_reply_anthropic`: when `config.library_path` is set and `INDEX.md` exists, load the manifest, define `tool_executor` closure, pass to `client.generate_reply`
- Feature activates automatically; users with no `library_path` see zero behavior change

## Index Artifacts

- **`{library_path}/INDEX.md`** — primary output; human-readable manifest with one-line summaries and a timestamp of when the index was last completed
- **`~/.local/state/mdc/index-progress.json`** — partial progress file written during indexing; deleted on successful completion; used to resume interrupted runs
- No JSON cache in `~/.cache`

## `INDEX.md` Format

```markdown
# Index
{timestamp}

| File | Title | Summary |
|------|-------|---------|
| philosophy/stoicism.md | Stoicism | The Stoic school of thought... |
...
```

The timestamp is used to detect stale entries: any `.md` file modified after it gets re-summarized on the next `mdc index` run.

## Transcript Format

No changes. Tool calls and results are ephemeral; only the final assistant reply is appended to the markdown file, exactly as today.

## No New Dependencies

All library logic uses Python stdlib (`re`, `collections`, `json`, `pathlib`). Matches the project's lean footprint.

## Verification

1. Set `library_path = "~/notes"` in `~/.config/mdc/config.toml`
2. Run `mdc index` — observe per-file progress, `INDEX.md` written to `~/notes/`
3. Run `mdc index` again — all files show `cached`, only files modified since last run are re-summarized
4. Interrupt `mdc index` mid-run — verify `~/.local/state/mdc/index-progress.json` exists
5. Run `mdc index` again — resumes from where it left off
6. Run `mdc reply` — observe manifest loaded from `INDEX.md`, tool calls printed during streaming
7. Verify final reply appended to transcript; transcript file unchanged structurally
8. Test path traversal rejection: tool call with `path = "../../secret"` returns error string

## Build Order (each step backward-compatible)

1. `config.py` — add fields ✓
2. `library.py` — new file ✓
3. `cli.py` `mdc index` — stub ✓; complete with `INDEX.md` output and resume logic
4. `assets.py` — additive `library_manifest` parameter
5. `anthropic_client.py` — tool loop
6. `cli.py` — wire library into `mdc reply`
7. Tests in `tests/test_library.py`
