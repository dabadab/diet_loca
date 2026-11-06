"""
Fit Profile ingest: the GE body-composition scale's weigh-ins.

There is no public API. The scale talks Bluetooth to the Fit Profile phone app,
which uploads every weigh-in to Yolanda's cloud -- the same QN platform
Renpho's app used -- and this module reads them back the way the app does.
Everything here was read out of the Android app (1.38.0) and confirmed against
the live service; none of it is documented, so the same two rules apply as for
Bosch: the response shape is not a contract, and raw payloads are archived
before anything parses them.

What the app does, and so what this has to do:

  * **Every request and reply is AES-wrapped.** AES-128-ECB, PKCS#7, under a
    key built into the app. A GET carries its parameters as one encrypted
    `ciphertext` query parameter; a POST body is `{"ciphertext": ...}`; a reply's
    `data` is the encrypted JSON. A fixed set of plain "public" parameters rides
    along on every request (`app_id=fit_profile` and so on).
  * **Sign-in RSA-encrypts the password** with a public key also built into the
    app, and returns a bearer token good for 180 days with no refresh token.
    So the stored blob keeps the password and the session signs in again by
    itself when the token is refused or close to expiry.
  * **Listing is an incremental sync**: "everything updated since T", plus the
    ids deleted since T. Weigh-ins are edited and deleted in the app, so the
    deletions are applied, not ignored.

The AES key and RSA key are the app's, not secrets of ours: they are the same
in every copy of the app, and they protect nothing an account holder could not
see in their own phone.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .fieldmap import describe_payload, parse_utc

log = logging.getLogger("diet.fitprofile")

API_BASE = "https://fit-profile.qnclouds.com/"
SIGN_IN_PATH = "api/v4/users/sign_in"
LIST_PATH = "api/v4/measurements/list_measurement"

# EncryptKeyPrefix + EncryptKeySuffix in the app; only the first 16 bytes are
# used, which is all there are.
AES_KEY = b"7ae4274d0c2385ef"
# From the app's H5 bundle (index-*.js), where the login page encrypts with it.
RSA_PUBLIC_KEY = b"""-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQC+25I2upukpfQ7rIaaTZtVE744
u2zV+HaagrUhDOTq8fMVf9yFQvEZh2/HKxFudUxP0dXUa8F6X4XmWumHdQnum3zm
Jr04fz2b2WCcN0ta/rbF2nYAnMVAk2OJVZAMudOiMWhcxV1nNJiKgTNNr13de0EQ
IiOL2CUBzu+HmIfUbQIDAQAB
-----END PUBLIC KEY-----"""

# What the app's PublicParamsInterceptor adds to every request. The server has
# not been seen to care about the device fields, but app_id is how it knows
# which brand's users to look in.
PUBLIC_PARAMS = {
    "locale": "en", "platform": "android", "app_revision": "1.38.0",
    "app_id": "fit_profile", "system_type": "14_34",
    "cellphone_type": "diet-log", "html_version": "11.38.0",
    "ver_channal": "official",
}

# The app's own response code for "this token is no good" (JWTInterceptor
# logs the user out on it). 200 is success; anything else is an error message.
CODE_OK = 200
CODE_LOGGED_OUT = 403

# Sign in again this long before the token's stated expiry.
RENEW_MARGIN_S = 7 * 86400
MAX_PAGES = 50


class FitProfileError(RuntimeError):
    """Anything the caller can act on: a refused request, an unreadable reply."""


class Refused(FitProfileError):
    """The service answered, with an error code rather than data."""


class NeedsLogin(FitProfileError):
    """The stored password no longer signs in. Only a human fixes this."""


class RateLimited(FitProfileError):
    """The service asked us to stop. Distinct so the poller can back off."""


# --- the wire format ----------------------------------------------------------

def _cipher():
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    return Cipher(algorithms.AES(AES_KEY), modes.ECB())


def encrypt(text: str) -> str:
    """The app's EncryptUtil.encrypt: URL-safe base64, no padding, no wrap."""
    from cryptography.hazmat.primitives import padding
    padder = padding.PKCS7(128).padder()
    data = padder.update(text.strip().encode()) + padder.finalize()
    enc = _cipher().encryptor()
    out = enc.update(data) + enc.finalize()
    return base64.urlsafe_b64encode(out).decode().rstrip("=")


