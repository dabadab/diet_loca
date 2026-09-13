"""
The Garmin poller. One shot: run it, it exits, the exit code means something.

    python -m app.poller [--days 3] [--user someone@example.com]   # one shot
    python -m app.poller --loop [--interval 3600]                  # sidecar

Design points that matter operationally:

  * **One user's failure does not end the run.** An expired session belongs to
    one person; everybody else still syncs. Each user's outcome is recorded in
    diet.sync_state, so the frontend can show per-person staleness rather than
    a single global "sync broken".
  * **A rejected reading does not lose the day.** Each upsert runs inside a
    savepoint, so one implausible value from Garmin costs that value and
    nothing else.
  * **It exits non-zero when anything failed**, loudly and with a summary.
    Under --loop there is no exit code to read, so the outcome of each cycle is
    also written to a status file that the container healthcheck reads: a run
    of failed cycles turns the container unhealthy, which is the sidecar's
    equivalent of exiting loudly. Per-user failures additionally land in
    diet.sync_state, which is what the status panel already reads.

Enumeration runs on the owner connection, because listing every user is an
admin operation the RLS-bound app role deliberately cannot do. Every *write*
still goes through db.user_tx(), so the rows land under the same row-level
security as anything else.
"""

from __future__ import annotations

import argparse
import logging
import os
import random
import signal
import sys
import time
# `time` stays the stdlib module; the datetime one is aliased so the
# loop's time.sleep()/time.time() are not shadowed.
from datetime import date, datetime, timedelta, timezone
from datetime import time as time_of_day
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

from . import config, db, garmin, secretbox

log = logging.getLogger("diet.poller")

UPSERT = """
INSERT INTO diet.measurements
  (user_id, ts_utc, source, metric, value, unit, external_id)
VALUES (diet.current_user_id(), %(ts)s, 'garmin', %(metric)s, %(value)s, %(unit)s, %(ext)s)
ON CONFLICT (user_id, source, metric, external_id) DO UPDATE
  SET value = EXCLUDED.value, ts_utc = EXCLUDED.ts_utc, ingested_at = now()
"""


ACTIVITY_UPSERT = """
INSERT INTO diet.activities
  (user_id, source, external_id, started_at, activity_type, name,
   duration_s, moving_s, distance_m, kcal, avg_hr, max_hr,
   elevation_gain_m, avg_speed_mps,
   training_effect_aerobic, training_effect_anaerobic, raw)
VALUES (diet.current_user_id(), 'garmin', %(external_id)s, %(started_at)s,
        %(activity_type)s, %(name)s, %(duration_s)s, %(moving_s)s, %(distance_m)s,
        %(kcal)s, %(avg_hr)s, %(max_hr)s, %(elevation_gain_m)s, %(avg_speed_mps)s,
        %(training_effect_aerobic)s, %(training_effect_anaerobic)s, %(raw)s)
ON CONFLICT (user_id, source, external_id) DO UPDATE SET
  started_at = EXCLUDED.started_at, activity_type = EXCLUDED.activity_type,
  name = EXCLUDED.name, duration_s = EXCLUDED.duration_s,
  moving_s = EXCLUDED.moving_s, distance_m = EXCLUDED.distance_m,
  kcal = EXCLUDED.kcal, avg_hr = EXCLUDED.avg_hr, max_hr = EXCLUDED.max_hr,
  elevation_gain_m = EXCLUDED.elevation_gain_m,
  avg_speed_mps = EXCLUDED.avg_speed_mps,
  training_effect_aerobic = EXCLUDED.training_effect_aerobic,
  training_effect_anaerobic = EXCLUDED.training_effect_anaerobic,
  raw = EXCLUDED.raw, ingested_at = now()
"""


def _owner_conn(settings):
    if not settings.migration_database_url:
        sys.exit("MIGRATION_DATABASE_URL is not set; the poller needs the owner role "
                 "to enumerate users")
    return psycopg.connect(settings.migration_database_url, row_factory=dict_row)


