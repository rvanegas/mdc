from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Final
import sys
import tomllib


def _platform_dir(env_var: str, xdg_subpath: str, win_fn: str) -> Path:
    override = os.environ.get(env_var, "").strip()
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        import platformdirs
        return Path(getattr(platformdirs, win_fn)("mdc", appauthor=False))
    return Path.home() / xdg_subpath / "mdc"


# A server sets both: one system-wide config, one cache for its API key.
_config_dir = _platform_dir("MDC_CONFIG_DIR", ".config", "user_config_dir")
_cache_dir  = _platform_dir("MDC_CACHE_DIR", ".cache", "user_cache_dir")

# Index state lives in the library itself, so each library carries its own.
LIBRARY_STATE_DIRNAME: Final[str] = ".mdc"

# The heading for the user's turns, whoever the user is; their own names come
# from config or, per run, from MDC_USER_NAMES.
PROMPT_HEADING: Final[str] = "Prompt"


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

_BASE_SYSTEM_PROMPT = (
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
    system_prompt: str = _BASE_SYSTEM_PROMPT
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    ollama_base_url: str = "http://localhost:11434/v1"
    library_path: Path | None = None
    index_model: str = "claude-haiku-4-5"
    user_names: tuple[str, ...] = (PROMPT_HEADING,)
    llm_names: tuple[str, ...] = ("Claude", "GPT")
    wrap_width: int = 100
    revision_retention_days: int = 7
    roxana_api_url: str | None = None
    roxana_api_key: str | None = None
    roxana_web_url: str | None = None
    dianoia_api_url: str | None = None
    dianoia_token: str | None = None


def _write_default_config(path: Path) -> None:
    from importlib.resources import files
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(files("mdc").joinpath("config.example.toml").read_text(encoding="utf-8"), encoding="utf-8")


def library_state_dir(library_path: Path) -> Path:
    return library_path / LIBRARY_STATE_DIRNAME


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

    # The run's library: --lib (which sets MDC_LIBRARY), else the config's.
    raw_library = os.environ.get("MDC_LIBRARY", "").strip() or str(data.get("library_path", "")).strip()
    library_path = Path(raw_library).expanduser().resolve() if raw_library else None
    system_prompt = _build_system_prompt(library_path)
    index_model = str(data.get("index_model", "")).strip() or "claude-haiku-4-5"

    # The client names its user per run (MDC_USER_NAMES, comma-separated);
    # the Prompt heading is everyone's.
    raw_names = os.environ.get("MDC_USER_NAMES", "").strip()
    names = raw_names.split(",") if raw_names else data.get("user_names", [])
    user_names = tuple(dict.fromkeys([PROMPT_HEADING, *(str(n).strip() for n in names if str(n).strip())]))
    llm_names  = tuple(str(n).strip() for n in data.get("llm_names",  ["Claude", "GPT"])     if str(n).strip())
    wrap_width = int(data.get("wrap_width", 100))
    revision_retention_days = int(data.get("revision_retention_days", 7))

    roxana_api_url = str(data.get("roxana_api_url", "")).strip() or os.environ.get("ROXANA_API_URL", "").strip() or None
    roxana_api_key = str(data.get("roxana_api_key", "")).strip() or os.environ.get("ROXANA_API_KEY", "").strip() or None
    roxana_web_url = str(data.get("roxana_web_url", "")).strip() or os.environ.get("ROXANA_WEB_URL", "").strip() or None

    dianoia_api_url = str(data.get("dianoia_api_url", "")).strip() or os.environ.get("DIANOIA_API_URL", "").strip() or None
    dianoia_token = str(data.get("dianoia_token", "")).strip() or os.environ.get("DIANOIA_TOKEN", "").strip() or None

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
        roxana_api_url=roxana_api_url,
        roxana_api_key=roxana_api_key,
        roxana_web_url=roxana_web_url,
        dianoia_api_url=dianoia_api_url,
        dianoia_token=dianoia_token,
    )


def _build_system_prompt(library_path: Path | None) -> str:
    """The base prompt, then the library's own SYSTEM_PROMPT.md."""
    prompt = f"{_BASE_SYSTEM_PROMPT}\n\n{_REFERENCE_INSTRUCTION}"
    if library_path:
        lib_prompt_path = library_path / "SYSTEM_PROMPT.md"
        if lib_prompt_path.exists():
            lib_extra = lib_prompt_path.read_text(encoding="utf-8").strip()
            if lib_extra:
                prompt = f"{prompt}\n\n{lib_extra}"
    return prompt
