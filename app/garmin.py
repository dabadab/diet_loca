"""
Garmin Connect ingest.

There is no usable free official API -- the Health API needs partner approval --
so this rides the same undocumented endpoints the mobile app uses, via
`garminconnect`. Two consequences shape everything below:

  * **The response shape is not a contract.** Field names move. So parsing is
    declarative and defensive: a metric whose key is missing is skipped, not
    guessed at, and never fabricated. `describe_payload()` exists to show what
    actually arrived when a field moves.
  * **Raw JSON is written to disk before parsing.** When the shape shifts, the
    evidence of what Garmin sent is already saved rather than needing a
    reproduction against a live account.

The session is a token blob from `Client.dumps()`, stored encrypted in
Postgres. The password is used once, interactively, and never persisted.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date as date_cls
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from .fieldmap import (FieldSpec, describe_payload, dig, parse_utc,
                       strip_location)

log = logging.getLogger("diet.garmin")

# Re-exported: these were defined here before Bosch arrived and needed them too.
__all__ = ["describe_payload", "extract", "extract_activity", "fetch_day",
           "fetch_activities", "is_rate_limited", "looks_empty", "resume"]


@dataclass(frozen=True)
class Reading:
    metric: str
    value: float
    unit: str


@dataclass(frozen=True)
class MetricSpec:
    metric: str
    unit: str
    # Several candidates because Garmin has renamed these before and the
    # endpoints disagree with each other about where a value lives.
    paths: Sequence[str]
    convert: Callable[[float], float] = lambda v: float(v)

    def read(self, payload: dict) -> Reading | None:
        for path in self.paths:
            raw = dig(payload, path)
            if raw is None:
                continue
            try:
                value = self.convert(float(raw))
            except (TypeError, ValueError):
                log.warning("garmin: %s at %s was %r, not a number",
                            self.metric, path, raw)
                continue
            return Reading(self.metric, value, self.unit)
        return None


# The metric names must stay inside the measurements_metric_known CHECK, and
# the values inside measurements_value_sane; the schema is the backstop for
# anything that changes here.
DAILY_SPECS: tuple[MetricSpec, ...] = (
    MetricSpec("total_kcal", "kcal", ("totalKilocalories", "totalKilocalorie")),
    MetricSpec("active_kcal", "kcal", ("activeKilocalories", "activeKilocalorie")),
    MetricSpec("steps", "count", ("totalSteps", "steps")),
    MetricSpec("resting_hr", "bpm",
               ("restingHeartRate", "restingHeartRateTimestamp.value",
                "allMetrics.metricsMap.WELLNESS_RESTING_HEART_RATE.0.value")),
    # Garmin reports body weight in grams.
    MetricSpec("weight_kg", "kg", ("weight", "totalAverage.weight"),
               convert=lambda v: v / 1000.0),
    MetricSpec("body_fat_pct", "%", ("bodyFat", "totalAverage.bodyFat")),
)

SLEEP_SPECS: tuple[MetricSpec, ...] = (
    MetricSpec("sleep_minutes", "min",
               ("dailySleepDTO.sleepTimeSeconds", "sleepTimeSeconds"),
               convert=lambda v: v / 60.0),
)


def extract(daily: dict | None, sleep: dict | None) -> list[Reading]:
    """Turn one day's raw payloads into readings. Missing data yields nothing."""
    out: list[Reading] = []
    for payload, specs in ((daily, DAILY_SPECS), (sleep, SLEEP_SPECS)):
        if not isinstance(payload, dict):
            continue
        for spec in specs:
            reading = spec.read(payload)
            if reading is not None:
                out.append(reading)
    return out


# A day Garmin has not populated yet comes back as a skeleton -- in practice
# four numeric fields (userProfileId, netRemainingKilocalories, from, until).
# A populated day carries around forty. The test is the *shape*, not the field
# names, so it still holds if Garmin renames things: a populated day whose
# fields moved keeps its many keys and is still reported as a problem.
EMPTY_DAY_MAX_NUMERIC_KEYS = 8


def looks_empty(daily: dict | None, sleep: dict | None) -> bool:
    """True when Garmin simply has nothing for the day yet."""
    keys = describe_payload(daily or {}) + describe_payload(sleep or {})
    return len(keys) <= EMPTY_DAY_MAX_NUMERIC_KEYS


# --- session ---------------------------------------------------------------

def _client_class():
    # Imported lazily so the web app does not pull the Garmin stack at startup.
    from garminconnect import Garmin
    return Garmin


def login_interactive(email: str, password: str,
                      mfa_prompt: Callable[[], str]) -> tuple[Any, str]:
    """
    One interactive login. Returns (client, token_json).

    The password is used here and nowhere else; only the resulting session
    tokens are ever stored.
    """
    Garmin = _client_class()
    api = Garmin(email=email, password=password, prompt_mfa=mfa_prompt)
    api.login()
    return api, api.client.dumps()


def resume(token_json: str) -> Any:
    """Restore a stored session. Raises if the tokens are no longer usable."""
    Garmin = _client_class()
    api = Garmin()
    api.login(tokenstore=token_json)
    return api


def session_tokens(api: Any) -> str:
    """Current tokens, which may have been refreshed during the run."""
    return api.client.dumps()


