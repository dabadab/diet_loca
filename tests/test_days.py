"""
The day view and the figures the summary is built from.

Most of the bugs this project has actually shipped lived here, and they were
all the same shape: a figure averaged over one set of days and compared against
a total over a different set. So these tests assert on arithmetic over a known
fixture rather than on "a number came back".
"""

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from app import queries, writes

TZ = "Europe/Budapest"


@pytest.fixture
def today():
    return dt.datetime.now(ZoneInfo(TZ)).date()


@pytest.fixture
def log(pool, user):
    """Record a day: a meal of `kcal`, and/or Garmin's expenditure figures."""
    def _log(day, *, kcal=None, protein=None, total=None, active=None, weight=None):
        with pool.user_tx(user) as cur:
            if kcal is not None:
                # Macros chosen to satisfy the meal_items plausibility CHECK.
                p = protein if protein is not None else round(kcal * 0.2 / 4, 1)
                fat = round(kcal * 0.3 / 9, 1)
                carb = round((kcal - p * 4 - fat * 9) / 4, 1)
                writes.log_meal(
                    cur, description="lunch",
                    # Timezone-aware on purpose: a naive timestamp would be read
                    # in the session's zone and could land on the wrong day.
                    eaten_at=dt.datetime.combine(day, dt.time(13, 0),
                                                 tzinfo=ZoneInfo(TZ)),
                    items=[{"food": "food", "grams": 500, "kcal": kcal,
                            "protein_g": p, "carb_g": carb, "fat_g": fat,
                            "confidence": 0.8}])
            for metric, value in (("total_kcal", total), ("active_kcal", active),
                                  ("weight_kg", weight)):
                if value is None:
                    continue
                cur.execute(
                    """INSERT INTO diet.measurements
                       (user_id, ts_utc, source, metric, value, unit, external_id)
                       VALUES (diet.current_user_id(), %s, 'garmin', %s, %s, 'u', %s)
                       ON CONFLICT (user_id, source, metric, external_id)
                       DO UPDATE SET value = EXCLUDED.value""",
                    (dt.datetime.combine(day, dt.time(12, 0), tzinfo=ZoneInfo(TZ)),
                     metric, value, day.isoformat()))
    return _log


def day_rows(pool, user, n=30):
    with pool.user_tx(user) as cur:
        return {r["date"]: r for r in queries.days(cur, TZ, n)}


class TestEnergyOutIsPartial:
    """'Partial' means the figure is not final, and that is the gate on the
    page's projection. It was once defined as "only the active portion is
    known", which is false all day for today: Garmin reports a total from the
    morning onwards and accrues passive burn into it as the hours pass."""

    def test_today_is_partial_even_with_a_total(self, pool, user, log, today):
        log(today, total=780, active=120)
        assert day_rows(pool, user)[today.isoformat()]["energy_out_partial"] is True

    def test_a_finished_day_with_a_total_is_not_partial(self, pool, user, log, today):
        day = today - dt.timedelta(days=2)
        log(day, total=2400, active=400)
        assert day_rows(pool, user)[day.isoformat()]["energy_out_partial"] is False

    def test_a_finished_day_with_only_active_is_partial(self, pool, user, log, today):
        day = today - dt.timedelta(days=2)
        log(day, active=400)
        assert day_rows(pool, user)[day.isoformat()]["energy_out_partial"] is True

    def test_a_day_with_no_expenditure_at_all_is_not_partial(
            self, pool, user, log, today):
        """Otherwise the page would project a whole day's passive burn from
        nothing at all."""
        day = today - dt.timedelta(days=2)
        log(day, kcal=2000)
        row = day_rows(pool, user)[day.isoformat()]
        assert row["energy_out_kcal"] is None
        assert row["energy_out_partial"] is False


