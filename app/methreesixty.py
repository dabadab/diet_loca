"""
MeThreeSixty ingest: body measurements from Size Stream's 3D body scans.

MeThreeSixty is Size Stream's phone app. Each scan becomes a set of body
measurements (girths, body fat, lean mass, weight). These are computed in Size
Stream's cloud, and the app reads them from there, so this module reads the same
place. Everything below comes from the Android app (v6.1.0): its
`amplifyconfiguration.json` and the decompiled `com.sizestream.data.*` classes.
There is no published API.

How the app reaches its data, and so how this module does:

  * **Sign-in is a Cognito user pool**, here through its hosted login page with
    Google as the identity provider (the pool also does an emailed one-time
    code and Apple). The app's redirect is `methreesixty://auth`. Cognito
    refuses any other redirect for this client (`http://localhost` gives
    `redirect_mismatch`), so `manage.py methreesixty-login` hands over the
    authorize URL and takes the redirect back by hand, as bosch-login does.
    After that the refresh token carries it. It is not rotated.
  * **The API is IAM-authorised**, not bearer-token. The pool's ID token is
    swapped at the Cognito *identity* pool for short-lived AWS keys, and each
    request is SigV4-signed with them for `execute-api` in us-east-1.
  * The API itself: `GET /users` is the caller's profile (its `userId` keys
    everything else), `GET /users/{userId}/scans` lists scans (`count`,
    `startAfter`, header `paginate: true`), and
    `GET /scans/{scanId}/data/moving-average/1` returns a scan's measures.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import logging
import secrets
import time
import urllib.parse
from typing import Any

from .fieldmap import describe_payload

log = logging.getLogger("diet.methreesixty")

# --- constants from the app's amplifyconfiguration.json --------------------
REGION = "us-east-1"
USER_POOL_ID = "us-east-1_4uWNmVlOX"
CLIENT_ID = "48cjfdrg3iblggonatqrqcba7r"
IDENTITY_POOL_ID = "us-east-1:da70eb1a-db8a-42b4-b106-296bcab98f1e"
HOSTED_UI = "https://prod-otp-three-d-id.auth.us-east-1.amazoncognito.com"
REDIRECT_URI = "methreesixty://auth"
SCOPES = "openid email aws.cognito.signin.user.admin"
API_BASE = "https://three-d-id.sizestream.com"
IDENTITY_ENDPOINT = f"https://cognito-identity.{REGION}.amazonaws.com/"
LOGIN_KEY = f"cognito-idp.{REGION}.amazonaws.com/{USER_POOL_ID}"
# The app passes this as `brand` on the measures call, with a fixed function
# name. Both are constants in RemoteScanningDataSourceImpl.
BRAND = "methreesixty"
MEASURES_FUNCTION = "methreesixty-custom-measures"

# Refresh this long before a token or key expires, so a slow request cannot
# start valid and finish rejected.
EXPIRY_MARGIN_S = 120
PAGE_SIZE = 100
MAX_PAGES = 20


class MeThreeSixtyError(RuntimeError):
    """Anything the caller can act on: a dead session, a refused request."""


class NeedsLogin(MeThreeSixtyError):
    """The stored tokens can no longer be refreshed. Only a human fixes this."""


class RateLimited(MeThreeSixtyError):
    """Size Stream asked us to stop. Distinct so the poller can back off."""


# --- the interactive half --------------------------------------------------

def start_login(provider: str = "Google") -> tuple[str, str]:
    """
    Returns (authorize URL, PKCE verifier).

    `identity_provider` skips Cognito's own page and goes straight to Google,
    which is how the user signs in to the app.
    """
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    query = urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPES,
        "identity_provider": provider,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": secrets.token_urlsafe(16),
    })
    return f"{HOSTED_UI}/oauth2/authorize?{query}", verifier


def code_from_redirect(redirect: str) -> str:
    """
    Pull the authorization code out of the `methreesixty://auth?code=...` URL
    the browser could not follow. A bare code is accepted too.
    """
    text = (redirect or "").strip().strip('"').strip("'")
    if text.lower().startswith("location:"):
        text = text.split(":", 1)[1].strip()
    if not text:
        raise MeThreeSixtyError("nothing pasted")
    query = urllib.parse.urlparse(text).query or text.partition("?")[2]
    params = urllib.parse.parse_qs(query)
    if "error" in params:
        detail = params.get("error_description", [""])[0]
        raise MeThreeSixtyError(
            f"sign-in was refused: {params['error'][0]} {detail}".strip())
    code = params.get("code", [None])[0]
    if code:
        return code
    if "?" not in text and "/" not in text and len(text) >= 10:
        return text
    raise MeThreeSixtyError(
        "no code in that. Expected the whole methreesixty://auth?code=... URL "
        "(the 'location:' header of the last amazoncognito.com 302 in devtools' "
        "Network tab), or just the code from it")


# --- user-pool tokens ------------------------------------------------------

def _post_token(form: dict) -> dict:
    import httpx2 as httpx
    try:
        reply = httpx.post(f"{HOSTED_UI}/oauth2/token",
                           data={"client_id": CLIENT_ID, **form}, timeout=30)
    except Exception as exc:
        raise MeThreeSixtyError(f"could not reach Cognito: {exc}") from exc
    if reply.status_code >= 400:
        detail = reply.text[:200]
        if reply.status_code in (400, 401):
            raise NeedsLogin(f"Cognito refused the grant: {detail}")
        raise MeThreeSixtyError(f"Cognito returned {reply.status_code}: {detail}")
    tokens = reply.json()
    tokens["expires_at"] = time.time() + int(tokens.get("expires_in") or 3600)
    return tokens


def exchange_code(code: str, verifier: str) -> dict:
    """One interactive login's worth of tokens."""
    return _post_token({"grant_type": "authorization_code", "code": code,
                        "redirect_uri": REDIRECT_URI, "code_verifier": verifier})