def _accounts(settings, only_email: str | None) -> list[dict]:
    with _owner_conn(settings) as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT u.user_id, u.email, u.timezone, c.secret_ciphertext, c.key_id
            FROM diet.garmin_credentials c
            JOIN auth.users u USING (user_id)
            WHERE u.is_active AND (%s::text IS NULL OR u.email = lower(%s))
            ORDER BY u.email""", (only_email, only_email))
        return cur.fetchall()


def _store_tokens(user_id, token_json: str) -> None:
    """
    Tokens refresh as they are used; persist the new ones or the next run
    re-authenticates. Goes through user_tx rather than the owner connection --
    the row is the user's own and RLS scopes it, so this needs no superuser.
    """
    ciphertext, key_id = secretbox.encrypt(token_json)
    with db.user_tx(user_id) as cur:
        cur.execute("""UPDATE diet.garmin_credentials
                       SET secret_ciphertext = %s, key_id = %s, updated_at = now()
                       WHERE user_id = diet.current_user_id()""", (ciphertext, key_id))


def claim(user_id, min_gap_seconds: float = 90.0) -> bool:
    """
    Take the right to sync this account, or report that something else has it.

    The conditional UPDATE is the lock: the row is taken under a row lock, so
    the manual button and the sidecar cannot both be talking to Garmin at once
    and cannot both spend requests against a rate limit that is already tight.
    """
    with db.user_tx(user_id) as cur:
        cur.execute("""
            INSERT INTO diet.sync_state (user_id, connector, last_attempt_at)
            VALUES (diet.current_user_id(), 'garmin', now())
            ON CONFLICT (user_id, connector) DO UPDATE SET last_attempt_at = now()
              WHERE diet.sync_state.last_attempt_at IS NULL
                 OR diet.sync_state.last_attempt_at < now() - make_interval(secs => %s)
            RETURNING 1""", (min_gap_seconds,))
        return cur.fetchone() is not None


def credential_for(user_id) -> dict | None:
    """The account's own stored Garmin session. RLS scopes it; no owner needed."""
    with db.user_tx(user_id) as cur:
        cur.execute("""SELECT secret_ciphertext, key_id FROM diet.garmin_credentials
                       WHERE user_id = diet.current_user_id()""")
        return cur.fetchone()


def _record(user_id, *, ok: bool, error: str | None) -> None:
    with db.user_tx(user_id) as cur:
        cur.execute("""
            INSERT INTO diet.sync_state (user_id, connector, last_attempt_at,
                                         last_success_at, last_error)
            VALUES (diet.current_user_id(), 'garmin', now(),
                    CASE WHEN %(ok)s THEN now() END, %(err)s)
            ON CONFLICT (user_id, connector) DO UPDATE
              SET last_attempt_at = now(),
                  last_success_at = CASE WHEN %(ok)s THEN now()
                                         ELSE diet.sync_state.last_success_at END,
                  last_error = %(err)s""", {"ok": ok, "err": error})


def _write_readings(user_id, tz: ZoneInfo, day: date,
                    readings: list[garmin.Reading]) -> tuple[int, list[str]]:
    """Upsert one day. Returns (stored, rejected descriptions)."""
    # Noon local, so the day a reading belongs to survives the conversion to UTC
    # and back whatever the offset. Midnight would land on the previous day for
    # any user west of Greenwich.
    ts = datetime.combine(day, time_of_day(12, 0), tzinfo=tz).astimezone(timezone.utc)
    stored, rejected = 0, []
    if not readings:
        return 0, []
    with db.user_tx(user_id) as cur:
        for r in readings:
            cur.execute("SAVEPOINT reading")
            try:
                cur.execute(UPSERT, {"ts": ts, "metric": r.metric, "value": r.value,
                                     "unit": r.unit, "ext": day.isoformat()})
                cur.execute("RELEASE SAVEPOINT reading")
                stored += 1
            except psycopg.Error as exc:
                cur.execute("ROLLBACK TO SAVEPOINT reading")
                # Almost always measurements_value_sane: Garmin sent something
                # implausible, the schema caught it, the rest of the day stands.
                rejected.append(f"{r.metric}={r.value} ({getattr(exc.diag, 'constraint_name', '?')})")
    return stored, rejected


