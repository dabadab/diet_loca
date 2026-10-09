"""
The self-hosted OAuth 2.1 authorization server, end to end.

This is the flow Claude.ai drives: discover, register, authorize, consent,
exchange, refresh. The steps are ordered as a client experiences them, and the
fixtures mirror a browser -- the consent screen is reached with the same session
cookie the web UI uses, which is the whole reason it can be a page rather than a
second login.
"""

import base64
import hashlib
import json
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

import pytest

REDIRECT = "https://claude.ai/api/mcp/auth_callback"
PASSWORD = "a-long-test-password"


def pkce():
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


@pytest.fixture
def http(server):
    """A browser-ish client: keeps cookies, does not follow redirects."""
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(CookieJar()), NoRedirect)

    def _call(path, *, json_body=None, form=None, method=None):
        data, headers = None, {}
        if json_body is not None:
            data = json.dumps(json_body).encode()
            headers["content-type"] = "application/json"
        elif form is not None:
            data = urllib.parse.urlencode(form).encode()
            headers["content-type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(
            server + path, data=data, headers=headers,
            method=method or ("POST" if data else "GET"))
        try:
            with opener.open(req, timeout=15) as r:
                return r.status, dict(r.headers), r.read().decode(errors="replace")
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read().decode(errors="replace")
    return _call


