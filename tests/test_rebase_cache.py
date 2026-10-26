import importlib.util
import json
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "rebase_cache", Path(__file__).resolve().parent.parent / "scripts" / "rebase_cache.py"
)
rebase_cache = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(rebase_cache)


def test_rebases_inside_and_drops_outside(tmp_path):
    source = tmp_path / "cache.json"
    source.write_text(json.dumps({
        "/Users/u/Chats/2026/a.pdf": {"file_id": "file_1"},
        "/Users/u/Chats-other/b.pdf": {"file_id": "file_2"},
        "/elsewhere/c.pdf": {"file_id": "file_3"},
    }))
    dest = tmp_path / "out.json"
    rebase_cache.main([str(source), str(dest), "--old-root", "/Users/u/Chats", "--new-root", "/data/libraries/u/philosophy"])
    assert json.loads(dest.read_text()) == {"/data/libraries/u/philosophy/2026/a.pdf": {"file_id": "file_1"}}
