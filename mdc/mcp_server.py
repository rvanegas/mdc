"""claude.ai's way into a library: an MCP server at /mcp (SERVER.md, Phase 5).

The reading tools `mdc reply` gives the model, on the caller's libraries, for
Claude anywhere a custom connector reaches (claude.ai on the web and the
phone, Claude Desktop, Claude Code). They run in-process on the server's copy
of the library, which is canonical.

Callers authenticate with a bearer token: an OAuth access token from mdc's
own authorization server (oauth.py; claude.ai's connector signs in there with
the person's mdc token), or a minted mdc token sent as it is (Claude Code).

Nothing here writes. edit_file is `mdc reply`'s alone, and append_turn is
gone: claude.ai edits with its own documents, and a conversation reaches the
library by its export and `mdc import`, which is whole where a turn-by-turn
write depended on Claude remembering to make it.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

from mdc import library as lib
from mdc.oauth import provider
from mdc.service import LibraryError, library_ids, library_root


INSTRUCTIONS = "\n\n".join([
    "These tools reach the user's mdc library: their own writing (chats, documents, "
    "argument files) and the sources they keep. Files are named "
    "<date>-<slug>.<role>.<ext>, the role one of chat, document, source, argument, "
    "analysis, review; a name is unique across the library. Use list_documents to find "
    "names and read_file to read one. Nothing in the library is written from here.",
    lib.LIBRARY_TOOLS_PROMPT,
])


_TOOLS: list = []


def _tool(fn):
    _TOOLS.append(fn)
    return fn


def build() -> MCPServer:
    """A fresh MCP server: a streamable-HTTP session manager runs only once."""
    server = MCPServer(
        name="mdc",
        title="mdc library",
        instructions=INSTRUCTIONS,
        # mdc is its own authorization server (oauth.py), whose provider also
        # takes a minted mdc token sent as it is.
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=public_url(),
            resource_server_url=f"{public_url()}/mcp",
            validate_token_resource=False,  # the provider knows its tokens
            client_registration_options=ClientRegistrationOptions(enabled=True),
            revocation_options=RevocationOptions(enabled=True),
        ),
    )
    for fn in _TOOLS:
        server.add_tool(fn)
    return server


def public_url() -> str:
    return os.environ.get("MDC_PUBLIC_URL", "https://mdc.rvanegas.co").rstrip("/")


class _Mount:
    """The ASGI app mounted in mdc serve; forwards to this lifespan's MCP app."""

    def __init__(self) -> None:
        self.app = None

    async def __call__(self, scope, receive, send) -> None:
        if self.app is None:  # not started (or stopped): nothing is here
            from starlette.responses import PlainTextResponse
            await PlainTextResponse("Not Found", status_code=404)(scope, receive, send)
            return
        await self.app(scope, receive, send)


mcp_app = _Mount()


@asynccontextmanager
async def mcp_lifespan():
    server = build()
    mcp_app.app = server.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[public_url().split("://", 1)[1], "127.0.0.1:*", "localhost:*", "testserver"],
            allowed_origins=["https://claude.ai", "https://claude.com"],
        ),
    )
    async with server.session_manager.run():
        try:
            yield
        finally:
            mcp_app.app = None


# ---------------------------------------------------------------------------
# The caller's library
# ---------------------------------------------------------------------------

def _caller() -> str:
    token = get_access_token()
    if token is None or not token.subject:
        raise ToolError("not authenticated")
    return token.subject


def _root(library: str | None) -> Path:
    who = _caller()
    ids = library_ids(who)
    if library is None:
        if len(ids) != 1:
            raise ToolError(f"name a library: {', '.join(ids) or 'none is yours'}")
        library = ids[0]
    user, _, name = library.partition("/")
    try:
        return library_root(user, name, who)
    except LibraryError as exc:
        raise ToolError(str(exc)) from None


def _find(root: Path, name: str) -> Path:
    from mdc.cli import _library_files
    if "/" in name or name.startswith("."):
        raise ToolError(f"a file is named, not pathed: {name!r}")
    found = [p for p in _library_files(root) if p.name == name]
    if not found:
        raise ToolError(f"no file named {name!r} in the library")
    return found[0]


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@_tool
def guide() -> str:
    """The user's mdc library: read this once per conversation, before the other
    mdc tools. How the library is organized, and how to find and cite its
    documents."""
    return INSTRUCTIONS


@_tool
def list_documents(query: str = "", library: str | None = None) -> str:
    """List the library's indexed documents: name and title, newest first. With a
    query, only those whose title, summary or index terms match it, best first.
    Files are named <date>-<slug>.<role>.<ext> (role: chat, document, source, argument, analysis or review; e.g. 2026-06-29-the-eleatic-critique.chat.md), kept in a folder per year, and a name is unique across the library. Call guide first if you have not."""
    root = _root(library)
    entries = lib.load_entries(root)
    if query:
        entries = lib.search_library(entries, query)
    else:
        entries = sorted(entries, key=lambda e: Path(e.rel_path).name, reverse=True)
    if not entries:
        return "No documents found."
    return "\n".join(f"{Path(e.rel_path).name} — {e.title}" for e in entries[:200])


@_tool
def lookup_term(term: str, library: str | None = None) -> str:
    """Look up an index term: the documents that carry it, with summaries, and
    related terms to follow. Look up every plausibly relevant term before
    answering; the documents are the user's own writing, dated, and later ones
    may supersede earlier. Call guide first if you have not."""
    return lib.lookup_term(_root(library), term)


@_tool
def read_document(path: str, library: str | None = None) -> str:
    """Read an indexed document in full, by the path lookup_term gives (e.g.
    2026/<name>). Cite a library document only after reading it, under a
    '## Related' section, one per line as '| *Exact Title*'."""
    return lib.read_document(_root(library), path)


@_tool
def read_file(name: str, library: str | None = None) -> str:
    """Read any text file of the library by its name. Files are named <date>-<slug>.<role>.<ext> (role: chat, document, source, argument, analysis or review; e.g. 2026-06-29-the-eleatic-critique.chat.md), kept in a folder per year, and a name is unique across the library. A source is
    third-party material, not the user's writing; an analysis or review is
    written by mdc."""
    path = _find(_root(library), name)
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return f"{name} is not text (a {path.suffix} file); it can be linked from a chat, not read here."
