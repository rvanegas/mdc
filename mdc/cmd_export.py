from __future__ import annotations

import json
import sys
from pathlib import Path


_ID_ATTEMPTS = 8


def _primary(companion: Path) -> Path:
    return companion.with_suffix("").with_suffix(".md")


def _layout_entry(index: int, sentence_id: str) -> dict:
    return {
        "index": index,
        "id": sentence_id,
        "status": "committed",
        "accepted": [],
        "rejected": [],
        "cleared": [],
        "goal": [],
        "hidden": False,
    }


def _argument_contents(argument: list[dict]) -> list[str]:
    """Roxana argument sentences: space-joined indices, justifiers first, conclusion last."""
    justified = [s for s in argument if s.get("justifiers")]
    justified.sort(key=lambda s: int(s["symbol"]))
    return [" ".join([*s["justifiers"], s["symbol"]]) for s in justified]


def run_export(path: Path, dry_run: bool = False) -> int:
    from mdc.argue import markdown_to_argument
    from mdc.config import load_config
    from mdc import roxana_client

    if path.name.endswith(".argument.md"):
        companion = path
    elif path.name.endswith(".document.md") or path.suffix.lower() == ".md":
        companion = path.with_suffix("").with_suffix(".argument.md")
    else:
        print(f"Error: '{path}' does not have a .md extension.")
        return 1

    if not companion.exists():
        print(f"Error: '{companion.name}' does not exist. Run 'mdc argue {_primary(companion).name}' first.")
        return 1

    try:
        args_dict = markdown_to_argument(companion.read_text(encoding="utf-8"))
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    argument = args_dict["argument"]
    proposition_contents = [s["proposition"] for s in sorted(argument, key=lambda s: int(s["symbol"]))]
    argument_contents = _argument_contents(argument)

    if dry_run:
        print("Propositions:")
        for i, content in enumerate(proposition_contents, start=1):
            print(f"  {i}: {content}")
        print("Arguments:")
        for i, content in enumerate(argument_contents, start=1):
            print(f"  {i}: {content}")
        print("Dry run — nothing sent to Roxana.")
        return 0

    config = load_config()
    if not config.roxana_api_url or not config.roxana_api_key:
        print("Error: set roxana_api_url and roxana_api_key in ~/.config/mdc/config.toml.")
        return 1
    url, api_key = config.roxana_api_url, config.roxana_api_key

    try:
        for _ in range(_ID_ATTEMPTS):
            discussion_id = roxana_client.generate_discussion_id()
            if not roxana_client.discussion_exists(url, api_key, discussion_id):
                break
        else:
            print("Error: could not find an available discussion id.", file=sys.stderr)
            return 1

        created_ids: list[str] = []
        try:
            proposition_entries = []
            for index, content in enumerate(proposition_contents, start=1):
                sentence_id = roxana_client.create_sentence(url, api_key, content, discussion_id)
                created_ids.append(sentence_id)
                proposition_entries.append(_layout_entry(index, sentence_id))
            argument_entries = []
            for index, content in enumerate(argument_contents, start=1):
                sentence_id = roxana_client.create_sentence(url, api_key, content, discussion_id)
                created_ids.append(sentence_id)
                argument_entries.append(_layout_entry(index, sentence_id))
            layout = json.dumps({"propositions": proposition_entries, "arguments": argument_entries})
            roxana_client.create_discussion(url, api_key, discussion_id, layout)
        except RuntimeError:
            for sentence_id in created_ids:
                try:
                    roxana_client.delete_sentence(url, api_key, sentence_id)
                except RuntimeError:
                    pass
            raise
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    print(f"Exported {len(proposition_entries)} propositions, {len(argument_entries)} arguments "
          f"to discussion {discussion_id}.")
    if config.roxana_web_url:
        print(f"{config.roxana_web_url.rstrip('/')}/discussions/{discussion_id}")
    return 0
