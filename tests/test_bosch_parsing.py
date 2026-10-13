"""
Parsing Bosch Flow's ride payloads. No database, no network.

Bosch publishes no schema at all, so these tests pin down the behaviour that
has to hold whatever the field names turn out to be: a value that cannot be
found yields nothing rather than a guess, a ride that cannot be identified is
dropped rather than stored half-formed, and no location ever reaches storage.

The field *names* in app/bosch.py are hypotheses until `manage.py bosch-probe`
has been run against a live account. These tests therefore exercise each
candidate spelling, so that when the real one is known the others can be removed
and the suite still says whether anything broke.
"""

import datetime as dt
from datetime import timezone

import pytest

from app import bosch
from app.fieldmap import FieldSpec, dig, parse_utc, strip_location

RIDE = {
    "activityId": "ride-1", "name": "Morning commute",
    "startTime": "2026-10-12T07:30:00Z",
    "duration": 2400, "movingDuration": 2280, "distance": 18500,
    "calories": 310, "averageRiderPower": 112, "averageCadence": 68,
    "elevationGain": 140, "averageSpeed": 7.7,
    "riderEnergy": 1300, "motorEnergy": 2600,
    "startLatitude": 47.4979, "startLongitude": 19.0402,
    "trackPolyline": "abc", "coordinates": [[1, 2]],
}


class TestIdentity:
    def test_the_fields_that_become_columns(self):
        row = bosch.extract_activity(RIDE)
        assert row["external_id"] == "ride-1"
        assert row["started_at"] == dt.datetime(2026, 10, 12, 7, 30, tzinfo=timezone.utc)
        assert row["activity_type"] == "ebike_ride"
        assert row["name"] == "Morning commute"
        assert (row["duration_s"], row["moving_s"]) == (2400.0, 2280.0)
        assert (row["distance_m"], row["kcal"]) == (18500.0, 310.0)
        assert row["avg_rider_power_w"] == 112.0
        assert row["avg_cadence_rpm"] == 68.0

    @pytest.mark.parametrize("missing, item", [
        ("an id", {"startTime": "2026-10-12T07:30:00Z", "duration": 600}),
        ("a start time", {"activityId": "x", "duration": 600}),
        ("an unparseable start", {"activityId": "x", "startTime": "soon"}),
        ("anything at all", "not even a dict"),
    ])
    def test_an_unidentifiable_ride_is_skipped(self, missing, item):
        """An id and a start are the row's identity, and the start is also the
        only thing a Garmin ride can be matched against."""
        assert bosch.extract_activity(item) is None, f"should skip: no {missing}"

    def test_the_type_is_named_after_the_system_not_the_sport(self):
        """A Bosch row and a Garmin cycling row may both describe one outing;
        calling both of them 'cycling' would hide that at a glance."""
        assert bosch.extract_activity(RIDE)["activity_type"] == "ebike_ride"


class TestMissingValues:
    def test_a_field_that_matches_nothing_is_null_not_zero(self):
        """The difference matters most for power: a bike with no torque reading
        is not a bike the rider pedalled with no effort."""
        sparse = {"activityId": "x", "startTime": "2026-10-12T07:30:00Z"}
        row = bosch.extract_activity(sparse)
        for column in ("duration_s", "distance_m", "kcal", "avg_rider_power_w",
                       "avg_cadence_rpm", "motor_energy_share"):
            assert row[column] is None, f"{column} should be NULL, not invented"

    def test_an_unreadable_value_costs_only_itself(self):
        junk = dict(RIDE, averageCadence="quite fast")
        row = bosch.extract_activity(junk)
        assert row["avg_cadence_rpm"] is None
        assert row["avg_rider_power_w"] == 112.0, "the rest of the ride stands"

    def test_the_bike_reports_no_heart_rate(self):
        """It has no such sensor, so these must stay NULL rather than borrow a
        number from somewhere plausible-looking."""
        row = bosch.extract_activity(dict(RIDE, averageHR=142, maxHR=170))
        assert row["avg_hr"] is None and row["max_hr"] is None
        assert row["hr_zones"] is None