def _write_activities(user_id, rows: list[dict]) -> tuple[int, list[str]]:
    """Upsert a window's activities. Returns (stored, rejected descriptions)."""
    if not rows:
        return 0, []
    stored, rejected = 0, []
    with db.user_tx(user_id) as cur:
        for row in rows:
            params = dict(row, raw=Json(row["raw"]))
            cur.execute("SAVEPOINT activity")
            try:
                cur.execute(ACTIVITY_UPSERT, params)
                cur.execute("RELEASE SAVEPOINT activity")
                stored += 1
            except psycopg.Error as exc:
                cur.execute("ROLLBACK TO SAVEPOINT activity")
                # Same idea as _write_readings: Garmin sent something
                # implausible, the schema caught it, the rest of the window
                # stands.
                rejected.append(f"{row['external_id']} "
                                f"({getattr(exc.diag, 'constraint_name', '?')})")
    return stored, rejected


def _zone_backlog(user_id, budget: int) -> list[dict]:
    """
    Activities still owed a zone request, newest first.

    Selected through user_tx so RLS scopes it -- the poller never names a user
    in SQL. `zone_attempts` is what makes a persistently failing activity give
    up rather than burn a request every pass forever.
    """
    with db.user_tx(user_id) as cur:
        cur.execute("""
            SELECT external_id, (raw ->> 'avgPower') IS NOT NULL AS has_power
            FROM diet.activities
            WHERE user_id = diet.current_user_id() AND source = 'garmin'
              AND zones_fetched_at IS NULL AND zone_attempts < 3
            ORDER BY started_at DESC
            LIMIT %s""", (budget,))
        return cur.fetchall()


def _record_zones(user_id, external_id: str, hr, power, *, attempted_only: bool) -> None:
    """
    One activity's zone outcome.

    The attempt is recorded *before* the request and the result after, so a
    process that dies mid-fetch still counts the attempt. Stamping
    zones_fetched_at even when both zone sets came back empty is deliberate: a
    pool swim has no power zones and an activity without a HR strap has no HR
    zones, and neither should be asked about again.
    """
    with db.user_tx(user_id) as cur:
        if attempted_only:
            cur.execute("""UPDATE diet.activities SET zone_attempts = zone_attempts + 1
                           WHERE user_id = diet.current_user_id()
                             AND source = 'garmin' AND external_id = %s""",
                        (external_id,))
        else:
            cur.execute("""UPDATE diet.activities
                           SET hr_zones = %s, power_zones = %s, zones_fetched_at = now()
                           WHERE user_id = diet.current_user_id()
                             AND source = 'garmin' AND external_id = %s""",
                        (Json(hr) if hr else None, Json(power) if power else None,
                         external_id))


def backfill_zones(api, user_id, budget: int) -> int:
    """
    Fill in time-in-zone for activities that do not have it yet.

    Once per activity, ever -- zones do not change after Garmin has processed
    the workout -- so this settles at roughly one request a day once the
    backlog is drained. The budget is what keeps a first run over a week's
    window from firing thirty requests in one pass.
    """
    if budget <= 0:
        return 0
    done = 0
    for row in _zone_backlog(user_id, budget):
        external_id = row["external_id"]
        _record_zones(user_id, external_id, None, None, attempted_only=True)
        try:
            hr, power, answered = garmin.fetch_zones(
                api, external_id, power=row["has_power"])
        except Exception as exc:
            if garmin.is_rate_limited(exc):
                raise RateLimited(str(exc)) from exc
            raise
        if not answered:
            # The attempt is on the record; leave zones_fetched_at NULL so the
            # next pass retries, until zone_attempts gives up on it.
            continue
        _record_zones(user_id, external_id, hr, power, attempted_only=False)
        done += 1
    return done


