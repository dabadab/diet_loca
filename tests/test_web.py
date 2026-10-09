"""
The page's own arithmetic, run in node against web/index.html.

The browser decides several numbers the server never sees -- which days the
summary counts, what today's projected expenditure is -- and every one of those
has been wrong at some point in a way the backend tests could not have caught.
The harness lifts the real functions out of the page rather than reimplementing
them, so a change to the page changes what these tests run.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HARNESS = Path(__file__).resolve().parent / "web_harness.js"
PAGE = REPO / "web" / "index.html"

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node is not installed")


def page(state: dict, calls: dict) -> dict:
    """Evaluate page functions against a given state. calls maps label -> [fn, *args]."""
    done = subprocess.run(
        ["node", str(HARNESS), str(PAGE)],
        input=json.dumps({"state": state, "calls": calls}),
        text=True, capture_output=True)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def day(date, **over):
    """A day row shaped like /api/days returns one."""
    base = {"date": date, "energy_in_kcal": 2000, "protein_g": 100.0,
            "carb_g": 250.0, "fat_g": 67.0, "energy_out_kcal": 2400,
            "energy_out_partial": False, "active_kcal": 400, "total_kcal": 2400,
            "weight_kg": None, "target_kcal": 1800, "target_protein_g": 150.0,
            "meals_logged": 1, "ignored": False, "ignored_reason": None,
            "intake_source": "claude-estimate"}
    return {**base, **over}


# Newest first, as the page receives them.
DAYS = [day(f"2026-09-{d:02d}") for d in range(20, 9, -1)]
TARGET = {"effective_from": "2026-09-14", "kcal": 1800, "protein_g": 150.0,
          "note": None}


class TestWhichDaysCount:
    """One definition decides this, used by both the Summary tab and the day
    list's dimming. A dimmed row that is actually being averaged in, or a
    normal-looking row that is not, is worse than no marking at all."""

    def _reasons(self, days, target=None):
        state = {"days": days, "target": target}
        calls = {d["date"]: ["uncountedReason", d] for d in days}
        return page(state, calls)

    def test_today_is_never_counted(self):
        """It is a partial day: dinner is not eaten and Garmin has not finished
        accumulating. Averaging it in flatters every figure."""
        got = self._reasons(DAYS)
        assert "today" in got["2026-09-20"]
        assert got["2026-09-19"] is None

    def test_a_day_with_nothing_recorded_is_not_counted(self):
        days = [DAYS[0], day("2026-09-19", meals_logged=0, energy_in_kcal=None,
                             energy_out_kcal=None)]
        assert self._reasons(days)["2026-09-19"] == "nothing recorded"

    def test_an_ignored_day_reports_its_own_reason(self):
        days = [DAYS[0], day("2026-09-19", ignored=True,
                             ignored_reason="food poisoning")]
        assert self._reasons(days)["2026-09-19"] == "food poisoning"

    def test_days_before_the_goal_are_not_counted(self):
        """The summary is about the goal in force. Scoring a cut against the
        maintenance weeks before it compares two different intentions."""
        got = self._reasons(DAYS, TARGET)
        assert got["2026-09-14"] is None, "the goal's first day counts"
        assert "before the current goal" in got["2026-09-13"]

    def test_with_no_goal_set_nothing_is_excluded_for_that_reason(self):
        got = self._reasons(DAYS, None)
        assert all(r is None for d, r in got.items() if d != "2026-09-20")


class TestExclusionAccounting:
    """The header used to read "31 days · 31 with meals · today excluded · 7
    ignored", which left it unclear whether the seven were inside the
    thirty-one -- and they were counted over the whole fetched window rather
    than over the days that were ever candidates, so neither reading added up.
    These assert the arithmetic now closes."""

    def _state(self, days, target=None):
        return {"days": days, "target": target}

    def test_counted_and_excluded_account_for_every_day(self):
        days = [day("2026-09-20"),                                     # today
                *[day(f"2026-09-{d:02d}", ignored=True,
                      ignored_reason="travel") for d in (19, 18)],
                day("2026-09-17", meals_logged=0, energy_in_kcal=None,
                    energy_out_kcal=None),
                *[day(f"2026-09-{d:02d}") for d in (16, 15, 14)]]
        got = page(self._state(days),
                   {"summary": ["uncountedSummary"],
                    "stats": ["statsFor", [d for d in days]]})
        assert got["summary"] == "2 ignored · 1 with nothing recorded · today"
        # 3 counted + 2 ignored + 1 empty + today == the whole window
        counted = page(self._state(days),
                       {k: ["uncountedKind", d] for k, d in
                        ((d["date"], d) for d in days)})
        assert sum(1 for v in counted.values() if v is None) == 3
        assert len(days) == 3 + 2 + 1 + 1

    def test_pregoal_days_are_accounted_for_too(self):
        days = [day("2026-09-20"), day("2026-09-19"), day("2026-09-13")]
        target = {"effective_from": "2026-09-14", "kcal": 1800,
                  "protein_g": 150.0, "note": None}
        got = page(self._state(days, target), {"s": ["uncountedSummary"]})["s"]
        assert got == "1 before the goal · today"

    def test_today_is_never_given_a_count(self):
        """It is always exactly one day; "1 today" would read as a quantity."""
        days = [day("2026-09-20"), day("2026-09-19")]
        assert page(self._state(days), {"s": ["uncountedSummary"]})["s"] == "today"

    def test_an_ignored_day_that_was_never_a_candidate_is_not_double_counted(self):
        """A day before the goal that is also ignored is reported once, under a
        single reason -- otherwise the parts sum to more than the window."""
        days = [day("2026-09-20"), day("2026-09-19"),
                day("2026-09-13", ignored=True, ignored_reason="travel")]
        target = {"effective_from": "2026-09-14", "kcal": 1800,
                  "protein_g": 150.0, "note": None}
        got = page(self._state(days, target), {"s": ["uncountedSummary"]})["s"]
        assert got == "1 ignored · today", got


class TestTodaysProjection:
    """Garmin's figure for today accrues through the day, so the raw number
    reads as an enormous surplus all morning. The page projects instead: a
    whole day's passive burn plus the activity done so far."""

    def _out(self, today_row, history=DAYS[1:]):
        state = {"days": [today_row, *history], "target": None}
        return page(state, {"out": ["outFor", today_row],
                            "passive": ["passiveAverage"]})

    def test_passive_is_averaged_from_finished_days(self):
        got = self._out(day("2026-09-20", energy_out_partial=True,
                            total_kcal=780, active_kcal=120, energy_out_kcal=780))
        assert got["passive"] == 2000, "total 2400 less active 400"

    def test_today_is_projected_not_reported(self):
        got = self._out(day("2026-09-20", energy_out_partial=True,
                            total_kcal=780, active_kcal=120, energy_out_kcal=780))
        assert got["out"]["out"] == 2120, "passive 2000 plus the 120 done so far"
        assert "passive" in got["out"]["why"]
        assert got["out"]["firm"] is True

    def test_a_finished_day_passes_straight_through(self):
        state = {"days": DAYS, "target": None}
        got = page(state, {"out": ["outFor", DAYS[1]]})["out"]
        assert got == {"out": 2400, "why": None, "firm": True}

    def test_with_nothing_to_project_from_the_balance_is_withheld(self):
        """On an account with no complete day yet there is no passive average.
        Drawing a balance against a third of a day's burn would show a large
        invented surplus, so the figure is shown and the balance is not."""
        only_today = day("2026-09-20", energy_out_partial=True,
                         total_kcal=780, active_kcal=120, energy_out_kcal=780)
        got = page({"days": [only_today], "target": None},
                   {"out": ["outFor", only_today]})["out"]
        assert got["out"] == 780 and got["why"] is None
        assert got["firm"] is False, "a balance here would be fiction"


