"""
The authorization server.

Claude.ai custom connectors can only authenticate over OAuth, so this service
issues its own tokens rather than delegating to a third party. They are issued
against the same auth.users rows the browser signs in with, which is what makes
"one authorization server, two clients, same accounts" real rather than
aspirational: the consent step below reuses the existing session cookie, so
approving the connector is not a second login.

FastMCP supplies the endpoints -- /authorize, /token, /register, /revoke and
the two well-known metadata documents. What it does not supply, and what this
module is mostly about, is storage and the consent decision.

Structure follows FastMCP's own InMemoryOAuthProvider, with its dicts replaced
by Postgres and its auto-approving authorize() replaced by a real one.
"""

from __future__ import annotations

import logging
import secrets
import time
from datetime import datetime, timedelta, timezone
from functools import partial
from typing import Any

import anyio.to_thread
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from psycopg.types.json import Json
from pydantic import AnyUrl

from fastmcp.server.auth import OAuthProvider
from fastmcp.server.auth.auth import ClientRegistrationOptions, RevocationOptions

from . import db
from .auth import token_digest

log = logging.getLogger("diet.oauth")

CONSENT_PATH = "/oauth/consent"
PENDING_TTL = timedelta(minutes=10)
CODE_TTL = timedelta(minutes=5)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _epoch(dt: datetime | None) -> int | None:
    return int(dt.timestamp()) if dt else None


async def _off(fn, *args, **kwargs):
    """Run a synchronous database call without blocking the event loop."""
    return await anyio.to_thread.run_sync(partial(fn, *args, **kwargs))


# --- storage (synchronous; every one of these is called through _off) -------

def _sql_get_client(client_id: str) -> dict | None:
    with db.auth_tx() as cur:
        cur.execute(
            """UPDATE auth.oauth_clients SET last_seen_at = now()
               WHERE client_id = %s RETURNING client_info""", (client_id,))
        return cur.fetchone()


def _sql_register_client(client_id: str, info: dict, name: str | None) -> None:
    with db.auth_tx() as cur:
        cur.execute(
            """INSERT INTO auth.oauth_clients (client_id, client_info, client_name)
               VALUES (%s, %s, %s)
               ON CONFLICT (client_id) DO UPDATE
                 SET client_info = EXCLUDED.client_info,
                     client_name = EXCLUDED.client_name""",
            (client_id, Json(info), name))
        # Claude registers a fresh client on every new connection, so without
        # this the table grows without bound. Pruning only on last_seen_at IS
        # NULL missed the common case: a client that connected once and was then
        # abandoned keeps its stamp for ever. Age it out instead, but never
        # touch one still holding a usable token -- that would silently force a
        # working connector back through consent.
        cur.execute("""
            DELETE FROM auth.oauth_clients c
             WHERE coalesce(c.last_seen_at, c.created_at) < now() - interval '30 days'
               AND NOT EXISTS (
                     SELECT 1 FROM auth.oauth_tokens t
                      WHERE t.client_id = c.client_id
                        AND t.revoked_at IS NULL
                        AND (t.expires_at IS NULL OR t.expires_at > now()))""")


def _sql_put_pending(pending_id: str, client_id: str, params: dict) -> None:
    with db.auth_tx() as cur:
        cur.execute("DELETE FROM auth.oauth_pending WHERE expires_at < now()")
        cur.execute(
            """INSERT INTO auth.oauth_pending (pending_id, client_id, params, expires_at)
               VALUES (%s, %s, %s, %s)""",
            (pending_id, client_id, Json(params), _now() + PENDING_TTL))


def _sql_get_pending(pending_id: str) -> dict | None:
    with db.auth_tx() as cur:
        cur.execute(
            """SELECT p.pending_id, p.client_id, p.params, p.session_hash,
                      c.client_name, c.client_info
               FROM auth.oauth_pending p
               JOIN auth.oauth_clients c USING (client_id)
               WHERE p.pending_id = %s AND p.expires_at > now()""", (pending_id,))
        return cur.fetchone()


