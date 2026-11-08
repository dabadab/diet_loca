"""
The Garmin ingest path, driven by fixtures instead of a live account.

A real account would make these tests depend on someone's actual sleep, cost
requests against a rate limit that has bitten this deployment before, and go
red when Garmin had an outage. The seam is `garmin.fetch_*`: everything below
it is HTTP and everything above it is ours, so the fakes replace exactly that.
"""

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from app import garmin, poller, queries

STATS = {
    "totalKilocalories": 2634.0, "activeKilocalories": 812.0, "totalSteps": 11423,
    "restingHeartRate": 51, "weight": 83700.0, "bodyFat": 19.4,
}
SLEEP = {"dailySleepDTO": {"sleepTimeSeconds": 26100}}
DAY = dt.date(2026, 9, 8)


def activity(aid, **over):
    base = {"activityId": aid, "activityName": f"W{aid}",
            "activityType": {"typeKey": "running"},
            "startTimeGMT": "2026-09-08 06:00:00", "duration": 1800.0,
            "calories": 410.0, "averageHR": 142.0,
            "hrTimeInZone_1": 900.0, "hrTimeInZone_2": 900.0}
    return {**base, **over}


class FakeApi:
    """Stands in for a logged-in Garmin client, and counts what was asked for.

    Anything not explicitly provided raises, so a test fails loudly if the
    poller starts calling an endpoint nobody decided to spend a request on.
    """

    def __init__(self, stats=STATS, sleep=SLEEP, activities=()):
        self.stats, self.sleep, self.activities = stats, sleep, list(activities)
        self.calls = []

    def get_stats_and_body(self, cdate):
        self.calls.append(("stats", cdate))
        return self.stats

    def get_sleep_data(self, cdate):
        self.calls.append(("sleep", cdate))
        return self.sleep

    def get_activities_by_date(self, start, end):
        self.calls.append(("activities", start, end))
        return self.activities

    def __getattr__(self, name):
        raise AssertionError(f"unexpected Garmin call: {name}")


@pytest.fixture
def account(user, owner, pool):
    """A user with a stored Garmin session, as _accounts() would return it."""
    owner.execute(
        """INSERT INTO diet.garmin_credentials (user_id, secret_ciphertext, key_id)
           VALUES (%s, %s, 'k')""", (user, b"ciphertext"))
    return {"user_id": user, "email": "a@example.com",
            "timezone": "Europe/Budapest", "secret_ciphertext": b"ciphertext",
            "key_id": "k"}


@pytest.fixture
def fake_garmin(monkeypatch):
    """Swap the network seam, and hand back a factory for the fake client."""
    from app import secretbox
    monkeypatch.setattr(secretbox, "decrypt", lambda ct, kid: "{}")
    monkeypatch.setattr(garmin, "session_tokens", lambda api: "")

    def install(api):
        monkeypatch.setattr(garmin, "resume", lambda token: api)
        return api
    return install