def refresh(blob: dict) -> dict:
    """
    Fresh ID and access tokens. Cognito does not rotate the refresh token
    unless the pool is set to, so the old one is kept when none comes back.
    """
    rt = (blob or {}).get("refresh_token")
    if not rt:
        raise NeedsLogin("no refresh token stored")
    fresh = _post_token({"grant_type": "refresh_token", "refresh_token": rt})
    return {**blob, **fresh}


# --- AWS keys from the identity pool ---------------------------------------

def _identity_call(target: str, body: dict) -> dict:
    """Cognito Identity's JSON-RPC. Both calls used here are unsigned."""
    import httpx2 as httpx
    headers = {"Content-Type": "application/x-amz-json-1.1",
               "X-Amz-Target": f"AWSCognitoIdentityService.{target}"}
    try:
        reply = httpx.post(IDENTITY_ENDPOINT, content=json.dumps(body),
                           headers=headers, timeout=30)
    except Exception as exc:
        raise MeThreeSixtyError(f"could not reach Cognito Identity: {exc}") from exc
    if reply.status_code >= 400:
        detail = reply.text[:200]
        if "NotAuthorized" in detail:
            raise NeedsLogin(f"Cognito Identity refused the ID token: {detail}")
        raise MeThreeSixtyError(f"Cognito Identity {target} returned "
                                f"{reply.status_code}: {detail}")
    return reply.json()


def aws_credentials(id_token: str, identity_id: str | None) -> tuple[str, dict]:
    """(identity id, {access_key, secret_key, session_token, expires_at})."""
    logins = {LOGIN_KEY: id_token}
    if not identity_id:
        identity_id = _identity_call(
            "GetId", {"IdentityPoolId": IDENTITY_POOL_ID, "Logins": logins})["IdentityId"]
    reply = _identity_call("GetCredentialsForIdentity",
                           {"IdentityId": identity_id, "Logins": logins})
    c = reply["Credentials"]
    return identity_id, {"access_key": c["AccessKeyId"], "secret_key": c["SecretKey"],
                         "session_token": c["SessionToken"],
                         "expires_at": float(c["Expiration"])}


