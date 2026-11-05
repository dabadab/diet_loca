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
import os
import secrets
import urllib.parse
from datetime import date as date_cls, timedelta
from pathlib import Path
from typing import Any

from .fieldmap import (FieldSpec, apply_specs, describe_payload, dig, parse_utc,
                       strip_location)

log = logging.getLogger("diet.bosch")

# --- the identity provider -------------------------------------------------
# Two identity providers, because there are two kinds of client. The Flow app's
# own client signs in at SingleKey. A client registered at the Data Act portal
# (ids look like `euda-<uuid>`) does not: Bosch's portal gives the Keycloak
# realm below as the login address for those, and SingleKey has never heard of
# the id -- it answers "misconfigured application", exactly as it does for an
# invented one. Both from the providers' openid-configuration documents.
SINGLEKEY_ISSUER = "https://singlekey-id.com/auth"
DATA_ACT_ISSUER = "https://p9.authz.bosch.com/auth/realms/obc"

_client_id_env = os.environ.get("BOSCH_CLIENT_ID") or ""
ISSUER = (os.environ.get("BOSCH_ISSUER")
          or (DATA_ACT_ISSUER if _client_id_env.startswith("euda-")
              else SINGLEKEY_ISSUER)).rstrip("/")
IS_KEYCLOAK = ISSUER != SINGLEKEY_ISSUER
if IS_KEYCLOAK:
    AUTHORIZE_URL = f"{ISSUER}/protocol/openid-connect/auth"
    TOKEN_URL = f"{ISSUER}/protocol/openid-connect/token"
    IDP = "Bosch's login"
else:
    AUTHORIZE_URL = f"{ISSUER}/connect/authorize"
    TOKEN_URL = f"{ISSUER}/connect/token"
    IDP = "SingleKey"
# The Flow app's own public client. SingleKey no longer recognises it -- it
# answers with the same "misconfigured application" it gives an invented id --
# so both are overridable, which is what a client registered at
# portal.bosch-ebike.com/data-act needs.
CLIENT_ID = os.environ.get("BOSCH_CLIENT_ID") or "one-bike-app"
REDIRECT_URI = os.environ.get("BOSCH_REDIRECT_URI") or "onebikeapp-ios://"
# The realm advertises hundreds of scopes, most of them other clients'. Asking
# for one this client was not given fails the whole login with invalid_scope,
# and a client registered at the Data Act portal is not offered
# activity:summaries:read even though the Activity Records spec names it --
# the portal's own sign-in refuses it. Community clients read rides with a
# bare `openid` token, so the access is evidently granted by the rider's data
# sharing consent instead. BOSCH_SCOPES overrides.
SCOPES = (os.environ.get("BOSCH_SCOPES")
          or ("openid offline_access" if IS_KEYCLOAK
              else "openid profile email offline_access"))

# Two different backends depending on which client you hold. The app's own
# endpoints went with its client id; the Data Act host is what a client
# registered at portal.bosch-ebike.com/data-act talks to. Overridable because
# neither is documented anywhere we can read without an account.
APP_ACTIVITY_HOST = "https://obc-rider-activity.prod.connected-biking.cloud"
DATA_ACT_HOST = "https://api.bosch-ebike.com"
API_BASE = (os.environ.get("BOSCH_API_BASE")
            or (DATA_ACT_HOST if os.environ.get("BOSCH_CLIENT_ID")
                else APP_ACTIVITY_HOST))
# The path rides are served at. The smart-system one comes from community
# clients that use the Data Act API (eBike System 2 bikes serve
# /activity/ebike-system-2/v1/activities instead); Bosch's own documentation is
# behind the portal login, which is why it stays a setting.
ACTIVITY_PATH = (os.environ.get("BOSCH_ACTIVITY_PATH")
                 or ("/activity/smart-system/v1/activities" if IS_KEYCLOAK
                     else "/activities"))
PAGE_SIZE = 50
MAX_PAGES = 20

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


