"""
Bosch eBike Flow ingest.

There is no public Bosch API. Flow's rides sit behind the same undocumented
endpoints the phone app uses, reached with the rider's own SingleKey ID account.
That makes two things true, and the module is shaped by both:

  * **The response shape is not a contract.** Parsing is declarative and
    defensive, through the shared `fieldmap` specs, and raw payloads are
    archived before anything reads them -- when a field moves, the evidence of
    where it went is already on disk.
  * **The login is interactive, once.** The app's OAuth redirect is
    `onebikeapp-ios://`, a mobile scheme no desktop browser will follow, so
    `manage.py bosch-login` hands over an authorize URL and takes back the
    redirect it could not follow. After that the refresh token carries it, and
    the rotated tokens are stored encrypted exactly as Garmin's session is.

Why bother, when Garmin already records cycling: Bosch measures the pedal
torque. Its rider power and calories are *measured*, where a watch infers them
from heart rate -- and on an ebike that inference is wrong in a specific
direction, because the motor is doing part of the work and the heart rate does
not know it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import urllib.parse
from datetime import date as date_cls
from pathlib import Path
from typing import Any

from .fieldmap import (FieldSpec, apply_specs, describe_payload, dig, parse_utc,
                       strip_location)

log = logging.getLogger("diet.bosch")

# --- the identity provider -------------------------------------------------
# Plain OIDC, from https://singlekey-id.com/auth/.well-known/openid-configuration
ISSUER = "https://singlekey-id.com/auth"
AUTHORIZE_URL = f"{ISSUER}/connect/authorize"
TOKEN_URL = f"{ISSUER}/connect/token"
CLIENT_ID = "one-bike-app"          # the app's public client; no secret exists
REDIRECT_URI = "onebikeapp-ios://"
SCOPES = "openid profile email offline_access"

ACTIVITY_HOST = "https://obc-rider-activity.prod.connected-biking.cloud"
PROFILE_HOST = "https://obc-rider-profile.prod.connected-biking.cloud"

# Refresh this long before the access token actually expires, so a slow request
# cannot start valid and finish rejected.
EXPIRY_MARGIN_S = 120


class BoschError(RuntimeError):
    """Anything the caller can act on: a dead session, a refused request."""


class NeedsLogin(BoschError):
    """The stored tokens can no longer be refreshed. Only a human fixes this."""


class RateLimited(BoschError):
    """Bosch asked us to stop. Distinct so the poller can back off."""


# --- the interactive half --------------------------------------------------

def start_login() -> tuple[str, str]:
    """
    Returns (authorize URL, PKCE verifier).

    The verifier has to survive until the code comes back, and the code comes
    back through a human with a clipboard, so it is handed to the caller rather
    than kept in module state.
    """
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    query = urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPES,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": secrets.token_urlsafe(16),
    })
    return f"{AUTHORIZE_URL}?{query}", verifier


def code_from_redirect(redirect: str) -> str:
    """
    Pull the authorization code out of the URL the browser could not follow.

    Accepts the whole `onebikeapp-ios://...` line as pasted, because that is
    what a person actually has in their clipboard, and says something useful
    when the paste is the wrong thing entirely -- which it will be the first
    time, since the URL has to be dug out of a devtools pane.
    """
    text = (redirect or "").strip().strip('"').strip("'")
    if not text:
        raise BoschError("nothing pasted")
    query = urllib.parse.urlparse(text).query or text.partition("?")[2]
    params = urllib.parse.parse_qs(query)
    if "error" in params:
        raise BoschError(f"sign-in was refused: {params['error'][0]}")
    code = params.get("code", [None])[0]
    if not code:
        raise BoschError(
            "no ?code= in that URL. It should start with onebikeapp-ios:// and "
            "is the request the browser could not open -- in devtools' network "
            "pane it is the failed one named oauth2redirect")
    return code


# --- tokens ----------------------------------------------------------------

def _post_token(form: dict) -> dict:
    import httpx2 as httpx
    try:
        reply = httpx.post(TOKEN_URL, data={"client_id": CLIENT_ID, **form},
                           timeout=30)
    except Exception as exc:
        raise BoschError(f"could not reach SingleKey: {exc}") from exc
    if reply.status_code >= 400:
        detail = reply.text[:200]
        if reply.status_code in (400, 401):
            raise NeedsLogin(f"SingleKey refused the grant: {detail}")
        raise BoschError(f"SingleKey returned {reply.status_code}: {detail}")
    return reply.json()


def exchange_code(code: str, verifier: str) -> dict:
    """One interactive login's worth of tokens."""
    return _post_token({"grant_type": "authorization_code", "code": code,
                        "redirect_uri": REDIRECT_URI, "code_verifier": verifier})


