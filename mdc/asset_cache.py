"""Remote file IDs of uploaded assets, keyed by content.

An uploaded file is identified by its bytes, not by where it lies or when it
was touched: a copy, a sync, a rename or a move into a year directory leaves
it the same file, and the same bytes linked from two places are uploaded
once. The path is kept only as a label for the person reading the cache.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from mdc.assets import LocalAssetReference


@dataclass
class AssetCacheEntry:
    file_id: str
    kind: str
    size: int
    path: str


class AssetCache:
    def __init__(self, filename: str) -> None:
        from mdc.config import _cache_dir
        self._path = _cache_dir / filename
        self._entries = self._load()
        # One hash per file and run: lookup and store both need it.
        self._hashes: dict[tuple[Path, int, int], str] = {}

    def lookup(self, asset: LocalAssetReference) -> str | None:
        entry = self._entries.get(self._sha256(asset.path))
        if entry is None or entry.kind != asset.kind:
            return None
        return entry.file_id

    def store(self, asset: LocalAssetReference, file_id: str) -> None:
        self._entries[self._sha256(asset.path)] = AssetCacheEntry(
            file_id=file_id,
            kind=asset.kind,
            size=asset.path.stat().st_size,
            path=str(asset.path),
        )
        self._persist()

    def delete(self, asset: LocalAssetReference) -> None:
        if self._entries.pop(self._sha256(asset.path), None) is not None:
            self._persist()

    def _sha256(self, path: Path) -> str:
        stat = path.stat()
        key = (path, stat.st_size, stat.st_mtime_ns)
        if key not in self._hashes:
            self._hashes[key] = sha256_file(path)
        return self._hashes[key]

    def _load(self) -> dict[str, AssetCacheEntry]:
        if not self._path.exists():
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        entries: dict[str, AssetCacheEntry] = {}
        for sha256, value in data.items():
            try:
                entries[sha256] = AssetCacheEntry(
                    file_id=str(value["file_id"]),
                    kind=str(value["kind"]),
                    size=int(value["size"]),
                    path=str(value["path"]),
                )
            except (KeyError, TypeError, ValueError):
                continue
        return entries

    def _persist(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            sha256: {"file_id": e.file_id, "kind": e.kind, "size": e.size, "path": e.path}
            for sha256, e in self._entries.items()
        }
        self._path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