class TestReadings:
    def test_a_days_metrics_land_with_their_units(self, pool, user):
        tz = ZoneInfo("Europe/Budapest")
        stored, rejected = poller._write_readings(
            user, tz, DAY, garmin.extract(STATS, SLEEP))
        assert (stored, rejected) == (7, [])
        with pool.user_tx(user) as cur:
            rows = {r["metric"]: r["value"] for r in cur.execute(
                "SELECT metric, value FROM diet.measurements").fetchall()}
        assert rows["weight_kg"] == 83.7
        assert rows["sleep_minutes"] == 435.0

    def test_readings_are_timestamped_noon_so_the_day_cannot_slip(self, pool, make_user):
        """Midnight local converts to the previous day in UTC for anyone west of
        Greenwich. Noon survives the round trip at any offset."""
        for tz_name in ("Europe/Budapest", "America/Denver", "Pacific/Auckland"):
            uid = make_user(f"{tz_name.replace('/', '-').lower()}@example.com",
                            timezone=tz_name)
            poller._write_readings(uid, ZoneInfo(tz_name), DAY,
                                   garmin.extract(STATS, None))
            with pool.user_tx(uid) as cur:
                dates = {r["local_date"] for r in cur.execute(
                    "SELECT DISTINCT local_date FROM diet.measurements").fetchall()}
            assert dates == {DAY}, f"{tz_name} put the reading on the wrong day"

    def test_refetching_a_day_updates_rather_than_duplicates(self, pool, user):
        """Garmin backfills and revises, so (user, source, metric, date) is the
        identity of a reading and a second sight of it must overwrite."""
        tz = ZoneInfo("Europe/Budapest")
        poller._write_readings(user, tz, DAY, garmin.extract(STATS, SLEEP))
        revised = dict(STATS, totalKilocalories=2700.0)
        poller._write_readings(user, tz, DAY, garmin.extract(revised, SLEEP))
        with pool.user_tx(user) as cur:
            n = cur.execute("SELECT count(*) AS n FROM diet.measurements").fetchone()["n"]
            total = cur.execute("SELECT value FROM diet.measurements "
                                "WHERE metric = 'total_kcal'").fetchone()["value"]
        assert n == 7, "re-fetching the same day must not stack rows"
        assert total == 2700.0, "the revision should win"

    def test_one_implausible_value_costs_only_itself(self, pool, user):
        """Each upsert runs in a savepoint, so a value the schema refuses does
        not take the rest of the day with it."""
        bad = dict(STATS, weight=8400000.0)          # 8400 kg
        stored, rejected = poller._write_readings(
            user, ZoneInfo("Europe/Budapest"), DAY, garmin.extract(bad, SLEEP))
        assert stored == 6
        assert len(rejected) == 1 and "weight_kg" in rejected[0]
        assert "measurements_value_sane" in rejected[0]


class TestActivities:
    def test_a_window_of_activities_is_stored(self, pool, user):
        rows = [garmin.extract_activity(activity(i)) for i in (1, 2, 3)]
        assert poller._write_activities(user, rows) == (3, [])
        with pool.user_tx(user) as cur:
            got = cur.execute("SELECT external_id, hr_zones FROM diet.activities "
                              "ORDER BY external_id").fetchall()
        assert [r["external_id"] for r in got] == ["1", "2", "3"]
        assert got[0]["hr_zones"] == [{"zone": 1, "secs": 900.0},
                                      {"zone": 2, "secs": 900.0}]

    def test_a_revised_activity_overwrites(self, pool, user):
        """Garmin reprocesses heart rate and lets people rename a workout."""
        poller._write_activities(user, [garmin.extract_activity(activity(1))])
        poller._write_activities(user, [garmin.extract_activity(
            activity(1, activityName="Renamed", calories=555.0))])
        with pool.user_tx(user) as cur:
            got = cur.execute("SELECT name, kcal FROM diet.activities").fetchall()
        assert len(got) == 1
        assert got[0]["name"] == "Renamed" and float(got[0]["kcal"]) == 555.0

    def test_one_bad_activity_costs_only_itself(self, pool, user):
        rows = [garmin.extract_activity(activity(1, averageHR=999.0)),
                garmin.extract_activity(activity(2))]
        stored, rejected = poller._write_activities(user, rows)
        assert stored == 1
        assert len(rejected) == 1 and "activities_avg_hr_check" in rejected[0]


