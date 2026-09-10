"""
The MCP server: the only surface Claude talks to.

Two rules carried over from the HTTP API, which are the whole basis of the
design and the reason this file is boring:

  * No tool takes a user id. Identity comes from the verified bearer token and
    nothing else, so there is no argument by which a confused model -- or an
    injected instruction inside a meal description -- can name another user.
  * Every tool body runs inside db.user_tx() or db.readonly_tx(). Postgres
    row-level security, not code in this file, is what keeps users apart.
"""

from __future__ import annotations

import logging
from datetime import date as date_cls, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import psycopg
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token
from pydantic import BaseModel, Field

from fastmcp.server.auth import MultiAuth

from . import config, db, queries, writes
from .mcp_auth import PostgresTokenVerifier

log = logging.getLogger("diet.mcp")
settings = config.load()

MAX_SQL_ROWS = 500

# Bearer tokens and OAuth on the same server. Claude.ai does the OAuth dance;
# Claude Code, MCP Inspector and scripts keep using a token from
# `manage.py issue-token`. Both resolve to the same AccessToken shape, so the
# tools below cannot tell -- and do not need to tell -- which was used.
_bearer = PostgresTokenVerifier()
if settings.oauth_enabled:
    from .oauth_provider import DietOAuthProvider

    oauth_provider = DietOAuthProvider(
        base_url=settings.mcp_public_url,
        access_ttl_minutes=settings.oauth_access_ttl_minutes,
        refresh_ttl_days=settings.oauth_refresh_ttl_days,
    )
    _auth = MultiAuth(server=oauth_provider, verifiers=[_bearer])
else:
    oauth_provider = None
    _auth = _bearer

mcp = FastMCP(
    name="Diet log",
    instructions=(
        "Personal diet and fitness log. Meals you record are estimates and are "
        "stored as such; measurements come from a fitness tracker and are not "
        "writable here. Days are calendar days in the user's own timezone. "
        "When correcting a meal, use correct_meal rather than logging a second "
        "one -- corrections keep the original for comparison."
    ),
    auth=_auth,
)


# --- identity and time -----------------------------------------------------

def _identity() -> tuple[str, ZoneInfo]:
    token = get_access_token()
    if token is None or not token.subject:
        raise ToolError("not authenticated")
    name = token.claims.get("timezone") or "UTC"
    try:
        return token.subject, ZoneInfo(name)
    except ZoneInfoNotFoundError:            # pragma: no cover - schema forbids it
        return token.subject, ZoneInfo("UTC")


def _parse_when(value: str | None, tz: ZoneInfo) -> datetime:
    """
    ISO 8601 in, aware datetime out.

    A bare local time means what the user means by it, so a naive value is
    read in their timezone rather than silently in UTC.
    """
    if value is None:
        return datetime.now(tz)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ToolError(f"could not read {value!r} as a date-time; use ISO 8601, "
                        "e.g. 2026-09-07T13:20") from None
    return parsed.replace(tzinfo=tz) if parsed.tzinfo is None else parsed


def _parse_date(value: str | None, tz: ZoneInfo) -> date_cls:
    if value is None:
        return datetime.now(tz).date()
    try:
        return date_cls.fromisoformat(value)
    except ValueError:
        raise ToolError(f"could not read {value!r} as a date; use YYYY-MM-DD") from None


def _explain(exc: psycopg.Error) -> ToolError:
    """
    Turn a constraint violation into something a model can act on.

    The schema is the validation layer on purpose -- it is what reliably catches
    a plausible but wrong number -- so its refusals have to come back legibly
    rather than as a stack trace.
    """
    diag = getattr(exc, "diag", None)
    constraint = getattr(diag, "constraint_name", None)
    message = getattr(diag, "message_primary", None) or str(exc).splitlines()[0]
    hints = {
        "meal_items_macros_plausible":
            "the protein/carb/fat figures do not add up to the kcal figure "
            "(roughly 4/4/9 per gram) — check which one is wrong",
        "meal_items_kcal_check": "kcal must be between 0 and 10000 for a single item",
        "meal_items_grams_check": "grams must be above 0 and at most 20000",
        "measurements_value_sane": "that value is outside the plausible range for the metric",
    }
    if constraint in hints:
        return ToolError(f"rejected by the database: {hints[constraint]}")
    if constraint:
        return ToolError(f"rejected by the database ({constraint}): {message}")
    return ToolError(f"rejected by the database: {message}")


