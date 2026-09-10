"""
Read queries against diet.*.

Every function takes a cursor from db.user_tx(), so none of them filters by
user id: row-level security has already narrowed what the cursor can see. If a
WHERE user_id = ... ever appears in this file, something has gone wrong.
"""

from __future__ import annotations

_DAYS_SQL = """
WITH bounds AS (
    SELECT (now() AT TIME ZONE %(tz)s::text)::date                  AS to_date,
           (now() AT TIME ZONE %(tz)s::text)::date - (%(n)s::int-1) AS from_date
),
cal AS (
    SELECT d::date AS local_date
    FROM bounds, generate_series(from_date, to_date, interval '1 day') AS d
),
intake AS (
    SELECT m.local_date,
           sum(i.kcal)                              AS kcal,
           sum(i.protein_g)                         AS protein_g,
           sum(i.carb_g)                            AS carb_g,
           sum(i.fat_g)                             AS fat_g,
           bool_or(m.source = 'claude-estimate')    AS any_estimated
    FROM diet.meals m
    JOIN diet.meal_items i ON i.meal_id = m.meal_id
    WHERE m.superseded_by IS NULL
      AND m.local_date >= (SELECT from_date FROM bounds)
    GROUP BY m.local_date
),
logged AS (
    SELECT local_date, count(*) AS meals
    FROM diet.meals
    WHERE superseded_by IS NULL
      AND local_date >= (SELECT from_date FROM bounds)
    GROUP BY local_date
),
meas AS (
    SELECT local_date,
           max(value) FILTER (WHERE metric = 'total_kcal')  AS total_kcal,
           max(value) FILTER (WHERE metric = 'active_kcal') AS active_kcal,
           avg(value) FILTER (WHERE metric = 'weight_kg')   AS weight_kg
    FROM diet.measurements
    WHERE local_date >= (SELECT from_date FROM bounds)
    GROUP BY local_date
)
SELECT cal.local_date                                        AS date,
       round(intake.kcal)::int                               AS energy_in_kcal,
       round(intake.protein_g, 1)                            AS protein_g,
       round(intake.carb_g, 1)                               AS carb_g,
       round(intake.fat_g, 1)                                AS fat_g,
       round(coalesce(meas.total_kcal, meas.active_kcal))::int AS energy_out_kcal,
       -- true when only the active portion is known, so the UI can avoid
       -- presenting an incomplete figure as a real expenditure total.
       (meas.total_kcal IS NULL AND meas.active_kcal IS NOT NULL) AS energy_out_partial,
       round(meas.weight_kg::numeric, 1)                     AS weight_kg,
       tgt.kcal::int                                         AS target_kcal,
       round(tgt.protein_g, 1)                               AS target_protein_g,
       coalesce(logged.meals, 0)::int                        AS meals_logged,
       CASE WHEN intake.any_estimated IS TRUE THEN 'claude-estimate'
            WHEN intake.kcal IS NOT NULL      THEN 'manual'
            ELSE NULL END                                    AS intake_source
FROM cal
-- The target in force on that day: the latest row dated on or before it.
-- NULL means none had been set yet, which must survive to the UI rather than
-- being defaulted -- a day before the first target was not a missed target.
LEFT JOIN LATERAL (
    SELECT t.kcal, t.protein_g
    FROM diet.targets t
    WHERE t.effective_from <= cal.local_date
    ORDER BY t.effective_from DESC
    LIMIT 1
) tgt ON true
LEFT JOIN intake ON intake.local_date = cal.local_date
LEFT JOIN logged ON logged.local_date = cal.local_date
LEFT JOIN meas   ON meas.local_date   = cal.local_date
ORDER BY cal.local_date DESC
"""


def days(cur, timezone: str, n: int) -> list[dict]:
    """One row per calendar day in the user's own timezone, newest first."""
    cur.execute(_DAYS_SQL, {"tz": timezone, "n": n})
    rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        r["date"] = r["date"].isoformat()
        for k in ("weight_kg", "protein_g", "carb_g", "fat_g", "target_protein_g"):
            if r[k] is not None:
                r[k] = float(r[k])
    # A window of entirely empty days is not data; say so, so the UI can show
    # its "nothing recorded yet" state instead of a wall of dashes.
    if all(r["meals_logged"] == 0 and r["energy_out_kcal"] is None
           and r["weight_kg"] is None for r in rows):
        return []
    return rows