class TestPollUser:
    def test_a_pass_costs_two_requests_a_day_plus_one_for_the_window(
            self, account, fake_garmin, pool):
        """The activity list covers the whole window in a single call, so widening
        the window does not multiply activity requests."""
        api = fake_garmin(FakeApi(activities=[activity(1), activity(2)]))
        stored, activities, rejected = poller.poll_user(account, 3, None)
        assert activities == 2 and rejected == []
        kinds = [c[0] for c in api.calls]
        assert kinds.count("activities") == 1
        assert kinds.count("stats") == 3 and kinds.count("sleep") == 3

    def test_the_activity_window_matches_the_days_requested(
            self, account, fake_garmin):
        api = fake_garmin(FakeApi())
        today = dt.datetime.now(ZoneInfo(account["timezone"])).date()
        poller.poll_user(account, 7, None)
        call = next(c for c in api.calls if c[0] == "activities")
        assert call[1] == (today - dt.timedelta(days=6)).isoformat()
        assert call[2] == today.isoformat()

    def test_a_rate_limit_is_raised_as_such(self, account, fake_garmin, monkeypatch):
        """Distinct from any other failure so the loop backs off instead of
        retrying into an endpoint that asked us to stop."""
        def boom(*a, **k):
            raise RuntimeError("429 Too Many Requests")
        monkeypatch.setattr(garmin, "resume", boom)
        with pytest.raises(poller.RateLimited):
            poller.poll_user(account, 1, None)

    def test_raw_payloads_are_archived_before_parsing(
            self, account, fake_garmin, tmp_path):
        """When the shape shifts, the evidence should already be on disk rather
        than needing a reproduction against a live account."""
        fake_garmin(FakeApi(activities=[activity(1)]))
        poller.poll_user(account, 2, tmp_path)
        written = sorted(p.name for p in (tmp_path / account["email"]).glob("*.json"))
        assert len(written) == 3, written          # two days plus the window
        assert any(n.startswith("activities-") for n in written)


class TestClaim:
    def test_a_second_sync_within_the_gap_is_refused(self, pool, user):
        """The claim is what stops the sidecar and the page's Sync now button
        both talking to Garmin at once."""
        assert poller.claim(user, min_gap_seconds=90) is True
        assert poller.claim(user, min_gap_seconds=90) is False

    def test_the_claim_is_released_once_the_gap_has_passed(self, pool, user):
        assert poller.claim(user, min_gap_seconds=90) is True
        assert poller.claim(user, min_gap_seconds=0) is True

    def test_one_users_claim_does_not_block_another(self, pool, user, other_user):
        assert poller.claim(user, min_gap_seconds=90) is True
        assert poller.claim(other_user, min_gap_seconds=90) is True


class TestSyncState:
    def test_a_failure_is_recorded_without_losing_the_last_success(self, pool, user):
        poller._record(user, ok=True, error=None)
        poller._record(user, ok=False, error="session expired")
        with pool.user_tx(user) as cur:
            garmin_state = queries.sync_snapshot(cur)["connectors"]["garmin"]
        assert garmin_state["last_success_at"] is not None, \
            "a later failure must not erase when it last worked"
        assert garmin_state["last_error"] == "session expired"

    def test_one_users_sync_state_is_invisible_to_another(
            self, pool, user, other_user):
        poller._record(user, ok=False, error="session expired")
        with pool.user_tx(other_user) as cur:
            assert queries.sync_snapshot(cur)["connectors"] == {}


# ----------------------------------------------------------------- bosch ---

_YESTERDAY = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).date()
BOSCH_RIDE = {
    "id": "ride-1", "title": "Commute",
    "startTime": f"{_YESTERDAY}T07:30:00Z", "endTime": f"{_YESTERDAY}T08:10:00Z",
    "durationWithoutStops": 2280, "distance": 18500,
    "caloriesBurned": 310, "riderPower": {"average": 112, "maximum": 250},
    "cadence": {"average": 68, "maximum": 80},
}


class FakeSession:
    """Stands in for a logged-in Bosch rider, and counts what was asked for."""

    def __init__(self, rides=(BOSCH_RIDE,), fail=None, refreshed=False):
        self.rides, self.fail, self.refreshed = list(rides), fail, refreshed
        self.calls = []
        self.tokens = {"access_token": "a", "refresh_token": "r"}

    def get(self, url, **params):
        self.calls.append((url, params))
        if self.fail:
            raise self.fail
        return {"activitySummaries": self.rides,
                "pagination": {"total": len(self.rides)}}


@pytest.fixture
def bosch_account(user, owner, pool):
    owner.execute(
        """INSERT INTO diet.bosch_credentials (user_id, secret_ciphertext, key_id)
           VALUES (%s, %s, 'k')""", (user, b"ciphertext"))
    return {"user_id": user, "email": "a@example.com",
            "timezone": "Europe/Budapest", "secret_ciphertext": b"ciphertext",
            "key_id": "k"}


