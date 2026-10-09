"""
Fixtures: a throwaway Postgres, the schema applied to it, and a live app.

Every test runs against a real cluster rather than a mocked cursor, because
almost everything worth testing here *is* the database -- row-level security,
the CHECK constraints that catch a model's implausible numbers, the upsert
identity that stops Garmin's revisions duplicating. A fake cursor would agree
with whatever the code believes and catch none of it.

By default a `postgres:17-alpine` container is started on a free port and
removed afterwards. Set DIET_TEST_DSN to use a cluster you already have -- it
must be one you do not mind losing, since the schema ALTERs roles and the
fixtures drop and recreate the database.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

APP_PW, RO_PW, OWNER_PW = "testpw", "ropw", "ownerpw"
CONTAINER = "diet-tests-pg"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(check, *, timeout: float, what: str):
    """Poll until `check` returns truthy. Fails the test rather than hanging."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            if check():
                return
        except Exception as exc:          # not ready yet is the normal case
            last = exc
        time.sleep(0.25)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}"
                         + (f" (last error: {last})" if last else ""))


# --------------------------------------------------------------- cluster ---

@pytest.fixture(scope="session")
def pg_dsn() -> str:
    """An owner-role DSN to a cluster that may be destroyed."""
    existing = os.environ.get("DIET_TEST_DSN")
    if existing:
        return existing

    if not subprocess.run(["which", "docker"], capture_output=True).stdout:
        pytest.skip("docker is not on PATH and DIET_TEST_DSN is not set")

    port = free_port()
    subprocess.run(["docker", "rm", "-f", CONTAINER],
                   capture_output=True, check=False)
    subprocess.run(
        ["docker", "run", "-d", "--name", CONTAINER,
         "-e", f"POSTGRES_PASSWORD={OWNER_PW}", "-e", "POSTGRES_USER=diet_owner",
         "-e", "POSTGRES_DB=diet", "-p", f"127.0.0.1:{port}:5432",
         "postgres:17-alpine"],
        check=True, capture_output=True)
    dsn = f"postgresql://diet_owner:{OWNER_PW}@127.0.0.1:{port}/diet"
    try:
        wait_for(lambda: subprocess.run(
            ["docker", "exec", CONTAINER, "pg_isready", "-U", "diet_owner", "-q"],
            capture_output=True).returncode == 0,
            timeout=60, what="postgres to accept connections")
        # pg_isready can pass a moment before the socket really takes queries.
        wait_for(lambda: psycopg.connect(dsn, connect_timeout=2).close() or True,
                 timeout=30, what="postgres to answer a query")
        yield dsn
    finally:
        subprocess.run(["docker", "rm", "-f", CONTAINER],
                       capture_output=True, check=False)


@pytest.fixture
def owner_dsn(pg_dsn) -> str:
    """
    A freshly created, freshly migrated database, per test.

    Per test rather than per session on purpose: these tests assert on counts
    and averages over the whole table, and a suite where one test's leftovers
    move another's numbers teaches you to distrust it.
    """
    admin = pg_dsn.rsplit("/", 1)[0] + "/postgres"
    name = "t" + uuid.uuid4().hex[:16]
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute(f'CREATE DATABASE "{name}" OWNER diet_owner')
    dsn = pg_dsn.rsplit("/", 1)[0] + "/" + name
    from app import migrate
    migrate.apply(dsn, app_password=APP_PW, readonly_password=RO_PW)
    try:
        yield dsn
    finally:
        with psycopg.connect(admin, autocommit=True) as c:
            c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def dsns(owner_dsn) -> dict:
    """The three roles the app uses, against the per-test database."""
    base, name = owner_dsn.rsplit("/", 1)
    host = base.split("@", 1)[1]
    return {
        "owner": owner_dsn,
        "app": f"postgresql://diet_app:{APP_PW}@{host}/{name}",
        "ro": f"postgresql://diet_ro:{RO_PW}@{host}/{name}",
    }


@pytest.fixture
def owner(owner_dsn):
    """An autocommitting owner connection, for setting up and inspecting."""
    with psycopg.connect(owner_dsn, row_factory=dict_row, autocommit=True) as c:
        yield c


# ------------------------------------------------------------------ pool ---

@pytest.fixture
def pool(dsns):
    """
    app.db's module-level pools, opened against the test database.

    They are module state, so they have to be closed again or the next test
    silently keeps talking to the previous database.
    """
    from app import db
    db.close_pool()
    db.close_ro_pool()
    db.open_pool(dsns["app"], max_size=4)
    db.open_ro_pool(dsns["ro"], max_size=2)
    try:
        yield db
    finally:
        db.close_pool()
        db.close_ro_pool()


# ----------------------------------------------------------------- users ---

@pytest.fixture
def make_user(owner):
    """Create a user and return its id. Password is set only if asked for."""
    from app import auth

    def _make(email: str, *, timezone: str = "Europe/Budapest",
              password: str | None = None) -> str:
        row = owner.execute(
            """INSERT INTO auth.users (email, display_name, password_hash, timezone)
               VALUES (%s, %s, %s, %s) RETURNING user_id""",
            (email, email.split("@")[0],
             auth.hash_password(password) if password else "x", timezone),
        ).fetchone()
        return str(row["user_id"])
    return _make


@pytest.fixture
def user(make_user) -> str:
    return make_user("a@example.com")


@pytest.fixture
def other_user(make_user) -> str:
    """A second account, so isolation can be asserted rather than assumed."""
    return make_user("b@example.com")


@pytest.fixture
def issue_token(owner):
    """Mint an MCP bearer token the way manage.py does: store only the digest."""
    from app import auth

    def _issue(user_id: str, label: str = "tests") -> str:
        raw, digest = auth.new_api_token()
        owner.execute(
            """INSERT INTO auth.api_tokens (token_hash, user_id, label)
               VALUES (%s, %s, %s)""", (digest, user_id, label))
        return raw
    return _issue


# ------------------------------------------------------------ live server ---

@pytest.fixture
def server(dsns, tmp_path):
    """
    A real uvicorn process. Returns its base URL.

    Started as a subprocess rather than with an in-process test client because
    the things most worth checking here -- the redirect status on consent, the
    307 on POST /mcp, cookie flags, the CSP header -- are properties of the
    server, not of the ASGI app object.
    """
    port = free_port()
    env = {
        **os.environ,
        "PYTHONPATH": str(REPO),
        "DATABASE_URL": dsns["app"],
        "MIGRATION_DATABASE_URL": dsns["owner"],
        "READONLY_DATABASE_URL": dsns["ro"],
        "APP_DB_PASSWORD": APP_PW,
        "READONLY_DB_PASSWORD": RO_PW,
        "COOKIE_SECURE": "0",
        "MCP_PUBLIC_URL": f"http://127.0.0.1:{port}",
        "GARMIN_RAW_DIR": str(tmp_path / "raw"),
    }
    log = open(tmp_path / "uvicorn.log", "w+")
    proc = subprocess.Popen(
        [str(REPO / ".venv/bin/uvicorn"), "app.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    try:
        def up():
            if proc.poll() is not None:
                log.seek(0)
                raise AssertionError("server exited:\n" + log.read()[-2000:])
            with urllib.request.urlopen(base + "/api/health", timeout=2) as r:
                return r.status == 200
        wait_for(up, timeout=45, what="the app to come up")
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