class RateLimited(RuntimeError):
    """Garmin asked us to stop. Distinct so the loop can back off rather than retry."""


def poll_user(account: dict, days: int, raw_root: Path | None,
              zone_budget: int = 0) -> tuple[int, int, int, list[str]]:
    # zone_budget of 0 means "do not spend requests on zones this pass" -- it is
    # the caller, not the window size, that decides which cycle this is.
    user_id = account["user_id"]
    tz = ZoneInfo(account["timezone"])
    token_json = secretbox.decrypt(account["secret_ciphertext"], account["key_id"])

    try:
        api = garmin.resume(token_json)
    except Exception as exc:
        if garmin.is_rate_limited(exc):
            raise RateLimited(str(exc)) from exc
        raise
    total, rejected = 0, []
    today = datetime.now(tz).date()
    raw_dir = (raw_root / account["email"]) if raw_root else None

    for day in garmin.days_back(today, days):
        daily, sleep = garmin.fetch_day(api, day, raw_dir)
        readings = garmin.extract(daily, sleep)
        if not readings and (daily or sleep):
            if garmin.looks_empty(daily, sleep):
                # Ordinary and expected: today, before Garmin has anything for
                # it. Not worth a warning once an hour until the day fills in.
                log.info("garmin: %s has no data for %s yet", account["email"], day)
            else:
                # A populated day that matched nothing: the shape has moved.
                log.warning("garmin: %s %s produced no readings; numeric keys seen: %s",
                            account["email"], day,
                            ", ".join(garmin.describe_payload(daily or {})[:12]) or "none")
        n, bad = _write_readings(user_id, tz, day, readings)
        total += n
        rejected.extend(f"{day}: {b}" for b in bad)

    # One request for the whole window, however many activities are in it.
    start = today - timedelta(days=max(days - 1, 0))
    try:
        items = garmin.fetch_activities(api, start, today, raw_dir)
    except Exception as exc:
        if garmin.is_rate_limited(exc):
            raise RateLimited(str(exc)) from exc
        raise
    rows = [r for r in (garmin.extract_activity(i) for i in items) if r]
    activities, act_rejected = _write_activities(user_id, rows)
    rejected.extend(f"activity {b}" for b in act_rejected)

    zones = backfill_zones(api, user_id, zone_budget)

    refreshed = garmin.session_tokens(api)
    if refreshed and refreshed != token_json:
        _store_tokens(user_id, refreshed)
        log.info("garmin: refreshed session tokens for %s", account["email"])
    return total, activities, zones, rejected


STATUS_FILE = Path(os.environ.get("POLLER_STATUS_FILE", "/tmp/poller-status"))


def _write_status(ok: bool, detail: str) -> None:
    """Read by the container healthcheck; the only failure signal a sidecar has."""
    try:
        STATUS_FILE.write_text(f"{'ok' if ok else 'fail'} {int(time.time())} {detail}\n")
    except OSError as exc:
        log.warning("could not write status file %s: %s", STATUS_FILE, exc)


def check_status(max_age: int) -> int:
    """`--healthcheck`: 0 if the last cycle succeeded recently, 1 otherwise."""
    try:
        state, stamp, _, _ = (STATUS_FILE.read_text().strip() + "  ").split(" ", 3)
    except (OSError, ValueError):
        print("no status yet")
        return 1
    age = int(time.time()) - int(stamp)
    if state != "ok":
        print(f"last cycle failed {age}s ago")
        return 1
    if age > max_age:
        print(f"last successful cycle was {age}s ago (limit {max_age}s)")
        return 1
    print(f"ok, {age}s ago")
    return 0