@pytest.fixture
def fake_bosch(monkeypatch):
    from app import bosch, secretbox
    monkeypatch.setattr(secretbox, "decrypt", lambda ct, kid: "{}")

    def install(session):
        monkeypatch.setattr(bosch, "resume", lambda token: session)
        return session
    return install


class TestBoschRides:
    def test_a_window_of_rides_is_stored(self, bosch_account, fake_bosch, pool):
        from app import bosch
        api = fake_bosch(FakeSession())
        stored, rejected = poller.poll_bosch(bosch_account, 7, None)
        assert (stored, rejected) == (1, [])
        with pool.user_tx(bosch_account["user_id"]) as cur:
            row = cur.execute(
                "SELECT source, activity_type, kcal, avg_rider_power_w, "
                "motor_energy_share FROM diet.activities").fetchone()
        assert row["source"] == "bosch" and row["activity_type"] == "ebike_ride"
        assert row["kcal"] is None, "Bosch calories are deliberately not stored"
        assert float(row["avg_rider_power_w"]) == 112.0
        assert row["motor_energy_share"] is None, "the summary does not say"

    def test_the_window_is_asked_for_once(self, bosch_account, fake_bosch):
        api = fake_bosch(FakeSession())
        poller.poll_bosch(bosch_account, 7, None)
        assert len(api.calls) == 1, "one request for the whole window"
        _, params = api.calls[0]
        today = dt.datetime.now(ZoneInfo(bosch_account["timezone"])).date()
        assert params == {"limit": 50, "offset": 0, "sort": "-startTime"}

    def test_a_re_fetched_ride_updates_rather_than_duplicates(
            self, bosch_account, fake_bosch, pool):
        fake_bosch(FakeSession())
        poller.poll_bosch(bosch_account, 7, None)
        fake_bosch(FakeSession([dict(BOSCH_RIDE, distance=19000)]))
        poller.poll_bosch(bosch_account, 7, None)
        with pool.user_tx(bosch_account["user_id"]) as cur:
            got = cur.execute("SELECT distance_m FROM diet.activities").fetchall()
        assert len(got) == 1 and float(got[0]["distance_m"]) == 19000.0

    def test_rotated_tokens_are_persisted(self, bosch_account, fake_bosch, pool,
                                          monkeypatch):
        """SingleKey rotates the refresh token on use. Not storing the new one
        works until it does not, and then needs a human at a browser."""
        from app import bosch, secretbox
        monkeypatch.setattr(secretbox, "encrypt", lambda s: (b"new-ct", "k2"))
        session = fake_bosch(FakeSession(refreshed=True))
        session.tokens = {"access_token": "fresh", "refresh_token": "rotated"}
        poller.poll_bosch(bosch_account, 7, None)
        with pool.user_tx(bosch_account["user_id"]) as cur:
            row = cur.execute("SELECT secret_ciphertext, key_id "
                              "FROM diet.bosch_credentials").fetchone()
        assert bytes(row["secret_ciphertext"]) == b"new-ct" and row["key_id"] == "k2"

    def test_tokens_are_left_alone_when_nothing_was_refreshed(
            self, bosch_account, fake_bosch, pool):
        fake_bosch(FakeSession(refreshed=False))
        poller.poll_bosch(bosch_account, 7, None)
        with pool.user_tx(bosch_account["user_id"]) as cur:
            row = cur.execute("SELECT secret_ciphertext "
                              "FROM diet.bosch_credentials").fetchone()
        assert bytes(row["secret_ciphertext"]) == b"ciphertext"

    def test_a_rate_limit_is_raised_as_such(self, bosch_account, fake_bosch):
        from app import bosch
        fake_bosch(FakeSession(fail=bosch.RateLimited("429")))
        with pytest.raises(poller.RateLimited):
            poller.poll_bosch(bosch_account, 7, None)

    def test_an_expired_session_is_distinguishable_from_a_blip(
            self, bosch_account, fake_bosch):
        """Only a person at a browser can fix it, so the poller must be able to
        say that rather than retrying forever."""
        from app import bosch
        fake_bosch(FakeSession(fail=bosch.NeedsLogin("refresh token spent")))
        with pytest.raises(bosch.NeedsLogin):
            poller.poll_bosch(bosch_account, 7, None)

    def test_an_ordinary_failure_costs_no_rides(self, bosch_account, fake_bosch):
        """Bosch being unreachable returns nothing rather than propagating, so
        it cannot take the Garmin half of the pass down with it."""
        fake_bosch(FakeSession(fail=RuntimeError("connection reset")))
        assert poller.poll_bosch(bosch_account, 7, None) == (0, [])

    def test_raw_payloads_are_archived(self, bosch_account, fake_bosch, tmp_path):
        fake_bosch(FakeSession())
        poller.poll_bosch(bosch_account, 3, tmp_path)
        written = list((tmp_path / bosch_account["email"]).glob("bosch-*.json"))
        assert len(written) == 1
        assert "startLatitude" not in written[0].read_text()


