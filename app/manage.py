"""
Admin CLI. Runs inside the app container:

    docker compose exec app python -m app.manage adduser you@example.com "Your Name" Europe/Budapest
    docker compose exec app python -m app.manage passwd you@example.com
    docker compose exec app python -m app.manage seed-demo you@example.com

adduser and passwd connect as the *owner* role, because writing into auth.users
is deliberately outside what the runtime role may do. seed-demo goes through
the ordinary user_tx path, so the rows it writes are subject to exactly the
same row-level security as any other write.
"""

from __future__ import annotations

import argparse
import getpass
import random
import sys
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.rows import dict_row

from . import auth, config, db

settings = config.load()


def _owner_conn():
    if not settings.migration_database_url:
        sys.exit("MIGRATION_DATABASE_URL is not set; this command needs the owner role")
    return psycopg.connect(settings.migration_database_url, row_factory=dict_row)


def _read_password() -> str:
    pw = getpass.getpass("password: ")
    if pw != getpass.getpass("again: "):
        sys.exit("passwords did not match")
    try:
        return auth.hash_password(pw)
    except ValueError as exc:
        sys.exit(str(exc))


def cmd_adduser(args) -> None:
    digest = _read_password()
    with _owner_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO auth.users (email, display_name, timezone, password_hash)
               VALUES (lower(%s), %s, %s, %s) RETURNING user_id""",
            (args.email, args.display_name, args.timezone, digest))
        print("created", cur.fetchone()["user_id"])
        conn.commit()


def cmd_passwd(args) -> None:
    digest = _read_password()
    with _owner_conn() as conn, conn.cursor() as cur:
        cur.execute("UPDATE auth.users SET password_hash = %s WHERE email = lower(%s)",
                    (digest, args.email))
        if cur.rowcount == 0:
            sys.exit(f"no such user: {args.email}")
        # Password change invalidates every existing cookie.
        cur.execute("""DELETE FROM auth.sessions WHERE user_id =
                       (SELECT user_id FROM auth.users WHERE email = lower(%s))""",
                    (args.email,))
        conn.commit()
        print("password changed; existing sessions revoked")


def cmd_seed_demo(args) -> None:
    """Plausible sample rows, so the UI can be checked before Garmin exists."""
    with _owner_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT user_id, timezone FROM auth.users WHERE email = lower(%s)",
                    (args.email,))
        row = cur.fetchone()
    if row is None:
        sys.exit(f"no such user: {args.email}")

    db.open_pool(settings.database_url)
    rng = random.Random(args.email)
    now = datetime.now(timezone.utc)
    with db.user_tx(row["user_id"]) as cur:
        for i in range(args.days):
            day = now - timedelta(days=i)
            weight = 84.0 - i * 0.05 + rng.uniform(-0.3, 0.3)
            for metric, value, unit in (
                ("weight_kg", round(weight, 1), "kg"),
                ("total_kcal", 2200 + rng.randint(-150, 400), "kcal"),
                ("active_kcal", 400 + rng.randint(-150, 350), "kcal"),
                ("steps", 6000 + rng.randint(-2000, 7000), "count"),
                ("resting_hr", 52 + rng.randint(-4, 6), "bpm"),
            ):
                cur.execute(
                    """INSERT INTO diet.measurements
                         (user_id, ts_utc, source, metric, value, unit, external_id)
                       VALUES (%s, %s, 'garmin', %s, %s, %s, %s)
                       ON CONFLICT (user_id, source, metric, external_id)
                       DO UPDATE SET value = EXCLUDED.value, ts_utc = EXCLUDED.ts_utc""",
                    (row["user_id"], day, metric, value, unit,
                     f"demo-{metric}-{day.date()}"))

            for meal, items in _DEMO_MEALS:
                eaten = day.replace(hour=rng.choice([8, 13, 19]), minute=rng.randint(0, 59))
                cur.execute(
                    """INSERT INTO diet.meals
                         (user_id, eaten_at, description, source, raw_analysis)
                       VALUES (%s, %s, %s, 'claude-estimate', NULL)
                       RETURNING meal_id""",
                    (row["user_id"], eaten, meal))
                meal_id = cur.fetchone()["meal_id"]
                for food, grams, kcal, p, c, f in items:
                    cur.execute(
                        """INSERT INTO diet.meal_items
                             (meal_id, user_id, food, grams, kcal, protein_g, carb_g, fat_g, confidence)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                        (meal_id, row["user_id"], food, grams, kcal, p, c, f, 0.7))

        cur.execute(
            """INSERT INTO diet.sync_state (user_id, connector, last_attempt_at, last_success_at)
               VALUES (%s, 'garmin', now(), now() - interval '40 minutes')
               ON CONFLICT (user_id, connector)
               DO UPDATE SET last_attempt_at = EXCLUDED.last_attempt_at,
                             last_success_at = EXCLUDED.last_success_at,
                             last_error = NULL""",
            (row["user_id"],))
    db.close_pool()
    print(f"seeded {args.days} days for {args.email}")


# (description, [(food, grams, kcal, protein_g, carb_g, fat_g)])
_DEMO_MEALS = [
    ("porridge with milk and a banana", [
        ("rolled oats", 60, 228, 8.4, 40.2, 4.2),
        ("whole milk", 250, 158, 8.2, 12.0, 8.5),
        ("banana", 120, 107, 1.3, 27.4, 0.4)]),
    ("chicken thigh with rice and salad", [
        ("chicken thigh, skinless", 200, 356, 51.0, 0.0, 16.0),
        ("white rice, cooked", 220, 286, 5.9, 62.0, 0.6),
        ("mixed salad with olive oil", 150, 141, 1.5, 5.0, 13.0)]),
]


def main() -> None:
    ap = argparse.ArgumentParser(prog="app.manage")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("adduser", help="create an account")
    a.add_argument("email")
    a.add_argument("display_name")
    a.add_argument("timezone", nargs="?", default="UTC")
    a.set_defaults(fn=cmd_adduser)

    p = sub.add_parser("passwd", help="change a password and revoke sessions")
    p.add_argument("email")
    p.set_defaults(fn=cmd_passwd)

    s = sub.add_parser("seed-demo", help="insert sample measurements and meals")
    s.add_argument("email")
    s.add_argument("--days", type=int, default=7)
    s.set_defaults(fn=cmd_seed_demo)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