@pytest.fixture
def client_id(http):
    """Dynamic client registration -- the first thing Claude.ai does."""
    status, _, body = http("/register", json_body={
        "client_name": "Claude", "redirect_uris": [REDIRECT],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"], "token_endpoint_auth_method": "none"})
    assert status in (200, 201), body
    return json.loads(body)["client_id"]


@pytest.fixture
def signed_in(http, make_user):
    make_user("a@example.com", password=PASSWORD)
    status, _, _ = http("/api/login",
                        json_body={"email": "a@example.com", "password": PASSWORD})
    assert status == 200


def authorize(http, client_id, challenge, state="xyz"):
    """GET /authorize and return the pending id from the consent redirect."""
    query = urllib.parse.urlencode({
        "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT,
        "code_challenge": challenge, "code_challenge_method": "S256",
        "state": state, "resource": "http://127.0.0.1/mcp"})
    status, headers, _ = http(f"/authorize?{query}")
    assert status == 302, status
    location = headers["location"]
    assert "/oauth/consent" in location
    return re.search(r"pending=([^&]+)", location).group(1)


def approve(http, pending):
    """View the consent screen, then submit it, as a browser does.

    The GET is not optional: the server binds the pending request to the
    viewing session while rendering, so posting the form without having been
    shown it is refused as belonging to a different sign-in. Skipping it is a
    harness shortcut, not a shortcut a real client has.
    """
    assert http(f"/oauth/consent?pending={pending}")[0] == 200
    status, headers, body = http("/oauth/consent",
                                 form={"pending": pending, "decision": "allow"})
    # 303 specifically: a 307 would replay the POST at the client's callback.
    assert status == 303, f"{status}: {body[:300]}"
    return re.search(r"[?&]code=([^&]+)", headers["location"]).group(1)


def exchange(http, client_id, code, verifier):
    return http("/token", form={
        "grant_type": "authorization_code", "code": code,
        "redirect_uri": REDIRECT, "client_id": client_id,
        "code_verifier": verifier})


class TestDiscovery:
    def test_the_authorization_server_metadata_advertises_pkce(self, http):
        status, _, body = http("/.well-known/oauth-authorization-server")
        assert status == 200
        assert "S256" in json.loads(body)["code_challenge_methods_supported"]

    def test_the_protected_resource_metadata_points_at_this_server(self, http, server):
        """RFC 9728, and path-scoped to the resource: a client that finds /mcp
        looks for the document under that same path."""
        status, _, body = http("/.well-known/oauth-protected-resource/mcp")
        assert status == 200
        doc = json.loads(body)
        assert doc["resource"] == f"{server}/mcp"
        assert doc["authorization_servers"] == [f"{server}/"]


class TestConsent:
    def test_signed_out_the_consent_page_asks_for_a_password(
            self, http, client_id):
        """Not an approve button: approving while signed out would bind the
        grant to nobody."""
        pending = authorize(http, client_id, pkce()[1])
        status, _, body = http(f"/oauth/consent?pending={pending}")
        assert status == 200
        assert "password" in body.lower() and "sign in" in body.lower()

    def test_signed_in_it_names_the_client(self, http, client_id, signed_in):
        pending = authorize(http, client_id, pkce()[1])
        status, _, body = http(f"/oauth/consent?pending={pending}")
        assert status == 200 and "Claude" in body

    def test_the_approval_redirect_is_allowed_by_the_response_csp(
            self, http, client_id, signed_in):
        """The app-wide `form-action 'self'` blocks the redirect that follows
        this POST, which looked exactly like "click Allow, nothing happens".
        The per-request policy has to name the client's origin."""
        pending = authorize(http, client_id, pkce()[1])
        http(f"/oauth/consent?pending={pending}")
        status, headers, _ = http("/oauth/consent",
                                  form={"pending": pending, "decision": "allow"})
        assert status == 303
        assert "claude.ai" in headers.get("content-security-policy", "")


class TestAuthorizationCode:
    def test_the_happy_path(self, http, client_id, signed_in):
        verifier, challenge = pkce()
        code = approve(http, authorize(http, client_id, challenge, state="st8"))
        status, _, body = exchange(http, client_id, code, verifier)
        assert status == 200
        token = json.loads(body)
        assert token["token_type"].lower() == "bearer"
        assert token["access_token"] and token["refresh_token"]

    def test_state_is_echoed_back_unchanged(self, http, client_id, signed_in):
        pending = authorize(http, client_id, pkce()[1], state="st8")
        http(f"/oauth/consent?pending={pending}")
        _, headers, _ = http("/oauth/consent",
                             form={"pending": pending, "decision": "allow"})
        assert "state=st8" in headers["location"]

    def test_a_code_cannot_be_replayed(self, http, client_id, signed_in):
        verifier, challenge = pkce()
        code = approve(http, authorize(http, client_id, challenge))
        assert exchange(http, client_id, code, verifier)[0] == 200
        status, _, body = exchange(http, client_id, code, verifier)
        assert status >= 400 and json.loads(body)["error"] == "invalid_grant"

    def test_the_wrong_verifier_is_refused(self, http, client_id, signed_in):
        """PKCE is what stops an intercepted code being usable."""
        _, challenge = pkce()
        code = approve(http, authorize(http, client_id, challenge))
        status, _, _ = exchange(http, client_id, code, "wrong-verifier-entirely")
        assert status >= 400

    def test_an_unregistered_redirect_uri_is_refused(self, http, client_id, signed_in):
        """Accepting one would turn the server into an open redirector that
        hands out codes."""
        query = urllib.parse.urlencode({
            "response_type": "code", "client_id": client_id,
            "redirect_uri": "https://attacker.example/callback",
            "code_challenge": pkce()[1], "code_challenge_method": "S256",
            "state": "x", "resource": "http://127.0.0.1/mcp"})
        status, headers, _ = http(f"/authorize?{query}")
        assert status != 302 or "attacker.example" not in headers.get("location", "")


class TestRefresh:
    def test_a_refresh_token_yields_a_new_pair(self, http, client_id, signed_in):
        verifier, challenge = pkce()
        code = approve(http, authorize(http, client_id, challenge))
        first = json.loads(exchange(http, client_id, code, verifier)[2])
        status, _, body = http("/token", form={
            "grant_type": "refresh_token", "refresh_token": first["refresh_token"],
            "client_id": client_id})
        assert status == 200
        second = json.loads(body)
        assert second["access_token"] != first["access_token"]
        assert second["refresh_token"] != first["refresh_token"], \
            "refresh tokens should rotate"

    def test_the_old_refresh_token_stops_working_after_rotation(
            self, http, client_id, signed_in):
        verifier, challenge = pkce()
        code = approve(http, authorize(http, client_id, challenge))
        first = json.loads(exchange(http, client_id, code, verifier)[2])
        http("/token", form={"grant_type": "refresh_token",
                             "refresh_token": first["refresh_token"],
                             "client_id": client_id})
        status, _, _ = http("/token", form={
            "grant_type": "refresh_token", "refresh_token": first["refresh_token"],
            "client_id": client_id})
        assert status >= 400


def test_an_oauth_access_token_works_on_the_mcp_endpoint(
        http, client_id, signed_in, server):
    """The point of the whole dance."""
    import asyncio
    from fastmcp import Client

    verifier, challenge = pkce()
    code = approve(http, authorize(http, client_id, challenge))
    token = json.loads(exchange(http, client_id, code, verifier)[2])["access_token"]

    async def go():
        async with Client(f"{server}/mcp", auth=token) as c:
            return [t.name for t in await c.list_tools()]
    assert "get_day" in asyncio.run(go())


def test_changing_the_password_revokes_issued_tokens(
        http, client_id, signed_in, server, dsns):
    """A password change is what someone does when they think a credential is
    known to someone else. An OAuth token surviving it still reaches log_meal
    and query_sql for that account, so the gesture has to take everything.

    Driven through the real CLI, because that is the only way to change a
    password -- there is deliberately no HTTP endpoint for it.
    """
    import asyncio
    import os
    import subprocess
    import sys
    from pathlib import Path
    from fastmcp import Client

    verifier, challenge = pkce()
    code = approve(http, authorize(http, client_id, challenge))
    token = json.loads(exchange(http, client_id, code, verifier)[2])["access_token"]

    async def go():
        async with Client(f"{server}/mcp", auth=token) as c:
            return await c.list_tools()
    assert asyncio.run(go()), "the token should work before the change"

    repo = Path(__file__).resolve().parent.parent
    new = "another-long-password"
    done = subprocess.run(
        [sys.executable, "-m", "app.manage", "passwd", "a@example.com"],
        cwd=repo, input=f"{new}\n{new}\n", text=True, capture_output=True,
        env={**os.environ, "PYTHONPATH": str(repo),
             "DATABASE_URL": dsns["app"],
             "MIGRATION_DATABASE_URL": dsns["owner"],
             "APP_DB_PASSWORD": "testpw"})
    assert done.returncode == 0, done.stderr
    assert "OAuth token" in done.stdout, done.stdout

    with pytest.raises(Exception):
        asyncio.run(go())