def check_client(url: str) -> None:
    """
    Ask the identity provider whether it will entertain this client at all.

    SingleKey answers an unknown client_id by redirecting to
    /errors/misconfigured-application -- and it answers an invented client id
    exactly the same way, which is how `one-bike-app` was found to be no longer
    recognised. Checking first costs one unauthenticated request and saves
    someone signing in and then hunting a devtools pane for a redirect that was
    never going to come.
    """
    import httpx2 as httpx
    try:
        reply = httpx.get(url, follow_redirects=False, timeout=20)
    except Exception as exc:
        log.warning("bosch: could not pre-check the client: %s", exc)
        return                      # a network blip is not a verdict
    if IS_KEYCLOAK:
        # Keycloak does not redirect to an error page: it renders one, with a
        # 400, and names the parameter it objects to.
        if reply.status_code == 400:
            body = " ".join((reply.text or "").split())
            raise NeedsLogin(
                f"Bosch's login refused the request for client {CLIENT_ID!r}:\n\n"
                f"  {body[:300]}\n\n"
                f"Check at portal.bosch-ebike.com/data-act that {REDIRECT_URI!r} "
                "is registered as a redirect URI character for character (a "
                "trailing slash counts), and that Bosch has approved the client "
                "-- an unapproved one can be refused for days.")
        return
    location = reply.headers.get("location", "")
    if "misconfigured-application" not in location:
        return

    head = f"SingleKey does not recognise the client id {CLIENT_ID!r}.\n\n"
    if os.environ.get("BOSCH_CLIENT_ID"):
        # A client the rider registered themselves. The id is probably right and
        # something about its registration is not, so point at that rather than
        # at the withdrawn app client, which is not what they are using.
        raise NeedsLogin(
            head +
            "That is the id from BOSCH_CLIENT_ID, so the registration is the "
            "thing to check at portal.bosch-ebike.com/data-act:\n\n"
            f"  * does the client still exist, and is the id exactly {CLIENT_ID!r}?\n"
            f"  * is {REDIRECT_URI!r} registered as its redirect URI, character "
            "for character? A trailing slash counts.\n"
            "  * were the data-recipient terms accepted?\n\n"
            "SingleKey gives this same answer for an unknown id and a mismatched "
            "redirect, so it cannot tell you which of those it is.")
    raise NeedsLogin(
        head +
        "This is the undocumented client the Flow app itself uses, and SingleKey "
        "answers an invented client id with exactly the same error -- so it has "
        "been withdrawn, and no amount of retrying will help.\n\n"
        "Two routes that do not depend on it:\n"
        "  * register your own client at portal.bosch-ebike.com/data-act (the EU "
        "Data Act gives you a right to this data, and the client is yours rather "
        "than borrowed), then set BOSCH_CLIENT_ID\n"
        "  * read the rides from Strava, which Flow syncs to officially")


def is_http_callback() -> bool:
    """Whether the redirect is one a browser will follow at all."""
    return REDIRECT_URI.startswith(("http://localhost", "http://127.0.0.1"))


def in_container() -> bool:
    """
    Whether this is running somewhere the rider's browser almost certainly is
    not.

    The deployment this was written for runs the app in Docker on a server and
    the browser on a laptop, so a listener on the container's own localhost is
    unreachable from where the sign-in happens. Catching that by default beats
    waiting five minutes for a callback that was never going to arrive.
    """
    return os.path.exists("/.dockerenv")


def listen_for_code(timeout: float = 300.0) -> str:
    """
    Catch the authorization code on a local callback, and hand the browser
    something civil to show for it.

    Only possible because a client registered at the Data Act portal nominates
    its own redirect URI, so it can be http://localhost -- which a browser will
    actually follow, unlike the app's onebikeapp-ios:// scheme. This is the
    whole reason that route is pleasanter: no devtools, no copy-paste.
    """
    import http.server
    import urllib.parse as up

    parsed = up.urlparse(REDIRECT_URI)
    port = parsed.port or 4200
    caught: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):                                  # noqa: N802
            params = up.parse_qs(up.urlparse(self.path).query)
            got = "code" in params or "error" in params
            if got:
                caught.update({k: v[0] for k, v in params.items()})
            body = ("<h2>Signed in.</h2><p>You can close this tab and go back "
                    "to the terminal.</p>" if "code" in params
                    else f"<h2>Sign-in failed.</h2><p>{caught.get('error','')}</p>"
                    if "error" in params else "<p>Waiting&hellip;</p>")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"<!doctype html><meta charset=utf-8>{body}".encode())

        def log_message(self, *a):       # the terminal is showing instructions
            pass

    try:
        server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    except OSError as exc:
        raise BoschError(
            f"could not listen on 127.0.0.1:{port} ({exc}). The redirect URI "
            f"registered with Bosch is {REDIRECT_URI}, so that port has to be "
            "free here -- and this has to run where your browser is, not inside "
            "the container") from exc

    server.timeout = timeout
    deadline = __import__("time").monotonic() + timeout
    with server:
        while not caught and __import__("time").monotonic() < deadline:
            server.handle_request()       # favicon and friends come through too
    if "error" in caught:
        raise BoschError(f"sign-in was refused: {caught['error']} "
                         f"{caught.get('error_description', '')}".strip())
    if "code" not in caught:
        raise BoschError(f"no callback arrived within {int(timeout)}s")
    return caught["code"]


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
        detail = params.get("error_description", [""])[0]
        raise BoschError(f"sign-in was refused: {params['error'][0]} {detail}".strip())
    code = params.get("code", [None])[0]
    if code:
        return code

    # A bare code is a perfectly good paste. Digging a whole URL out of a
    # devtools pane is the fiddly part of this, and someone who managed to read
    # the code off the screen should not be sent back for the rest of it.
    if "?" not in text and "/" not in text and len(text) >= 10:
        return text

    raise BoschError(
        "no code in that. Expected either the whole onebikeapp-ios:// URL -- "
        "the 'location:' response header of the last singlekey-id.com 302 in "
        "devtools' Network tab -- or just the code from it on its own")


# --- tokens ----------------------------------------------------------------

