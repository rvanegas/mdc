from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Final
import tomllib


def _default_assistant_name(model: str) -> str:
    if model.startswith("claude-"):
        return "Claude"
    if model.startswith("ollama/"):
        # e.g. "ollama/llama3.2" -> "Llama", "ollama/mistral" -> "Mistral"
        raw = model.removeprefix("ollama/").split("/")[-1]
        word = raw.split("-")[0].split(":")[0]  # drop variant suffixes
        alpha = "".join(ch for ch in word if ch.isalpha())
        return alpha.capitalize() if alpha else "Ollama"
    return "GPT"


DEFAULT_CONFIG_PATH: Final[Path] = Path("~/.config/mdc/config.toml").expanduser()
DEFAULT_SYSTEM_PROMPT_PATH: Final[Path] = Path("~/.config/mdc/system.md").expanduser()

_FALLBACK_SYSTEM_PROMPT = (
    "You are continuing a hand-edited markdown chat transcript. "
    "Respond helpfully to the latest human message."
)


@dataclass(frozen=True)
class AppConfig:
    model: str | None = None
    system_prompt: str = _FALLBACK_SYSTEM_PROMPT
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    ollama_base_url: str = "http://localhost:11434/v1"


def load_config() -> AppConfig:
    config_path = DEFAULT_CONFIG_PATH
    data: dict[str, object] = {}
    if config_path.exists():
        with config_path.open("rb") as handle:
            loaded = tomllib.load(handle)
            if not isinstance(loaded, dict):
                raise ValueError(f"Invalid config structure in {config_path}")
            data = loaded

    model = str(data.get("model", "")).strip() or None
    openai_api_key = str(data.get("openai_api_key", "")).strip() or os.environ.get("OPENAI_API_KEY", "").strip() or None
    anthropic_api_key = str(data.get("anthropic_api_key", "")).strip() or os.environ.get("ANTHROPIC_API_KEY", "").strip() or None
    ollama_base_url = str(data.get("ollama_base_url", "")).strip() or "http://localhost:11434/v1"

    system_prompt_path = Path(str(data.get("system_prompt_file", "")).strip() or DEFAULT_SYSTEM_PROMPT_PATH).expanduser()
    system_prompt = _load_system_prompt(system_prompt_path)

    return AppConfig(
        model=model,
        system_prompt=system_prompt,
        openai_api_key=openai_api_key,
        anthropic_api_key=anthropic_api_key,
        ollama_base_url=ollama_base_url,
    )


def _load_system_prompt(path: Path) -> str:
    if not path.exists():
        return _FALLBACK_SYSTEM_PROMPT
    content = path.read_text(encoding="utf-8").strip()
    return content or _FALLBACK_SYSTEM_PROMPT
