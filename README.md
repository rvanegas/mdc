# mdc

A CLI tool for managing hand-edited markdown conversation files — structured transcripts of AI conversations that you can edit, validate, and extend with live AI replies.

## Format

Each transcript is a plain `.md` file with a title, date, and labeled sections for each speaker:

```markdown

# My Question About Sorting Algorithms
2026-04-16

## Prompt

What is the difference between quicksort and mergesort?

## Claude

Quicksort and mergesort are both comparison-based sorting algorithms...
```

Files are human-editable at any point. Add a new `## Prompt` section, run `mdc reply`, and the AI's response is appended automatically.

## Installation

Requires Python 3.11+.

```bash
# From a wheel (recommended for distribution):
pipx install mdc-0.1.0-py3-none-any.whl

# From source:
pip install -e .
```

## Configuration

On first run, mdc writes a starter config to `~/.config/mdc/config.toml` and a starter system prompt to `~/.config/mdc/system.md`. Edit them to suit your setup.

```toml
# ~/.config/mdc/config.toml

# Model used by "mdc reply". Required unless passed via --model each time.
model = "claude-sonnet-4-6"

# API keys. Can also be set via ANTHROPIC_API_KEY / OPENAI_API_KEY env vars.
anthropic_api_key = "sk-ant-..."
openai_api_key    = "sk-..."

# System prompt file. Lines starting with "//" are stripped (use for comments).
system_prompt_file = "~/.config/mdc/system.md"

# Directory of markdown files to use as a knowledge library (see "mdc index").
library_path = "~/notes"

# Model used by "mdc index". Default: claude-haiku-4-5.
# Prefix with "ollama/" for a local model: "ollama/qwen2.5:14b".
index_model = "claude-haiku-4-5"

# Column width for paragraph wrapping in "mdc reply" edits. Default: 100.
wrap_width = 100

# Section headings treated as the user's own voice. Used during indexing.
user_names = ["Prompt", "YourName"]

# Section headings treated as LLM voice.
llm_names = ["Claude", "GPT"]

# Ollama server endpoint. Default shown.
ollama_base_url = "http://localhost:11434/v1"
```

Run `mdc config` to print the resolved paths for config, system prompt, state, and cache directories.

## Commands

### `mdc new`

Create a new transcript file in the current directory. The filename is derived from the title and today's date (`YYYY-MM-DD-slugified-title.md`). If `$EDITOR` is set, opens the new file(s) immediately.

```bash
mdc new                            # creates 2026-04-24-untitled.md
mdc new -t "My Question"           # creates 2026-04-24-my-question.md
mdc new -t "My Essay" --edit       # creates the essay file + an editor transcript
```

