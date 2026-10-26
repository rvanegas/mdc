import json
import os
from pathlib import Path

import pytest

import mdc.config
from mdc.asset_cache import AssetCache
from mdc.assets import LocalAssetReference


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(mdc.config, "_cache_dir", tmp_path / "cache")
    return tmp_path / "cache"


def _asset(path: Path) -> LocalAssetReference:
    return LocalAssetReference(label="x", raw_target=path.name, path=path, kind="pdf")


def test_a_copy_with_another_name_and_time_hits(cache_dir, tmp_path):
    original = tmp_path / "parfit2011.pdf"
    original.write_bytes(b"%PDF parfit")
    AssetCache("c.json").store(_asset(original), "file_1")
    moved = tmp_path / "2026" / "2026-10-19-parfit2011.pdf"
    moved.parent.mkdir()
    moved.write_bytes(original.read_bytes())
    os.utime(moved, ns=(1_000_000_000, 1_000_000_000))
    assert AssetCache("c.json").lookup(_asset(moved)) == "file_1"


def test_changed_content_misses(cache_dir, tmp_path):
    path = tmp_path / "a.pdf"
    path.write_bytes(b"one")
    cache = AssetCache("c.json")
    cache.store(_asset(path), "file_1")
    path.write_bytes(b"two")
    assert AssetCache("c.json").lookup(_asset(path)) is None


def test_keyed_by_hash_with_path_as_label(cache_dir, tmp_path):
    path = tmp_path / "a.pdf"
    path.write_bytes(b"one")
    AssetCache("c.json").store(_asset(path), "file_1")
    data = json.loads((cache_dir / "c.json").read_text())
    (sha, entry), = data.items()
    assert len(sha) == 64
    assert entry == {"file_id": "file_1", "kind": "pdf", "size": 3, "path": str(path)}


def test_delete(cache_dir, tmp_path):
    path = tmp_path / "a.pdf"
    path.write_bytes(b"one")
    cache = AssetCache("c.json")
    cache.store(_asset(path), "file_1")
    cache.delete(_asset(path))
    assert AssetCache("c.json").lookup(_asset(path)) is None
