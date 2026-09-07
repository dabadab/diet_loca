"""
Bearer-token verification for the MCP server.

Deliberately not FastMCP's StaticTokenVerifier, whose own documentation says it
holds tokens in plain text and must never be used in production. This verifies
against auth.api_tokens instead, so MCP credentials are revocable, expire, and
are stored only as digests -- the same treatment session cookies already get.

It is also not throwaway scaffolding: when OAuth lands it stays, composed under
MultiAuth beside the OAuth provider, so scripts and Claude Code keep working
against the same server that Claude.ai talks to.
"""

from __future__ import annotations

import logging

import anyio.to_thread
from fastmcp.server.auth import AccessToken, TokenVerifier

from . import auth

log = logging.getLogger("diet.mcp.auth")


class PostgresTokenVerifier(TokenVerifier):
    """Resolves a bearer token to a user, or to nothing."""

    async def verify_token(self, token: str) -> AccessToken | None:
        # The database layer is sync psycopg; hop to a worker thread rather
        # than blocking the event loop for the duration of the lookup.
        user = await anyio.to_thread.run_sync(auth.resolve_api_token, token)
        if user is None:
            return None
        return AccessToken(
            token=token,
            client_id=f"api-token:{user.user_id}",
            scopes=[],
            subject=user.user_id,
            claims={
                "email": user.email,
                "display_name": user.display_name,
                # Carried so tools can resolve "today" without a second query.
                "timezone": user.timezone,
            },
        )