# --- schemas ---------------------------------------------------------------

class MealItem(BaseModel):
    """One food within a meal, with its estimated nutrition."""
    food: str = Field(description="What it is, e.g. 'rolled oats' or 'chicken thigh, skinless'")
    kcal: float = Field(description="Estimated energy for this item, in kilocalories")
    grams: float | None = Field(default=None, description="Estimated weight in grams")
    protein_g: float | None = Field(default=None, description="Protein in grams")
    carb_g: float | None = Field(default=None, description="Carbohydrate in grams")
    fat_g: float | None = Field(default=None, description="Fat in grams")
    confidence: float | None = Field(
        default=None, ge=0, le=1,
        description="How sure you are of this estimate, 0 to 1. Be honest; it is stored.")


# --- tools -----------------------------------------------------------------

@mcp.tool
def log_meal(description: str, items: list[MealItem], eaten_at: str | None = None) -> dict:
    """
    Record a meal the user describes, with a per-item nutrition breakdown.

    Pass the user's own words as `description` and your parsed estimate as
    `items`. Both are kept: the description is what they said, the items are
    what you made of it. `eaten_at` is ISO 8601 and defaults to now; a value
    without a timezone is read in the user's own timezone.
    """
    user_id, tz = _identity()
    when = _parse_when(eaten_at, tz)
    payload = [i.model_dump() for i in items]
    try:
        with db.user_tx(user_id) as cur:
            return writes.log_meal(cur, description=description, eaten_at=when,
                                   items=payload,
                                   raw_analysis={"items": payload, "eaten_at": eaten_at})
    except writes.WriteRejected as exc:
        raise ToolError(str(exc)) from None
    except psycopg.Error as exc:
        raise _explain(exc) from None


@mcp.tool
def correct_meal(meal_id: str, description: str | None = None,
                 items: list[MealItem] | None = None,
                 eaten_at: str | None = None) -> dict:
    """
    Correct a meal already logged, keeping the original.

    Use this rather than logging a second meal when the user says something
    like "actually it was two slices". Omitted fields keep their previous
    value; omit `items` entirely to keep the existing breakdown. Returns the
    new meal_id, which is what any further correction should target.
    """
    user_id, tz = _identity()
    try:
        with db.user_tx(user_id) as cur:
            return writes.correct_meal(
                cur, meal_id=meal_id, description=description,
                eaten_at=_parse_when(eaten_at, tz) if eaten_at else None,
                items=[i.model_dump() for i in items] if items is not None else None,
                raw_analysis={"corrects": meal_id})
    except writes.WriteRejected as exc:
        raise ToolError(str(exc)) from None
    except psycopg.Error as exc:
        raise _explain(exc) from None


@mcp.tool
def get_day(date: str | None = None) -> dict:
    """
    Everything recorded for one day: meals with their items, and measurements.

    `date` is YYYY-MM-DD in the user's own timezone and defaults to today.
    Superseded meals are not included — you see the current version only.
    """
    user_id, tz = _identity()
    with db.user_tx(user_id) as cur:
        return queries.day_detail(cur, _parse_date(date, tz))


@mcp.tool
def get_range(from_date: str | None = None, to_date: str | None = None,
              metrics: list[str] | None = None) -> dict:
    """
    Measured metrics over a date range — weight, resting HR, sleep, steps,
    calories burned. These come from the fitness tracker and are not estimates.

    Dates are YYYY-MM-DD in the user's timezone; the default range is the last
    14 days. `metrics` defaults to all of them.
    """
    user_id, tz = _identity()
    end = _parse_date(to_date, tz)
    start = _parse_date(from_date, tz) if from_date else end - timedelta(days=13)
    if start > end:
        raise ToolError("from_date is after to_date")
    try:
        with db.user_tx(user_id) as cur:
            rows = queries.range_metrics(cur, start, end, metrics)
    except ValueError as exc:
        raise ToolError(str(exc)) from None
    return {"from": start.isoformat(), "to": end.isoformat(), "measurements": rows}