class TestMotorShare:
    def test_derived_from_the_two_energy_figures(self):
        assert bosch.extract_activity(RIDE)["motor_energy_share"] == \
            pytest.approx(2600 / 3900)

    def test_a_direct_percentage_is_normalised(self):
        row = bosch.extract_activity(dict(RIDE, motorEnergyShare=67))
        assert row["motor_energy_share"] == pytest.approx(0.67)

    def test_a_direct_fraction_is_left_alone(self):
        row = bosch.extract_activity(dict(RIDE, motorShare=0.67))
        assert row["motor_energy_share"] == pytest.approx(0.67)

    def test_unknown_is_none_not_zero(self):
        """"The motor did nothing" is a claim about an ebike ride, and quite a
        strong one. Not knowing is not the same as knowing it was zero."""
        bare = {k: v for k, v in RIDE.items()
                if k not in ("riderEnergy", "motorEnergy")}
        assert bosch.extract_activity(bare)["motor_energy_share"] is None


def test_no_location_reaches_the_stored_payload():
    """`raw` is readable through query_sql by the model. A ride's route is a
    location trace and has no bearing on diet."""
    raw = bosch.extract_activity(RIDE)["raw"]
    assert raw, "the rest of the ride should still be kept"
    assert raw["calories"] == 310
    for key in raw:
        assert not any(h in key for h in ("atitude", "ongitude", "olyline",
                                          "coordinate", "geo")), key


class TestLogin:
    def test_the_authorize_url_is_pkce_and_names_the_app_client(self):
        url, verifier = bosch.start_login()
        assert url.startswith(bosch.AUTHORIZE_URL + "?")
        assert "code_challenge_method=S256" in url
        assert "client_id=one-bike-app" in url
        assert "offline_access" in url, "without it there is no refresh token"
        assert len(verifier) >= 43, "PKCE verifiers have a minimum length"

    def test_each_login_gets_a_fresh_verifier(self):
        assert bosch.start_login()[1] != bosch.start_login()[1]

    def test_the_code_is_read_out_of_the_pasted_redirect(self):
        assert bosch.code_from_redirect(
            "onebikeapp-ios://callback?code=abc123&state=xy") == "abc123"

    def test_surrounding_whitespace_and_quotes_survive_a_paste(self):
        assert bosch.code_from_redirect(
            '  "onebikeapp-ios://cb?code=abc123"  ') == "abc123"

    def test_a_bare_code_is_accepted(self):
        """Digging the whole URL out of a devtools pane is the fiddly part.
        Someone who can read the code off the screen should not be sent back
        for the rest of it."""
        assert bosch.code_from_redirect("A1b2C3d4E5f6G7h8") == "A1b2C3d4E5f6G7h8"

    def test_a_refusal_carries_its_description(self):
        with pytest.raises(bosch.BoschError, match="user cancelled"):
            bosch.code_from_redirect(
                "onebikeapp-ios://cb?error=access_denied"
                "&error_description=user+cancelled")

    @pytest.mark.parametrize("pasted, because", [
        ("", "nothing pasted"),
        ("https://singlekey-id.com/auth/login", "the wrong URL entirely"),
        ("short", "too short to be a code"),
        ("onebikeapp-ios://cb?error=access_denied", "the sign-in was refused"),
    ])
    def test_a_bad_paste_says_what_was_wrong(self, pasted, because):
        """This is copied out of a devtools pane by hand, so it will be wrong
        the first time and the message is the only guidance there is."""
        with pytest.raises(bosch.BoschError):
            bosch.code_from_redirect(pasted)