def decrypt(text: str) -> str:
    """The inverse, tolerant of either base64 alphabet and missing padding."""
    from cryptography.hazmat.primitives import padding
    s = text.strip().strip('"').replace("-", "+").replace("_", "/")
    s += "=" * (-len(s) % 4)
    dec = _cipher().decryptor()
    raw = dec.update(base64.b64decode(s)) + dec.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return (unpadder.update(raw) + unpadder.finalize()).decode().strip()


def encrypt_password(password: str) -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import padding as apad
    key = serialization.load_pem_public_key(RSA_PUBLIC_KEY)
    return base64.b64encode(key.encrypt(password.encode(), apad.PKCS1v15())).decode()


def unwrap(reply: Any) -> dict:
    """A reply with its `data` decrypted in place. Raises on an error code."""
    if not isinstance(reply, dict):
        raise FitProfileError(f"reply was {type(reply).__name__}, not an object")
    data = reply.get("data")
    if isinstance(data, str) and data:
        try:
            plain = decrypt(data)
        except Exception as exc:
            raise FitProfileError(f"could not decrypt reply data: {exc}") from exc
        try:
            reply = {**reply, "data": json.loads(plain) if plain[:1] in "[{"
                     else plain}
        except ValueError as exc:
            raise FitProfileError(f"decrypted data is not JSON: {exc}") from exc
    try:
        code = int(reply.get("code", CODE_OK))
    except (TypeError, ValueError):
        code = -1
    if code == CODE_LOGGED_OUT:
        raise NeedsLogin(f"token refused: {reply.get('msg')}")
    if code != CODE_OK:
        raise Refused(f"code {reply.get('code')}: {reply.get('msg')}")
    return reply


def _request(method: str, path: str, params: dict, token: str = "") -> dict:
    import httpx2 as httpx
    url = API_BASE + path.lstrip("/")
    headers = {"Authorization": f"Bearer {token}"}
    query = dict(PUBLIC_PARAMS)
    body = None
    if method == "GET":
        query = {"ciphertext": encrypt(json.dumps(params)) if params else "",
                 **query}
    else:
        body = json.dumps({"ciphertext": encrypt(json.dumps(params))})
        headers["Content-Type"] = "application/json;charset=UTF-8"
    try:
        reply = httpx.request(method, url, params=query, content=body,
                              headers=headers, timeout=60)
    except Exception as exc:
        raise FitProfileError(f"could not reach Fit Profile: {exc}") from exc
    if reply.status_code == 429:
        raise RateLimited(f"Fit Profile rate-limited this client: {reply.text[:120]}")
    if reply.status_code in (401, 403):
        raise NeedsLogin(f"Fit Profile returned {reply.status_code}")
    if reply.status_code >= 400:
        raise FitProfileError(f"Fit Profile returned {reply.status_code} for "
                              f"{path}: {reply.text[:200]}")
    try:
        return unwrap(reply.json())
    except ValueError as exc:
        raise FitProfileError(f"{path} did not return JSON: {reply.text[:120]}") from exc


# --- the session ----------------------------------------------------------------

