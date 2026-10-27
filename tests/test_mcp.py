"""The MCP server at /mcp (SERVER.md, Phase 5), driven by the SDK's own client
over HTTP, as Claude Code or a claude.ai connector would."""

import asyncio
import hashlib
import json
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

from mdc.form import check_file

CHAT = "\n# Old Roman\n2025-04-25\n\n## Prompt\n\nHello.\n\n## Claude\n\nHi.\n\n## Related\n\n| *Essay*\n"
DOC = "\n# Essay\n2026-10-27\n\nProse here.\n"


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    data = tmp_path_factory.mktemp("data")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    mp = pytest.MonkeyPatch()
    mp.setenv("MDC_DATA_DIR", str(data))
    mp.setenv("MDC_CONFIG_DIR", str(data / "config"))
    mp.setenv("MDC_PUBLIC_URL", f"http://127.0.0.1:{port}")
    (data / "users.toml").write_text(
        f'[[tokens]]\nuser = "ann"\nsha256 = "{hashlib.sha256(b"ann").hexdigest()}"\n'
        f'[[tokens]]\nuser = "bob"\nsha256 = "{hashlib.sha256(b"bob").hexdigest()}"\n'
    )
    from mdc import server
    uv = uvicorn.Server(uvicorn.Config(server.app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=uv.run, daemon=True)
    thread.start()
    while not uv.started:
        time.sleep(0.05)
    yield data, f"http://127.0.0.1:{port}"
    uv.should_exit = True
    thread.join(5)
    mp.undo()


@pytest.fixture
def library(live):
    data, url = live
    root = data / "libraries" / "ann" / "philosophy"
    import shutil
    shutil.rmtree(root, ignore_errors=True)
    (root / "2025").mkdir(parents=True)
    (root / "2026").mkdir()
    (root / "2025" / "2025-04-25-old-roman.chat.md").write_text(CHAT)
    (root / "2026" / "2026-10-27-essay.document.md").write_text(DOC)
    (root / "2026" / "2026-10-19-parfit2011.source.md").write_text("\n# Parfit\n2026-10-19\n\nNot yours to edit.\n")
    (root / ".mdc").mkdir()
    (root / ".mdc" / "manifest.json").write_text(json.dumps({"entries": [
        {"rel_path": "2026/2026-10-27-essay.document.md", "title": "Essay", "word_count": 2,
         "summary": "On prose.", "terms": ["prose"], "refs": [], "related": []},
    ]}))
    (root / ".mdc" / "index.json").write_text(json.dumps({"terms": {
        "prose": [{"rel_path": "2026/2026-10-27-essay.document.md", "title": "Essay"}]}}))
    (data / "libraries" / "bob" / "notes").mkdir(parents=True, exist_ok=True)
    return root, url


def call(url, token, tool, args):
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client

    async def go():
        async with create_mcp_http_client(headers={"Authorization": f"Bearer {token}"}) as http:
            async with streamable_http_client(f"{url}/mcp", http_client=http) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    if tool is None:
                        return await session.list_tools()
                    return await session.call_tool(tool, args)

    return asyncio.run(go())


def text(result) -> str:
    return "\n".join(c.text for c in result.content)


def test_tools_and_auth(library):
    root, url = library
    assert [t.name for t in call(url, "ann", None, None).tools] == [
        "list_documents", "lookup_term", "read_document", "read_file", "edit_file", "append_turn"]
    assert httpx.post(f"{url}/mcp", json={}).status_code == 401
    assert httpx.post(f"{url}/mcp", json={}, headers={"Authorization": "Bearer nope"}).status_code == 401
    meta = httpx.get(f"{url}/.well-known/oauth-protected-resource/mcp").json()
    assert meta["resource"] == f"{url}/mcp"
    assert meta["authorization_servers"][0].startswith("https://cognito-idp.")


def test_reading(library):
    root, url = library
    assert "2026-10-27-essay.document.md — Essay" in text(call(url, "ann", "list_documents", {}))
    assert "Essay" in text(call(url, "ann", "list_documents", {"query": "prose"}))
    assert "Essay" in text(call(url, "ann", "lookup_term", {"term": "prose"}))
    assert "Prose here." in text(call(url, "ann", "read_file", {"name": "2026-10-27-essay.document.md"}))
    assert "Prose here." in text(call(url, "ann", "read_document", {"path": "2026/2026-10-27-essay.document.md"}))
    bad = call(url, "ann", "read_file", {"name": "../users.toml"})
    assert bad.is_error


def test_edit_keeps_a_backup_and_spares_sources(library):
    root, url = library
    result = call(url, "ann", "edit_file", {"name": "2026-10-27-essay.document.md",
                                             "old_str": "Prose here.", "new_str": "Prose, edited."})
    assert not result.is_error and "Prose, edited." in text(result)
    assert "Prose, edited." in (root / "2026" / "2026-10-27-essay.document.md").read_text()
    assert (root / "REVISIONS" / "2026-10-27-essay.document--1.md").read_text() == DOC
    assert "Prose, edited." in (root / "REVISIONS" / "2026-10-27-essay.document--2.md").read_text()
    for name in ("2026-10-19-parfit2011.source.md", "2025-04-25-old-roman.chat.md"):
        assert call(url, "ann", "edit_file", {"name": name, "old_str": "a", "new_str": "b"}).is_error


def test_append_turn(library):
    root, url = library
    call(url, "ann", "append_turn", {"name": "2025-04-25-old-roman.chat.md", "speaker": "Prompt",
                                     "text": "And then?\n\n## A heading"})
    chat = (root / "2025" / "2025-04-25-old-roman.chat.md").read_text()
    assert chat.index("And then?") < chat.index("## Related")
    assert "### A heading" in chat
    call(url, "ann", "append_turn", {"name": "2026-10-27-a-new-talk.chat.md", "speaker": "Prompt", "text": "Hi."})
    call(url, "ann", "append_turn", {"name": "2026-10-27-a-new-talk.chat.md", "speaker": "Claude", "text": "Hello."})
    new = root / "2026" / "2026-10-27-a-new-talk.chat.md"
    assert check_file(new) == []
    assert call(url, "ann", "append_turn", {"name": "x.chat.md", "speaker": "two words", "text": "x"}).is_error


def test_writes_wait_for_a_run(library):
    from mdc import service
    root, url = library
    lock = service.lock_for(root)
    asyncio.run(lock.acquire())
    try:
        result = call(url, "ann", "edit_file", {"name": "2026-10-27-essay.document.md",
                                                 "old_str": "Prose", "new_str": "Verse"})
        assert result.is_error and "busy" in text(result)
    finally:
        lock.release()
        service.locks.clear()


def test_another_users_library_is_out_of_reach(library):
    root, url = library
    result = call(url, "bob", "read_file", {"name": "2026-10-27-essay.document.md", "library": "ann/philosophy"})
    assert result.is_error and "not your library" in text(result)
