"""Environment-driven settings. Everything the container needs is here."""

from __future__ import annotations

import functools
import os
from dataclasses import dataclass
from pathlib import Path

_HERE = Path(__file__).resolve().parent


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # Runtime connection: the unprivileged, RLS-bound role.
    database_url: str
    # Owner connection, used once at startup to apply the schema. Unset it to
    # deploy against a database somebody else migrates.
    migration_database_url: str | None
    # Optional third connection: diet_ro, for the MCP query_sql tool only.
    readonly_database_url: str
    app_db_password: str
    readonly_db_password: str
    session_ttl_hours: int
    cookie_secure: bool
    cookie_name: str
    web_dir: Path
    mcp_public_url: str
    garmin_stale_after_hours: int
    pool_max_size: int
    mcp_enabled: bool
    mcp_path: str
    oauth_enabled: bool
    oauth_access_ttl_minutes: int
    oauth_refresh_ttl_days: int

    @property
    def run_migrations(self) -> bool:
        return bool(self.migration_database_url)

    @property
    def query_sql_enabled(self) -> bool:
        """query_sql needs the read-only role; without it the tool is hidden."""
        return bool(self.readonly_database_url)


@functools.lru_cache(maxsize=1)
def load() -> Settings:
    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        raise RuntimeError(
            "DATABASE_URL is not set. In the compose stack it is built from "
            "APP_DB_PASSWORD; see .env.example."
        )
    return Settings(
        database_url=dsn,
        migration_database_url=os.environ.get("MIGRATION_DATABASE_URL", "").strip() or None,
        readonly_database_url=os.environ.get("READONLY_DATABASE_URL", "").strip(),
        app_db_password=os.environ.get("APP_DB_PASSWORD", ""),
        readonly_db_password=os.environ.get("READONLY_DB_PASSWORD", ""),
        session_ttl_hours=int(os.environ.get("SESSION_TTL_HOURS", "720")),
        # Default on: the failure mode of a cookie sent in the clear is worse
        # than the failure mode of a login that does not work over http.
        cookie_secure=_flag("COOKIE_SECURE", True),
        cookie_name=os.environ.get("COOKIE_NAME", "diet_session"),
        web_dir=Path(os.environ.get("WEB_DIR", _HERE.parent / "web")),
        mcp_public_url=os.environ.get("MCP_PUBLIC_URL", "").strip(),
        garmin_stale_after_hours=int(os.environ.get("GARMIN_STALE_AFTER_HOURS", "24")),
        pool_max_size=int(os.environ.get("POOL_MAX_SIZE", "10")),
        mcp_enabled=_flag("MCP_ENABLED", True),
        # Claude.ai has a documented failure where a connector completes OAuth
        # and then sends nothing at all if the path is deeper than one segment.
        # Changing this is not advised.
        mcp_path=os.environ.get("MCP_PATH", "/mcp"),
        # OAuth needs a public https base URL, because the metadata documents
        # advertise absolute endpoints and Claude fetches them from outside.
        # Without one there is nothing to advertise, so it stays off.
        oauth_enabled=_flag("OAUTH_ENABLED", bool(os.environ.get("MCP_PUBLIC_URL", "").strip())),
        oauth_access_ttl_minutes=int(os.environ.get("OAUTH_ACCESS_TTL_MINUTES", "60")),
        oauth_refresh_ttl_days=int(os.environ.get("OAUTH_REFRESH_TTL_DAYS", "30")),
    )