def sign_in(email: str, password: str) -> dict:
    """
    One sign-in's worth of credentials, as the blob to store.

    A wrong password is NeedsLogin, not a transient error: retrying it every
    quarter hour would only look like an attack on the account.
    """
    try:
        reply = _request("POST", SIGN_IN_PATH,
                         {"email": email, "password": encrypt_password(password)})
    except Refused as exc:
        raise NeedsLogin(f"Fit Profile refused the sign-in: {exc}") from exc
    data = reply.get("data") or {}
    token_info = data.get("token_info") or {}
    user_info = data.get("user_info") or {}
    token, user_id = token_info.get("token"), user_info.get("user_id")
    if not token or not user_id:
        raise FitProfileError("sign-in reply carried no token or user id; keys: "
                              f"{sorted(data) if isinstance(data, dict) else data}")
    try:
        lifetime = float(token_info.get("remaining_time") or 0)
    except (TypeError, ValueError):
        lifetime = 0.0
    return {"email": email, "password": password, "token": token,
            "user_id": str(user_id),
            "expires_at": time.time() + (lifetime or 30 * 86400)}


@dataclass
class Session:
    """
    A signed-in Fit Profile account. Signs in again on its own when the token
    is refused or near expiry, and says so, so the caller can persist the blob.
    """
    blob: dict
    refreshed: bool = False
    calls: list = field(default_factory=list)

    def _renew(self) -> None:
        email, password = self.blob.get("email"), self.blob.get("password")
        if not email or not password:
            raise NeedsLogin("no stored password to sign in again with")
        self.blob = sign_in(email, password)
        self.refreshed = True

    def _token(self) -> str:
        expires_at = self.blob.get("expires_at") or 0
        if not self.blob.get("token") or time.time() > expires_at - RENEW_MARGIN_S:
            self._renew()
        return self.blob["token"]

    @property
    def user_id(self) -> str:
        if not self.blob.get("user_id"):
            self._renew()
        return self.blob["user_id"]

    def get(self, path: str, params: dict) -> dict:
        self.calls.append((path, params))
        token = self._token()
        try:
            return _request("GET", path, params, token)
        except NeedsLogin:
            if self.refreshed:
                raise           # a token minted moments ago: not an expiry
            # Once: the token may have been revoked early (a sign-in on another
            # phone, a password change). A second refusal is the real thing.
            self._renew()
            return _request("GET", path, params, self.blob["token"])


def resume(blob_json: str) -> Session:
    try:
        return Session(json.loads(blob_json))
    except (TypeError, ValueError) as exc:
        raise NeedsLogin(f"stored Fit Profile credentials are unreadable: {exc}") from exc


def session_blob(session: Session) -> str:
    return json.dumps(session.blob)


# --- weigh-ins --------------------------------------------------------------------
#
# Field names from the list_measurement reply, checked against the app's own CSV
# export (whose column headings say what each one means).

# column -> field in the reply. CSV headings in brackets where the name alone
# would not say it.
WEIGH_IN_FIELDS = {
    "body_fat_pct": "bodyfat",
    "bmi": "bmi",
    "skeletal_muscle_pct": "muscle",          # [Skeletal Muscle(%)]
    "muscle_mass_kg": "sinew",                # [Muscle Mass(kg)]
    "fat_free_mass_kg": "fat_free_weight",
    "subcutaneous_fat_pct": "subfat",
    "visceral_fat": "visfat",
    "body_water_pct": "water",
    "protein_pct": "protein",
    "bone_mass_kg": "bone",
    "bmr_kcal": "bmr",
    "metabolic_age": "bodyage",
}

BODY_COLUMNS = tuple(WEIGH_IN_FIELDS)


def _measured(item: dict, column: str, path: str) -> float | None:
    """
    A body-composition figure, or nothing.

    The scale reports 0.0 for every figure it could not take -- a weigh-in in
    socks has a weight and nothing else -- and 0 % body fat is a claim, not an
    absence. So zero is read as "not measured", silently, since it is the
    ordinary case; only a value that is not a number at all is worth a warning.
    """
    raw = item.get(path)
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        log.warning("fitprofile: %s at %s was %r, unusable", column, path, raw)
        return None
    return value if value > 0 else None