class TestSummaryArithmetic:
    """Every bug this file exists to prevent was the same shape: a figure summed
    over one set of days and compared against a total over a different set."""

    def test_totals_cover_only_the_days_that_carry_each_figure(self):
        days = [day("2026-09-19"),
                day("2026-09-18", energy_in_kcal=None, meals_logged=0),
                day("2026-09-17", energy_out_kcal=None, total_kcal=None,
                    active_kcal=None)]
        st = page({"days": days, "target": None}, {"s": ["statsFor", days]})["s"]
        assert st["intakeDays"] == 2 and st["kcal"] == 4000
        assert st["outDays"] == 2 and st["out"] == 4800
        # Only the days holding both figures can contribute a balance.
        assert st["bothDays"] == 1 and st["balance"] == 2000 - 2400

    def test_targets_are_summed_per_day_not_multiplied(self):
        """Multiplying today's target by the day count is what made a past week
        look like a failure the moment the goal moved."""
        days = [day("2026-09-19", target_kcal=1800),
                day("2026-09-18", target_kcal=2500)]
        st = page({"days": days, "target": None}, {"s": ["statsFor", days]})["s"]
        assert st["targetKcal"] == 4300

    def test_both_sides_of_the_bar_cover_the_same_days(self):
        """Summing intake over every logged day while summing targets over only
        the days that had one compares different sets and guarantees the bar
        reads as over."""
        days = [day("2026-09-19", target_kcal=1800),
                day("2026-09-18", target_kcal=None, target_protein_g=None)]
        st = page({"days": days, "target": None}, {"s": ["statsFor", days]})["s"]
        assert st["targetDays"] == 1
        assert st["scoredKcal"] == 2000, "intake restricted to scorable days"
        assert st["targetKcal"] == 1800


class TestDayList:
    def test_an_uncounted_day_is_marked_and_explains_itself(self):
        days = [DAYS[0], day("2026-09-19", ignored=True, ignored_reason="travel"),
                *DAYS[2:]]
        html = page({"days": days, "target": None, "activeDate": "2026-09-18"},
                    {"h": ["dayListCard"]})["h"]
        row = [r for r in html.split("<tr ") if "2026-09-19" in r][0]
        assert "uncounted" in row
        assert "not in the summary: travel" in row
        assert "✕" in row, "a deliberate exclusion should stay distinguishable"

    def test_a_counted_day_is_not_marked(self):
        html = page({"days": DAYS, "target": None, "activeDate": "2026-09-18"},
                    {"h": ["dayListCard"]})["h"]
        row = [r for r in html.split("<tr ") if "2026-09-18" in r][0]
        assert "uncounted" not in row and "✕" not in row

    def test_todays_row_shows_the_projection_the_day_card_shows(self):
        """They go through one outFor(), so the list and the card cannot report
        different balances for the same day."""
        today = day("2026-09-20", energy_in_kcal=1450, energy_out_partial=True,
                    total_kcal=780, active_kcal=120, energy_out_kcal=780)
        state = {"days": [today, *DAYS[1:]], "target": None,
                 "activeDate": "2026-09-20"}
        got = page(state, {"h": ["dayListCard"], "out": ["outFor", today]})
        row = [r for r in got["h"].split("<tr ") if "2026-09-20" in r][0]
        assert "2,120" in row, "the projected out, not the raw 780"
        assert "-670" in row, "1450 - 2120, so the row ties up"
        assert str(round(got["out"]["out"])) == "2120"