class TestConnectorsAreIndependent:
    def test_each_connector_claims_separately(self, pool, user):
        """One claim must not block the other, or whichever polled first would
        silently starve the other every cycle."""
        assert poller.claim(user, connector="garmin") is True
        assert poller.claim(user, connector="bosch") is True
        assert poller.claim(user, connector="bosch") is False

    def test_failures_are_recorded_against_the_right_connector(self, pool, user):
        poller._record(user, ok=True, error=None, connector="garmin")
        poller._record(user, ok=False, error="session expired", connector="bosch")
        with pool.user_tx(user) as cur:
            state = queries.sync_snapshot(cur)["connectors"]
        assert state["garmin"]["last_error"] is None
        assert state["bosch"]["last_error"] == "session expired"
        assert state["garmin"]["last_success_at"] is not None


# ------------------------------------------------------------ fit profile ---

def weigh_in(mid, ts, weight, bodyfat=22.4, **over):
    return {"measurement_id": mid, "user_id": "42", "time_stamp": ts,
            "weight": weight, "bodyfat": bodyfat, "muscle": 44.9 if bodyfat else 0.0,
            "water": 56.9 if bodyfat else 0.0, "visfat": 6.0 if bodyfat else 0.0,
            "bmr": 1819 if bodyfat else 0, "bodyage": 51 if bodyfat else 0,
            "bmi": 27.9, **over}


_NOW = int(dt.datetime.now(dt.timezone.utc).timestamp())


class FakeScale:
    """Stands in for a signed-in Fit Profile session: one page, then done."""

    def __init__(self, items=(), deleted=(), fail=None, refreshed=False):
        self.items, self.deleted = list(items), list(deleted)
        self.fail, self.refreshed = fail, refreshed
        self.user_id = "42"
        self.blob = {"token": "t2", "user_id": "42"}
        self.calls = []

    def get(self, path, params):
        self.calls.append((path, params))
        if self.fail:
            raise self.fail
        return {"code": 200, "data": {"measurements": self.items,
                                      "delete_measurement_ids": self.deleted,
                                      "finish_flag": 1}}


@pytest.fixture
def scale_account(user, owner, pool):
    owner.execute(
        """INSERT INTO diet.fitprofile_credentials (user_id, secret_ciphertext, key_id)
           VALUES (%s, %s, 'k')""", (user, b"ciphertext"))
    return {"user_id": user, "email": "a@example.com",
            "timezone": "Europe/Budapest", "secret_ciphertext": b"ciphertext",
            "key_id": "k"}


@pytest.fixture
def fake_scale(monkeypatch):
    from app import fitprofile, secretbox
    monkeypatch.setattr(secretbox, "decrypt", lambda ct, kid: "{}")

    def install(session):
        monkeypatch.setattr(fitprofile, "resume", lambda blob: session)
        return session
    return install


def stored_weigh_ins(pool, user):
    with pool.user_tx(user) as cur:
        return cur.execute("SELECT * FROM diet.weigh_ins ORDER BY measured_at").fetchall()


