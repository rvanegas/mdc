from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import os

import anthropic


# Maps CLI reasoning_effort levels to Anthropic effort values.
_EFFORT_MAP: dict[str, str] = {
    "low":   "low",
    "medium": "medium",
    "high":  "high",
    "xhigh": "max",
}

_THINKING_MAX_TOKENS = 32_000
_DEFAULT_MAX_TOKENS = 16_000


def _thinking_params(
    reasoning_effort: str | None,
) -> tuple[dict[str, object] | None, dict[str, object] | None, int]:
    """Return (thinking_kwarg, output_config_kwarg, max_tokens).

    Returns (None, None, _DEFAULT_MAX_TOKENS) when thinking is disabled.
    """
    if not reasoning_effort or reasoning_effort == "none":
        return None, None, _DEFAULT_MAX_TOKENS
    return (
        {"type": "adaptive"},
        {"effort": _EFFORT_MAP[reasoning_effort]},
        _THINKING_MAX_TOKENS,
    )


@dataclass
class AnthropicReply:
    text: str
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int = field(default=0)
    cache_read_tokens: int = field(default=0)


@dataclass
class AnthropicChatClient:
    model: str
    api_key: str | None = None

    def __post_init__(self) -> None:
        api_key = os.environ.get("ANTHROPIC_API_KEY") or self.api_key
        self._client = anthropic.Anthropic(api_key=api_key)

    def generate_reply(
        self,
        system: str | list[dict[str, object]],
        messages: list[dict[str, object]],
        on_delta: Callable[[str], None] | None = None,
        reasoning_effort: str | None = None,
    ) -> AnthropicReply:
        thinking, output_config, max_tokens = _thinking_params(reasoning_effort)
        stream_kwargs: dict[str, object] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
        }
        if thinking is not None:
            stream_kwargs["thinking"] = thinking
        if output_config is not None:
            stream_kwargs["output_config"] = output_config

        chunks: list[str] = []
        final_message = None
        try:
            with self._client.messages.stream(**stream_kwargs) as stream:  # type: ignore[arg-type]
                for text in stream.text_stream:
                    chunks.append(text)
                    if on_delta is not None:
                        on_delta(text)
                final_message = stream.get_final_message()
        except KeyboardInterrupt:
            if not chunks:
                raise

        text = "".join(chunks).strip()
        if not text:
            raise RuntimeError("The Anthropic API returned an empty reply.")

        usage = final_message.usage if final_message is not None else None
        return AnthropicReply(
            text=text,
            input_tokens=usage.input_tokens if usage else 0,
            output_tokens=usage.output_tokens if usage else 0,
            cache_creation_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0 if usage else 0,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0 if usage else 0,
        )