def _sql_bind_pending(pending_id: str, session_hash: bytes) -> None:
    """Record which session was shown the screen; first one to look wins."""
    with db.auth_tx() as cur:
        cur.execute("""UPDATE auth.oauth_pending SET session_hash = %s
                       WHERE pending_id = %s AND session_hash IS NULL""",
                    (session_hash, pending_id))


def _sql_drop_pending(pending_id: str) -> None:
    with db.auth_tx() as cur:
        cur.execute("DELETE FROM auth.oauth_pending WHERE pending_id = %s", (pending_id,))


def _sql_put_code(code_hash: bytes, client_id: str, user_id: str, params: dict,
                  expires_at: datetime) -> None:
    with db.auth_tx() as cur:
        cur.execute("DELETE FROM auth.oauth_codes WHERE expires_at < now() - interval '1 day'")
        cur.execute(
            """INSERT INTO auth.oauth_codes
                 (code_hash, client_id, user_id, redirect_uri,
                  redirect_uri_provided_explicitly, scopes, code_challenge,
                  resource, expires_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (code_hash, client_id, user_id, str(params["redirect_uri"]),
             bool(params.get("redirect_uri_provided_explicitly", True)),
             list(params.get("scopes") or []), params["code_challenge"],
             params.get("resource"), expires_at))


def _sql_load_code(code_hash: bytes, client_id: str) -> dict | None:
    with db.auth_tx() as cur:
        cur.execute(
            """SELECT * FROM auth.oauth_codes
               WHERE code_hash = %s AND client_id = %s
                 AND consumed_at IS NULL AND expires_at > now()""",
            (code_hash, client_id))
        return cur.fetchone()


def _sql_consume_code(code_hash: bytes) -> dict | None:
    """Single-use, enforced by the UPDATE itself rather than by a read first."""
    with db.auth_tx() as cur:
        cur.execute(
            """UPDATE auth.oauth_codes SET consumed_at = now()
               WHERE code_hash = %s AND consumed_at IS NULL AND expires_at > now()
               RETURNING *""", (code_hash,))
        return cur.fetchone()


def _sql_issue_pair(access_hash: bytes, refresh_hash: bytes, client_id: str,
                    user_id: str, scopes: list[str], resource: str | None,
                    access_expires: datetime, refresh_expires: datetime | None) -> None:
    with db.auth_tx() as cur:
        cur.execute(
            """INSERT INTO auth.oauth_tokens
                 (token_hash, kind, client_id, user_id, scopes, resource, pair_hash, expires_at)
               VALUES (%s,'access',%s,%s,%s,%s,%s,%s), (%s,'refresh',%s,%s,%s,%s,%s,%s)""",
            (access_hash, client_id, user_id, scopes, resource, refresh_hash, access_expires,
             refresh_hash, client_id, user_id, scopes, resource, access_hash, refresh_expires))


def _sql_load_token(token_hash: bytes, kind: str) -> dict | None:
    with db.auth_tx() as cur:
        # Through the definer function, not a join: the app role has no read
        # access to auth.users and is not getting any for this.
        cur.execute(
            """SELECT t.*, p.email, p.display_name, p.timezone
               FROM auth.oauth_tokens t, LATERAL auth.user_profile(t.user_id) p
               WHERE t.token_hash = %s AND t.kind = %s
                 AND t.revoked_at IS NULL
                 AND (t.expires_at IS NULL OR t.expires_at > now())
                 AND p.is_active""", (token_hash, kind))
        return cur.fetchone()


def _sql_revoke(token_hash: bytes) -> None:
    """Revokes the token and its counterpart: one half is never useful alone."""
    with db.auth_tx() as cur:
        cur.execute(
            """UPDATE auth.oauth_tokens SET revoked_at = now()
               WHERE revoked_at IS NULL
                 AND (token_hash = %(h)s
                      OR token_hash = (SELECT pair_hash FROM auth.oauth_tokens
                                       WHERE token_hash = %(h)s))""", {"h": token_hash})


# --- consent (driven by the routes in oauth_routes.py) ---------------------

async def pending_request(pending_id: str) -> dict | None:
    """What the consent page needs to show: which client is asking, for what."""
    row = await _off(_sql_get_pending, pending_id)
    if row is None:
        return None
    info = row["client_info"] or {}
    return {
        "pending_id": row["pending_id"],
        "client_id": row["client_id"],
        "client_name": row["client_name"] or info.get("client_name") or row["client_id"],
        "client_uri": info.get("client_uri"),
        "scopes": row["params"].get("scopes") or [],
        "session_hash": row["session_hash"],
        "redirect_uri": row["params"].get("redirect_uri"),
        "params": row["params"],
    }


async def bind_to_session(pending_id: str, session_hash: bytes) -> None:
    await _off(_sql_bind_pending, pending_id, session_hash)


async def approve(pending_id: str, user_id: str, session_hash: bytes) -> str:
    """Mint the authorization code and return where to send the browser."""
    row = await _off(_sql_get_pending, pending_id)
    if row is None:
        raise AuthorizeError(error="invalid_request",
                             error_description="this approval expired; start again")
    # The approval has to come from the session that was shown the screen. This
    # is the CSRF token and the confused-deputy check in one, and it does not
    # depend on the cookie's SameSite attribute holding.
    if row["session_hash"] is None or bytes(row["session_hash"]) != session_hash:
        raise AuthorizeError(
            error="access_denied",
            error_description="this approval belongs to a different sign-in; start again")
    params = row["params"]
    code = f"dc_{secrets.token_urlsafe(32)}"
    await _off(_sql_put_code, token_digest(code), row["client_id"], user_id,
               params, _now() + CODE_TTL)
    await _off(_sql_drop_pending, pending_id)
    log.info("authorization approved user=%s client=%s", user_id, row["client_id"])
    return construct_redirect_uri(str(params["redirect_uri"]),
                                  code=code, state=params.get("state"))


async def deny(pending_id: str) -> str:
    row = await _off(_sql_get_pending, pending_id)
    if row is None:
        raise AuthorizeError(error="invalid_request", error_description="expired")
    params = row["params"]
    await _off(_sql_drop_pending, pending_id)
    return construct_redirect_uri(str(params["redirect_uri"]),
                                  error="access_denied",
                                  error_description="the user declined",
                                  state=params.get("state"))


# --- the provider ----------------------------------------------------------

class DietOAuthProvider(OAuthProvider):
    """OAuth 2.1 authorization server over auth.users."""

    def __init__(self, *, base_url: str, access_ttl_minutes: int = 60,
                 refresh_ttl_days: int = 30, required_scopes: list[str] | None = None):
        super().__init__(
            base_url=base_url,
            # Claude.ai's out-of-the-box path is Dynamic Client Registration, so
            # it has to be on even though the current MCP spec revision marks
            # DCR deprecated in favour of Client ID Metadata Documents.
            client_registration_options=ClientRegistrationOptions(enabled=True),
            revocation_options=RevocationOptions(enabled=True),
            required_scopes=required_scopes,
        )
        self.access_ttl = timedelta(minutes=access_ttl_minutes)
        self.refresh_ttl = timedelta(days=refresh_ttl_days) if refresh_ttl_days else None

    # -- clients --
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        row = await _off(_sql_get_client, client_id)
        if row is None:
            return None
        return OAuthClientInformationFull.model_validate(row["client_info"])

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if client_info.client_id is None:
            raise ValueError("client_id is required for client registration")
        await _off(_sql_register_client, client_info.client_id,
                   client_info.model_dump(mode="json", exclude_none=True),
                   client_info.client_name)
        log.info("registered client %s (%s)", client_info.client_id, client_info.client_name)

    # -- authorization --
    async def authorize(self, client: OAuthClientInformationFull,
                        params: AuthorizationParams) -> str:
        """
        Park the request and send the browser to our own consent page.

        The reference implementation approves silently here. It cannot: nothing
        at this point has established *which user* is granting access, and the
        whole design rests on identity never being something a caller supplies.
        The consent page resolves it from the session cookie.
        """
        if client.client_id is None:
            raise AuthorizeError(error="invalid_client",
                                 error_description="client_id is required")
        pending_id = secrets.token_urlsafe(24)
        await _off(_sql_put_pending, pending_id, client.client_id,
                   params.model_dump(mode="json"))
        return f"{str(self.base_url).rstrip('/')}{CONSENT_PATH}?pending={pending_id}"

    async def load_authorization_code(self, client: OAuthClientInformationFull,
                                      authorization_code: str) -> AuthorizationCode | None:
        if client.client_id is None:
            return None
        row = await _off(_sql_load_code, token_digest(authorization_code), client.client_id)
        if row is None:
            return None
        return AuthorizationCode(
            code=authorization_code,
            client_id=row["client_id"],
            redirect_uri=AnyUrl(row["redirect_uri"]),
            redirect_uri_provided_explicitly=row["redirect_uri_provided_explicitly"],
            scopes=list(row["scopes"]),
            expires_at=row["expires_at"].timestamp(),
            code_challenge=row["code_challenge"],
            resource=row["resource"],
            subject=str(row["user_id"]),
        )

    # -- tokens --
    async def _issue(self, client_id: str, user_id: str, scopes: list[str],
                     resource: str | None) -> OAuthToken:
        access = f"dat_{secrets.token_urlsafe(32)}"
        refresh = f"drt_{secrets.token_urlsafe(32)}"
        access_expires = _now() + self.access_ttl
        refresh_expires = _now() + self.refresh_ttl if self.refresh_ttl else None
        await _off(_sql_issue_pair, token_digest(access), token_digest(refresh),
                   client_id, user_id, scopes, resource, access_expires, refresh_expires)
        return OAuthToken(
            access_token=access, token_type="Bearer",
            expires_in=int(self.access_ttl.total_seconds()),
            refresh_token=refresh, scope=" ".join(scopes),
        )

    async def exchange_authorization_code(self, client: OAuthClientInformationFull,
                                          authorization_code: AuthorizationCode) -> OAuthToken:
        # Consuming is the check: the UPDATE only matches an unused code, so a
        # replayed one loses the race rather than being validated twice.
        row = await _off(_sql_consume_code, token_digest(authorization_code.code))
        if row is None:
            raise TokenError("invalid_grant", "authorization code already used or expired")
        return await self._issue(row["client_id"], str(row["user_id"]),
                                 list(row["scopes"]), row["resource"])

    async def load_refresh_token(self, client: OAuthClientInformationFull,
                                 refresh_token: str) -> RefreshToken | None:
        row = await _off(_sql_load_token, token_digest(refresh_token), "refresh")
        if row is None or row["client_id"] != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token, client_id=row["client_id"], scopes=list(row["scopes"]),
            expires_at=_epoch(row["expires_at"]), resource=row["resource"],
            subject=str(row["user_id"]),
        )

    async def exchange_refresh_token(self, client: OAuthClientInformationFull,
                                     refresh_token: RefreshToken,
                                     scopes: list[str]) -> OAuthToken:
        if not set(scopes).issubset(set(refresh_token.scopes)):
            raise TokenError("invalid_scope",
                             "requested scopes exceed those the refresh token carries")
        if refresh_token.subject is None:
            raise TokenError("invalid_grant", "refresh token is not bound to a user")
        # Rotation: the presented token dies with its access token before the
        # replacement is issued, as required for public clients.
        await _off(_sql_revoke, token_digest(refresh_token.token))
        return await self._issue(refresh_token.client_id, refresh_token.subject,
                                 scopes or list(refresh_token.scopes),
                                 refresh_token.resource)

    async def load_access_token(self, token: str) -> AccessToken | None:
        row = await _off(_sql_load_token, token_digest(token), "access")
        if row is None:
            return None
        return AccessToken(
            token=token, client_id=row["client_id"], scopes=list(row["scopes"]),
            expires_at=_epoch(row["expires_at"]), resource=row["resource"],
            # subject and claims are the contract the MCP tools read identity
            # from; an OAuth token and a bearer token look identical to them.
            subject=str(row["user_id"]),
            claims={"email": row["email"], "display_name": row["display_name"],
                    "timezone": row["timezone"]},
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        # MultiAuth swallows exceptions from a verifier so the next one still
        # gets a turn, which means a bug in here would otherwise surface only as
        # a bare "invalid_token". Log it rather than lose it.
        try:
            return await self.load_access_token(token)
        except Exception:
            log.exception("OAuth token verification failed")
            raise

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        await _off(_sql_revoke, token_digest(token.token))