class TestWeighIns:
    def test_every_weigh_in_is_stored_with_its_figures(
            self, scale_account, fake_scale, pool):
        fake_scale(FakeScale([weigh_in("1", _NOW - 7200, 86.7, 0.0),
                              weigh_in("2", _NOW - 3600, 86.4)]))
        stored, removed, rejected = poller.poll_fitprofile(scale_account, 1, None)
        assert (stored, removed, rejected) == (2, 0, [])
        rows = stored_weigh_ins(pool, scale_account["user_id"])
        assert [r["source"] for r in rows] == ["fitprofile", "fitprofile"]
        assert rows[0]["body_fat_pct"] is None and rows[0]["visceral_fat"] is None
        assert float(rows[1]["body_fat_pct"]) == 22.4
        assert float(rows[1]["skeletal_muscle_pct"]) == 44.9
        assert rows[1]["raw"]["measurement_id"] == "2"

    def test_a_re_fetched_weigh_in_updates_rather_than_duplicates(
            self, scale_account, fake_scale, pool):
        fake_scale(FakeScale([weigh_in("1", _NOW, 86.4)]))
        poller.poll_fitprofile(scale_account, 1, None)
        fake_scale(FakeScale([weigh_in("1", _NOW, 86.5)]))
        poller.poll_fitprofile(scale_account, 1, None)
        rows = stored_weigh_ins(pool, scale_account["user_id"])
        assert len(rows) == 1 and float(rows[0]["weight_kg"]) == 86.5

    def test_a_weigh_in_deleted_in_the_app_is_deleted_here(
            self, scale_account, fake_scale, pool):
        """Deleted weigh-ins are usually mistakes -- a bag on the scale, the
        wrong person. Left in place they would stay on the weight chart."""
        fake_scale(FakeScale([weigh_in("1", _NOW - 60, 86.4),
                              weigh_in("2", _NOW, 99.9)]))
        poller.poll_fitprofile(scale_account, 1, None)
        fake_scale(FakeScale(deleted=["2"]))
        _, removed, _ = poller.poll_fitprofile(scale_account, 1, None)
        assert removed == 1
        assert [r["external_id"] for r in stored_weigh_ins(
            pool, scale_account["user_id"])] == ["1"]

    def test_one_implausible_weigh_in_costs_only_itself(
            self, scale_account, fake_scale, pool):
        fake_scale(FakeScale([weigh_in("1", _NOW, 86.4),
                              weigh_in("2", _NOW, 864.0)]))
        stored, _, rejected = poller.poll_fitprofile(scale_account, 1, None)
        assert stored == 1 and "weigh_ins_weight_sane" in rejected[0]

    def test_the_window_asks_for_changes_since_local_midnight(
            self, scale_account, fake_scale):
        """By change time, not measurement time: the scale holds readings taken
        with the phone away and the app uploads them later."""
        scale = fake_scale(FakeScale())
        poller.poll_fitprofile(scale_account, 3, None)
        tz = ZoneInfo(scale_account["timezone"])
        start = dt.datetime.now(tz).date() - dt.timedelta(days=2)
        since = dt.datetime.combine(start, dt.time(0, 0), tzinfo=tz)
        assert scale.calls[0][1]["last_updated_at"] == int(since.timestamp())

    def test_a_fresh_sign_in_is_persisted(self, scale_account, fake_scale, pool,
                                          monkeypatch):
        from app import secretbox
        monkeypatch.setattr(secretbox, "encrypt", lambda s: (b"new-ct", "k2"))
        fake_scale(FakeScale(refreshed=True))
        poller.poll_fitprofile(scale_account, 1, None)
        with pool.user_tx(scale_account["user_id"]) as cur:
            row = cur.execute("SELECT secret_ciphertext, key_id "
                              "FROM diet.fitprofile_credentials").fetchone()
        assert bytes(row["secret_ciphertext"]) == b"new-ct" and row["key_id"] == "k2"

    def test_a_fresh_sign_in_is_persisted_even_when_the_fetch_fails(
            self, scale_account, fake_scale, pool, monkeypatch):
        from app import fitprofile, secretbox
        monkeypatch.setattr(secretbox, "encrypt", lambda s: (b"new-ct", "k2"))
        fake_scale(FakeScale(refreshed=True,
                             fail=fitprofile.FitProfileError("502")))
        with pytest.raises(fitprofile.FitProfileError):
            poller.poll_fitprofile(scale_account, 1, None)
        with pool.user_tx(scale_account["user_id"]) as cur:
            row = cur.execute("SELECT key_id FROM diet.fitprofile_credentials"
                              ).fetchone()
        assert row["key_id"] == "k2"

    def test_a_rate_limit_is_raised_as_such(self, scale_account, fake_scale):
        from app import fitprofile
        fake_scale(FakeScale(fail=fitprofile.RateLimited("429")))
        with pytest.raises(poller.RateLimited):
            poller.poll_fitprofile(scale_account, 1, None)

    def test_a_dead_password_is_distinguishable_from_a_blip(
            self, scale_account, fake_scale):
        from app import fitprofile
        fake_scale(FakeScale(fail=fitprofile.NeedsLogin("wrong password")))
        with pytest.raises(fitprofile.NeedsLogin):
            poller.poll_fitprofile(scale_account, 1, None)

    def test_weigh_ins_are_invisible_to_another_user(
            self, scale_account, fake_scale, pool, other_user):
        fake_scale(FakeScale([weigh_in("1", _NOW, 86.4)]))
        poller.poll_fitprofile(scale_account, 1, None)
        assert stored_weigh_ins(pool, other_user) == []


