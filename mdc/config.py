from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Final
import sys
import tomllib


def _platform_dir(xdg_subpath: str, win_fn: str) -> Path:
    if sys.platform == "win32":
        import platformdirs
        return Path(getattr(platformdirs, win_fn)("mdc", appauthor=False))
    return Path.home() / xdg_subpath / "mdc"


_config_dir = _platform_dir(".config", "user_config_dir")
_state_dir  = _platform_dir(".local/state", "user_state_dir")
_cache_dir  = _platform_dir(".cache", "user_cache_dir")


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


DEFAULT_CONFIG_PATH: Final[Path] = _config_dir / "config.toml"
DEFAULT_SYSTEM_PROMPT_PATH: Final[Path] = _config_dir / "system.md"

_FALLBACK_SYSTEM_PROMPT = (
    "You are continuing a hand-edited markdown chat transcript. "
    "Respond helpfully to the latest human message."
)

_REFERENCE_INSTRUCTION = """\
Place a brief list of references at the end of the response, formatted as follows:

| Last, First (year) *Title* optional text
| Last1, First1, First2 Last2, First3 Last3 (year) *Title* optional text

Year may be a single year (1989), a range (53-55), a slash pair (1781/1787), or approximate (c. 385-370 BCE)."""


@dataclass(frozen=True)
class AppConfig:
    model: str | None = None
    system_prompt: str = _FALLBACK_SYSTEM_PROMPT
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    ollama_base_url: str = "http://localhost:11434/v1"
    library_path: Path | None = None
    index_model: str = "claude-haiku-4-5"
    user_names: tuple[str, ...] = ("Prompt", "Rodrigo")
    llm_names: tuple[str, ...] = ("Claude", "GPT")
    wrap_width: int = 100
    revision_retention_days: int = 7


def _write_default_config(path: Path) -> None:
    from importlib.resources import files
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(files("mdc").joinpath("config.example.toml").read_text(encoding="utf-8"), encoding="utf-8")


def _write_default_system_prompt(path: Path) -> None:
    from importlib.resources import files
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(files("mdc").joinpath("system.example.md").read_text(encoding="utf-8"), encoding="utf-8")


def load_config() -> AppConfig:
    config_path = DEFAULT_CONFIG_PATH
    data: dict[str, object] = {}
    if not config_path.exists():
        _write_default_config(config_path)
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

    system_prompt = _load_system_prompt(DEFAULT_SYSTEM_PROMPT_PATH)

    raw_library = str(data.get("library_path", "")).strip()
    library_path = Path(raw_library).expanduser().resolve() if raw_library else None
    index_model = str(data.get("index_model", "")).strip() or "claude-haiku-4-5"

    user_names = tuple(str(n).strip() for n in data.get("user_names", ["Prompt", "Rodrigo"]) if str(n).strip())
    llm_names  = tuple(str(n).strip() for n in data.get("llm_names",  ["Claude", "GPT"])     if str(n).strip())
    wrap_width = int(data.get("wrap_width", 100))
    revision_retention_days = int(data.get("revision_retention_days", 7))

    return AppConfig(
        model=model,
        system_prompt=system_prompt,
        openai_api_key=openai_api_key,
        anthropic_api_key=anthropic_api_key,
        ollama_base_url=ollama_base_url,
        library_path=library_path,
        index_model=index_model,
        user_names=user_names,
        llm_names=llm_names,
        wrap_width=wrap_width,
        revision_retention_days=revision_retention_days,
    )


def _load_system_prompt(path: Path) -> str:
    if not path.exists():
        base = _FALLBACK_SYSTEM_PROMPT
    else:
        lines = path.read_text(encoding="utf-8").splitlines()
        content = "\n".join(line for line in lines if not line.startswith("//")).strip()
        base = content or _FALLBACK_SYSTEM_PROMPT
    return f"{base}\n\n{_REFERENCE_INSTRUCTION}"
