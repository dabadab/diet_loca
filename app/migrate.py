"""
Schema application at startup.

schema.sql is idempotent, so this just runs it -- under a session advisory lock
so that two app replicas starting at once do not both try to create the same
objects, and inside one transaction so a partial apply rolls back.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import psycopg

log = logging.getLogger("diet.migrate")

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"
# Arbitrary but fixed: any other process using this key is us.
LOCK_KEY = 0x2D1E7_0DB


def apply(dsn: str, *, app_password: str, readonly_password: str = "",
          retries: int = 10, delay: float = 2.0) -> None:
    if not app_password:
        raise RuntimeError("APP_DB_PASSWORD must be set: the migration creates "
                           "the runtime role with it")
    sql = SCHEMA_PATH.read_text()

    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            with psycopg.connect(dsn, connect_timeout=5) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_lock(%s)", (LOCK_KEY,))
                    # Passwords reach schema.sql as session settings rather than
                    # as literals in the file or in the log.
                    cur.execute("SELECT set_config('diet.app_password', %s, false)",
                                (app_password,))
                    cur.execute("SELECT set_config('diet.ro_password', %s, false)",
                                (readonly_password or "",))
                    cur.execute(sql)
                conn.commit()
            log.info("schema applied")
            return
        except psycopg.OperationalError as exc:  # database still coming up
            last = exc
            log.warning("database not ready (attempt %d/%d): %s", attempt, retries, exc)
            time.sleep(delay)
    raise RuntimeError(f"could not reach the database to migrate: {last}")
