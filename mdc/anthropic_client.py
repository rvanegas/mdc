from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path

import anthropic

from mdc.assets import LocalAssetReference


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


_FILES_BETA = "files-api-2025-04-14"


@dataclass(frozen=True)
class _ResolvedAssetFile:
    file_id: str
    cache_hit: bool


@dataclass
class _AssetCacheEntry:
    file_id: str
    sha256: str
    size: int
    mtime_ns: int
    kind: str


class _AnthropicAssetCache:
    def __init__(self) -> None:
        from mdc.config import _cache_dir
        self._path = _cache_dir / "anthropic-asset-cache.json"
        self._entries = self._load()

    def lookup(self, asset: LocalAssetReference) -> str | None:
        key = str(asset.path)
        entry = self._entries.get(key)
        if entry is None:
            return None
        stat = asset.path.stat()
        if entry.kind != asset.kind or entry.size != stat.st_size or entry.mtime_ns != stat.st_mtime_ns:
            return None
        if entry.sha256 != _sha256_file(asset.path):
            return None
        return entry.file_id

    def store(self, asset: LocalAssetReference, file_id: str) -> None:
        stat = asset.path.stat()
        self._entries[str(asset.path)] = _AssetCacheEntry(
            file_id=file_id,
            sha256=_sha256_file(asset.path),
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            kind=asset.kind,
        )
        self._persist()

    def all_entries(self) -> dict[str, "_AssetCacheEntry"]:
        return dict(self._entries)

    def delete(self, asset: LocalAssetReference) -> None:
        key = str(asset.path)
        if key in self._entries:
            del self._entries[key]
            self._persist()

    def _load(self) -> dict[str, _AssetCacheEntry]:
        if not self._path.exists():
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        entries: dict[str, _AssetCacheEntry] = {}
        for key, value in data.items():
            if not isinstance(value, dict):
                continue
            try:
                entries[key] = _AssetCacheEntry(
                    file_id=str(value["file_id"]),
                    sha256=str(value["sha256"]),
                    size=int(value["size"]),
                    mtime_ns=int(value["mtime_ns"]),
                    kind=str(value["kind"]),
                )
            except (KeyError, TypeError, ValueError):
                continue
        return entries

    def _persist(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            key: {
                "file_id": entry.file_id,
                "sha256": entry.sha256,
                "size": entry.size,
                "mtime_ns": entry.mtime_ns,
                "kind": entry.kind,
            }
            for key, entry in self._entries.items()
        }
        self._path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class AnthropicChatClient:
    model: str
    api_key: str | None = None

    def __post_init__(self) -> None:
        api_key = os.environ.get("ANTHROPIC_API_KEY") or self.api_key
        self._client = anthropic.Anthropic(api_key=api_key)
        self._asset_cache = _AnthropicAssetCache()

    def ensure_asset_file(self, asset: LocalAssetReference) -> _ResolvedAssetFile:
        cached = self._asset_cache.lookup(asset)
        if cached is not None:
            return _ResolvedAssetFile(file_id=cached, cache_hit=True)
        mime = _asset_mime_type(asset)
        with asset.path.open("rb") as handle:
            uploaded = self._client.beta.files.upload(
                file=(asset.path.name, handle, mime),
            )
        self._asset_cache.store(asset, uploaded.id)
        return _ResolvedAssetFile(file_id=uploaded.id, cache_hit=False)

    def ensure_asset_file_id(self, asset: LocalAssetReference) -> str:
        return self.ensure_asset_file(asset).file_id

    def invalidate_asset_file(self, asset: LocalAssetReference) -> None:
        self._asset_cache.delete(asset)

    def generate_reply(
        self,
        system: str | list[dict[str, object]],
        messages: list[dict[str, object]],
        on_delta: Callable[[str], None] | None = None,
        reasoning_effort: str | None = None,
        tools: list[dict[str, object]] | None = None,
        tool_executor: Callable[[str, dict[str, object]], str] | None = None,
        post_batch: Callable[[], None] | None = None,
        format_tool_annotation: Callable[[str, dict[str, object]], str] | None = None,
        use_files_api: bool = False,
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
        if use_files_api:
            stream_kwargs["betas"] = [_FILES_BETA]

        streamer = self._client.beta.messages if use_files_api else self._client.messages

        total_input = total_output = total_cache_create = total_cache_read = 0
        all_text_chunks: list[str] = []

        for _ in range(10):
            chunks: list[str] = []
            final_message = None
            try:
                with streamer.stream(**stream_kwargs) as stream:  # type: ignore[arg-type]
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
            tool_annotations: list[str] = []
            for tu in tool_uses:
                label = format_tool_annotation(tu.name, dict(tu.input)) if format_tool_annotation else f"[{tu.name}]"
                prefix = "\n\n" if not tool_annotations else "\n"
                marker = f"{prefix}| {label}"
                if on_delta is not None:
                    on_delta(marker)
                tool_annotations.append(marker)
                result = tool_executor(tu.name, dict(tu.input))
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": result,
                })
            all_text_chunks.extend(tool_annotations)

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


def _asset_mime_type(asset: LocalAssetReference) -> str:
    from mdc.assets import IMAGE_MEDIA_TYPES
    if asset.kind == "pdf":
        return "application/pdf"
    if asset.kind == "image":
        return IMAGE_MEDIA_TYPES[asset.path.suffix.lower()]
    return "text/plain"
