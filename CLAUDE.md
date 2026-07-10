# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

**MDC** is a CLI tool for managing hand-edited markdown conversation files — structured transcripts of AI conversations (Claude, GPT, Ollama). It validates, fixes, and extends these files by appending AI replies.

## Development Philosophy

No backwards compatibility in code — deprecation shims, compatibility flags, and legacy fallbacks are not welcome. If something changes, change it completely. Data migrations are still required when existing files would otherwise break.

## Development Setup

```bash
pip install -e .          # Install in development mode
python -m mdc --help      # Run via module
mdc --help                # Run via installed command
```

## Commands

```bash
pytest tests/             # Run tests (suite is planned but not yet implemented)
```

No linter is configured. Tests use pytest (`testpaths = ["tests"]` in pyproject.toml).

## Architecture

The tool has two main pipelines: **CLI → Transcript parsing → Format validation → Asset collection → API client** (for conversation files), and **CLI → Library scanning → AI summarization → Index writing** (for the document library).

### Key modules

- **`cli.py`** — 7 subcommands: `index`, `new`, `check`, `validate`, `fix`, `reply`, `pdf`
- **`transcript.py`** — Core parsing: splits markdown into preamble + conversational turns, detects pending human turns awaiting reply, manages reference sections
- **`form.py`** — Enforces the 12-rule transcript format (blank lines, headers, speaker labels, reference formatting, filename derivation)
- **`assets.py`** — Collects local file references from markdown links, validates paths stay within the transcript directory, builds API-specific inputs with caching directives
- **`config.py`** — Loads `~/.config/mdc/config.toml`; also reads `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` env vars
- **`library.py`** — Library indexing: scans a directory of markdown documents, generates AI summaries and index terms, writes `MANIFEST.md` and `INDEX.md`, reads `KEYS.md` for term canonicalization
- **`anthropic_client.py`**, **`openai_client.py`**, **`ollama_client.py`** — Per-provider streaming clients

### Data flow for `mdc reply`

1. `cli.py` reads config + transcript file
2. `transcript.py` parses the file and finds the pending human turn
3. `assets.py` collects any local asset references from that turn
4. The appropriate client streams the reply (with token cost output)
5. `transcript.py` appends the reply and writes the file back

### Key design patterns

- **Frozen dataclasses** for core data types (`Turn`, `Transcript`, `Preamble`, `LocalAssetReference`)
- **Streaming via callbacks** — clients accept an `on_delta` callable and print progressively
- **`TranscriptError`** for all validation failures
- **Anthropic prompt caching** — up to 4 cache slots; 3 are allocated to assets
- **OpenAI asset uploads** — files are cached by ID with expiry tracking; client auto-retries with fresh uploads on cache miss
- **Heading promotion** — `##` headings in replies are automatically promoted to `###` to avoid conflicts with the transcript structure

## Transcript Format

Files must satisfy 12 rules enforced by `form.py`:
1. Blank first line
2. `# Title` as first heading
3. `yyyy-mm-dd` date line immediately after title
4. Filename derived from slugified title + date
5. Blank line after the date
6–9. `##` section headers, one word each (speaker names or `Claude`/`GPT`)
10. References section (if present) must be the final section
11–12. Reference lines formatted as `| Author (year) *Italicized Title*`

## Distribution

To build a wheel for distribution:

```bash
pip install build
python -m build
# produces dist/mdc-0.1.0-py3-none-any.whl
```

Recipients install it with:

```bash
pip install mdc-0.1.0-py3-none-any.whl
# or, recommended for CLI tools:
pipx install mdc-0.1.0-py3-none-any.whl
```

Bump `version` in `pyproject.toml` before each build. The wheel filename reflects that version.

## Library Index

`mdc index [library_path]` scans a directory of markdown documents, calls an AI model to generate summaries and index terms per document, and writes:
- **`MANIFEST.md`** — human-readable document listing with title, word count, terms, and summary per document
- **`INDEX.md`** — inverted term index for use as `reply` context (`-i` flag)

State is cached in `~/.local/state/mdc/library-index.json` and `library-terms.json`; only changed documents are re-indexed.

### KEYS.md

An optional `KEYS.md` in the library directory controls term canonicalization with four sections:

- **`## Plural`** — each line is a canonical term; its plural (`term + "s"`) is auto-aliased to it
- **`## Alias`** — groups of aliases mapping to a canonical: the canonical is a bare line, aliases are `- alias` bullets beneath it
- **`## Exclude`** — terms to suppress from the index entirely
- **`## Group`** — hierarchical grouping: a parent term followed by `- subterm` bullets; subterms appear nested under the parent in `INDEX.md`

### Semantic relations

`mdc relate` builds a relations map between index terms, used by `lookup_term` to suggest adjacent terms during `mdc reply -l`. Several tactics combine to produce it:

