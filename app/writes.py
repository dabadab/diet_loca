"""
Write paths against diet.*.

Like queries.py, every function here takes a cursor from db.user_tx(), so none
of them takes or filters by a user id. They go further: rows are stamped with
`diet.current_user_id()` in SQL rather than with a value passed in from Python,
so there is no argument anywhere in this module by which a caller -- or a model
driving an MCP tool -- could name a user. The RLS policy's WITH CHECK would
refuse it anyway; this makes it unrepresentable.

local_date is never supplied. The meals_local_date trigger derives it from the
user's own timezone, which is the only place that can get it right.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from psycopg.types.json import Json

_ITEM_FIELDS = ("food", "grams", "kcal", "protein_g", "carb_g", "fat_g", "confidence")


class WriteRejected(Exception):
    """A write refused for a reason the caller can act on."""


def _insert_items(cur, meal_id, items: Iterable[Mapping[str, Any]]) -> int:
    rows = list(items)
    for item in rows:
        if not item.get("food"):
            raise WriteRejected("every item needs a 'food' name")
        if item.get("kcal") is None:
            raise WriteRejected(f"item {item['food']!r} has no kcal")
        cur.execute(
            """INSERT INTO diet.meal_items
                 (meal_id, user_id, food, grams, kcal, protein_g, carb_g, fat_g, confidence)
               VALUES (%s, diet.current_user_id(), %s, %s, %s, %s, %s, %s, %s)""",
            (meal_id, *(item.get(f) for f in _ITEM_FIELDS)),
        )
    return len(rows)


def _touch_sync_state(cur) -> None:
    """Records that the connector is alive, so the status panel stops guessing."""
    cur.execute(
        """INSERT INTO diet.sync_state (user_id, connector, last_attempt_at, last_success_at)
           VALUES (diet.current_user_id(), 'claude-mcp', now(), now())
           ON CONFLICT (user_id, connector) DO UPDATE
             SET last_attempt_at = now(), last_success_at = now(), last_error = NULL"""
    )


def log_meal(cur, *, description: str, eaten_at, items: Iterable[Mapping[str, Any]],
             raw_analysis: Any = None) -> dict:
    """Record a meal and its per-item breakdown. Returns the new meal."""
    items = list(items)
    if not items:
        raise WriteRejected("a meal needs at least one item")

    cur.execute(
        """INSERT INTO diet.meals (user_id, eaten_at, description, source, raw_analysis)
           VALUES (diet.current_user_id(), %s, %s, 'claude-estimate', %s)
           RETURNING meal_id, local_date, eaten_at""",
        (eaten_at, description, Json(raw_analysis) if raw_analysis is not None else None),
    )
    meal = dict(cur.fetchone())
    meal["items"] = _insert_items(cur, meal["meal_id"], items)
    _touch_sync_state(cur)

    cur.execute(
        """SELECT sum(kcal)::float AS kcal FROM diet.meal_items WHERE meal_id = %s""",
        (meal["meal_id"],))
    meal["total_kcal"] = cur.fetchone()["kcal"]
    meal["meal_id"] = str(meal["meal_id"])
    meal["local_date"] = meal["local_date"].isoformat()
    meal["eaten_at"] = meal["eaten_at"].isoformat()
    return meal


def correct_meal(cur, *, meal_id, description: str | None = None, eaten_at=None,
                 items: Iterable[Mapping[str, Any]] | None = None,
                 raw_analysis: Any = None) -> dict:
    """
    Correct a meal without losing what was originally recorded.

    Append-only, per the schema's superseded_by design: the corrected version is
    a new row, and the old one is pointed at it. "Actually it was two slices" is
    the common case, and the first estimate stays inspectable afterwards.
    """
    cur.execute(
        """SELECT meal_id, eaten_at, description, superseded_by
           FROM diet.meals WHERE meal_id = %s""", (meal_id,))
    old = cur.fetchone()
    if old is None:
        # RLS makes another user's meal indistinguishable from a missing one,
        # which is the correct amount of information to give back.
        raise WriteRejected(f"no meal {meal_id}")
    if old["superseded_by"] is not None:
        raise WriteRejected(
            f"meal {meal_id} was already corrected; correct {old['superseded_by']} instead")

    cur.execute(
        """INSERT INTO diet.meals (user_id, eaten_at, description, source, raw_analysis)
           VALUES (diet.current_user_id(), %s, %s, 'claude-estimate', %s)
           RETURNING meal_id, local_date, eaten_at""",
        (eaten_at or old["eaten_at"],
         description if description is not None else old["description"],
         Json(raw_analysis) if raw_analysis is not None else None),
    )
    new = dict(cur.fetchone())

    if items is not None:
        count = _insert_items(cur, new["meal_id"], items)
    else:
        # Only the description or the time changed; carry the breakdown across.
        cur.execute(
            """INSERT INTO diet.meal_items
                 (meal_id, user_id, food, grams, kcal, protein_g, carb_g, fat_g, confidence)
               SELECT %s, user_id, food, grams, kcal, protein_g, carb_g, fat_g, confidence
               FROM diet.meal_items WHERE meal_id = %s""",
            (new["meal_id"], old["meal_id"]))
        count = cur.rowcount

    # Only now: the composite FK requires the replacement to exist first.
    cur.execute("UPDATE diet.meals SET superseded_by = %s WHERE meal_id = %s",
                (new["meal_id"], old["meal_id"]))
    _touch_sync_state(cur)

    return {"meal_id": str(new["meal_id"]),
            "supersedes": str(old["meal_id"]),
            "local_date": new["local_date"].isoformat(),
            "eaten_at": new["eaten_at"].isoformat(),
            "items": count}