def fetch_day(api: Any, day: date_cls, raw_dir: Path | None = None) -> tuple[dict | None, dict | None]:
    """One day's payloads, written to disk before anything parses them."""
    cdate = day.isoformat()
    daily = sleep = None
    try:
        daily = api.get_stats_and_body(cdate)
    except Exception as exc:
        log.warning("garmin: stats for %s failed: %s", cdate, exc)
    try:
        sleep = api.get_sleep_data(cdate)
    except Exception as exc:
        log.warning("garmin: sleep for %s failed: %s", cdate, exc)

    if raw_dir is not None:
        try:
            raw_dir.mkdir(parents=True, exist_ok=True)
            (raw_dir / f"{cdate}.json").write_text(
                json.dumps({"stats_and_body": daily, "sleep": sleep},
                           indent=2, default=str))
        except OSError as exc:
            # Losing the archive must not lose the run.
            log.warning("garmin: could not write raw payload for %s: %s", cdate, exc)
    return daily, sleep


# --- activities ------------------------------------------------------------
# A workout, unlike a daily metric, is an interval with a dozen correlated
# fields. The whole window comes back in one request, so this costs one call
# per poll however many activities are in it.

ACTIVITY_SPECS: tuple[FieldSpec, ...] = (
    FieldSpec("name", ("activityName",), convert=lambda v: str(v)[:300] or None),
    FieldSpec("duration_s", ("duration", "elapsedDuration")),
    FieldSpec("moving_s", ("movingDuration",)),
    FieldSpec("distance_m", ("distance",)),
    FieldSpec("kcal", ("calories",)),
    FieldSpec("avg_hr", ("averageHR",)),
    FieldSpec("max_hr", ("maxHR",)),
    FieldSpec("elevation_gain_m", ("elevationGain",)),
    FieldSpec("avg_speed_mps", ("averageSpeed",)),
    FieldSpec("training_effect_aerobic", ("aerobicTrainingEffect",)),
    FieldSpec("training_effect_anaerobic", ("anaerobicTrainingEffect",)),
)


# Garmin sends the intensity breakdown inline, as hrTimeInZone_1..5 holding
# seconds. There is a per-activity /hrTimeInZones endpoint too, and this code
# used to call it -- one request per workout for figures that were already in
# hand. Reading them here is what keeps an activity poll to a single request.
HR_ZONE_COUNT = 5


def _hr_zones(item: dict) -> list[dict] | None:
    """Seconds per heart-rate zone, or None for an activity recorded without."""
    out = []
    for n in range(1, HR_ZONE_COUNT + 1):
        secs = item.get(f"hrTimeInZone_{n}")
        if secs is None:
            continue
        try:
            out.append({"zone": n, "secs": float(secs)})
        except (TypeError, ValueError):
            log.warning("garmin: hrTimeInZone_%d was %r, not a number", n, secs)
    return out or None


def extract_activity(item: Any) -> dict | None:
    """
    One list item to one row, or None if it cannot be identified.

    activityId, a start time and a type are the row's identity; without all
    three there is nothing useful to store, so the item is skipped rather than
    stored half-formed. Everything else is optional by design.
    """
    if not isinstance(item, dict):
        return None
    external_id = item.get("activityId")
    started_at = parse_utc(item.get("startTimeGMT"))
    activity_type = dig(item, "activityType.typeKey") or item.get("activityTypeKey")
    if external_id is None or started_at is None or not activity_type:
        log.warning("garmin: skipping activity with no id/start/type: %s",
                    ", ".join(sorted(item)[:10]) if isinstance(item, dict) else item)
        return None

    row: dict[str, Any] = {
        "external_id": str(external_id),
        "started_at": started_at,
        "activity_type": str(activity_type)[:100],
        "hr_zones": _hr_zones(item),
        "raw": strip_location(item),
    }
    for spec in ACTIVITY_SPECS:
        row[spec.column] = spec.read(item)
    return row


def fetch_activities(api: Any, start: date_cls, end: date_cls,
                     raw_dir: Path | None = None) -> list[dict]:
    """
    Every activity in a date window, in one request.

    An outage here must not cost the day's measurements, so a failure is logged
    and returns nothing rather than propagating.
    """
    try:
        items = api.get_activities_by_date(start.isoformat(), end.isoformat())
    except Exception as exc:
        if is_rate_limited(exc):
            raise
        log.warning("garmin: activities for %s..%s failed: %s", start, end, exc)
        return []
    if not isinstance(items, list):
        log.warning("garmin: activities for %s..%s returned %s, not a list",
                    start, end, type(items).__name__)
        return []

    if raw_dir is not None:
        try:
            raw_dir.mkdir(parents=True, exist_ok=True)
            (raw_dir / f"activities-{start}_{end}.json").write_text(
                json.dumps([strip_location(i) for i in items if isinstance(i, dict)],
                           indent=2, default=str))
        except OSError as exc:
            log.warning("garmin: could not write raw activities for %s..%s: %s",
                        start, end, exc)
    return items


def is_rate_limited(exc: BaseException) -> bool:
    """
    Whether Garmin is telling us to slow down.

    Matched by name and message rather than by importing the exception: the
    Garmin stack is imported lazily so the web app does not pull it in, and the
    library has renamed these before.
    """
    name = type(exc).__name__
    text = str(exc).lower()
    return ("TooManyRequests" in name or "429" in text
            or "rate limit" in text or "too many" in text)


def days_back(today: date_cls, n: int) -> Iterable[date_cls]:
    """
    Trailing window, newest first.

    Deliberately re-fetches days already stored: sleep and body-battery land
    late and Garmin revises figures afterwards, which is exactly why the
    measurements table upserts instead of inserting.
    """
    from datetime import timedelta
    for i in range(n):
        yield today - timedelta(days=i)