def test_a_window_of_entirely_empty_days_reports_nothing_rather_than_dashes(
        pool, user, today, log):
    """So the page can show its "nothing recorded yet" state instead of a wall
    of empty rows. A target alone is not data."""
    with pool.user_tx(user) as cur:
        writes.set_target(cur, effective_from=today, kcal=1800, protein_g=150)
    with pool.user_tx(user) as cur:
        assert queries.days(cur, TZ, 7) == []
    log(today - dt.timedelta(days=1), kcal=2000)
    with pool.user_tx(user) as cur:
        assert len(queries.days(cur, TZ, 7)) == 7


class TestTargets:
    """Effective-dated: a target applies from its date until a later one
    supersedes it, so setting one retroactively re-scores exactly the days it
    should and no others."""

    def test_the_target_in_force_is_resolved_per_day(self, pool, user, log, today):
        for i in range(1, 27):
            log(today - dt.timedelta(days=i), kcal=2000)
        with pool.user_tx(user) as cur:
            writes.set_target(cur, effective_from=today - dt.timedelta(days=20),
                              kcal=2500, protein_g=120, note="maintenance")
            writes.set_target(cur, effective_from=today - dt.timedelta(days=5),
                              kcal=1800, protein_g=150, note="cut")
        rows = day_rows(pool, user)
        assert rows[(today - dt.timedelta(days=25)).isoformat()]["target_kcal"] is None
        assert rows[(today - dt.timedelta(days=10)).isoformat()]["target_kcal"] == 2500
        assert rows[(today - dt.timedelta(days=1)).isoformat()]["target_kcal"] == 1800

    def test_days_before_the_first_target_have_none(self, pool, user, log, today):
        """A day before any target was set was not a missed target, and must not
        inherit one backwards."""
        for i in range(1, 5):
            log(today - dt.timedelta(days=i), kcal=2000)
        with pool.user_tx(user) as cur:
            writes.set_target(cur, effective_from=today, kcal=1800, protein_g=150)
        row = day_rows(pool, user)[(today - dt.timedelta(days=3)).isoformat()]
        assert row["target_kcal"] is None

    def test_an_omitted_field_is_inherited_not_defaulted(self, pool, user, today):
        with pool.user_tx(user) as cur:
            writes.set_target(cur, effective_from=today - dt.timedelta(days=5),
                              kcal=1800, protein_g=150)
            got = writes.set_target(cur, effective_from=today, protein_g=170)
        assert got["kcal"] == 1800, "raising protein should not restate calories"
        assert got["protein_g"] == 170.0

    def test_setting_the_same_date_replaces_rather_than_stacks(
            self, pool, user, today):
        with pool.user_tx(user) as cur:
            writes.set_target(cur, effective_from=today, kcal=1800, protein_g=150)
            writes.set_target(cur, effective_from=today, kcal=1900, protein_g=150)
            assert len(queries.targets_list(cur)) == 1
            assert queries.target_on(cur, today)["kcal"] == 1900

    def test_clearing_only_matches_the_exact_date(self, pool, user, today):
        """Deleting by proximity would silently shift the whole timeline after
        it -- the opposite of what correcting a mistyped date wants."""
        with pool.user_tx(user) as cur:
            writes.set_target(cur, effective_from=today, kcal=1800, protein_g=150)
            assert writes.clear_target(cur, effective_from=today - dt.timedelta(1)) is False
            assert writes.clear_target(cur, effective_from=today) is True


