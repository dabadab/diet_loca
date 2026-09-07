"""
Connection pool and the transaction wrappers.

The pool is module-private on purpose. Nothing outside this file gets a raw
connection, so there is no code path that can reach diet.* without app.user_id
being set first -- which is the whole basis of the row-level security design.
"""

from __future__ import annotations

import contextlib
import time
import uuid
from typing import Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

_pool: ConnectionPool | None = None
_ro_pool: ConnectionPool | None = None


def open_pool(dsn: str, *, max_size: int = 10) -> None:
    global _pool
    if _pool is not None:
        return
    _pool = ConnectionPool(
        dsn,
        min_size=1,
        max_size=max_size,
        max_idle=300,
        kwargs={"application_name": "diet-api"},
        open=True,
        timeout=10,
    )
    _pool.wait(timeout=15)


def open_ro_pool(dsn: str, *, max_size: int = 4) -> None:
    """
    Optional second pool for the MCP query_sql tool, connecting as diet_ro.

    Separate from the main pool because it is a different Postgres role with
    different privileges -- that role, not query inspection, is what makes
    handing a model an arbitrary-SQL tool defensible.
    """
    global _ro_pool
    if _ro_pool is not None or not dsn:
        return
    _ro_pool = ConnectionPool(
        dsn, min_size=0, max_size=max_size, max_idle=300,
        kwargs={"application_name": "diet-mcp-ro"}, open=True, timeout=10,
    )


def close_ro_pool() -> None:
    global _ro_pool
    if _ro_pool is not None:
        _ro_pool.close()
        _ro_pool = None


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def ro_pool_ready() -> bool:
    return _ro_pool is not None


def _require_pool() -> ConnectionPool:
    if _pool is None:
        raise RuntimeError("connection pool is not open")
    return _pool


@contextlib.contextmanager
def user_tx(user_id) -> Iterator[psycopg.Cursor]:
    """
    The only way to read or write diet.* data.

    set_config(..., is_local => true) is transaction-scoped, so the identity
    cannot outlive the transaction and cannot leak to the next borrower of a
    pooled connection. SET LOCAL is not used directly because it takes no
    parameters and would mean interpolating an id into SQL.
    """
    uid = str(uuid.UUID(str(user_id)))  # rejects anything that is not an id
    with _require_pool().connection() as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT set_config('app.user_id', %s, true)", (uid,))
                yield cur


@contextlib.contextmanager
def auth_tx() -> Iterator[psycopg.Cursor]:
    """
    For the identity bootstrap only -- the lookups that run *before* a user is
    known, and so cannot set app.user_id. Deliberately separate from user_tx so
    that "this query runs unscoped" is visible at the call site. The app role
    has no table privileges in auth, only EXECUTE on the definer functions, so
    this wrapper still cannot read arbitrary identity data.
    """
    with _require_pool().connection() as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                yield cur


@contextlib.contextmanager
def readonly_tx(user_id, *, statement_timeout_ms: int = 10_000) -> Iterator[psycopg.Cursor]:
    """
    Like user_tx, but as diet_ro: SELECT on diet.* only, still RLS-scoped, and
    with no access to the auth schema at all.

    The read-only transaction and the timeout are depth, not the control -- the
    role's grants already forbid writing. They are here so that a future grant
    mistake does not silently become a write path, and so one pathological
    query cannot pin a connection.
    """
    if _ro_pool is None:
        raise RuntimeError("read-only pool is not open; set READONLY_DATABASE_URL")
    uid = str(uuid.UUID(str(user_id)))
    with _ro_pool.connection() as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SET TRANSACTION READ ONLY")
                cur.execute("SELECT set_config('app.user_id', %s, true)", (uid,))
                cur.execute("SELECT set_config('statement_timeout', %s, true)",
                            (str(statement_timeout_ms),))
                yield cur


def ping() -> float:
    """Round-trip time to Postgres in milliseconds. Raises if unreachable."""
    started = time.perf_counter()
    with _require_pool().connection() as conn:
        conn.execute("SELECT 1")
    return (time.perf_counter() - started) * 1000.0