- **AI semantic pass** — the model is shown all terms and asked to identify related ones per batch; inclusion criteria cover same-concept-different-angle, broader/narrower forms, frequent co-occurrence in the literature, and morphological variants of the same root
- **Co-occurrence supplementation** — after the AI pass, term pairs that co-occur as document tags in at least 2 documents are automatically added as related; this catches corpus-specific clustering the model might miss by reasoning from general knowledge rather than the actual library
- **KEYS.md Alias** — terms aliased to a canonical are resolved before lookup, so a query for an alias navigates to the canonical's relations transparently

## Companion Files

Files without a secondary suffix (`YYYY-MM-DD-slug.md`) are standalone documents with no companions. A document can acquire up to three companions, each identified by a secondary suffix:

| Suffix | Role |
|---|---|
| `.document.md` | The prose document being written/edited |
| `.chat.md` | Chat transcript that drives `mdc reply`; companion `.document.md` and `.argument.md` files are auto-discovered by stem |
| `.argument.md` | A pure numbered list of propositions, extracted by `mdc argue` |
| `.analysis.md` | Per-argument dianoia analysis, produced by `mdc analyze <doc> <proposition>`; never hand-edited |

All four share the same date-slug stem: `YYYY-MM-DD-slug.{suffix}.md`.

The library indexer indexes bare `*.md` files and `*.document.md` files. Chat, argument, and analysis companions (`.chat.md`, `.argument.md`, `.analysis.md`) are excluded.

### Argument files

`.argument.md` files contain exactly one section, `## Argument`, a list of propositions:

```
## Argument
- 1: premise text
- 2 (from: 1): premise text
- 3 (from: 1, 2): conclusion text
```

Proposition numbers are a strict integer succession starting at 1 with no gaps, and are never renumbered once assigned (`validate_proposition_numbering` in `argue.py` enforces this on every AI-driven edit). A proposition with justifiers (a `(from: ...)` clause) is an "argument" — its justifiers are premises, itself the conclusion. `mdc analyze <doc> <proposition>` submits one argument's chain to dianoia and writes the result to `<stem>.analysis.md`, labeling it with a letter (A, B, C, …, Z, AA, …) following Roxana's convention exactly: computed from the proposition's ascending position among all justified propositions, never persisted (`assign_argument_labels` / `_to_alpha_index` in `argue.py`).

`mdc audit <doc>` checks the whole `.argument.md` companion against dianoia's structural conditions (connectivity, order-independence, conclusion legibility) via `dianoia audit`, printing per-finding issues and revision pointers; exits 1 when findings exist.

## Writing Assistant

MDC supports a document-editing workflow where the AI reads and rewrites the user's own files rather than just appending replies to the transcript.

### Setup

1. Create a writing session with `mdc new "My Essay" -e`. This produces two files:
   - `YYYY-MM-DD-my-essay.document.md` — the document being written/edited
   - `YYYY-MM-DD-my-essay.chat.md` — the transcript file that drives `mdc reply`

2. When `mdc reply` runs against a `.chat.md` file, it automatically discovers any sibling `.document.md` and `.argument.md` files with the same stem, loads them into the system prompt, and gives the model an `edit_file` tool.

### How it works

- `resolve_edit_targets` (`edit_tools.py`) discovers companion `.document.md` and `.argument.md` files by stripping the `.chat.md` suffix and checking for siblings with those extensions.
- `build_edit_context` injects the target files into the system prompt between `--- filename ---` fences, preceded by editing instructions.
- `make_edit_executor` returns a tool executor for the `edit_file` tool. The tool takes `path`, `old_str`, and `new_str`; the executor does an exact-string replacement and rewrites the file.
- Before the first edit to a file, a numbered backup is saved automatically (`stem--1.ext`, `stem--2.ext`, …).
- After each edit the tool returns a unified diff, which the model uses to confirm what changed.
- `mdc diff <chat-file>` shows the diff between the current document and its most recent backup.

### Paragraph wrapping

Edited content is reflowed via `wrap_paragraphs` at `config.wrap_width` (default 80). Code blocks and lines beginning with `|` (tables/references) are passed through unchanged.

### VOICE labels

When a document is indexed for the library, each section is annotated with a `[VOICE: ...]` label:

- `[VOICE: user]` — direct words of the user
- `[VOICE: llm — collaborative elaboration, implicitly endorsed unless contradicted]` — AI-generated reply sections
- `[VOICE: third-party]` — external sources or quoted voices
- `[VOICE: this entire document is the user's own writing]` — plain documents with no transcript structure

These labels are stripped before sending to the model during `reply` but are present in library context so the model can reason about authorship.

## Configuration

Users create `~/.config/mdc/config.toml`:
```toml
model = "claude-sonnet-4-6"
anthropic_api_key = "sk-ant-..."
openai_api_key = "sk-..."
system_prompt_file = "~/.config/mdc/system.md"
ollama_base_url = "http://localhost:11434/v1"
library_path = "~/notes"
index_model = "ollama/qwen2.5:14b"
```