def refresh(token_blob: dict) -> dict:
    """
    A fresh access token. Returns the whole blob to store.

    SingleKey rotates refresh tokens, so the reply's refresh_token replaces the
    old one -- keeping the original would work until it did not, and then would
    need a human at a browser.
    """
    rt = (token_blob or {}).get("refresh_token")
    if not rt:
        raise NeedsLogin("no refresh token stored")
    fresh = _post_token({"grant_type": "refresh_token", "refresh_token": rt})
    return {**token_blob, **fresh}


class Session:
    """
    A logged-in rider. Refreshes on use and reports whether it had to, so the
    caller can persist the rotated tokens without being asked twice.
    """

    def __init__(self, tokens: dict):
        self.tokens = dict(tokens or {})
        self.refreshed = False

    def _bearer(self) -> str:
        import time
        expires_at = self.tokens.get("expires_at")
        stale = expires_at is None or time.time() > expires_at - EXPIRY_MARGIN_S
        if stale:
            self.tokens = refresh(self.tokens)
            self.tokens["expires_at"] = time.time() + int(
                self.tokens.get("expires_in") or 3600)
            self.refreshed = True
        token = self.tokens.get("access_token")
        if not token:
            raise NeedsLogin("no access token after refresh")
        return token

    def get(self, url: str, **params) -> Any:
        import httpx2 as httpx
        headers = {"Authorization": f"Bearer {self._bearer()}",
                   "Accept": "application/json"}
        try:
            reply = httpx.get(url, params=params or None, headers=headers,
                              timeout=60)
        except Exception as exc:
            raise BoschError(f"could not reach Bosch: {exc}") from exc
        if reply.status_code == 401:
            raise NeedsLogin("Bosch rejected the access token")
        if reply.status_code == 429:
            raise RateLimited(f"Bosch rate-limited this client: {reply.text[:120]}")
        if reply.status_code >= 400:
            raise BoschError(f"Bosch returned {reply.status_code} for {url}: "
                             f"{reply.text[:200]}")
        return reply.json()


def resume(token_json: str) -> Session:
    """Restore a stored session from the decrypted blob."""
    try:
        return Session(json.loads(token_json))
    except (TypeError, ValueError) as exc:
        raise NeedsLogin(f"stored Bosch tokens are unreadable: {exc}") from exc


def session_tokens(session: Session) -> str:
    return json.dumps(session.tokens)


def is_rate_limited(exc: BaseException) -> bool:
    name = type(exc).__name__
    text = str(exc).lower()
    return ("RateLimited" in name or "429" in text
            or "rate limit" in text or "too many" in text)


# --- rides -----------------------------------------------------------------
#
# PROVISIONAL FIELD MAP. Bosch publishes no schema, so each column lists every
# plausible spelling rather than one guess, and a column that matches nothing
# lands as NULL instead of a fabricated number. `manage.py bosch-probe` dumps a
# real payload's numeric keys so this list can be corrected against evidence --
# that is the first thing to run after a successful login, and until it has been
# run these names are hypotheses.

ACTIVITY_SPECS: tuple[FieldSpec, ...] = (
    FieldSpec("duration_s", ("duration", "durationSeconds", "totalDuration",
                             "ridingTime", "movingTime", "statistics.duration"),
              source="bosch"),
    FieldSpec("moving_s", ("movingDuration", "movingTime",
                           "statistics.movingTime"), source="bosch"),
    FieldSpec("distance_m", ("distance", "totalDistance", "distanceMeters",
                             "statistics.distance"), source="bosch"),
    # Rider calories, not system calories: what the person burned, which is the
    # whole reason this connector exists.
    FieldSpec("kcal", ("calories", "riderCalories", "kcal",
                       "energy.riderCalories", "statistics.calories"),
              source="bosch"),
    FieldSpec("avg_rider_power_w", ("averageRiderPower", "avgRiderPower",
                                    "riderPower.average", "averagePower",
                                    "statistics.averageRiderPower"),
              source="bosch"),
    FieldSpec("avg_cadence_rpm", ("averageCadence", "avgCadence",
                                  "cadence.average", "statistics.averageCadence"),
              source="bosch"),
    FieldSpec("elevation_gain_m", ("elevationGain", "totalAscent", "ascent",
                                   "statistics.elevationGain"), source="bosch"),
    FieldSpec("avg_speed_mps", ("averageSpeed", "avgSpeed", "speed.average",
                                "statistics.averageSpeed"), source="bosch"),
    FieldSpec("max_hr", (), source="bosch"),          # the bike has no HR sensor
    FieldSpec("avg_hr", (), source="bosch"),
    FieldSpec("training_effect_aerobic", (), source="bosch"),
    FieldSpec("training_effect_anaerobic", (), source="bosch"),
)

