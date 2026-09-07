"""
Sessions and passwords.

Identity is resolved from the session cookie and from nothing else. No endpoint
takes a user id, so a confused client -- or a model writing through a future
MCP tool -- has no way to name a user other than the one it is authenticated
as. That property is why this module is small and boring.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import threading
import time
from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error

from . import db

log = logging.getLogger("diet.auth")

_ph = PasswordHasher()
# Hashed once at import so an unknown email costs the same as a known one.
_DUMMY_HASH = _ph.hash("not-a-real-password-" + secrets.token_hex(8))

TOKEN_BYTES = 32


@dataclass(frozen=True)
class User:
    user_id: str
    email: str
    display_name: str
    timezone: str


def hash_password(password: str) -> str:
    if len(password) < 10:
        raise ValueError("password must be at least 10 characters")
    return _ph.hash(password)


def _digest(raw_token: str) -> bytes:
    return hashlib.sha256(raw_token.encode()).digest()


class Throttle:
    """
    Per-key sliding window, in process. Enough for a household-sized service
    behind one app container; if this ever runs multi-replica the counter has
    to move into Postgres or Redis.
    """

    def __init__(self, limit: int = 10, window: float = 900.0):
        self.limit, self.window = limit, window
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def blocked(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            hits = [t for t in self._hits.get(key, []) if now - t < self.window]
            self._hits[key] = hits
            return len(hits) >= self.limit

    def record_failure(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            self._hits.setdefault(key, []).append(now)
            if len(self._hits) > 10_000:  # crude bound on memory
                self._hits = {k: v for k, v in self._hits.items()
                              if v and now - v[-1] < self.window}

    def clear(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)


login_throttle = Throttle()


def authenticate(email: str, password: str) -> User | None:
    """Verify a password. Returns None for every kind of failure, alike."""
    email = (email or "").strip().lower()
    with db.auth_tx() as cur:
        cur.execute("SELECT * FROM auth.login_lookup(%s)", (email,))
        row = cur.fetchone()

    stored = row["password_hash"] if row else _DUMMY_HASH
    try:
        _ph.verify(stored, password or "")
    except Argon2Error:
        return None
    if row is None:
        return None  # verified against the dummy; same cost, no answer leaked
    return User(str(row["user_id"]), email, row["display_name"], row["timezone"])


def start_session(user: User, ttl_hours: int, user_agent: str | None) -> tuple[str, object]:
    """Returns (raw cookie value, expiry). Only the hash reaches the database."""
    raw = secrets.token_urlsafe(TOKEN_BYTES)
    with db.auth_tx() as cur:
        cur.execute(
            "SELECT auth.session_create(%s, %s, make_interval(hours => %s), %s) AS expires_at",
            (user.user_id, _digest(raw), ttl_hours, (user_agent or "")[:300]),
        )
        expires_at = cur.fetchone()["expires_at"]
    return raw, expires_at


def resolve(raw_token: str | None) -> User | None:
    """The replacement for the stub's resolve_user(). Cookie in, identity out."""
    if not raw_token:
        return None
    with db.auth_tx() as cur:
        cur.execute("SELECT * FROM auth.session_lookup(%s)", (_digest(raw_token),))
        row = cur.fetchone()
    if row is None:
        return None
    return User(str(row["user_id"]), row["email"], row["display_name"], row["timezone"])


def end_session(raw_token: str | None) -> None:
    if not raw_token:
        return
    with db.auth_tx() as cur:
        cur.execute("SELECT auth.session_delete(%s)", (_digest(raw_token),))


# --- MCP bearer tokens -----------------------------------------------------
# Same shape as sessions, for clients that hold a long-lived credential instead
# of a cookie. Minting is deliberately not here: it is an owner-connection
# operation in manage.py, so the app role can look a token up but can never
# issue one.


def new_api_token() -> tuple[str, bytes]:
    """Returns (value to hand out once, digest to store). No database access."""
    raw = secrets.token_urlsafe(TOKEN_BYTES)
    return raw, _digest(raw)


def resolve_api_token(raw_token: str | None) -> User | None:
    """Bearer token in, identity out. The MCP equivalent of resolve()."""
    if not raw_token:
        return None
    with db.auth_tx() as cur:
        cur.execute("SELECT * FROM auth.token_lookup(%s)", (_digest(raw_token),))
        row = cur.fetchone()
    if row is None:
        return None
    return User(str(row["user_id"]), row["email"], row["display_name"], row["timezone"])