class TestSharedParser:
    """app/fieldmap.py was lifted out of garmin.py when Bosch arrived; two
    copies of a defensive parser is two chances for one to stop being
    defensive."""

    @pytest.mark.parametrize("value, expected", [
        ("2026-10-12 07:30:00", dt.datetime(2026, 10, 12, 7, 30, tzinfo=timezone.utc)),
        ("2026-10-12T07:30:00Z", dt.datetime(2026, 10, 12, 7, 30, tzinfo=timezone.utc)),
        # An offset is honoured rather than ignored: 09:30+02:00 is 07:30 UTC,
        # and reading it as local would move a ride to the wrong day.
        ("2026-10-12T09:30:00+02:00", dt.datetime(2026, 10, 12, 7, 30, tzinfo=timezone.utc)),
        (1760254200000, dt.datetime(2025, 10, 12, 7, 30, tzinfo=timezone.utc)),
    ])
    def test_timestamps_land_in_utc(self, value, expected):
        assert parse_utc(value) == expected

    @pytest.mark.parametrize("value", ["", "soon", None, {}, "2026-13-45"])
    def test_an_unreadable_timestamp_is_none(self, value):
        assert parse_utc(value) is None

    def test_dig_tolerates_a_path_that_is_not_there(self):
        assert dig({"a": {"b": 1}}, "a.b") == 1
        assert dig({"a": {"b": 1}}, "a.c.d") is None
        assert dig({"a": 1}, "a.b") is None

    def test_a_spec_tries_each_candidate_in_turn(self):
        spec = FieldSpec("x", ("new.name", "oldName"))
        assert spec.read({"oldName": "7"}) == 7.0
        assert spec.read({"new": {"name": 9}}) == 9.0
        assert spec.read({"somethingElse": 1}) is None

    def test_strip_location_keeps_everything_that_is_not_a_place(self):
        kept = strip_location({"kcal": 1, "startLatitude": 2, "geoJson": 3,
                               "trackPolyline": 4, "endLongitude": 5})
        assert kept == {"kcal": 1}


class TestClientPreflight:
    """SingleKey answers an unknown client id and an invented one identically,
    so the only way to tell a withdrawn client from a typo is to notice the
    error at all -- before sending someone to sign in."""

    def test_a_misconfigured_application_redirect_is_fatal(self, monkeypatch):
        class Reply:
            status_code = 302
            headers = {"location": "https://singlekey-id.com/auth/errors/"
                                   "misconfigured-application?errorId=xyz"}
        import httpx2
        monkeypatch.setattr(httpx2, "get", lambda *a, **k: Reply())
        with pytest.raises(bosch.NeedsLogin, match="does not recognise"):
            bosch.check_client("https://singlekey-id.com/auth/connect/authorize?x=1")

    def test_the_message_names_the_routes_that_do_not_depend_on_it(self, monkeypatch):
        class Reply:
            status_code = 302
            headers = {"location": "/errors/misconfigured-application?errorId=x"}
        import httpx2
        monkeypatch.setattr(httpx2, "get", lambda *a, **k: Reply())
        with pytest.raises(bosch.NeedsLogin) as caught:
            bosch.check_client("https://x/")
        assert "data-act" in str(caught.value) and "Strava" in str(caught.value)

    def test_a_working_client_passes_quietly(self, monkeypatch):
        class Reply:
            status_code = 200
            headers = {}
        import httpx2
        monkeypatch.setattr(httpx2, "get", lambda *a, **k: Reply())
        bosch.check_client("https://x/")

    def test_a_network_blip_is_not_a_verdict(self, monkeypatch):
        """Refusing to start because the check itself could not run would turn
        a flaky connection into a diagnosis."""
        import httpx2
        def boom(*a, **k):
            raise RuntimeError("connection reset")
        monkeypatch.setattr(httpx2, "get", boom)
        bosch.check_client("https://x/")

    def test_the_client_id_can_be_overridden(self, monkeypatch):
        """A client registered at Bosch's Data Act portal is the rider's own,
        so it has to be settable without editing the source."""
        monkeypatch.setenv("BOSCH_CLIENT_ID", "my-own-client")
        monkeypatch.setenv("BOSCH_REDIRECT_URI", "http://localhost:4200")
        import importlib
        reloaded = importlib.reload(bosch)
        try:
            url, _ = reloaded.start_login()
            assert "client_id=my-own-client" in url
            assert "localhost%3A4200" in url
        finally:
            monkeypatch.delenv("BOSCH_CLIENT_ID")
            monkeypatch.delenv("BOSCH_REDIRECT_URI")
            importlib.reload(bosch)