def extract_weigh_in(item: Any, user_id: str | None = None) -> dict | None:
    """
    One stored row, or None when the item is not a usable weigh-in.

    `user_id` drops weigh-ins belonging to someone else on the account -- the
    app supports family members under one login, and their weight is not ours.
    """
    if not isinstance(item, dict):
        return None
    ext = item.get("measurement_id")
    measured_at = parse_utc(item.get("time_stamp"))
    try:
        weight = float(item.get("weight"))
    except (TypeError, ValueError):
        weight = 0.0
    if not ext or measured_at is None or weight <= 0:
        log.warning("fitprofile: skipped an item with id=%r time=%r weight=%r",
                    ext, item.get("time_stamp"), item.get("weight"))
        return None
    if user_id is not None and str(item.get("user_id")) != str(user_id):
        return None
    body = {col: _measured(item, col, path) for col, path in WEIGH_IN_FIELDS.items()}
    return {"external_id": str(ext), "measured_at": measured_at,
            "weight_kg": round(weight, 2), **body, "raw": item}


def fetch_weigh_ins(session: Session, since: datetime,
                    raw_dir: Path | None = None) -> tuple[list[dict], list[str]]:
    """
    Every weigh-in added or changed since `since`, and the ids deleted since.

    The cursor is the server's: each page says where it got to and whether it
    is finished, and the next request hands that back.
    """
    updated_at = int(since.timestamp())
    last_id = "0"
    items: list = []
    deleted: list[str] = []
    for _ in range(MAX_PAGES):
        reply = session.get(LIST_PATH, {"last_updated_at": updated_at,
                                        "last_measurement_id": last_id,
                                        "user_id": session.user_id})
        data = reply.get("data") or {}
        if not isinstance(data, dict):
            raise FitProfileError(f"list returned {type(data).__name__}, not an object")
        page = data.get("measurements") or []
        if not isinstance(page, list):
            raise FitProfileError("measurements was not a list")
        items.extend(page)
        deleted.extend(str(i) for i in data.get("delete_measurement_ids") or [])
        if data.get("finish_flag", 1) in (1, "1", True) or not page:
            break
        updated_at = data.get("last_updated_at", updated_at)
        last_id = str(data.get("last_measurement_id") or last_id)
    else:
        log.warning("fitprofile: stopped after %d pages without finish_flag", MAX_PAGES)

    if raw_dir is not None:
        try:
            raw_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            (raw_dir / f"fitprofile-{stamp}.json").write_text(json.dumps(
                {"since": since.isoformat(), "measurements": items,
                 "delete_measurement_ids": deleted}, indent=2, default=str))
        except OSError as exc:
            log.warning("fitprofile: could not write raw weigh-ins: %s", exc)
    return items, deleted


def probe(session: Session, since: datetime) -> list[str]:
    """Human-readable account of what the account returns, for manage.py."""
    items, deleted = fetch_weigh_ins(session, since)
    lines = [f"{len(items)} weigh-in(s) since {since:%Y-%m-%d}, "
             f"{len(deleted)} deletion(s)"]
    for item in sorted((i for i in items if isinstance(i, dict)),
                       key=lambda i: i.get("time_stamp") or 0):
        row = extract_weigh_in(item)
        if row is None:
            lines.append(f"  unusable: {describe_payload(item)[:6]}")
            continue
        fat = row["body_fat_pct"]
        lines.append(f"  {row['measured_at']:%Y-%m-%d %H:%M}Z  "
                     f"{row['weight_kg']:6.2f} kg  "
                     + (f"fat {fat:4.1f} %  muscle {row['skeletal_muscle_pct']} %  "
                        f"water {row['body_water_pct']} %"
                        if fat is not None else "weight only")
                     + f"  [{item.get('scale_name')}]")
    if items:
        missing = [c for c in BODY_COLUMNS
                   if all((extract_weigh_in(i) or {}).get(c) is None for i in items)]
        if missing:
            lines.append(f"never present: {', '.join(missing)}")
    return lines
