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
       round(coalesce(meas.total_kcal, meas.active_kcal))::int AS energy_out_kcal,
       -- true when only the active portion is known, so the UI can avoid
       -- presenting an incomplete figure as a real expenditure total.
       (meas.total_kcal IS NULL AND meas.active_kcal IS NOT NULL) AS energy_out_partial,
       round(meas.weight_kg::numeric, 1)                     AS weight_kg,
       coalesce(logged.meals, 0)::int                        AS meals_logged,
       CASE WHEN intake.any_estimated IS TRUE THEN 'claude-estimate'
            WHEN intake.kcal IS NOT NULL      THEN 'manual'
            ELSE NULL END                                    AS intake_source
FROM cal
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
        if r["weight_kg"] is not None:
            r["weight_kg"] = float(r["weight_kg"])
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

    return {
        "date": str(local_date),
        "meals": meals,
        "measurements": measurements,
        "energy_in_kcal": round(sum(m["kcal"] for m in meals)) if meals else None,
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