class TestIgnoredDays:
    def test_an_ignored_day_keeps_its_figures(self, pool, user, log, today):
        """Nothing is deleted or hidden; the day is only left out of averages."""
        day = today - dt.timedelta(days=2)
        log(day, kcal=5000, total=2400, active=400)
        with pool.user_tx(user) as cur:
            writes.ignore_day(cur, local_date=day, reason="food poisoning")
        row = day_rows(pool, user)[day.isoformat()]
        assert row["ignored"] is True
        assert row["ignored_reason"] == "food poisoning"
        assert row["energy_in_kcal"] == 5000, "the day's own figures stay real"

    def test_re_ignoring_replaces_the_reason(self, pool, user, today):
        day = today - dt.timedelta(days=2)
        with pool.user_tx(user) as cur:
            writes.ignore_day(cur, local_date=day, reason="first")
            writes.ignore_day(cur, local_date=day, reason="second")
            listed = queries.ignored_days(cur)
        assert [d["reason"] for d in listed] == ["second"]

    def test_unignoring_reports_whether_it_did_anything(self, pool, user, today):
        day = today - dt.timedelta(days=2)
        with pool.user_tx(user) as cur:
            writes.ignore_day(cur, local_date=day, reason="x")
            assert writes.unignore_day(cur, local_date=day) is True
            assert writes.unignore_day(cur, local_date=day) is False

    def test_ignoring_moves_the_average_it_was_dragging(
            self, pool, user, log, today):
        """The point of the feature, stated as arithmetic."""
        for i in range(1, 8):
            log(today - dt.timedelta(days=i), kcal=5000 if i == 3 else 2000)
        counted = lambda rows: [r for r in rows.values()
                                if r["energy_in_kcal"] is not None and not r["ignored"]]
        before = counted(day_rows(pool, user))
        avg_before = sum(r["energy_in_kcal"] for r in before) / len(before)
        with pool.user_tx(user) as cur:
            writes.ignore_day(cur, local_date=today - dt.timedelta(days=3),
                              reason="blowout")
        after = counted(day_rows(pool, user))
        avg_after = sum(r["energy_in_kcal"] for r in after) / len(after)
        assert round(avg_before) == 2429 and round(avg_after) == 2000


class TestActivitiesDoNotTouchEnergy:
    def test_the_day_view_is_identical_with_and_without_activities(
            self, pool, user, log, today):
        """Activity calories are already inside the day's active_kcal. Summing
        them into expenditure would double-count exactly the days with training
        on them, so the energy view must not reference the table at all."""
        from app import garmin, poller
        day = today - dt.timedelta(days=2)
        log(day, kcal=2000, total=2400, active=900)
        before = day_rows(pool, user)[day.isoformat()]
        poller._write_activities(user, [garmin.extract_activity({
            "activityId": 1, "activityType": {"typeKey": "running"},
            "startTimeGMT": f"{day.isoformat()} 10:00:00",
            "duration": 3600.0, "calories": 742.0})])
        after = day_rows(pool, user)[day.isoformat()]
        assert before == after

    def test_day_detail_lists_them_without_counting_them(
            self, pool, user, log, today):
        from app import garmin, poller
        day = today - dt.timedelta(days=2)
        log(day, kcal=2000, total=2400, active=900)
        poller._write_activities(user, [garmin.extract_activity({
            "activityId": 1, "activityType": {"typeKey": "running"},
            "startTimeGMT": f"{day.isoformat()} 10:00:00",
            "duration": 3600.0, "calories": 742.0})])
        with pool.user_tx(user) as cur:
            detail = queries.day_detail(cur, day)
        assert len(detail["activities"]) == 1
        assert detail["energy_in_kcal"] == 2000, "intake is meals, never activities"


class TestIsolation:
    """No read in queries.py filters by user id -- row-level security has
    already narrowed the cursor. These assert that is actually true."""

    def test_days_and_details_see_only_their_own(
            self, pool, user, other_user, log, today):
        day = today - dt.timedelta(days=1)
        log(day, kcal=2000, total=2400, active=400)
        with pool.user_tx(user) as cur:
            writes.ignore_day(cur, local_date=day, reason="x")
            writes.set_target(cur, effective_from=day, kcal=1800, protein_g=150)
        with pool.user_tx(other_user) as cur:
            assert queries.days(cur, TZ, 7) == []
            assert queries.ignored_days(cur) == []
            assert queries.targets_list(cur) == []
            assert queries.target_on(cur, day) is None
            detail = queries.day_detail(cur, day)
            assert detail["meals"] == [] and detail["measurements"] == []
