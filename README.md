# mdc

A CLI tool for managing hand-edited markdown conversation files in the **mdform** format — structured transcripts of AI conversations that you can edit, validate, and extend with live AI replies.

## What is mdform?

Mdform is a lightweight markdown convention for recording AI conversations. Each file is a plain `.md` file with a title, date, and labeled sections for each speaker:

```markdown

# My Question About Sorting Algorithms
2026-04-16

## Prompt

What is the difference between quicksort and mergesort?

## Claude

Quicksort and mergesort are both comparison-based sorting algorithms...
```

Files are human-editable at any point. You can add a new `## Prompt` section, run `mdc reply`, and the AI's response is appended automatically.

## Installation

Requires Python 3.11+.

```bash
pip install git+https://github.com/yourusername/mdc.git
```

Or with `pipx` (recommended for CLI tools):

```bash
pipx install git+https://github.com/yourusername/mdc.git
```

## Configuration

Create `~/.config/mdc/config.toml`:

```toml
model = "claude-sonnet-4-6"
anthropic_api_key = "sk-ant-..."
openai_api_key = "sk-..."
system_prompt_file = "~/.config/mdc/system.md"
```

API keys can also be set via environment variables `ANTHROPIC_API_KEY` and `OPENAI_API_KEY`.

## Commands

### `mdc new <title>`

Create a new conversation file in the current directory. The filename is derived from the title and today's date.

```bash
mdc new "My Question About Sorting"
# creates: 2026-04-16-my-question-about-sorting.md
```

### `mdc reply <file>`

Append an AI reply to the pending human turn in a transcript. Streams the reply to stdout as it arrives, then writes it to the file.

```bash
mdc reply 2026-04-16-my-question-about-sorting.md
mdc reply --model gpt-4o conversation.md
mdc reply --verbose conversation.md
```

**Options:**

| Flag | Description |
|------|-------------|
| `-m`, `--model` | Model to use (e.g. `claude-sonnet-4-6`, `gpt-4o`). Overrides config file. |
| `-r`, `--reasoning-effort` | `none`, `low`, `medium`, `high`, `xhigh` (default: `low`) |
| `-t`, `--text-verbosity` | `low`, `medium`, `high` (default: `medium`) |
| `-v`, `--verbose` | Print progress messages and token usage |

**Supported providers:** Anthropic (Claude models) and OpenAI (GPT and o-series models).

Local file references in markdown links (e.g. `[data](./data.csv)`) are automatically collected and sent to the model as context.

### `mdc check <file>`

Validate a transcript's structure and report whether a reply is pending.

```bash
mdc check conversation.md
# OK: transcript is valid, 2 local asset(s) resolved, and a reply is pending for 'Prompt'.
```

### `mdc validate <file> [file ...]`

Run all mdform format rules on one or more files and report any violations.

```bash
mdc validate *.md
```

### `mdc fix <file> [file ...]`

Auto-fix correctable format violations. Shows a diff of proposed changes and prompts for confirmation before modifying each file. A `.bak` backup is created before any changes are written.

```bash
mdc fix conversation.md
```

Fixable violations include: missing blank lines, stray asterisks around the date, `## ChatGPT` headers (renamed to `## GPT`), and RTL Unicode spans.

### `mdc pdf <file>`

Convert a markdown transcript to PDF using [pandoc](https://pandoc.org/), and open the result automatically. This command requires `pandoc` to be installed and on your PATH — all other commands work without it.

```bash
mdc pdf conversation.md          # converts and opens
mdc pdf --quiet conversation.md  # converts without opening
```

## Mdform Format Rules

Files must satisfy these rules (enforced by `mdc validate`, auto-fixed where possible by `mdc fix`):

1. Blank first line
2. `# Title` as the first heading
3. `yyyy-mm-dd` date line immediately after the title
4. Filename derived from the slugified title and date (e.g. `2026-04-16-my-title.md`)
5. Blank line after the date
6. `##` section headers with non-empty labels
7. Use `## GPT` not `## ChatGPT`
8. Each section preceded and followed by a blank line
9. `## References` section, if present, must be the final section
10. Reference lines formatted as `| Author, First (year) *Italicized Title*`

## References Section

The AI will automatically append a `## References` section if it cites sources. Reference lines follow this format:

```markdown
## References

| Knuth, Donald E. (1997) *The Art of Computer Programming*
| Cormen, Thomas H., Leiserson, Charles E., Rivest, Ronald L., Stein, Clifford (2009) *Introduction to Algorithms*
```