| Flag | Description |
|------|-------------|
| `-t`, `--title TITLE` | Title of the conversation. Defaults to `Untitled`. |
| `-e`, `--edit` | Also create a paired editor transcript (see [Writing assistant](#writing-assistant)). |

---

### `mdc reply`

Append one AI reply to a transcript. Finds the last unanswered human turn, streams the reply to stdout, and writes it to the file. A numbered backup of the previous file contents is saved automatically (`stem--1.md`, `stem--2.md`, …).

```bash
mdc reply conversation.md
mdc reply -m gpt-4o conversation.md
mdc reply -v -l -t "sorting algorithms" conversation.md
```

| Flag | Description |
|------|-------------|
| `path` | Path to the transcript. |
| `-m`, `--model MODEL` | Model to use (e.g. `claude-sonnet-4-6`, `gpt-4o`, `ollama/llama3.2`). Overrides config. |
| `-r`, `--reasoning-effort` | `none` / `low` / `medium` / `high` / `xhigh`. Default: `low`. |
| `-v`, `--verbose` | Print progress messages and token usage / cost. |
| `-w`, `--watch` | Poll the transcript once per second and reply whenever a pending turn appears. Useful when editing in another window. |
| `-l`, `--library` | Give the model access to the library index tools (`lookup_term`, `read_document`). Requires `library_path` in config. |
| `-t`, `--term TERM` | Pre-look up a library index term and inject the results into context before the model runs. Requires `-l`. May be repeated. |
| `--strict` | Abort if any `-t` term is not found in the index (default: warn and proceed). |

**Supported providers:** Anthropic (any `claude-*` model), OpenAI (any `gpt-*` or `o*` model), Ollama (any `ollama/<name>` model).

Local file references in markdown links (e.g. `[data](./data.csv)`) are collected and sent as context. Anthropic models use prompt caching; OpenAI models upload files and cache by ID.

---

### `mdc check`

Validate a transcript's structure and report whether a reply is pending.

```bash
mdc check conversation.md
# OK: transcript is valid, 2 local asset(s) resolved, and a reply is pending for 'Prompt'.
```

| Argument | Description |
|----------|-------------|
| `path` | Path to the transcript. |

Exits 0 on success, 1 on any error.

---

### `mdc validate`

Run all format rules on one or more files and report violations.

```bash
mdc validate conversation.md
mdc validate -t plain-document.md   # force transcript rules on a plain doc
mdc validate *.md
```

| Flag | Description |
|------|-------------|
| `file.md ...` | One or more files to check. |
| `-t`, `--transcript` | Force transcript validation even for files that look like plain documents. |

If `library_path` is set, also checks that any `## Related` titles resolve to actual library files. Exits 0 only if all files pass.

---

### `mdc fix`

Auto-fix correctable format violations. Shows a unified diff of proposed changes and prompts for confirmation before modifying each file. A `.bak` backup is created before writing.

```bash
mdc fix conversation.md
mdc fix *.md
```

| Argument | Description |
|----------|-------------|
| `file.md ...` | One or more files to fix. |

Fixable violations include: `U+FFFC` (object replacement character), `[char]{dir="rtl"}` pandoc RTL spans, missing blank lines around the title section, and `## ChatGPT` headers renamed to `## GPT`.

---

### `mdc diff`

Show changes made to a document by the most recent `mdc reply` edit. Compares the current file against its backup revisions and colorizes the output when running in a terminal.

```bash
mdc diff essay.md                # show the most recent change
mdc diff essay.md -d 2           # show the 2nd-most-recent change
mdc diff essay.md -r 3           # diff revision 3 against current
mdc diff essay.md -- -w          # pass -w (ignore whitespace) to diff
```

| Flag | Description |
|------|-------------|
| `path` | Path to the edited document (not the editor transcript). |
| `-r N`, `--revision N` | Diff backup revision N against the current file. |
| `-d N`, `--delta N` | Show the Nth most recent change. Default: 1 (most recent). |
| `-- ...` | Any trailing arguments after `--` are passed directly to the system `diff` command. |

---

### `mdc index`

Build or update the library document index. Scans `library_path` (or a path you supply), calls an AI model to generate a summary and index terms for each document, and writes two files into the library directory:

- **`MANIFEST.md`** — human-readable listing: title, word count, terms, and summary per document.
- **`INDEX.md`** — inverted term index for use as context during `mdc reply -l`.

State is cached; only changed documents are re-indexed on subsequent runs.

```bash
mdc index                        # use library_path from config
mdc index ~/notes                # explicit path
mdc index --all                  # reindex everything from scratch
mdc index --refs-only-all        # extract references only, no AI calls
```

| Flag | Description |
|------|-------------|
| `library_path` | Optional path to the library directory. Overrides config. |
| `--all` | Reindex all documents and rebuild all semantic relations from scratch. |
| `--refs-only-all` | Extract references from all documents without making any AI calls. |

After indexing, mdc automatically builds a **semantic relations map** between index terms so that `lookup_term` can suggest adjacent terms. Relations are derived from an AI semantic pass plus co-occurrence analysis of document tags.

#### KEYS.md

Place an optional `KEYS.md` in the library directory to control term canonicalization:

```markdown
## Plural
belief
concept

## Alias
free will
- freedom of the will
- libertarian free will

## Exclude
the
conversation

## Group
epistemology
- knowledge
- justification
- reliabilism
```

- **Plural** — each listed term's plural form (`term + "s"`) is auto-aliased to it.
- **Alias** — canonical term on a bare line; aliases as `- alias` bullets beneath it.
- **Exclude** — suppress these terms from the index entirely.
- **Group** — subterms appear nested under the parent in `INDEX.md`.

---

### `mdc pdf`

Convert a markdown file to PDF via [pandoc](https://pandoc.org/) (must be installed separately), then open the result.

```bash
mdc pdf conversation.md          # convert and open
mdc pdf --quiet conversation.md  # convert without opening
```

| Flag | Description |
|------|-------------|
| `path` | Path to the markdown file. |
| `-q`, `--quiet` | Do not open the PDF after conversion. |

---

### `mdc config`

Print the resolved paths for configuration and data files, then exit.

```bash
mdc config
# Config file:   /Users/you/.config/mdc/config.toml
# System prompt: /Users/you/.config/mdc/system.md
# State dir:     /Users/you/.local/state/mdc
# Cache dir:     /Users/you/.cache/mdc
```

---

## Format Rules

Files must satisfy these rules, enforced by `mdc validate` and auto-fixed where possible by `mdc fix`:

1. Blank first line.
2. `# Title` as the first heading.
3. `yyyy-mm-dd` date line immediately after the title.
4. Filename derived from the slugified title and date (e.g. `2026-04-16-my-title.md`).
5. Blank line after the date.
6. `##` section headers with non-empty labels.
7. Use `## GPT` not `## ChatGPT`.
8. Each section preceded and followed by a blank line.
9. `## Notes` (if present) must come before `## Related`; `## Related` (if present) must come before `## References`; `## References` must be the final section.
10. Reference lines: `| Last, First (year) *Italicized Title*`.
11. Multi-author: `| Last1, First1, First2 Last2, ... (year) *Title*`.
12. Note lines: `| [n] Text` with n consecutive starting from 1.

---

## Writing Assistant

`mdc reply` supports an editing workflow where the AI rewrites your own document rather than just appending to the transcript.

### Setup

```bash
mdc new -t "My Essay" --edit
# creates: 2026-04-24-my-essay.md          ← the document
#          2026-04-24-my-essay-editor.md   ← the transcript that drives reply
```

The editor transcript contains `[Edit: 2026-04-24-my-essay.md]` in its preamble. When `mdc reply` runs against the editor file, it loads the document into the system prompt and gives the model an `edit_file` tool.

### How edits work

- The model calls `edit_file` with `path`, `old_str`, and `new_str`; mdc does an exact-string replacement and rewrites the document.
- A numbered backup is saved before the first edit to each file (`stem--1.md`, `stem--2.md`, …).
- After each edit the tool returns a unified diff so the model can confirm what changed.
- Edited prose is reflowed to `wrap_width` columns (default 100). Code blocks and table/reference lines beginning with `|` are passed through unchanged.

Use `mdc diff essay.md` to review the changes the AI made.