# --- SigV4 -----------------------------------------------------------------

def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def _quote(value: str) -> str:
    return urllib.parse.quote(value, safe="-_.~")


def sign(method: str, url: str, headers: dict[str, str], body: bytes,
         access_key: str, secret_key: str, session_token: str | None = None,
         service: str = "execute-api", region: str = REGION,
         now: dt.datetime | None = None) -> dict[str, str]:
    """
    Headers to send for an AWS Signature Version 4 request. Plain SigV4, so it
    is checked against AWS's own test vectors in the tests rather than trusted.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = amz_date[:8]
    parts = urllib.parse.urlsplit(url)
    out = {k: v for k, v in headers.items()}
    out["host"] = parts.netloc
    out["x-amz-date"] = amz_date
    if session_token:
        out["x-amz-security-token"] = session_token

    canon_headers = {k.lower().strip(): " ".join(str(v).split()) for k, v in out.items()}
    signed = ";".join(sorted(canon_headers))
    query = sorted(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
    canonical = "\n".join([
        method.upper(),
        _quote_path(parts.path or "/"),
        "&".join(f"{_quote(k)}={_quote(v)}" for k, v in query),
        "".join(f"{k}:{canon_headers[k]}\n" for k in sorted(canon_headers)),
        signed,
        hashlib.sha256(body).hexdigest(),
    ])
    scope = f"{datestamp}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope,
                         hashlib.sha256(canonical.encode()).hexdigest()])
    key = _hmac(_hmac(_hmac(_hmac(f"AWS4{secret_key}".encode(), datestamp),
                            region), service), "aws4_request")
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    out["Authorization"] = (f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
                            f"SignedHeaders={signed}, Signature={signature}")
    return out


def _quote_path(path: str) -> str:
    # Every service but S3 signs the path with each segment encoded once more
    # on top of how it travels. The paths used here are plain, so this changes
    # nothing for them, but an id with a reserved character still signs right.
    return "/".join(_quote(seg) for seg in path.split("/"))


# --- the session -----------------------------------------------------------

class Session:
    """
    A signed-in user. Refreshes tokens and AWS keys on use, and reports whether
    it had to, so the caller can store the new blob.
    """

    def __init__(self, blob: dict):
        self.blob = dict(blob or {})
        self.refreshed = False

    def _keys(self) -> dict:
        now = time.time()
        if now > float(self.blob.get("expires_at") or 0) - EXPIRY_MARGIN_S:
            self.blob = refresh(self.blob)
            self.blob.pop("aws", None)
            self.refreshed = True
        aws = self.blob.get("aws") or {}
        if now > float(aws.get("expires_at") or 0) - EXPIRY_MARGIN_S:
            token = self.blob.get("id_token")
            if not token:
                raise NeedsLogin("no ID token stored")
            identity_id, aws = aws_credentials(token, self.blob.get("identity_id"))
            self.blob["identity_id"] = identity_id
            self.blob["aws"] = aws
            self.refreshed = True
        return aws

    def get(self, path: str, params: dict | None = None,
            headers: dict | None = None) -> Any:
        import httpx2 as httpx
        aws = self._keys()
        url = API_BASE + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        signed = sign("GET", url, {"accept": "application/json", **(headers or {})},
                      b"", aws["access_key"], aws["secret_key"], aws["session_token"])
        try:
            reply = httpx.get(url, headers=signed, timeout=60)
        except Exception as exc:
            raise MeThreeSixtyError(f"could not reach Size Stream: {exc}") from exc
        if reply.status_code in (401, 403):
            raise NeedsLogin(f"Size Stream refused the request ({reply.status_code}): "
                             f"{reply.text[:200]}")
        if reply.status_code == 429:
            raise RateLimited(f"Size Stream rate-limited this client: {reply.text[:120]}")
        if reply.status_code >= 400:
            raise MeThreeSixtyError(f"Size Stream returned {reply.status_code} for "
                                    f"{path}: {reply.text[:200]}")
        return reply.json() if reply.content else None


def resume(blob_json: str) -> Session:
    try:
        return Session(json.loads(blob_json))
    except (TypeError, ValueError) as exc:
        raise NeedsLogin(f"stored MeThreeSixty tokens are unreadable: {exc}") from exc


def session_blob(session: Session) -> str:
    return json.dumps(session.blob)


# --- the API ---------------------------------------------------------------

def fetch_profile(session: Session) -> dict:
    """The caller's own profile; its `userId` keys the scan list."""
    reply = session.get("/users")
    if isinstance(reply, list):
        reply = reply[0] if reply else {}
    return reply if isinstance(reply, dict) else {}


