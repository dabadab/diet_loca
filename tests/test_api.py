"""
The HTTP surface, against a real server.

Headers, status codes and cookie flags are properties of the running server
rather than of the ASGI app object, so these go over the wire.
"""

import datetime as dt
import json
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from zoneinfo import ZoneInfo

import pytest

from app import writes

TZ = "Europe/Budapest"
PASSWORD = "a-long-test-password"


@pytest.fixture
def client(server):
    """A tiny cookie-aware HTTP client. Returns (status, headers, body)."""
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(CookieJar()))

    def decode(headers, raw):
        """JSON when the server says so, bytes otherwise -- the favicon is not
        text and must not be guessed at."""
        if not raw:
            return None
        if "json" in headers.get("content-type", ""):
            return json.loads(raw)
        return raw

    def _call(path, data=None, method=None):
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(
            server + path, data=body, method=method or ("POST" if body else "GET"),
            headers={"content-type": "application/json"} if body else {})
        try:
            with opener.open(req, timeout=15) as r:
                return r.status, dict(r.headers), decode(dict(r.headers), r.read())
        except urllib.error.HTTPError as e:
            headers = dict(e.headers)
            return e.code, headers, decode(headers, e.read())
    return _call


@pytest.fixture
def signed_in(client, make_user):
    uid = make_user("a@example.com", timezone=TZ, password=PASSWORD)
    status, _, _ = client("/api/login",
                          {"email": "a@example.com", "password": PASSWORD})
    assert status == 200
    return uid


@pytest.fixture
def today():
    return dt.datetime.now(ZoneInfo(TZ)).date()


@pytest.fixture
def log_meal(pool):
    """A logged meal, so the window is not the all-empty case where /api/days
    deliberately returns nothing at all."""
    def _log(uid, day, kcal=2000):
        with pool.user_tx(uid) as cur:
            writes.log_meal(cur, description="lunch",
                            eaten_at=dt.datetime.combine(day, dt.time(13, 0),
                                                         tzinfo=ZoneInfo(TZ)),
                            items=[{"food": "food", "grams": 500, "kcal": kcal,
                                    "protein_g": 100, "carb_g": 250, "fat_g": 67,
                                    "confidence": 0.8}])
    return _log


class TestAuth:
    def test_the_api_is_closed_until_you_sign_in(self, client):
        status, _, _ = client("/api/days")
        assert status == 401

    def test_a_wrong_password_is_refused(self, client, make_user):
        make_user("a@example.com", password=PASSWORD)
        status, _, _ = client("/api/login",
                              {"email": "a@example.com", "password": "wrong"})
        assert status == 401

    def test_an_unknown_account_answers_the_same_as_a_wrong_password(self, client):
        """Otherwise the response tells an attacker which addresses exist."""
        status, _, body = client("/api/login",
                                 {"email": "nobody@example.com", "password": "x"})
        assert status == 401

    def test_the_session_cookie_is_httponly_and_samesite(self, client, make_user):
        make_user("a@example.com", password=PASSWORD)
        _, headers, _ = client("/api/login",
                               {"email": "a@example.com", "password": PASSWORD})
        cookie = headers.get("set-cookie", "")
        assert "HttpOnly" in cookie, "script-readable session cookie"
        assert "SameSite" in cookie

    def test_signing_out_invalidates_the_session(self, client, signed_in):
        assert client("/api/days")[0] == 200
        client("/api/logout", {})
        assert client("/api/days")[0] == 401


def test_security_headers_are_present(client, server):
    _, headers, _ = client("/api/health")
    csp = headers.get("content-security-policy", "")
    assert "default-src 'self'" in csp
    assert headers.get("x-content-type-options") == "nosniff"


class TestDaysWindow:
    """The summary covers the goal in force, so the window has to reach back to
    the day that goal took effect -- resolved server-side so the page still
    boots on one request."""

    def _set_goal(self, pool, uid, day):
        with pool.user_tx(uid) as cur:
            writes.set_target(cur, effective_from=day, kcal=1800, protein_g=150)

    def test_the_window_stays_as_asked_when_the_goal_is_inside_it(
            self, client, signed_in, pool, today):
        self._set_goal(pool, signed_in, today - dt.timedelta(days=10))
        _, _, body = client("/api/days?days=28")
        assert body["window_days"] == 28

    def test_the_window_stretches_to_reach_an_older_goal(
            self, client, signed_in, pool, today, log_meal):
        self._set_goal(pool, signed_in, today - dt.timedelta(days=90))
        log_meal(signed_in, today - dt.timedelta(days=1))
        _, _, body = client("/api/days?days=28")
        assert body["window_days"] == 91
        assert body["days"][-1]["date"] == (today - dt.timedelta(days=90)).isoformat()

    def test_the_stretch_stops_at_the_ceiling(
            self, client, signed_in, pool, today, log_meal):
        """And the payload carries max_days, so the page can tell it is covering
        less than its heading claims rather than silently under-reporting."""
        self._set_goal(pool, signed_in, today - dt.timedelta(days=900))
        log_meal(signed_in, today - dt.timedelta(days=1))
        _, _, body = client("/api/days?days=28")
        assert body["window_days"] == body["max_days"] == 400
        assert body["days"][-1]["date"] > body["target"]["effective_from"]

    def test_without_a_goal_the_window_is_the_one_requested(
            self, client, signed_in, today):
        _, _, body = client("/api/days?days=28")
        assert body["target"] is None and body["window_days"] == 28

    @pytest.mark.parametrize("asked, expected", [(0, 1), (-5, 1), (9999, 400)])
    def test_the_window_is_clamped(self, client, signed_in, asked, expected):
        _, _, body = client(f"/api/days?days={asked}")
        assert body["window_days"] == expected


def test_a_day_carries_everything_the_page_renders(
        client, signed_in, pool, today, log_meal):
    with pool.user_tx(signed_in) as cur:
        writes.set_target(cur, effective_from=today, kcal=1800, protein_g=150)
    log_meal(signed_in, today - dt.timedelta(days=1))
    _, _, body = client("/api/days?days=3")
    assert set(body) == {"timezone", "days", "target", "window_days", "max_days"}
    assert body["timezone"] == TZ
    expected = {"date", "energy_in_kcal", "protein_g", "carb_g", "fat_g",
                "energy_out_kcal", "energy_out_partial", "active_kcal",
                "total_kcal", "weight_kg", "target_kcal", "target_protein_g",
                "meals_logged", "ignored", "ignored_reason", "intake_source"}
    assert set(body["days"][0]) == expected


def test_a_user_cannot_reach_another_users_day(client, signed_in, make_user, pool, today):
    """No endpoint takes a user id, so there is no argument by which to try --
    this asserts the absence stays true."""
    other = make_user("b@example.com", timezone=TZ)
    with pool.user_tx(other) as cur:
        writes.log_meal(cur, description="theirs",
                        eaten_at=dt.datetime.now(ZoneInfo(TZ)),
                        items=[{"food": "x", "grams": 100, "kcal": 500,
                                "protein_g": 25, "carb_g": 50, "fat_g": 17,
                                "confidence": 0.9}])
    _, _, body = client(f"/api/day/{today.isoformat()}")
    assert body["meals"] == []


def test_the_favicon_is_served_from_the_root(client):
    """Browsers ask for it there whatever the page links to."""
    status, headers, _ = client("/favicon.ico")
    assert status == 200
    assert "image" in headers.get("content-type", "")
