"""
Diet tracker API.

Replaces stub_api.py. The stub's resolve_user() read a header the client chose;
here identity comes from a session cookie and nothing else, and every data
query runs inside db.user_tx() so Postgres -- not this file -- is what keeps
one user's rows away from another.
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone as dt_timezone

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastmcp.utilities.lifespan import combine_lifespans
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import auth, config, db, migrate, queries

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("diet.api")

STARTED_AT = time.time()
settings = config.load()


@asynccontextmanager
async def lifespan(_: FastAPI):
    if settings.run_migrations:
        migrate.apply(settings.migration_database_url,
                      app_password=settings.app_db_password,
                      readonly_password=settings.readonly_db_password)
    else:
        log.info("MIGRATION_DATABASE_URL unset; assuming the schema is managed elsewhere")
    db.open_pool(settings.database_url, max_size=settings.pool_max_size)
    if settings.query_sql_enabled:
        try:
            db.open_ro_pool(settings.readonly_database_url)
            log.info("read-only pool open; query_sql available")
        except Exception as exc:
            # One optional tool is not worth refusing to start for.
            log.warning("read-only pool unavailable, query_sql disabled: %s", exc)
    else:
        log.info("READONLY_DATABASE_URL unset; the query_sql tool is disabled")
    log.info("ready")
    try:
        yield
    finally:
        db.close_ro_pool()
        db.close_pool()


if settings.mcp_enabled and settings.oauth_enabled:
    from . import oauth_routes

# The MCP server is mounted at exactly /mcp, one path segment deep. That is not
# cosmetic: Claude.ai has a documented failure where the connector completes the
# OAuth handshake and then sends no traffic at all when the path is deeper.
if settings.mcp_enabled:
    from .mcp_server import mcp as _mcp

    # path="/mcp" here, not "/", and mounted at "/" below. The obvious shape
    # (http_app(path="/") mounted at "/mcp") makes Starlette 307-redirect a bare
    # POST /mcp to /mcp/ -- a method-preserving redirect on the exact URL the
    # connector posts to, which is a documented way to break the handshake.
    _mcp_app = _mcp.http_app(path=settings.mcp_path)
    # Both lifespans have to run: ours opens the database pools, FastMCP's
    # starts the session manager. Nested lifespans are not picked up.
    _lifespan = combine_lifespans(lifespan, _mcp_app.lifespan)
else:
    _mcp_app, _lifespan = None, lifespan

app = FastAPI(title="Diet tracker", docs_url=None, redoc_url=None,
              openapi_url=None, lifespan=_lifespan)


# --- plumbing --------------------------------------------------------------

@app.exception_handler(HTTPException)
async def http_error(_: Request, exc: HTTPException):
    # The frontend switches on this shape to tell "signed out" from "broken".
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code,
                        headers=exc.headers or {})


@app.middleware("http")
async def headers(request: Request, call_next):
    # MCP responses are streamed and are not browser documents: a CSP header is
    # meaningless on them, and turning a mid-stream error into a JSON body would
    # corrupt the transport. Let them through untouched.
    if request.url.path.startswith(settings.mcp_path):
        return await call_next(request)
    try:
        response = await call_next(request)
    except psycopg.Error as exc:           # pool exhausted, database gone, ...
        log.exception("database error on %s", request.url.path)
        response = JSONResponse({"error": "database_unavailable",
                                 "detail": str(exc).splitlines()[0]}, status_code=503)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


def current_user(request: Request) -> auth.User:
    user = auth.resolve(request.cookies.get(settings.cookie_name))
    if user is None:
        raise HTTPException(401, "unauthenticated")
    return user


def _client_key(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return (fwd.split(",")[0].strip() or (request.client.host if request.client else "?"))


def _ago(then: datetime | None) -> str:
    if then is None:
        return "never"
    secs = (datetime.now(dt_timezone.utc) - then).total_seconds()
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if secs >= size:
            n = int(secs // size)
            return f"{n} {unit}{'s' if n != 1 else ''} ago"
    return "just now"


def _uptime() -> str:
    secs = int(time.time() - STARTED_AT)
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    return f"{secs // 3600}h {secs % 3600 // 60}m"


# --- routes ----------------------------------------------------------------

@app.get("/", include_in_schema=False)
def index():
    page = settings.web_dir / "index.html"
    if not page.exists():
        raise HTTPException(500, "frontend_missing")
    return FileResponse(page, media_type="text/html; charset=utf-8")


@app.get("/api/health")
def health():
    """Unauthenticated, for the container healthcheck and for nginx."""
    try:
        ms = db.ping()
    except Exception as exc:
        return JSONResponse({"status": "degraded", "database": str(exc).splitlines()[0]},
                            status_code=503)
    return {"status": "ok", "database_ms": round(ms, 1),
            "uptime_s": round(time.time() - STARTED_AT)}


class Credentials(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=1024)


@app.post("/api/login")
def login(body: Credentials, request: Request, response: Response):
    key = _client_key(request)
    if auth.login_throttle.blocked(key):
        raise HTTPException(429, "too_many_attempts")

    user = auth.authenticate(body.email, body.password)
    if user is None:
        auth.login_throttle.record_failure(key)
        raise HTTPException(401, "invalid_credentials")

    auth.login_throttle.clear(key)
    raw, expires_at = auth.start_session(
        user, settings.session_ttl_hours, request.headers.get("user-agent"))
    response.set_cookie(
        settings.cookie_name, raw,
        max_age=settings.session_ttl_hours * 3600,
        httponly=True, samesite="lax", secure=settings.cookie_secure, path="/")
    log.info("login user=%s from=%s", user.user_id, key)
    return {"user_id": user.user_id, "display_name": user.display_name,
            "timezone": user.timezone, "expires_at": expires_at}


@app.post("/api/logout")
def logout(request: Request, response: Response):
    auth.end_session(request.cookies.get(settings.cookie_name))
    response.delete_cookie(settings.cookie_name, path="/")
    return {"ok": True}


@app.get("/api/me")
def me(user: auth.User = Depends(current_user)):
    return {"user_id": user.user_id, "display_name": user.display_name,
            "email": user.email, "timezone": user.timezone}


@app.get("/api/days")
def days(days: int = 7, user: auth.User = Depends(current_user)):
    n = max(1, min(int(days), 60))
    with db.user_tx(user.user_id) as cur:
        rows = queries.days(cur, user.timezone, n)
    return {"timezone": user.timezone, "days": rows}


@app.get("/api/status")
def status(user: auth.User = Depends(current_user)):
    """
    The chain the frontend draws, in data-flow order. Every line is measured,
    not asserted -- a stage says ok only because something answered.
    """
    stages = [
        {"stage": "Web server", "state": "ok",
         "detail": "static files served", "ms": None},
        {"stage": "API", "state": "ok",
         "detail": f"up {_uptime()}", "ms": None},
        {"stage": "Sign-in", "state": "ok",
         "detail": f"session valid for {user.display_name}", "ms": None},
    ]

    try:
        db_ms = db.ping()
        with db.user_tx(user.user_id) as cur:
            cur.execute("SHOW server_version")
            version = cur.fetchone()["server_version"].split()[0]
            snap = queries.sync_snapshot(cur)
    except psycopg.Error as exc:
        stages.append({"stage": "Database", "state": "down",
                       "detail": str(exc).splitlines()[0][:120], "ms": None})
        stages.append({"stage": "Garmin sync", "state": "down",
                       "detail": "unknown, database unreachable", "ms": None})
        stages.append({"stage": "Claude connector", "state": "down",
                       "detail": "unknown, database unreachable", "ms": None})
        return {"checked_at": time.time(), "stages": stages}

    counts = snap["counts"]
    stages.append({
        "stage": "Database", "state": "ok",
        "detail": (f"postgres {version} — {counts['meals']} meals, "
                   f"{counts['measurements']} measurements"),
        "ms": round(db_ms, 1)})

    garmin = snap["connectors"].get("garmin")
    if garmin is None:
        stages.append({"stage": "Garmin sync", "state": "down",
                       "detail": "poller has never run for this account", "ms": None})
    else:
        age_h = (garmin["success_age_s"] or 1e9) / 3600
        failing = (garmin["last_error"] and garmin["last_success_at"]
                   and garmin["last_attempt_at"]
                   and garmin["last_attempt_at"] > garmin["last_success_at"])
        state = "down" if (failing or garmin["last_success_at"] is None) else (
            "ok" if age_h < settings.garmin_stale_after_hours else "stale")
        detail = f"last success {_ago(garmin['last_success_at'])}"
        if failing:
            detail += f"; last attempt failed: {garmin['last_error'][:80]}"
        stages.append({"stage": "Garmin sync", "state": state,
                       "detail": detail, "ms": None})

    # Read from sync_state, which only an actual MCP tool call writes. The
    # earlier version inferred this from claude-estimate meals, which seed-demo
    # also produces -- so it would have reported a connector that was never
    # there.
    if not settings.mcp_enabled:
        stages.append({"stage": "Claude connector", "state": "down",
                       "detail": "MCP server disabled on this instance", "ms": None})
    else:
        mcp_state = snap["connectors"].get("claude-mcp")
        where = settings.mcp_public_url or f"mounted at {settings.mcp_path}"
        if mcp_state is None or mcp_state["last_success_at"] is None:
            stages.append({
                "stage": "Claude connector", "state": "stale",
                "detail": f"{where}; no tool has been called yet", "ms": None})
        else:
            age_h = (mcp_state["success_age_s"] or 0) / 3600
            stages.append({
                "stage": "Claude connector",
                "state": "ok" if age_h < 24 * 14 else "stale",
                "detail": f"{where}; last tool call {_ago(mcp_state['last_success_at'])}",
                "ms": None})

    return {"checked_at": time.time(), "stages": stages}


# The consent screen. Registered before the mount below so it wins, and it has
# to live on the app that holds the session cookie -- that cookie is the whole
# reason approving the connector is not a second login.
if settings.mcp_enabled and settings.oauth_enabled:
    app.include_router(oauth_routes.router)


# Mounted at "/" but registered last: Starlette matches routes in order, so
# every route above wins and only unmatched paths reach the MCP app, which
# serves the one route it has at exactly /mcp.
if _mcp_app is not None:
    app.mount("/", _mcp_app)
