from __future__ import annotations

import sys
from pathlib import Path


def _print_argument(args_dict: dict) -> None:
    from mdc.argue import assign_argument_labels

    argument = args_dict.get("argument", [])
    labels = assign_argument_labels(argument)
    print("Argument:")
    for s in argument:
        j = f" (from: {', '.join(s['justifiers'])})" if s.get("justifiers") else ""
        label = f" [{labels[s['symbol']]}]" if s["symbol"] in labels else ""
        print(f"  {s['symbol']}{j}{label}: {s['proposition']}")


def run_argue(path: Path, max_props: int | None = None) -> int:
    from mdc.argue import _read_title_date, argument_to_markdown
    from mdc import dianoia_client
    from mdc.config import load_config
    from mdc.form import check_global_issues

    def _primary(companion: Path) -> Path:
        return companion.with_suffix("").with_suffix(".md")

    def _read_file(p: Path) -> str:
        try:
            return p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = p.read_text(encoding="latin-1")
            p.with_suffix(p.suffix + ".bak").write_bytes(p.read_bytes())
            p.write_text(text, encoding="utf-8")
            print(f"Warning: {p.name} was not UTF-8; converted in place (backup: {p.name}.bak).", file=sys.stderr)
            return text

    if path.name.endswith(".argument.md"):
        companion = path
        path = _primary(path)
    elif path.name.endswith(".document.md"):
        if not path.exists():
            print(f"Error: '{path}' does not exist.")
            return 1
        companion = path.with_suffix("").with_suffix(".argument.md")
    else:
        if not path.exists():
            print(f"Error: '{path}' does not exist.")
            return 1
        if path.suffix.lower() != ".md":
            print(f"Error: '{path}' does not have a .md extension.")
            return 1
        # Adopt bare document into the companion model by renaming it.
        document = path.with_suffix(".document.md")
        path.rename(document)
        path = document
        companion = path.with_suffix("").with_suffix(".argument.md")

    if companion.exists():
        print(f"'{companion.name}' already exists. To analyze an argument in it, run: mdc analyze {path.name} <proposition>")
        return 0

    # Extract: no companion yet — validate and extract from the primary document
    text = _read_file(path)
    from mdc.library import is_library_transcript
    config = load_config()
    if is_library_transcript(text, config.user_names, config.llm_names):
        print("Error: mdc argue requires a plain document, not a transcript.")
        return 1
    errs = check_global_issues(path)
    if errs:
        for e in errs:
            print(f"  error: {e}")
        return 1

    print("Extracting argument…")
    try:
        args_dict = dianoia_client.extract(text, max_props=max_props)
    except (FileNotFoundError, RuntimeError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    try:
        title, date_str = _read_title_date(text)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    try:
        companion_text = argument_to_markdown(args_dict, title, date_str)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    companion.write_text(companion_text, encoding="utf-8")
    _print_argument(args_dict)
    print(f"\nWritten to {companion.name}. Edit it, then run: mdc analyze {path.name} <proposition>")
    return 0