def run_once(settings, args, raw_root: Path | None, days: int | None = None,
             fetch_zones: bool = True) -> int:
    """
    One pass over every account. `days` overrides the configured window.

    `fetch_zones` is false on the short cycle: a zone lookup costs a request per
    activity, and the whole point of the 15-minute cycle is that it is one
    request. It is a separate argument rather than inferred from `days` so that
    raising GARMIN_POLL_SMALL_DAYS cannot quietly start spending them.
    """
    window = args.days if days is None else days
    zone_budget = settings.garmin_zone_budget if fetch_zones else 0
    prune_raw(raw_root, args.keep_raw_days)
    accounts = _accounts(settings, args.user)
    if not accounts:
        log.warning("no Garmin credentials stored%s; nothing to do",
                    f" for {args.user}" if args.user else "")
        _write_status(True, "no-accounts")
        return 0

    db.open_pool(settings.database_url, max_size=4)
    failures = 0
    rate_limited = False
    try:
        for account in accounts:
            email = account["email"]
            try:
                if not claim(account["user_id"]):
                    log.info("garmin: %s skipped, a sync is already in flight", email)
                    continue
                stored, activities, zones, rejected = poll_user(
                    account, window, raw_root, zone_budget)
                _record(account["user_id"], ok=True, error=None)
                log.info("garmin: %s stored %d reading(s) and %d activity(-ies) "
                         "over %d day(s)%s%s",
                         email, stored, activities, window,
                         f", {zones} zone set(s) backfilled" if zones else "",
                         f", {len(rejected)} rejected: {'; '.join(rejected[:4])}"
                         if rejected else "")
            except RateLimited as exc:
                failures += 1
                rate_limited = True
                message = f"Garmin rate-limited this IP: {exc}"[:400]
                log.error("garmin: %s %s", email, message)
                try:
                    _record(account["user_id"], ok=False, error=message)
                except Exception:
                    log.exception("garmin: could not record rate limit for %s", email)
            except Exception as exc:
                failures += 1
                message = f"{type(exc).__name__}: {exc}"[:400]
                log.error("garmin: %s failed: %s", email, message)
                try:
                    _record(account["user_id"], ok=False, error=message)
                except Exception:
                    log.exception("garmin: could not record failure for %s", email)
    finally:
        db.close_pool()

    if failures:
        log.error("garmin: %d of %d account(s) failed", failures, len(accounts))
        _write_status(False, f"{failures}/{len(accounts)}-failed")
        # 2 means "back off", distinct from 1 so the loop can tell them apart.
        return 2 if rate_limited else 1
    log.info("garmin: %d account(s) synced over %d day(s)", len(accounts), window)
    _write_status(True, f"{len(accounts)}-synced")
    return 0


