"""
One outing, two instruments.

A ride can be recorded by the watch, by the ebike, or by both. Which of those
happened decides whether the day's `active_kcal` already contains that effort,
and that is not something a reader can infer from the rows — so it is resolved
here and reported as a flag.

The matching is done at read time rather than stamped on write, because the two
connectors poll on different cadences and the Garmin twin routinely arrives
*after* the Bosch ride. These tests include that ordering explicitly, since it
is the case a write-time match would get wrong and then never revisit.
"""

import datetime as dt
from datetime import timezone

import pytest

from app import queries

DAY = dt.date(2026, 10, 12)
T = dt.datetime(2026, 10, 12, 6, 0, tzinfo=timezone.utc)


@pytest.fixture
def ride(pool, user):
    """Record an activity. Minutes, because that is how rides are talked about."""
    def _ride(source, external_id, *, start_min=0, minutes=40, kcal=None, **extra):
        cols = {"duration_s": minutes * 60, "kcal": kcal, **extra}
        with pool.user_tx(user) as cur:
            cur.execute(
                f"""INSERT INTO diet.activities
                    (user_id, source, external_id, started_at, activity_type,
                     {','.join(cols)})
                    VALUES (diet.current_user_id(), %s, %s, %s, %s,
                     {','.join(['%s'] * len(cols))})""",
                (source, external_id, T + dt.timedelta(minutes=start_min),
                 "ebike_ride" if source == "bosch" else "cycling", *cols.values()))
    return _ride


def rows(pool, user):
    with pool.user_tx(user) as cur:
        return {a["external_id"]: a for a in queries.activities_on(cur, DAY)}


class TestWhatCountsAsTheSameOuting:
    def test_a_watch_and_a_bike_recording_one_ride_are_matched(
            self, pool, user, ride):
        """The watch is started by hand, so the two never agree to the second."""
        ride("garmin", "g", start_min=0, minutes=40, kcal=540)
        ride("bosch", "b", start_min=1, minutes=39, kcal=310)
        got = rows(pool, user)
        assert got["g"]["matched_external_id"] == "b"
        assert got["b"]["matched_external_id"] == "g"

    def test_activities_that_merely_touch_are_not_the_same_outing(
            self, pool, user, ride):
        """A ride that ends as a run begins shares a few minutes. Matching on
        any overlap at all would fuse them into one."""
        ride("garmin", "run", start_min=36, minutes=30)
        ride("bosch", "commute", start_min=0, minutes=40)
        got = rows(pool, user)
        assert got["run"]["matched_external_id"] is None
        assert got["commute"]["matched_external_id"] is None

    def test_a_short_ride_inside_a_long_one_matches(self, pool, user, ride):
        """The bike is switched off at the cafe; the watch keeps running. The
        overlap is most of the *shorter* ride, which is the test."""
        ride("garmin", "g", start_min=0, minutes=90)
        ride("bosch", "b", start_min=10, minutes=60)
        assert rows(pool, user)["b"]["matched_external_id"] == "g"

    def test_two_rides_from_the_same_instrument_never_match(self, pool, user, ride):
        """Whatever they overlap. One instrument recording twice is two
        activities; it is only across instruments that a pair means one outing."""
        ride("garmin", "g1", start_min=0, minutes=40)
        ride("garmin", "g2", start_min=1, minutes=39)
        got = rows(pool, user)
        assert got["g1"]["matched_external_id"] is None
        assert got["g2"]["matched_external_id"] is None

    def test_the_match_does_not_depend_on_which_arrived_first(
            self, pool, user, ride):
        """The connectors poll on different cadences, so the Bosch ride is
        often written first and the Garmin twin only hours later. A match
        stamped at write time would be wrong for every one of those, forever."""
        ride("bosch", "b", start_min=1, minutes=39, kcal=310)
        assert rows(pool, user)["b"]["matched_external_id"] is None
        ride("garmin", "g", start_min=0, minutes=40, kcal=540)
        assert rows(pool, user)["b"]["matched_external_id"] == "g", \
            "the match should appear as soon as the twin does"

    def test_an_activity_with_no_duration_matches_nothing(self, pool, user, ride):
        """Without a duration there is no interval to overlap, and guessing one
        would invent a match out of a single timestamp."""
        ride("garmin", "g", start_min=0, minutes=40)
        with pool.user_tx(user) as cur:
            cur.execute("""INSERT INTO diet.activities
                (user_id, source, external_id, started_at, activity_type)
                VALUES (diet.current_user_id(), 'bosch', 'b', %s, 'ebike_ride')""",
                (T,))
        got = rows(pool, user)
        assert got["b"]["matched_external_id"] is None
        assert got["g"]["matched_external_id"] is None

    def test_the_best_overlap_wins_when_several_could_match(self, pool, user, ride):
        ride("bosch", "b", start_min=0, minutes=60)
        ride("garmin", "near", start_min=0, minutes=58)
        ride("garmin", "far", start_min=50, minutes=40)
        assert rows(pool, user)["b"]["matched_external_id"] == "near"


