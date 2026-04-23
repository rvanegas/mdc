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


def _content_block_dict(block: object) -> dict[str, object]:
    """Serialize a content block to only the fields the API accepts on round-trip."""
    t = getattr(block, "type", None)
    if t == "text":
        return {"type": "text", "text": block.text}  # type: ignore[union-attr]
    if t == "tool_use":
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": dict(block.input)}  # type: ignore[union-attr]
    if t == "thinking":
        return {"type": "thinking", "thinking": block.thinking, "signature": block.signature}  # type: ignore[union-attr]
    return block.model_dump()  # type: ignore[union-attr]


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
        tools: list[dict[str, object]] | None = None,
        tool_executor: Callable[[str, dict[str, object]], str] | None = None,
        post_batch: Callable[[], None] | None = None,
    ) -> AnthropicReply:
        thinking, output_config, max_tokens = _thinking_params(reasoning_effort)
        stream_kwargs: dict[str, object] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": list(messages),
        }
        if thinking is not None:
            stream_kwargs["thinking"] = thinking
        if output_config is not None:
            stream_kwargs["output_config"] = output_config
        if tools:
            stream_kwargs["tools"] = tools

        total_input = total_output = total_cache_create = total_cache_read = 0
        all_text_chunks: list[str] = []

        for _ in range(10):
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
                raise

            if all_text_chunks and chunks:
                all_text_chunks.append("\n\n")
            all_text_chunks.extend(chunks)

            usage = final_message.usage if final_message is not None else None
            total_input += usage.input_tokens if usage else 0
            total_output += usage.output_tokens if usage else 0
            total_cache_create += getattr(usage, "cache_creation_input_tokens", 0) or 0 if usage else 0
            total_cache_read += getattr(usage, "cache_read_input_tokens", 0) or 0 if usage else 0

            if final_message is None or final_message.stop_reason != "tool_use" or not tool_executor:
                break

            tool_uses = [b for b in final_message.content if b.type == "tool_use"]
            tool_results = []
            for tu in tool_uses:
                if on_delta is not None:
                    on_delta(f"\n[{tu.name}]\n")
                result = tool_executor(tu.name, dict(tu.input))
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": result,
                })

            if post_batch is not None:
                post_batch()

            msgs = stream_kwargs["messages"]
            assert isinstance(msgs, list)
            msgs.append({"role": "assistant", "content": [_content_block_dict(b) for b in final_message.content]})
            msgs.append({"role": "user", "content": tool_results})

        text = "".join(all_text_chunks).strip()
        if not text:
            raise RuntimeError("The Anthropic API returned an empty reply.")

        return AnthropicReply(
            text=text,
            input_tokens=total_input,
            output_tokens=total_output,
            cache_creation_tokens=total_cache_create,
            cache_read_tokens=total_cache_read,
        )