def prune_raw(raw_root: Path | None, keep_days: int) -> int:
    """
    Age out archived payloads.

    They are full health records -- sleep, resting heart rate, weight -- under a
    filename that identifies the person, and their diagnostic value is in the
    last few days, not the last few years.
    """
    if raw_root is None or keep_days <= 0 or not raw_root.exists():
        return 0
    cutoff = time.time() - keep_days * 86400
    removed = 0
    for f in raw_root.rglob("*.json"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
        except OSError as exc:
            log.warning("could not prune %s: %s", f, exc)
    if removed:
        log.info("pruned %d archived payload(s) older than %d days", removed, keep_days)
    return removed


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    ap = argparse.ArgumentParser(prog="app.poller", description="Pull recent Garmin data")
    ap.add_argument("--days", type=int, default=int(os.environ.get("GARMIN_POLL_DAYS", "3")),
                    help="trailing days to re-fetch (sleep lands late; upserts are idempotent)")
    ap.add_argument("--user", default=None, help="limit to one account by email")
    ap.add_argument("--raw-dir", default=os.environ.get("GARMIN_RAW_DIR", "/var/log/diet/garmin"),
                    help="where raw payloads are archived; empty to skip")
    ap.add_argument("--keep-raw-days", type=int,
                    default=int(os.environ.get("GARMIN_RAW_KEEP_DAYS", "30")),
                    help="delete archived payloads older than this; 0 keeps everything")
    ap.add_argument("--once", action="store_true",
                    help="force a single cycle even when GARMIN_POLL_LOOP is set; "
                         "for `docker compose run` against the sidecar's own service")
    ap.add_argument("--loop", action="store_true",
                    default=os.environ.get("GARMIN_POLL_LOOP", "").lower() in {"1","true","yes"},
                    help="stay running and poll on an interval (sidecar mode)")
    # GARMIN_POLL_INTERVAL was the single interval before the split; honoured as
    # the large one so an existing .env keeps working.
    _legacy = os.environ.get("GARMIN_POLL_INTERVAL")
    ap.add_argument("--small-days", type=int,
                    default=int(os.environ.get("GARMIN_POLL_SMALL_DAYS", "1")),
                    help="days fetched by the frequent cycle; 1 means today only")
    ap.add_argument("--interval-small", type=int,
                    default=int(os.environ.get("GARMIN_POLL_INTERVAL_SMALL", "900")),
                    help="seconds between today-only cycles in --loop mode")
    ap.add_argument("--interval-large", type=int,
                    default=int(os.environ.get("GARMIN_POLL_INTERVAL_LARGE", _legacy or "7200")),
                    help="seconds between full-window cycles in --loop mode")
    ap.add_argument("--backoff", type=int,
                    default=int(os.environ.get("GARMIN_BACKOFF_SECONDS", "1800")),
                    help="seconds to stop polling for after Garmin returns 429")
    ap.add_argument("--healthcheck", action="store_true",
                    help="exit 0 if the last cycle succeeded recently; for the container healthcheck")
    args = ap.parse_args()

    if args.healthcheck:
        return check_status(args.interval_small * 3 + 600)
    if args.once:
        args.loop = False

    settings = config.load()
    raw_root = Path(args.raw_dir) if args.raw_dir else None

    if not args.loop:
        return run_once(settings, args, raw_root)

    stopping = False

    def stop(signum, _frame):
        nonlocal stopping
        stopping = True
        log.info("signal %s received; finishing after this cycle", signum)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    # Two cadences. Today changes through the day, so it is polled often and
    # cheaply; the trailing window exists to catch Garmin's late arrivals and
    # revisions, which do not need checking every quarter hour. Fetching the
    # full window at the short interval would multiply the request count
    # against an API that has already rate-limited this deployment.
    def cycle(window: int, label: str, fetch_zones: bool = True) -> int:
        try:
            return run_once(settings, args, raw_root, days=window,
                            fetch_zones=fetch_zones)
        except Exception:
            # A crash must not end the loop; the status file records it.
            log.exception("garmin: %s cycle raised", label)
            _write_status(False, "cycle-crashed")
            return 1

    def jitter(interval: float) -> float:
        # So restarts do not synchronise onto Garmin at the same instant.
        return random.uniform(0, min(60.0, interval * 0.1))

    log.info("poller loop starting; today every %ds, %d-day window every %ds",
             args.interval_small, args.days, args.interval_large)

    now = time.monotonic()
    next_large, next_small = now, now + args.interval_small
    backoff_until = 0.0

    while not stopping:
        now = time.monotonic()
        if now >= backoff_until:
            code = None
            if now >= next_large:
                code = cycle(args.days, "full")
                next_large = now + args.interval_large + jitter(args.interval_large)
                # The full pass covered today, so the next short one can wait.
                next_small = now + args.interval_small + jitter(args.interval_small)
            elif now >= next_small:
                code = cycle(args.small_days, "today", fetch_zones=False)
                next_small = now + args.interval_small + jitter(args.interval_small)
            if code == 2:
                # Retrying into a rate limit only deepens it.
                backoff_until = time.monotonic() + args.backoff
                log.warning("garmin: rate-limited; pausing all polling for %ds", args.backoff)
        if stopping:
            break
        wake = max(backoff_until, min(next_small, next_large))
        for _ in range(max(1, int(wake - time.monotonic()))):
            if stopping:
                break
            time.sleep(1)
    log.info("poller loop stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