def _post_token(form: dict) -> dict:
    import httpx2 as httpx
    try:
        reply = httpx.post(TOKEN_URL, data={"client_id": CLIENT_ID, **form},
                           timeout=30)
    except Exception as exc:
        raise BoschError(f"could not reach {IDP}: {exc}") from exc
    if reply.status_code >= 400:
        detail = reply.text[:200]
        if reply.status_code in (400, 401):
            raise NeedsLogin(f"{IDP} refused the grant: {detail}")
        raise BoschError(f"{IDP} returned {reply.status_code}: {detail}")
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
# Field names are from Bosch's Activity Records API (Smart System) v1.4.0
# OpenAPI document, /activities, which a Data Act client can download from the
# portal. Units there: distance in metres, speeds in km/h, durations in
# seconds, rider power in watts.

def _kmh_to_mps(value: Any) -> float:
    return float(value) / 3.6


ACTIVITY_SPECS: tuple[FieldSpec, ...] = (
    # "Duration in motion". The elapsed time is endTime - startTime, which
    # extract_activity fills in because the summary has no field for it.
    FieldSpec("moving_s", ("durationWithoutStops",), source="bosch"),
    FieldSpec("distance_m", ("distance",), source="bosch"),
    # Deliberately not read. caloriesBurned is in the payload, but the decision
    # is to rely on Garmin's daily figure for energy expenditure and live with
    # its inaccuracies, so Bosch's number is never stored, never compared, and
    # cannot be mistaken for a second opinion on the day. (It also runs low
    # against the mechanical work, so it was not trustworthy anyway.)
    FieldSpec("kcal", (), source="bosch"),
    FieldSpec("avg_rider_power_w", ("riderPower.average",), source="bosch"),
    FieldSpec("avg_cadence_rpm", ("cadence.average",), source="bosch"),
    FieldSpec("elevation_gain_m", ("elevation.gain",), source="bosch"),
    FieldSpec("avg_speed_mps", ("speed.average",), convert=_kmh_to_mps,
              source="bosch"),
    # Present only when the rider paired a heart-rate strap with the Flow app.
    FieldSpec("max_hr", ("heartRate.maximum",), source="bosch"),
    FieldSpec("avg_hr", ("heartRate.average",), source="bosch"),
    FieldSpec("training_effect_aerobic", (), source="bosch"),
    FieldSpec("training_effect_anaerobic", (), source="bosch"),
)

_ID_PATHS = ("id",)
_START_PATHS = ("startTime",)
_END_PATHS = ("endTime",)


def _first(item: dict, paths) -> Any:
    for path in paths:
        value = dig(item, path)
        if value is not None:
            return value
    return None


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
        "name": _first(item, ("title",)),
        # The summary says nothing about how much the motor did, and NULL is
        # honest: "the motor did nothing" is a strong claim on an ebike.
        "motor_energy_share": None,
        "hr_zones": None,
        "raw": strip_location(item),
    }
    row.update(apply_specs(item, ACTIVITY_SPECS))
    ended_at = parse_utc(_first(item, _END_PATHS))
    if ended_at is not None and ended_at > started_at:
        row["duration_s"] = (ended_at - started_at).total_seconds()
    else:
        row["duration_s"] = None
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
    items: list = []
    try:
        offset = 0
        while True:
            payload = session.get(f"{API_BASE}{ACTIVITY_PATH}", limit=PAGE_SIZE,
                                  offset=offset, sort="-startTime")
            page = payload if isinstance(payload, list) else (
                payload.get("activitySummaries") or payload.get("activities")
                or payload.get("items") or payload.get("content") or []
            ) if isinstance(payload, dict) else []
            if not isinstance(page, list):
                log.warning("bosch: activities returned %s, not a list",
                            type(page).__name__)
                break
            items.extend(page)
            offset += PAGE_SIZE
            total = ((payload.get("pagination") or {}).get("total")
                     if isinstance(payload, dict) else None)
            starts = [parse_utc(_first(i, _START_PATHS)) for i in page
                      if isinstance(i, dict)]
            starts = [t for t in starts if t is not None]
            # Newest first, and the endpoint takes no date range that anyone has
            # confirmed, so stop once a page reaches back past the window.
            if (not page or len(page) < PAGE_SIZE
                    or (isinstance(total, int) and offset >= total)
                    or (starts and min(starts).date() < start - timedelta(days=1))
                    or offset >= PAGE_SIZE * MAX_PAGES):
                break
    except RateLimited:
        raise
    except NeedsLogin:
        raise
    except Exception as exc:
        log.warning("bosch: activities for %s..%s failed: %s", start, end, exc)
        return []

    # Bosch's own ordering has been seen to misbehave, so the window is applied
    # to what came back rather than trusted to the request. A day of slack each
    # side, because the caller's dates are the rider's local ones and these
    # timestamps are UTC.
    slack = timedelta(days=1)

    def _in_window(item: Any) -> bool:
        if not isinstance(item, dict):
            return False
        t = parse_utc(_first(item, _START_PATHS))
        return t is None or start - slack <= t.date() <= end + slack
    items = [i for i in items if _in_window(i)]

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
