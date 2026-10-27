"""claude.ai's way into a library: an MCP server at /mcp (SERVER.md, Phase 5).

The tools `mdc reply` gives the model, on the caller's libraries, for Claude
anywhere a custom connector reaches (claude.ai on the web and the phone,
Claude Desktop, Claude Code). They run in-process on the server's copy of
the library, which is canonical; a client's next `mdc sync` brings their
writes down, and a file also changed there becomes a `.server` conflict.

Callers authenticate with a bearer token. Phase 5a: a token minted by
scripts/add_token.py (as for /run). Phase 5b adds Cognito access tokens,
through the same verifier, for claude.ai's OAuth.

Writes (edit_file, append_turn) take the library's lock, as /run does, and
keep reply's numbered backups in REVISIONS/.
"""

from __future__ import annotations

import os
import re
from contextlib import asynccontextmanager
from pathlib import Path

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

from mdc import library as lib
from mdc.config import load_config
from mdc.edit_tools import _ARGUMENT_FORMAT, _EDIT_INSTRUCTIONS, build_edit_context, make_edit_executor
from mdc.form import fix_section_spacing, slugify
from mdc.service import LibraryError, library_ids, library_root, lock_for, user_for_token
from mdc.text_utils import _upgrade_reply_headings

# Roxana's user pool, the authorization server claude.ai is sent to (Phase 5b).
DEFAULT_ISSUER = "https://cognito-idp.us-west-2.amazonaws.com/us-west-2_JBus4am8w"

_SPEAKER_RE = re.compile(r"^[^\s#]+$")
_STRUCTURAL_RE = re.compile(r"^## (Notes|Related|References)\s*$", re.MULTILINE)

INSTRUCTIONS = "\n\n".join([
    "These tools reach the user's mdc library: their own writing (chats, documents, "
    "argument files) and the sources they keep. Files are named "
    "<date>-<slug>.<role>.<ext>, the role one of chat, document, source, argument, "
    "analysis, review; a name is unique across the library. Use list_documents to find "
    "names, read_file to read one, and edit_file only on a .document.md or .argument.md. "
    "To keep this conversation in the library, call append_turn after each exchange: "
    "the user's message as speaker \"Prompt\", your reply as \"Claude\".",
    lib.LIBRARY_TOOLS_PROMPT,
    _EDIT_INSTRUCTIONS,
    _ARGUMENT_FORMAT,
])


class TokenVerifier:
    """Bearer tokens from users.toml; the AccessToken's subject is the mdc user."""

    async def verify_token(self, token: str) -> AccessToken | None:
        user = user_for_token(token)
        if user is None:
            return None
        return AccessToken(token=token, client_id="mdc-token", scopes=[], subject=user)


_TOOLS: list = []


def _tool(fn):
    _TOOLS.append(fn)
    return fn


def build() -> tuple[MCPServer, str]:
    """A fresh MCP server: a streamable-HTTP session manager runs only once."""
    public_url = os.environ.get("MDC_PUBLIC_URL", "https://mdc.rvanegas.co").rstrip("/")
    server = MCPServer(
        name="mdc",
        title="mdc library",
        instructions=INSTRUCTIONS,
        token_verifier=TokenVerifier(),
        auth=AuthSettings(
            issuer_url=os.environ.get("MDC_OAUTH_ISSUER", DEFAULT_ISSUER),
            resource_server_url=f"{public_url}/mcp",
            validate_token_resource=False,  # the verifier knows its tokens
        ),
    )
    for fn in _TOOLS:
        server.add_tool(fn)
    return server, public_url


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
    server, public_url = build()
    mcp_app.app = server.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[public_url.split("://", 1)[1], "127.0.0.1:*", "localhost:*", "testserver"],
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


async def _locked(root: Path):
    lock = lock_for(root)
    if lock.locked():
        raise ToolError("the library is busy with a run; try again shortly")
    return lock


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@_tool
def list_documents(query: str = "", library: str | None = None) -> str:
    """List the library's indexed documents: name, title and date. With a query,
    only those whose title, summary or index terms match it, best first."""
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
    """Look up an index term: the documents that carry it, with summaries, and related terms."""
    return lib.lookup_term(_root(library), term)


@_tool
def read_document(path: str, library: str | None = None) -> str:
    """Read an indexed document in full, by the path lookup_term gives (e.g. 2026/<name>)."""
    return lib.read_document(_root(library), path)


@_tool
def read_file(name: str, library: str | None = None) -> str:
    """Read any text file of the library by its name: a chat, document, argument,
    analysis, review or text source."""
    path = _find(_root(library), name)
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return f"{name} is not text (a {path.suffix} file); it can be linked from a chat, not read here."


@_tool
async def edit_file(name: str, old_str: str, new_str: str, library: str | None = None) -> str:
    """Replace old_str, which must occur in the file, with new_str in a .document.md
    or .argument.md. A numbered backup is kept; returns the diff."""
    root = _root(library)
    path = _find(root, name)
    if not path.name.endswith((".document.md", ".argument.md")):
        raise ToolError("only a .document.md or .argument.md is edited")
    async with await _locked(root):
        wrap, revisions = load_config().wrap_width, root / "REVISIONS"
        # As reply does: record the version being edited before the first edit.
        build_edit_context([path], wrap_width=wrap, revisions_dir=revisions)
        execute = make_edit_executor([path], wrap_width=wrap, revisions_dir=revisions)
        return execute("edit_file", {"path": path.name, "old_str": old_str, "new_str": new_str})


@_tool
async def append_turn(name: str, speaker: str, text: str, library: str | None = None) -> str:
    """Add a turn to a .chat.md, before its Notes, Related and References. speaker is
    a one-word heading: "Prompt" for the user's message, "Claude" for yours. A chat
    named <date>-<slug>.chat.md that does not exist yet is created in its year."""
    root = _root(library)
    if not name.endswith(".chat.md") or "/" in name:
        raise ToolError("append_turn writes to a <date>-<slug>.chat.md, named")
    if not _SPEAKER_RE.match(speaker):
        raise ToolError("speaker is one word, such as Prompt or Claude")
    body = _upgrade_reply_headings(text.strip())
    async with await _locked(root):
        try:
            path = _find(root, name)
            current = path.read_text(encoding="utf-8")
        except ToolError:
            m = re.match(r"^(\d{4})-(\d{2})-(\d{2})-(.+)\.chat\.md$", name)
            if not m or slugify(m.group(4).replace("-", " ")) != m.group(4):
                raise ToolError("a new chat is named <yyyy-mm-dd>-<slug>.chat.md") from None
            path = root / m.group(1) / name
            path.parent.mkdir(exist_ok=True)
            title = m.group(4).replace("-", " ").capitalize()
            current = f"\n# {title}\n{name[:10]}\n"
        structural = _STRUCTURAL_RE.search(current)
        cut = structural.start() if structural else len(current)
        new = current[:cut].rstrip("\n") + f"\n\n## {speaker}\n\n{body}\n\n" + current[cut:]
        new, _ = fix_section_spacing(new)
        path.write_text(new, encoding="utf-8")
    return f"Added a {speaker} turn to {path.relative_to(root).as_posix()}."
