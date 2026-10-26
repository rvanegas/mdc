from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import json
import os
from openai import APIStatusError, OpenAI

from mdc.asset_cache import AssetCache
from mdc.assets import LocalAssetReference


@dataclass(frozen=True)
class OpenAIReply:
    text: str
    input_tokens: int | None
    output_tokens: int | None


ASSET_FILE_EXPIRATION = {"anchor": "created_at", "seconds": 3 * 24 * 60 * 60}


@dataclass
class OpenAIChatClient:
    model: str
    api_key: str | None = None
    reasoning_effort: str | None = None
    text_verbosity: str | None = None

    def __post_init__(self) -> None:
        api_key = os.environ.get("OPENAI_API_KEY") or self.api_key
        self._client = OpenAI(api_key=api_key)
        self._asset_cache = AssetCache("asset-cache.json")

    def generate_reply(
        self,
        messages: list[dict[str, object]],
        on_delta: Callable[[str], None] | None = None,
    ) -> OpenAIReply:
        chunks: list[str] = []
        request: dict[str, object] = {"model": self.model, "input": messages}
        if self.reasoning_effort is not None:
            request["reasoning"] = {"effort": self.reasoning_effort}
        if self.text_verbosity is not None:
            request["text"] = {"verbosity": self.text_verbosity}

        input_tokens: int | None = None
        output_tokens: int | None = None
        try:
            with self._client.responses.stream(**request) as stream:
                for event in stream:
                    if event.type == "response.output_text.delta":
                        chunks.append(event.delta)
                        if on_delta is not None:
                            on_delta(event.delta)
                response = stream.get_final_response()
            text = response.output_text.strip()
            if not text and chunks:
                text = "".join(chunks).strip()
            if response.usage is not None:
                input_tokens = response.usage.input_tokens
                output_tokens = response.usage.output_tokens
        except KeyboardInterrupt:
            if not chunks:
                raise
            text = "".join(chunks).strip()

        if not text:
            raise RuntimeError("The OpenAI API returned an empty reply.")
        return OpenAIReply(text=text, input_tokens=input_tokens, output_tokens=output_tokens)

    def ensure_asset_file_id(self, asset: LocalAssetReference) -> str:
        return self.ensure_asset_file(asset).file_id

    def ensure_asset_file(self, asset: LocalAssetReference) -> "_ResolvedAssetFile":
        cached = self._asset_cache.lookup(asset)
        if cached is not None:
            return _ResolvedAssetFile(file_id=cached, cache_hit=True)

        with asset.path.open("rb") as handle:
            uploaded = self._client.files.create(
                file=handle,
                purpose="user_data",
                expires_after=ASSET_FILE_EXPIRATION,
            )

        self._asset_cache.store(asset, uploaded.id)
        return _ResolvedAssetFile(file_id=uploaded.id, cache_hit=False)

    def invalidate_asset_file(self, asset: LocalAssetReference) -> None:
        self._asset_cache.delete(asset)

    def is_retriable_asset_error(self, exc: Exception) -> bool:
        if not isinstance(exc, APIStatusError):
            return False

        haystacks = [str(exc)]
        if exc.body is not None:
            try:
                haystacks.append(json.dumps(exc.body, sort_keys=True))
            except TypeError:
                haystacks.append(str(exc.body))

        combined = " ".join(haystacks).lower()
        mentions_file = "file" in combined
        missing_or_expired = any(
            phrase in combined
            for phrase in (
                "not found",
                "does not exist",
                "no such file",
                "invalid file",
                "unknown file",
                "expired",
            )
        )
        return mentions_file and missing_or_expired


@dataclass(frozen=True)
class _ResolvedAssetFile:
    file_id: str
    cache_hit: bool


