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

BOSCH_RIDE = {
    "activityId": "ride-1", "name": "Commute",
    "startTime": "2026-10-12T07:30:00Z", "duration": 2400, "distance": 18500,
    "calories": 310, "averageRiderPower": 112, "averageCadence": 68,
    "riderEnergy": 1300, "motorEnergy": 2600,
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
        return {"activities": self.rides}


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
        assert float(row["kcal"]) == 310.0
        assert float(row["avg_rider_power_w"]) == 112.0
        assert float(row["motor_energy_share"]) == pytest.approx(2600 / 3900, abs=1e-3)

    def test_the_window_is_asked_for_once(self, bosch_account, fake_bosch):
        api = fake_bosch(FakeSession())
        poller.poll_bosch(bosch_account, 7, None)
        assert len(api.calls) == 1, "one request for the whole window"
        _, params = api.calls[0]
        today = dt.datetime.now(ZoneInfo(bosch_account["timezone"])).date()
        assert params["from"] == (today - dt.timedelta(days=6)).isoformat()
        assert params["to"] == today.isoformat()

    def test_a_re_fetched_ride_updates_rather_than_duplicates(
            self, bosch_account, fake_bosch, pool):
        fake_bosch(FakeSession())
        poller.poll_bosch(bosch_account, 7, None)
        fake_bosch(FakeSession([dict(BOSCH_RIDE, calories=333)]))
        poller.poll_bosch(bosch_account, 7, None)
        with pool.user_tx(bosch_account["user_id"]) as cur:
            got = cur.execute("SELECT kcal FROM diet.activities").fetchall()
        assert len(got) == 1 and float(got[0]["kcal"]) == 333.0

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