_ID_PATHS = ("activityId", "id", "rideId", "uuid", "activityUuid")
_START_PATHS = ("startTime", "startedAt", "startTimeUtc", "start",
                "beginTime", "startDate")
_RIDER_ENERGY = ("riderEnergy", "energy.rider", "riderEnergyWh", "riderWh")
_MOTOR_ENERGY = ("motorEnergy", "energy.motor", "motorEnergyWh", "motorWh")


def _first(item: dict, paths) -> Any:
    for path in paths:
        value = dig(item, path)
        if value is not None:
            return value
    return None


def _motor_share(item: dict) -> float | None:
    """
    How much of the ride the motor did, 0-1.

    Reported directly if Bosch offers it; otherwise derived from the two energy
    figures, which is the form the share is usually published in. Returns None
    rather than 0 when neither is present -- "the motor did nothing" and "we do
    not know what the motor did" are not the same claim, and on an ebike the
    difference is the entire point.
    """
    direct = _first(item, ("motorEnergyShare", "motorShare", "assistShare"))
    if direct is not None:
        try:
            share = float(direct)
        except (TypeError, ValueError):
            share = None
        if share is not None:
            return share / 100 if share > 1 else share
    rider, motor = _first(item, _RIDER_ENERGY), _first(item, _MOTOR_ENERGY)
    try:
        rider, motor = float(rider), float(motor)
    except (TypeError, ValueError):
        return None
    total = rider + motor
    return motor / total if total > 0 else None


def extract_activity(item: Any) -> dict | None:
    """
    One ride to one row, or None if it cannot be identified.

    An id and a start time are the row's identity. Without both there is nothing
    useful to store and nothing to match a Garmin ride against, so the item is
    skipped rather than stored half-formed.
    """
    if not isinstance(item, dict):
        return None
    external_id = _first(item, _ID_PATHS)
    started_at = parse_utc(_first(item, _START_PATHS))
    if external_id is None or started_at is None:
        log.warning("bosch: skipping a ride with no id or start; keys seen: %s",
                    ", ".join(sorted(item)[:12]))
        return None

    row = {
        "external_id": str(external_id),
        "started_at": started_at,
        # Bosch rides are all the same thing. Naming it after the system rather
        # than "cycling" keeps it distinguishable from a Garmin cycling row at a
        # glance, which matters when both are rows for one outing.
        "activity_type": "ebike_ride",
        "name": _first(item, ("name", "title", "activityName")),
        "motor_energy_share": _motor_share(item),
        "hr_zones": None,
        "raw": strip_location(item),
    }
    row.update(apply_specs(item, ACTIVITY_SPECS))
    if row.get("name") is not None:
        row["name"] = str(row["name"])[:300]
    return row


def fetch_activities(session: Session, start: date_cls, end: date_cls,
                     raw_dir: Path | None = None) -> list[dict]:
    """
    Every ride in a date window, archived before anything parses it.

    A failure here is logged and returns nothing rather than propagating: Bosch
    being down must not cost the Garmin half of a poll. A rate limit is the
    exception, since the caller needs to back off rather than carry on.
    """
    try:
        payload = session.get(f"{ACTIVITY_HOST}/activities",
                              **{"from": start.isoformat(), "to": end.isoformat()})
    except RateLimited:
        raise
    except NeedsLogin:
        raise
    except Exception as exc:
        log.warning("bosch: activities for %s..%s failed: %s", start, end, exc)
        return []

    items = payload if isinstance(payload, list) else (
        payload.get("activities") or payload.get("items")
        or payload.get("content") or []) if isinstance(payload, dict) else []
    if not isinstance(items, list):
        log.warning("bosch: activities for %s..%s returned %s, not a list",
                    start, end, type(items).__name__)
        return []

    if raw_dir is not None:
        try:
            raw_dir.mkdir(parents=True, exist_ok=True)
            (raw_dir / f"bosch-{start}_{end}.json").write_text(
                json.dumps([strip_location(i) for i in items
                            if isinstance(i, dict)], indent=2, default=str))
        except OSError as exc:
            log.warning("bosch: could not write raw rides for %s..%s: %s",
                        start, end, exc)
    return items


def probe(session: Session, start: date_cls, end: date_cls) -> list[str]:
    """
    What a real payload actually contains, for correcting ACTIVITY_SPECS.

    The field map above is hypotheses until this has been run once against a
    live account; this is the evidence it gets replaced with.
    """
    items = fetch_activities(session, start, end)
    if not items:
        return []
    lines = [f"{len(items)} ride(s); keys on the most recent:"]
    lines += ["  " + k for k in describe_payload(items[0], depth=3)]
    lines.append("  non-numeric keys: " + ", ".join(
        sorted(k for k, v in items[0].items()
               if not isinstance(v, (int, float)) or isinstance(v, bool))))
    return lines
