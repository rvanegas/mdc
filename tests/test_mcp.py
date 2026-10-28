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


def call(url, token, tool, args, path="/mcp"):
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client

    async def go():
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        async with create_mcp_http_client(headers=headers) as http:
            async with streamable_http_client(f"{url}{path}", http_client=http) as streams:
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
    tools = {t.name: t.description for t in call(url, "ann", None, None).tools}
    assert list(tools) == ["guide", "list_documents", "lookup_term", "read_document",
                           "read_file"]
    # claude.ai shows the model tool descriptions, not server instructions:
    # the guidance travels in guide, and the essentials in the descriptions.
    assert "<date>-<slug>.<role>.<ext>" in text(call(url, "ann", "guide", {}))
    assert "<date>-<slug>.<role>.<ext>" in tools["list_documents"]
    assert "| *Exact Title*" in tools["read_document"]
    assert httpx.post(f"{url}/mcp", json={}).status_code == 401
    assert httpx.post(f"{url}/mcp", json={}, headers={"Authorization": "Bearer nope"}).status_code == 401
    meta = httpx.get(f"{url}/.well-known/oauth-protected-resource/mcp").json()
    assert meta["authorization_servers"] == [url] or meta["authorization_servers"] == [url + "/"]


def test_reading(library):
    root, url = library
    assert "2026-10-27-essay.document.md — Essay" in text(call(url, "ann", "list_documents", {}))
    assert "Essay" in text(call(url, "ann", "list_documents", {"query": "prose"}))
    assert "Essay" in text(call(url, "ann", "lookup_term", {"term": "prose"}))
    assert "Prose here." in text(call(url, "ann", "read_file", {"name": "2026-10-27-essay.document.md"}))
    assert "Prose here." in text(call(url, "ann", "read_document", {"path": "2026/2026-10-27-essay.document.md"}))
    bad = call(url, "ann", "read_file", {"name": "../users.toml"})
    assert bad.is_error


def test_nothing_writes(library):
    # claude.ai edits with its own documents, and a conversation reaches the
    # library by its export and mdc import: edit_file is mdc reply's alone,
    # and append_turn is gone.
    root, url = library
    for tool, args in (
        ("edit_file", {"name": "2026-10-27-essay.document.md", "old_str": "Prose here.", "new_str": "Edited."}),
        ("append_turn", {"name": "2025-04-25-old-roman.chat.md", "speaker": "Prompt", "text": "Hi."}),
    ):
        assert call(url, "ann", tool, args).is_error
        assert tool not in text(call(url, "ann", "guide", {}))
    assert (root / "2026" / "2026-10-27-essay.document.md").read_text() == DOC
    assert (root / "2025" / "2025-04-25-old-roman.chat.md").read_text() == CHAT


def test_another_users_library_is_out_of_reach(library):
    root, url = library
    result = call(url, "bob", "read_file", {"name": "2026-10-27-essay.document.md", "library": "ann/philosophy"})
    assert result.is_error and "not your library" in text(result)


def _pkce():
    import base64
    import secrets
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


CALLBACK = "https://claude.ai/api/mcp/auth_callback"


def _register(url, redirect=CALLBACK):
    return httpx.post(f"{url}/register", json={
        "redirect_uris": [redirect], "client_name": "Claude",
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
        "token_endpoint_auth_method": "client_secret_post"})


def _authorize(url, client, challenge, state="s1"):
    r = httpx.get(f"{url}/authorize", params={
        "response_type": "code", "client_id": client["client_id"], "redirect_uri": CALLBACK,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state})
    assert r.status_code == 302, r.text
    login = r.headers["location"]
    assert "/oauth/login?request=" in login
    return login.split("request=")[1]


def test_claude_ai_signs_in_with_an_mdc_token(library, live):
    from urllib.parse import parse_qs, urlparse
    data, url = live
    root, _ = library
    assert "token_endpoint" in httpx.get(f"{url}/.well-known/oauth-authorization-server").json()
    assert _register(url, "https://evil.example/cb").status_code == 400
    client = _register(url).json()
    verifier, challenge = _pkce()
    request_id = _authorize(url, client, challenge)
    page = httpx.get(f"{url}/oauth/login", params={"request": request_id})
    assert page.status_code == 200 and "Claude" in page.text and "claude.ai" in page.text
    wrong = httpx.post(f"{url}/oauth/login", data={"request": request_id, "token": "nope", "action": "allow"})
    assert wrong.status_code == 200 and "not an mdc token" in wrong.text
    back = httpx.post(f"{url}/oauth/login", data={"request": request_id, "token": "ann", "action": "allow"})
    assert back.status_code == 302 and back.headers["location"].startswith(CALLBACK)
    query = parse_qs(urlparse(back.headers["location"]).query)
    assert query["state"] == ["s1"]
    tokens = httpx.post(f"{url}/token", data={
        "grant_type": "authorization_code", "code": query["code"][0], "redirect_uri": CALLBACK,
        "client_id": client["client_id"], "client_secret": client["client_secret"],
        "code_verifier": verifier}).json()
    assert tokens["token_type"] == "Bearer" and tokens["expires_in"] == 3600
    # The OAuth token reaches the library as its user.
    assert "Prose here." in text(call(url, tokens["access_token"], "read_file", {"name": "2026-10-27-essay.document.md"}))
    # Refresh rotates: the new pair works, the old refresh token does not.
    refreshed = httpx.post(f"{url}/token", data={
        "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
        "client_id": client["client_id"], "client_secret": client["client_secret"]}).json()
    assert "Prose here." in text(call(url, refreshed["access_token"], "read_file", {"name": "2026-10-27-essay.document.md"}))
    again = httpx.post(f"{url}/token", data={
        "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
        "client_id": client["client_id"], "client_secret": client["client_secret"]})
    assert again.status_code == 400
    # Taking the person's mdc token away takes their OAuth access with it.
    users = data / "users.toml"
    kept = users.read_text()
    users.write_text(f'[[tokens]]\nuser = "bob"\nsha256 = "{hashlib.sha256(b"bob").hexdigest()}"\n')
    try:
        assert httpx.post(f"{url}/mcp", json={}, headers={
            "Authorization": f"Bearer {refreshed['access_token']}", "Content-Type": "application/json"}).status_code == 401
    finally:
        users.write_text(kept)


def test_deny_sends_access_denied(library, live):
    data, url = live
    client = _register(url).json()
    _, challenge = _pkce()
    request_id = _authorize(url, client, challenge, state="s2")
    back = httpx.post(f"{url}/oauth/login", data={"request": request_id, "action": "deny"})
    assert back.status_code == 302 and "error=access_denied" in back.headers["location"] and "state=s2" in back.headers["location"]