# ------------------------------------------- weigh-ins sent on to Garmin ---

_TZ = ZoneInfo("Europe/Budapest")


def _at(day, hh, mm=0):
    return dt.datetime.combine(day, dt.time(hh, mm), tzinfo=_TZ)


def add_weigh_in(pool, user, mid, at, weight, full=True):
    """Straight into the table: what is under test is the choice of what to
    send, not the Fit Profile parsing that put it there."""
    body = (dict(body_fat_pct=22.4, skeletal_muscle_pct=44.9, body_water_pct=56.9,
                 bone_mass_kg=4.41, bmr_kcal=1819, metabolic_age=51,
                 visceral_fat=6.0) if full else
            dict(body_fat_pct=None, skeletal_muscle_pct=None, body_water_pct=None,
                 bone_mass_kg=None, bmr_kcal=None, metabolic_age=None,
                 visceral_fat=None))
    with pool.user_tx(user) as cur:
        cur.execute("""
            INSERT INTO diet.weigh_ins
              (user_id, source, external_id, measured_at, weight_kg, bmi,
               body_fat_pct, skeletal_muscle_pct, body_water_pct, bone_mass_kg,
               bmr_kcal, metabolic_age, visceral_fat)
            VALUES (diet.current_user_id(), 'fitprofile', %(mid)s, %(at)s,
                    %(weight)s, 27.9, %(body_fat_pct)s, %(skeletal_muscle_pct)s,
                    %(body_water_pct)s, %(bone_mass_kg)s, %(bmr_kcal)s,
                    %(metabolic_age)s, %(visceral_fat)s)""",
                    {"mid": mid, "at": at, "weight": weight, **body})


def pushed_ids(pool, user):
    with pool.user_tx(user) as cur:
        return [r["external_id"] for r in cur.execute(
            """SELECT external_id FROM diet.weigh_ins
               WHERE garmin_pushed_at IS NOT NULL ORDER BY measured_at""")]


@pytest.fixture
def uploads(monkeypatch):
    """Every weigh-in handed to Garmin, in order. Set `.fail` to make the next
    upload raise instead."""
    class Recorder(list):
        fail = None
    sent = Recorder()

    def upload(api, row):
        if sent.fail:
            exc, sent.fail = sent.fail, None
            raise exc
        sent.append(row["external_id"])
        return True
    monkeypatch.setattr(garmin, "upload_weigh_in", upload)
    return sent