def sync_snapshot(cur) -> dict:
    """Per-connector freshness, plus enough counts to make the panel truthful."""
    cur.execute("""
        SELECT connector, last_attempt_at, last_success_at, last_error,
               extract(epoch FROM now() - last_success_at) AS success_age_s
        FROM diet.sync_state
    """)
    connectors = {r["connector"]: dict(r) for r in cur.fetchall()}

    cur.execute("""
        SELECT (SELECT count(*) FROM diet.meals WHERE superseded_by IS NULL) AS meals,
               (SELECT count(*) FROM diet.measurements)                      AS measurements,
               (SELECT max(created_at) FROM diet.meals
                 WHERE source = 'claude-estimate')                           AS last_claude_write
    """)
    counts = dict(cur.fetchone())
    return {"connectors": connectors, "counts": counts}


# Mirrors the measurements_metric_known CHECK in schema.sql. Kept here so a
# tool can reject an unknown metric with a helpful list instead of letting the
# constraint fire, and so the two lists are obviously meant to match.
KNOWN_METRICS = ("weight_kg", "resting_hr", "sleep_minutes", "steps",
                 "active_kcal", "total_kcal", "body_fat_pct")


def day_detail(cur, local_date) -> dict:
    """Everything recorded for one calendar day, in the user's own timezone."""
    cur.execute("""
        SELECT m.meal_id, m.eaten_at, m.description, m.source, m.created_at,
               coalesce(
                 json_agg(json_build_object(
                   'food', i.food, 'grams', i.grams, 'kcal', i.kcal,
                   'protein_g', i.protein_g, 'carb_g', i.carb_g, 'fat_g', i.fat_g,
                   'confidence', i.confidence
                 ) ORDER BY i.item_id) FILTER (WHERE i.item_id IS NOT NULL),
                 '[]'::json) AS items,
               coalesce(sum(i.kcal), 0)::float AS kcal
        FROM diet.meals m
        LEFT JOIN diet.meal_items i ON i.meal_id = m.meal_id
        WHERE m.local_date = %(d)s AND m.superseded_by IS NULL
        GROUP BY m.meal_id, m.eaten_at, m.description, m.source, m.created_at
        ORDER BY m.eaten_at
    """, {"d": local_date})
    meals = []
    for r in cur.fetchall():
        row = dict(r)
        row["meal_id"] = str(row["meal_id"])
        row["eaten_at"] = row["eaten_at"].isoformat()
        row["created_at"] = row["created_at"].isoformat()
        meals.append(row)

    cur.execute("""
        SELECT metric, value, unit, source, ts_utc
        FROM diet.measurements
        WHERE local_date = %(d)s
        ORDER BY metric
    """, {"d": local_date})
    measurements = [
        {**dict(r), "ts_utc": r["ts_utc"].isoformat()} for r in cur.fetchall()
    ]

    cur.execute(_TARGET_ON_SQL, {"d": local_date})
    tgt = cur.fetchone()

    return {
        "date": str(local_date),
        "meals": meals,
        "measurements": measurements,
        "energy_in_kcal": round(sum(m["kcal"] for m in meals)) if meals else None,
        "target_kcal": tgt["kcal"] if tgt else None,
        "target_protein_g": float(tgt["protein_g"]) if tgt else None,
    }


def range_metrics(cur, from_date, to_date, metrics: list[str] | None = None) -> list[dict]:
    """Measured series over a date range. Estimated data is not in here by design."""
    wanted = list(metrics) if metrics else list(KNOWN_METRICS)
    unknown = [m for m in wanted if m not in KNOWN_METRICS]
    if unknown:
        raise ValueError(
            f"unknown metric(s) {unknown}; known metrics are {list(KNOWN_METRICS)}")

    cur.execute("""
        SELECT local_date, metric, value, unit, source
        FROM diet.measurements
        WHERE local_date BETWEEN %(a)s AND %(b)s AND metric = ANY(%(m)s)
        ORDER BY local_date, metric
    """, {"a": from_date, "b": to_date, "m": wanted})
    return [{**dict(r), "local_date": r["local_date"].isoformat()} for r in cur.fetchall()]