def user_id(session: Session) -> str:
    """Size Stream's id for the signed-in user, cached in the session blob."""
    uid = session.blob.get("user_id")
    if not uid:
        profile = fetch_profile(session)
        uid = profile.get("userId") or profile.get("id")
        if not uid:
            raise MeThreeSixtyError(f"no userId in /users: keys {sorted(profile)}")
        session.blob["user_id"] = uid
        session.refreshed = True
    return uid


def fetch_scans(session: Session) -> list[dict]:
    """Every scan on the account, newest pages first as the API gives them."""
    uid = user_id(session)
    scans: list[dict] = []
    after = None
    for _ in range(MAX_PAGES):
        params = {"count": str(PAGE_SIZE)}
        if after:
            params["startAfter"] = after
        reply = session.get(f"/users/{uid}/scans", params,
                            headers={"paginate": "true"})
        if isinstance(reply, list):
            scans.extend(s for s in reply if isinstance(s, dict))
            break
        page = (reply or {}).get("scans") or (reply or {}).get("items") or []
        scans.extend(s for s in page if isinstance(s, dict))
        after = (reply or {}).get("continueFrom")
        if not after or not page:
            break
    return scans


def fetch_measures(session: Session, scan_id: str) -> dict:
    """One scan's measures payload, as the app requests it."""
    return session.get(f"/scans/{urllib.parse.quote(scan_id, safe='')}/data/moving-average/1",
                       {"brand": BRAND, "function": MEASURES_FUNCTION, "version": "1"},
                       headers={"timeout": "10"}) or {}


def probe(session: Session, show_values: bool = False) -> list[str]:
    """
    What the live API returns, for writing the parser against: the profile's
    keys, the scan list's shape, and the latest successful scan's measures.
    """
    lines: list[str] = []
    profile = fetch_profile(session)
    lines.append("profile keys: " + ", ".join(sorted(profile)))
    lines.append(f"userId: {user_id(session)}")
    scans = fetch_scans(session)
    lines.append(f"{len(scans)} scan(s)")
    for s in scans[:20]:
        lines.append(f"  {s.get('id')}  date={s.get('date')}  status={s.get('status')}"
                     f"  type={s.get('type')}  app={s.get('app')}")
    if scans:
        lines.append("scan keys: " + ", ".join(sorted(scans[0])))
    done = [s for s in scans if str(s.get("status", "")).lower()
            in ("success", "succeeded", "complete", "completed", "done")]
    target = (done or scans or [None])[0]
    if target is None:
        return lines
    payload = fetch_measures(session, str(target.get("id")))
    lines.append(f"measures for {target.get('id')}: top-level keys "
                 + ", ".join(sorted(payload)))
    output = payload.get("output") if isinstance(payload.get("output"), dict) else {}
    for name in ("measures", "moving_average"):
        block = output.get(name)
        if isinstance(block, dict):
            if show_values:
                lines.append(f"  output.{name}:")
                lines += [f"    {k}={v}" for k, v in sorted(block.items())]
            else:
                lines.append(f"  output.{name} keys: " + ", ".join(sorted(block)))
    if show_values:
        lines += ["  " + k for k in describe_payload(payload, depth=1)]
    return lines
