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
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

log = logging.getLogger("diet.garmin")


@dataclass(frozen=True)
class Reading:
    metric: str
    value: float
    unit: str


def _dig(payload: Any, path: str) -> Any:
    """Dotted lookup that tolerates the key simply not being there."""
    cur = payload
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
        if cur is None:
            return None
    return cur


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
            raw = _dig(payload, path)
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


def describe_payload(payload: Any, prefix: str = "", depth: int = 2) -> list[str]:
    """
    Flat list of the numeric-looking keys in a payload.

    For when a metric stops arriving: compare this against DAILY_SPECS to see
    where the field moved, instead of reading a wall of JSON.
    """
    found: list[str] = []
    if depth < 0 or not isinstance(payload, dict):
        return found
    for k, v in payload.items():
        path = f"{prefix}{k}"
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            found.append(f"{path}={v}")
        elif isinstance(v, dict):
            found.extend(describe_payload(v, f"{path}.", depth - 1))
    return found


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
