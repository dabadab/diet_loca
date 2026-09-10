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
from datetime import date, datetime, timezone
from datetime import time as time_of_day
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row

from . import config, db, garmin, secretbox

log = logging.getLogger("diet.poller")

UPSERT = """
INSERT INTO diet.measurements
  (user_id, ts_utc, source, metric, value, unit, external_id)
VALUES (diet.current_user_id(), %(ts)s, 'garmin', %(metric)s, %(value)s, %(unit)s, %(ext)s)
ON CONFLICT (user_id, source, metric, external_id) DO UPDATE
  SET value = EXCLUDED.value, ts_utc = EXCLUDED.ts_utc, ingested_at = now()
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


def _store_tokens(settings, user_id, token_json: str) -> None:
    """Tokens refresh as they are used; persist the new ones or the next run re-authenticates."""
    ciphertext, key_id = secretbox.encrypt(token_json)
    with _owner_conn(settings) as conn, conn.cursor() as cur:
        cur.execute("""UPDATE diet.garmin_credentials
                       SET secret_ciphertext = %s, key_id = %s, updated_at = now()
                       WHERE user_id = %s""", (ciphertext, key_id, user_id))
        conn.commit()


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


def poll_user(settings, account: dict, days: int, raw_root: Path | None) -> tuple[int, list[str]]:
    user_id = account["user_id"]
    tz = ZoneInfo(account["timezone"])
    token_json = secretbox.decrypt(account["secret_ciphertext"], account["key_id"])

    api = garmin.resume(token_json)
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

    refreshed = garmin.session_tokens(api)
    if refreshed and refreshed != token_json:
        _store_tokens(settings, user_id, refreshed)
        log.info("garmin: refreshed session tokens for %s", account["email"])
    return total, rejected


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


def run_once(settings, args, raw_root: Path | None) -> int:
    prune_raw(raw_root, args.keep_raw_days)
    accounts = _accounts(settings, args.user)
    if not accounts:
        log.warning("no Garmin credentials stored%s; nothing to do",
                    f" for {args.user}" if args.user else "")
        _write_status(True, "no-accounts")
        return 0

    db.open_pool(settings.database_url, max_size=4)
    failures = 0
    try:
        for account in accounts:
            email = account["email"]
            try:
                stored, rejected = poll_user(settings, account, args.days, raw_root)
                _record(account["user_id"], ok=True, error=None)
                log.info("garmin: %s stored %d reading(s)%s", email, stored,
                         f", {len(rejected)} rejected: {'; '.join(rejected[:4])}"
                         if rejected else "")
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
        return 1
    log.info("garmin: %d account(s) synced", len(accounts))
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
    ap.add_argument("--interval", type=int,
                    default=int(os.environ.get("GARMIN_POLL_INTERVAL", "3600")),
                    help="seconds between cycles in --loop mode")
    ap.add_argument("--healthcheck", action="store_true",
                    help="exit 0 if the last cycle succeeded recently; for the container healthcheck")
    args = ap.parse_args()

    if args.healthcheck:
        return check_status(args.interval * 3 + 600)
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

    log.info("poller loop starting; every %ds, %d day window", args.interval, args.days)
    while not stopping:
        try:
            run_once(settings, args, raw_root)
        except Exception:
            # A crash must not end the loop; the status file records it.
            log.exception("garmin: cycle raised")
            _write_status(False, "cycle-crashed")
        if stopping:
            break
        # Jitter so restarts do not synchronise onto Garmin at the same instant.
        delay = args.interval + random.uniform(0, min(60, args.interval * 0.1))
        for _ in range(int(delay)):
            if stopping:
                break
            time.sleep(1)
    log.info("poller loop stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
