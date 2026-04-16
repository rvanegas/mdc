from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import json
import os
from openai import APIStatusError, OpenAI
from pathlib import Path

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
        self._asset_cache = _AssetCache()

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


@dataclass
class _AssetCacheEntry:
    file_id: str
    sha256: str
    size: int
    mtime_ns: int
    kind: str


class _AssetCache:
    def __init__(self) -> None:
        self._path = Path("~/.cache/mdc/asset-cache.json").expanduser()
        self._entries = self._load()

    def lookup(self, asset: LocalAssetReference) -> str | None:
        key = str(asset.path)
        entry = self._entries.get(key)
        if entry is None:
            return None

        stat = asset.path.stat()
        if entry.kind != asset.kind or entry.size != stat.st_size or entry.mtime_ns != stat.st_mtime_ns:
            return None

        sha256 = _sha256_file(asset.path)
        if entry.sha256 != sha256:
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

    def delete(self, asset: LocalAssetReference) -> None:
        key = str(asset.path)
        if key not in self._entries:
            return
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