class TestPushWeighIns:
    def test_the_days_first_full_weigh_in_is_sent_and_nothing_else(
            self, account, fake_garmin, pool, uploads):
        """The socks-on reading just past midnight comes first but has no body
        composition; the evening one is full but noisy. Only the morning one
        goes to Garmin."""
        today = dt.datetime.now(_TZ).date()
        user = account["user_id"]
        add_weigh_in(pool, user, "midnight", _at(today, 0, 5), 86.9, full=False)
        add_weigh_in(pool, user, "morning", _at(today, 0, 30), 86.4)
        add_weigh_in(pool, user, "evening", _at(today, 0, 50), 87.2)
        fake_garmin(FakeApi())
        _, _, rejected = poller.poll_user(account, 1, None)
        assert uploads == ["morning"] and rejected == []
        assert pushed_ids(pool, user) == ["morning"]

    def test_a_day_is_sent_once(self, account, fake_garmin, pool, uploads):
        """Including when an earlier full reading turns up later -- the scale
        holds readings while the phone is away. Garmin already has that day."""
        today = dt.datetime.now(_TZ).date()
        user = account["user_id"]
        add_weigh_in(pool, user, "later", _at(today, 0, 40), 86.4)
        fake_garmin(FakeApi())
        poller.poll_user(account, 1, None)
        add_weigh_in(pool, user, "earlier", _at(today, 0, 10), 86.5)
        poller.poll_user(account, 1, None)
        assert uploads == ["later"]

    def test_a_day_with_only_weight_only_readings_sends_nothing(
            self, account, fake_garmin, pool, uploads):
        today = dt.datetime.now(_TZ).date()
        add_weigh_in(pool, account["user_id"], "socks", _at(today, 0, 5), 86.9,
                     full=False)
        fake_garmin(FakeApi())
        poller.poll_user(account, 1, None)
        assert uploads == []

    def test_only_days_inside_the_window_are_sent(
            self, account, fake_garmin, pool, uploads):
        """The short cycle must not reach back and send last month's weigh-ins,
        many of which Garmin already has, typed in by hand."""
        today = dt.datetime.now(_TZ).date()
        user = account["user_id"]
        add_weigh_in(pool, user, "old", _at(today - dt.timedelta(days=5), 7), 87.0)
        add_weigh_in(pool, user, "yesterday", _at(today - dt.timedelta(days=1), 7), 86.6)
        add_weigh_in(pool, user, "today", _at(today, 0, 30), 86.4)
        fake_garmin(FakeApi())
        poller.poll_user(account, 2, None)
        assert uploads == ["yesterday", "today"]

    def test_a_failed_upload_is_reported_and_retried_next_time(
            self, account, fake_garmin, pool, uploads):
        today = dt.datetime.now(_TZ).date()
        user = account["user_id"]
        add_weigh_in(pool, user, "morning", _at(today, 0, 30), 86.4)
        fake_garmin(FakeApi())
        uploads.fail = RuntimeError("API Error 500")
        _, _, rejected = poller.poll_user(account, 1, None)
        assert len(rejected) == 1 and "API Error 500" in rejected[0]
        assert pushed_ids(pool, user) == []
        poller.poll_user(account, 1, None)
        assert pushed_ids(pool, user) == ["morning"]

    def test_a_rate_limit_stops_the_pass_but_keeps_what_was_sent(
            self, account, fake_garmin, pool, uploads, monkeypatch):
        today = dt.datetime.now(_TZ).date()
        user = account["user_id"]
        add_weigh_in(pool, user, "yesterday", _at(today - dt.timedelta(days=1), 7), 86.6)
        add_weigh_in(pool, user, "today", _at(today, 0, 30), 86.4)
        fake_garmin(FakeApi())
        real = garmin.upload_weigh_in

        def second_one_429s(api, row):
            if row["external_id"] == "today":
                raise RuntimeError("429 Too Many Requests")
            return real(api, row)
        monkeypatch.setattr(garmin, "upload_weigh_in", second_one_429s)
        with pytest.raises(poller.RateLimited):
            poller.poll_user(account, 2, None)
        assert pushed_ids(pool, user) == ["yesterday"]

    def test_nothing_to_send_costs_no_request(self, account, fake_garmin, pool):
        """FakeApi raises on any call nobody provided for, upload included."""
        fake_garmin(FakeApi())
        _, _, rejected = poller.poll_user(account, 1, None)
        assert rejected == []
