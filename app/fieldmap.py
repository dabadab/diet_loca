"""
Reading values out of payloads nobody promised us.

Both connectors ride undocumented APIs whose response shape is not a contract:
fields get renamed, move between endpoints, and occasionally arrive as a string
where a number used to be. The rule this module exists to enforce is the same
for all of them -- **a field that is missing or unreadable yields nothing, never
a guess** -- and the diagnostics exist so that when a field does move, the
evidence of where it went is already in hand.

Lifted out of garmin.py when Bosch arrived. Two copies of a defensive parser is
two chances for one of them to quietly stop being defensive.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Sequence

log = logging.getLogger("diet.fieldmap")


def dig(payload: Any, path: str) -> Any:
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
class FieldSpec:
    """
    One stored column, and the places its value might be hiding.

    Several candidate paths rather than one, because these APIs have renamed
    fields before and their endpoints disagree with each other about where a
    value lives. The first path that yields something usable wins; a path that
    yields something unusable is logged and skipped rather than ending the read,
    so one bad field costs only itself.
    """
    column: str
    paths: Sequence[str]
    convert: Callable[[Any], Any] = float
    source: str = "api"

    def read(self, item: dict) -> Any:
        for path in self.paths:
            raw = dig(item, path)
            if raw is None:
                continue
            try:
                return self.convert(raw)
            except (TypeError, ValueError):
                log.warning("%s: %s at %s was %r, unusable",
                            self.source, self.column, path, raw)
        return None


def apply_specs(item: dict, specs: Sequence[FieldSpec]) -> dict:
    """Every spec's column, present even when the value was not."""
    return {spec.column: spec.read(item) for spec in specs}


def describe_payload(payload: Any, prefix: str = "", depth: int = 2) -> list[str]:
    """
    Flat list of the numeric-looking keys in a payload.

    For when a value stops arriving: compare this against the specs to see where
    the field went, instead of reading a wall of JSON.
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


# Coordinates are a location trace. They have nothing to do with diet, and the
# stored `raw` is readable by diet_ro -- the role behind the model-facing
# query_sql -- so they never make it into the database.
LOCATION_HINTS = ("atitude", "ongitude", "olyline", "coordinate", "geo")


def strip_location(item: dict) -> dict:
    """The payload minus anything that says where its owner was."""
    return {k: v for k, v in item.items()
            if not any(h in k for h in LOCATION_HINTS)}


def parse_utc(value: Any) -> datetime | None:
    """
    A timestamp from either connector, as an aware UTC datetime.

    Garmin sends 'YYYY-MM-DD HH:MM:SS' which is UTC despite carrying no zone;
    Bosch sends ISO 8601, usually with one. A naive string is read as UTC, since
    that is what both mean by it -- guessing local would move a ride to the
    wrong day for anyone outside Greenwich.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # Epoch milliseconds if it is far too large to be seconds.
        secs = value / 1000 if value > 1e11 else value
        try:
            return datetime.fromtimestamp(secs, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
                    "%Y-%m-%dT%H:%M:%S.%f"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None \
        else parsed.astimezone(timezone.utc)
