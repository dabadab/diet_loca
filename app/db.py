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


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


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


def ping() -> float:
    """Round-trip time to Postgres in milliseconds. Raises if unreachable."""
    started = time.perf_counter()
    with _require_pool().connection() as conn:
        conn.execute("SELECT 1")
    return (time.perf_counter() - started) * 1000.0
