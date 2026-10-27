"""mdc's own OAuth authorization server, for claude.ai's connector (SERVER.md, Phase 5b).

Real authentication with a plain sign-in, until a real identity plan exists:
claude.ai registers itself (dynamic client registration), sends the person to
mdc's /oauth/login, where they paste their mdc token (minted for them by
scripts/add_token.py), and gets OAuth tokens for that person: an access
token for an hour, a refresh token for 90 days, rotated on every use. The mdc
token is typed into mdc's page once and never held by claude.ai.

The MCP SDK serves the protocol (metadata, /register, /authorize, /token,
/revoke, PKCE); this is its provider: storage and the sign-in.

- **Redirects are limited** to claude.ai's and claude.com's callbacks and to
  localhost (Claude Code): registration is open to anyone, and the sign-in
  must not send a code where its asker says.
- **OAuth tokens follow the mdc token that signed in:** each records that
  token's hash, and stops working once it leaves users.toml.
- **Stored** in $MDC_DATA_DIR/oauth.json (mode 600), hashes only, so a deploy
  does not sign anybody out; codes and pending sign-ins live minutes, in memory.

`/mcp` also takes a minted mdc token as a bearer token directly (Claude Code,
the CLI), through `load_access_token`.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    RegistrationError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from mdc import service

ACCESS_TTL = 60 * 60
REFRESH_TTL = 90 * 24 * 60 * 60
CODE_TTL = 5 * 60
SIGN_IN_TTL = 10 * 60
SIGN_IN_ATTEMPTS = 5

ALLOWED_CALLBACKS = {
    ("https", "claude.ai", "/api/mcp/auth_callback"),
    ("https", "claude.com", "/api/mcp/auth_callback"),
}


def allowed_redirect(uri: str) -> bool:
    u = urlparse(uri)
    if (u.scheme, u.hostname, u.path) in ALLOWED_CALLBACKS:
        return True
    return u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1")


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass
class SignIn:
    client: OAuthClientInformationFull
    params: AuthorizationParams
    expires_at: float
    attempts: int = 0


class MdcOAuthProvider:
    def __init__(self) -> None:
        self.sign_ins: dict[str, SignIn] = {}
        self.codes: dict[str, AuthorizationCode] = {}
        self.code_via: dict[str, str] = {}

    # --- storage -----------------------------------------------------------

    def _path(self) -> Path:
        return service.data_dir() / "oauth.json"

    def _load(self) -> dict:
        try:
            return json.loads(self._path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"clients": {}, "access": {}, "refresh": {}}

    def _save(self, data: dict) -> None:
        now = time.time()
        for kind in ("access", "refresh"):
            data[kind] = {k: v for k, v in data[kind].items() if v["expires_at"] > now}
        path = self._path()
        tmp = path.with_name(".oauth.json.tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(path)

    # --- clients -----------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        raw = self._load()["clients"].get(client_id)
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        bad = [str(u) for u in (client_info.redirect_uris or []) if not allowed_redirect(str(u))]
        if bad:
            raise RegistrationError(error="invalid_redirect_uri",
                                    error_description=f"not a redirect this server sends to: {', '.join(bad)}")
        data = self._load()
        data["clients"][client_info.client_id] = client_info.model_dump(mode="json")
        self._save(data)

    # --- sign-in -----------------------------------------------------------

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        now = time.time()
        self.sign_ins = {k: v for k, v in self.sign_ins.items() if v.expires_at > now}
        request_id = secrets.token_urlsafe(24)
        self.sign_ins[request_id] = SignIn(client, params, now + SIGN_IN_TTL)
        from mdc.mcp_server import public_url
        return f"{public_url()}/oauth/login?request={request_id}"

    def pending(self, request_id: str) -> SignIn | None:
        sign_in = self.sign_ins.get(request_id)
        if sign_in is None or sign_in.expires_at < time.time() or sign_in.attempts >= SIGN_IN_ATTEMPTS:
            return None
        return sign_in

    def complete(self, request_id: str, mdc_token: str) -> str | None:
        """The redirect back to the client with a code, if the mdc token is someone's."""
        sign_in = self.pending(request_id)
        if sign_in is None:
            return None
        sign_in.attempts += 1
        user = service.user_for_token(mdc_token)
        if user is None or user == "*":
            return None
        del self.sign_ins[request_id]
        code = secrets.token_urlsafe(32)
        params = sign_in.params
        self.codes[code] = AuthorizationCode(
            code=code, scopes=params.scopes or [], expires_at=time.time() + CODE_TTL,
            client_id=sign_in.client.client_id, code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource, subject=user,
        )
        self.code_via[code] = _sha(mdc_token)
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    def refuse(self, request_id: str) -> str | None:
        """The redirect back with access_denied, when the person declines."""
        sign_in = self.sign_ins.pop(request_id, None)
        if sign_in is None:
            return None
        return construct_redirect_uri(str(sign_in.params.redirect_uri),
                                      error="access_denied", state=sign_in.params.state)

    # --- tokens ------------------------------------------------------------

    async def load_authorization_code(self, client, authorization_code: str) -> AuthorizationCode | None:
        code = self.codes.get(authorization_code)
        if code is None or code.client_id != client.client_id:
            return None
        return code

    def _issue(self, data: dict, client_id: str, user: str, scopes: list[str],
               resource: str | None, via: str) -> OAuthToken:
        now = int(time.time())
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        common = {"client_id": client_id, "user": user, "scopes": scopes, "resource": resource, "via": via}
        data["access"][_sha(access)] = {**common, "expires_at": now + ACCESS_TTL, "refresh": _sha(refresh)}
        data["refresh"][_sha(refresh)] = {**common, "expires_at": now + REFRESH_TTL}
        return OAuthToken(access_token=access, expires_in=ACCESS_TTL, refresh_token=refresh,
                          scope=" ".join(scopes) or None)

    async def exchange_authorization_code(self, client, authorization_code: AuthorizationCode) -> OAuthToken:
        self.codes.pop(authorization_code.code, None)
        via = self.code_via.pop(authorization_code.code, "")
        data = self._load()
        token = self._issue(data, client.client_id, authorization_code.subject or "",
                            authorization_code.scopes, authorization_code.resource, via)
        self._save(data)
        return token

    async def load_refresh_token(self, client, refresh_token: str) -> RefreshToken | None:
        entry = self._load()["refresh"].get(_sha(refresh_token))
        if (entry is None or entry["client_id"] != client.client_id
                or entry["expires_at"] < time.time() or not self._still_signed_in(entry)):
            return None
        return RefreshToken(token=refresh_token, client_id=entry["client_id"], scopes=entry["scopes"],
                            expires_at=entry["expires_at"], resource=entry["resource"], subject=entry["user"])

    async def exchange_refresh_token(self, client, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        data = self._load()
        entry = data["refresh"].pop(_sha(refresh_token.token))
        data["access"] = {k: v for k, v in data["access"].items() if v.get("refresh") != _sha(refresh_token.token)}
        token = self._issue(data, client.client_id, entry["user"], scopes or entry["scopes"],
                            entry["resource"], entry["via"])
        self._save(data)
        return token

    async def load_access_token(self, token: str) -> AccessToken | None:
        entry = self._load()["access"].get(_sha(token))
        if entry is not None:
            if entry["expires_at"] < time.time() or not self._still_signed_in(entry):
                return None
            return AccessToken(token=token, client_id=entry["client_id"], scopes=entry["scopes"],
                               expires_at=entry["expires_at"], resource=entry["resource"], subject=entry["user"])
        # A minted mdc token, sent as it is (Claude Code, the CLI).
        user = service.user_for_token(token)
        if user is not None:
            return AccessToken(token=token, client_id="mdc-token", scopes=[], subject=user)
        return None

    async def revoke_token(self, token) -> None:
        data = self._load()
        digest = _sha(token.token)
        if digest in data["access"]:
            refresh = data["access"].pop(digest).get("refresh")
            data["refresh"].pop(refresh, None)
        elif digest in data["refresh"]:
            data["refresh"].pop(digest)
            data["access"] = {k: v for k, v in data["access"].items() if v.get("refresh") != digest}
        self._save(data)

    @staticmethod
    def _still_signed_in(entry: dict) -> bool:
        return service.user_for_token_hash(entry.get("via", "")) == entry["user"]


provider = MdcOAuthProvider()
