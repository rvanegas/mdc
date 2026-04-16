# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

**MDC** is a CLI tool for managing hand-edited markdown conversation files in the "mdform" format — structured transcripts of AI conversations (Claude, GPT, Ollama). It validates, fixes, and extends these files by appending AI replies.

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

The tool is a pipeline: **CLI → Transcript parsing → Format validation → Asset collection → API client**.

### Key modules

- **`cli.py`** — 6 subcommands: `new`, `check`, `validate`, `fix`, `reply`, `pdf`
- **`transcript.py`** — Core parsing: splits markdown into preamble + conversational turns, detects pending human turns awaiting reply, manages reference sections
- **`form.py`** — Enforces the 12-rule mdform format (blank lines, headers, speaker labels, reference formatting, filename derivation)
- **`assets.py`** — Collects local file references from markdown links, validates paths stay within the transcript directory, builds API-specific inputs with caching directives
- **`config.py`** — Loads `~/.config/mdc/config.toml`; also reads `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` env vars
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

## Mdform Format

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

To build a wheel for distribution (no internet required on the recipient's machine):

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

## Configuration

Users create `~/.config/mdc/config.toml`:
```toml
model = "claude-sonnet-4-6"
anthropic_api_key = "sk-ant-..."
openai_api_key = "sk-..."
system_prompt_file = "~/.config/mdc/system.md"
ollama_base_url = "http://localhost:11434/v1"
```