class TestLocalCallback:
    """
    A client registered at Bosch's Data Act portal nominates its own recipient
    URL, so it can be localhost -- which a browser will actually follow, unlike
    the app client's mobile scheme. That is what removes the devtools step, so
    it is worth a test that really binds a socket.
    """

    @staticmethod
    def _with_port(monkeypatch, port):
        import importlib
        monkeypatch.setenv("BOSCH_CLIENT_ID", "test-client")
        monkeypatch.setenv("BOSCH_REDIRECT_URI", f"http://localhost:{port}")
        return importlib.reload(bosch)

    @staticmethod
    def _free_port():
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def _run(self, mod, timeout=15):
        import threading
        out = {}

        def go():
            try:
                out["code"] = mod.listen_for_code(timeout=timeout)
            except Exception as exc:
                out["err"] = exc
        t = threading.Thread(target=go)
        t.start()
        return out, t

    def _visit(self, port, path):
        import urllib.request
        import time
        for _ in range(40):                      # the server may still be binding
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}",
                                            timeout=3) as r:
                    return r.read().decode()
            except Exception:
                time.sleep(0.05)
        raise AssertionError("the callback server never came up")

    def test_the_code_is_caught_off_the_callback(self, monkeypatch):
        port = self._free_port()
        mod = self._with_port(monkeypatch, port)
        try:
            out, t = self._run(mod)
            page = self._visit(port, "/cb?code=THE-CODE&state=x")
            t.join(20)
            assert out.get("code") == "THE-CODE", out
            assert "close this tab" in page, "the browser needs telling it worked"
        finally:
            monkeypatch.undo()
            __import__("importlib").reload(bosch)

    def test_a_favicon_request_does_not_end_the_wait(self, monkeypatch):
        """Browsers ask for one unprompted. Treating any request as the callback
        would abandon the login a moment before the real one arrives."""
        port = self._free_port()
        mod = self._with_port(monkeypatch, port)
        try:
            out, t = self._run(mod)
            self._visit(port, "/favicon.ico")
            page = self._visit(port, "/cb?code=LATE&state=x")
            t.join(20)
            assert out.get("code") == "LATE", out
        finally:
            monkeypatch.undo()
            __import__("importlib").reload(bosch)

    def test_a_refusal_is_reported_as_one_not_as_a_timeout(self, monkeypatch):
        port = self._free_port()
        mod = self._with_port(monkeypatch, port)
        try:
            out, t = self._run(mod)
            self._visit(port, "/cb?error=access_denied&error_description=user+cancelled")
            t.join(20)
            assert isinstance(out.get("err"), bosch.BoschError)
            assert "user cancelled" in str(out["err"])
        finally:
            monkeypatch.undo()
            __import__("importlib").reload(bosch)

    def test_a_port_already_in_use_says_so_plainly(self, monkeypatch):
        import socket
        held = socket.socket()
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        port = held.getsockname()[1]
        mod = self._with_port(monkeypatch, port)
        try:
            with pytest.raises(bosch.BoschError, match="could not listen"):
                mod.listen_for_code(timeout=2)
        finally:
            held.close()
            monkeypatch.undo()
            __import__("importlib").reload(bosch)

    def test_the_route_decides_whether_a_browser_can_follow_the_redirect(
            self, monkeypatch):
        mod = self._with_port(monkeypatch, 4200)
        try:
            assert mod.uses_local_callback() is True
            assert mod.API_BASE == mod.DATA_ACT_HOST
        finally:
            monkeypatch.undo()
            __import__("importlib").reload(bosch)
        assert bosch.uses_local_callback() is False, \
            "the app client's mobile scheme still needs the paste"
