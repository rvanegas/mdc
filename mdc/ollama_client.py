from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from openai import OpenAI


@dataclass(frozen=True)
class OllamaReply:
    text: str
    input_tokens: int | None
    output_tokens: int | None


@dataclass
class OllamaChatClient:
    model: str
    base_url: str = "http://localhost:11434/v1"

    def __post_init__(self) -> None:
        self._client = OpenAI(base_url=self.base_url, api_key="ollama")

    def generate_reply(
        self,
        messages: list[dict[str, object]],
        on_delta: Callable[[str], None] | None = None,
    ) -> OllamaReply:
        chunks: list[str] = []
        try:
            stream = self._client.chat.completions.create(
                model=self.model,
                messages=messages,  # type: ignore[arg-type]
                stream=True,
            )
            for chunk in stream:
                delta = chunk.choices[0].delta.content if chunk.choices else None
                if delta:
                    chunks.append(delta)
                    if on_delta is not None:
                        on_delta(delta)
        except KeyboardInterrupt:
            if not chunks:
                raise

        text = "".join(chunks).strip()
        if not text:
            raise RuntimeError("Ollama returned an empty reply.")
        return OllamaReply(text=text, input_tokens=None, output_tokens=None)