class TestInExpenditure:
    """The flag exists so that "already counted" and "counted nowhere" can be
    told apart. Anything else it might say would be worse than nothing."""

    def test_a_watch_ride_is_already_in_the_days_expenditure(
            self, pool, user, ride):
        ride("garmin", "g", minutes=40, kcal=540)
        assert rows(pool, user)["g"]["in_expenditure"] is True

    def test_a_matched_bike_ride_is_already_in_it_too(self, pool, user, ride):
        """Through its twin: active_kcal comes from the watch's daily total."""
        ride("garmin", "g", start_min=0, minutes=40, kcal=540)
        ride("bosch", "b", start_min=1, minutes=39, kcal=310)
        assert rows(pool, user)["b"]["in_expenditure"] is True

    def test_an_unmatched_bike_ride_is_in_no_expenditure_figure(
            self, pool, user, ride):
        """The watch was left at home, so the day's active_kcal read a commute
        as sitting still."""
        ride("bosch", "b", minutes=30, kcal=240)
        assert rows(pool, user)["b"]["in_expenditure"] is False

    def test_it_is_never_null(self, pool, user, ride):
        """`a.source = 'garmin' OR twin.source = 'garmin'` is FALSE OR NULL —
        which is NULL — for an unmatched Bosch ride. A flag whose whole job is
        to separate two cases must not answer "neither"."""
        ride("garmin", "g", start_min=0, minutes=40)
        ride("bosch", "matched", start_min=1, minutes=39)
        ride("bosch", "alone", start_min=300, minutes=30)
        for row in rows(pool, user).values():
            assert row["in_expenditure"] in (True, False), row["external_id"]


class TestBoschColumns:
    def test_the_measured_figures_come_back(self, pool, user, ride):
        ride("bosch", "b", minutes=40, kcal=310, avg_rider_power_w=112,
             avg_cadence_rpm=68, motor_energy_share=0.667)
        got = rows(pool, user)["b"]
        assert got["avg_rider_power_w"] == 112.0
        assert got["avg_cadence_rpm"] == 68.0
        assert got["motor_energy_share"] == pytest.approx(0.667)
        assert got["source"] == "bosch"

    def test_a_garmin_row_has_none_of_them(self, pool, user, ride):
        ride("garmin", "g", minutes=40, kcal=540)
        got = rows(pool, user)["g"]
        assert got["avg_rider_power_w"] is None
        assert got["motor_energy_share"] is None


class TestIsolation:
    def test_a_ride_never_matches_another_accounts(
            self, pool, user, other_user, ride):
        """The join does not filter by user id — row-level security has already
        narrowed the cursor. This asserts that is actually true, because a
        cross-account match would leak one rider's schedule to another."""
        ride("garmin", "mine", start_min=0, minutes=40)
        with pool.user_tx(other_user) as cur:
            cur.execute("""INSERT INTO diet.activities
                (user_id, source, external_id, started_at, activity_type, duration_s)
                VALUES (diet.current_user_id(), 'bosch', 'theirs', %s,
                        'ebike_ride', 2340)""", (T + dt.timedelta(minutes=1),))
        assert rows(pool, user)["mine"]["matched_external_id"] is None
        with pool.user_tx(other_user) as cur:
            theirs = queries.activities_on(cur, DAY)
        assert [a["external_id"] for a in theirs] == ["theirs"]
        assert theirs[0]["matched_external_id"] is None


def test_the_energy_view_still_ignores_activities_entirely(pool, user, ride):
    """The invariant this whole feature had to not break: adding a Bosch ride,
    counted nowhere else, must not quietly change the day's balance."""
    with pool.user_tx(user) as cur:
        cur.execute("""INSERT INTO diet.measurements
            (user_id, ts_utc, source, metric, value, unit, external_id)
            VALUES (diet.current_user_id(), %s, 'garmin', 'total_kcal', 2400,
                    'kcal', %s)""", (T, DAY.isoformat()))
    with pool.user_tx(user) as cur:
        before = queries.days(cur, "UTC", 3)
    ride("bosch", "b", minutes=40, kcal=310, avg_rider_power_w=112)
    with pool.user_tx(user) as cur:
        after = queries.days(cur, "UTC", 3)
    assert before == after, "a Bosch ride must not move the energy figures"