@mcp.tool
def get_targets(date: str | None = None) -> dict:
    """
    The calorie and protein targets, and which one applies on a given day.

    Targets are effective-dated: one is set from a date and applies until a
    later one supersedes it, so a day is always scored against what was being
    aimed at then rather than against the current figure. `date` is YYYY-MM-DD
    in the user's timezone and defaults to today. Days before the earliest
    target have none, which is reported as null rather than as a missed target.
    """
    user_id, tz = _identity()
    when = _parse_date(date, tz)
    with db.user_tx(user_id) as cur:
        return {"date": when.isoformat(),
                "in_force": queries.target_on(cur, when),
                "history": queries.targets_list(cur)}


@mcp.tool
def set_target(effective_from: str, kcal: float | None = None,
               protein_g: float | None = None, note: str | None = None) -> dict:
    """
    Set the target that applies from a date onwards.

    Works retroactively: give a past `effective_from` and every day from there
    until the next target re-scores, which is how to correct a target that was
    changed in real life before it was recorded here. Future dates are allowed.

    Omit `kcal` or `protein_g` to keep whatever is already in force on that date
    — so raising protein alone does not require restating calories. Setting the
    same date twice replaces that entry rather than stacking another.

    `note` is free text for why it changed ("start of cut"), and is worth
    filling in: it is the only record of the reason.
    """
    user_id, tz = _identity()
    when = _parse_date(effective_from, tz)
    try:
        with db.user_tx(user_id) as cur:
            saved = writes.set_target(cur, effective_from=when, kcal=kcal,
                                      protein_g=protein_g, note=note)
            saved["history"] = queries.targets_list(cur)
            return saved
    except writes.WriteRejected as exc:
        raise ToolError(str(exc)) from None
    except psycopg.Error as exc:
        raise _explain(exc) from None


@mcp.tool
def clear_target(effective_from: str) -> dict:
    """
    Remove the target entry set on exactly this date.

    For undoing an entry made on the wrong date — the days it covered fall back
    to the entry before it. Only an exact date matches, because removing a
    nearby one instead would shift the whole timeline after it.
    """
    user_id, tz = _identity()
    when = _parse_date(effective_from, tz)
    with db.user_tx(user_id) as cur:
        if not writes.clear_target(cur, effective_from=when):
            existing = [t["effective_from"] for t in queries.targets_list(cur)]
            raise ToolError(
                f"no target is set on {when.isoformat()}. "
                + (f"Dates with an entry: {', '.join(existing[:8])}"
                   if existing else "No targets have been set at all."))
        return {"cleared": when.isoformat(), "history": queries.targets_list(cur)}


@mcp.tool
def query_sql(sql: str) -> dict:
    """
    Run a read-only SQL query against the user's own data, for questions the
    other tools cannot answer.

    Connects as a role that holds SELECT and nothing else, and that cannot see
    the identity tables at all. Row-level security still applies, so you can
    only ever read this user's rows — write the query as if the tables held
    their data alone. Tables: diet.meals, diet.meal_items, diet.measurements,
    diet.sync_state, diet.targets. Single SELECT (or WITH) statement, at most
    500 rows returned.
    """
    if not db.ro_pool_ready():
        raise ToolError("ad-hoc SQL is not available on this server "
                        "(the read-only database role is not configured)")
    user_id, _ = _identity()

    stripped = sql.strip().rstrip(";").strip()
    if not stripped:
        raise ToolError("empty query")
    if ";" in stripped:
        raise ToolError("one statement at a time")
    if not stripped.lower().startswith(("select", "with")):
        raise ToolError("only SELECT (or WITH ... SELECT) queries are allowed")

    try:
        with db.readonly_tx(user_id) as cur:
            cur.execute(stripped)
            rows = cur.fetchmany(MAX_SQL_ROWS)
            truncated = cur.fetchone() is not None
    except psycopg.Error as exc:
        raise ToolError(f"query failed: {str(exc).splitlines()[0]}") from None

    return {
        "columns": list(rows[0].keys()) if rows else [],
        "rows": [{k: (v.isoformat() if hasattr(v, "isoformat") else v)
                  for k, v in r.items()} for r in rows],
        "row_count": len(rows),
        "truncated": truncated,
    }