def garmin_diagnostics(cur, timezone: str) -> dict:
    """
    Everything needed to answer "why is no Garmin data arriving".

    The three failure points are distinct and need distinguishing: no stored
    session, a poller that is not running, and a poller that runs and fails.
    A fourth looks like success -- syncing fine, but Garmin has nothing recent.
    """
    cur.execute("SELECT updated_at, key_id FROM diet.garmin_credentials")
    cred = cur.fetchone()

    cur.execute("""SELECT last_attempt_at, last_success_at, last_error
                   FROM diet.sync_state WHERE connector = 'garmin'""")
    sync = cur.fetchone()

    cur.execute("""
        SELECT max(local_date)                                          AS newest,
               count(*)                                                 AS total,
               count(*) FILTER (WHERE local_date
                     > (now() AT TIME ZONE %(tz)s::text)::date - 7)     AS last_7_days
        FROM diet.measurements WHERE source = 'garmin'
    """, {"tz": timezone})
    counts = dict(cur.fetchone())

    cur.execute("""
        SELECT metric,
               max(local_date)                                          AS last_seen,
               count(*)                                                 AS n,
               (array_agg(value ORDER BY local_date DESC, ts_utc DESC))[1] AS latest,
               (array_agg(unit  ORDER BY local_date DESC, ts_utc DESC))[1] AS unit
        FROM diet.measurements WHERE source = 'garmin'
        GROUP BY metric ORDER BY metric
    """)
    metrics = [{**dict(r), "last_seen": r["last_seen"].isoformat()} for r in cur.fetchall()]
    seen = {m["metric"] for m in metrics}

    return {
        "credential": None if cred is None else {
            "stored_at": cred["updated_at"].isoformat(), "key_id": cred["key_id"]},
        "last_attempt_at": sync["last_attempt_at"].isoformat() if sync and sync["last_attempt_at"] else None,
        "last_success_at": sync["last_success_at"].isoformat() if sync and sync["last_success_at"] else None,
        "last_error": sync["last_error"] if sync else None,
        "newest_reading": counts["newest"].isoformat() if counts["newest"] else None,
        "readings_total": counts["total"],
        "readings_last_7_days": counts["last_7_days"],
        "metrics": metrics,
        # Reported rather than treated as a fault: body_fat_pct needs a scale
        # that measures impedance, so its absence is normal for most people.
        "metrics_never_seen": [m for m in KNOWN_METRICS if m not in seen],
    }


# --- targets ---------------------------------------------------------------
# Effective-dated: a row applies from its date until a later one supersedes it.
# Everything here resolves by that rule rather than reading a "current" value,
# which is what lets a target set retroactively re-score exactly the days it
# should.

_TARGET_ON_SQL = """
    SELECT effective_from, kcal::int AS kcal, round(protein_g, 1) AS protein_g, note
    FROM diet.targets
    WHERE effective_from <= %(d)s
    ORDER BY effective_from DESC
    LIMIT 1
"""


def target_on(cur, day) -> dict | None:
    """The target in force on a date, or None if none had been set by then."""
    cur.execute(_TARGET_ON_SQL, {"d": day})
    row = cur.fetchone()
    if row is None:
        return None
    return {"effective_from": row["effective_from"].isoformat(),
            "kcal": row["kcal"],
            "protein_g": float(row["protein_g"]),
            "note": row["note"]}


def targets_list(cur) -> list[dict]:
    """Every target ever set, newest first -- the timeline, not just the tip."""
    cur.execute("""
        SELECT effective_from, kcal::int AS kcal, round(protein_g, 1) AS protein_g,
               note, created_at
        FROM diet.targets ORDER BY effective_from DESC
    """)
    return [{"effective_from": r["effective_from"].isoformat(),
             "kcal": r["kcal"],
             "protein_g": float(r["protein_g"]),
             "note": r["note"],
             "set_at": r["created_at"].isoformat()} for r in cur.fetchall()]
