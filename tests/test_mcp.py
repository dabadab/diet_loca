"""
The MCP tool surface, over HTTP with a real bearer token.

The property worth defending here is that **no tool takes a user id**. A
confused client -- or a model writing through a tool -- has no argument by
which to name anyone but the account it authenticated as. Several of these
tests exist only to assert that the absence stays an absence.
"""

import asyncio
import datetime as dt
from zoneinfo import ZoneInfo

import pytest
from fastmcp import Client

TZ = "Europe/Budapest"

MEAL = [{"food": "sourdough bread", "grams": 80, "kcal": 208,
         "protein_g": 8.0, "carb_g": 40.0, "fat_g": 1.6, "confidence": 0.8},
        {"food": "butter", "grams": 15, "kcal": 108,
         "protein_g": 0.1, "carb_g": 0.0, "fat_g": 12.2, "confidence": 0.6}]


def run(coro):
    return asyncio.run(coro)


def payload(result):
    return result.data if getattr(result, "data", None) is not None else result.content


@pytest.fixture
def mcp(server, user, issue_token):
    """Call a tool as the first user. Returns its result, or raises."""
    token = issue_token(user)

    async def _call(name, **kwargs):
        async with Client(f"{server}/mcp", auth=token) as c:
            return payload(await c.call_tool(name, kwargs))
    return lambda name, **kw: run(_call(name, **kw))


@pytest.fixture
def mcp_other(server, other_user, issue_token):
    token = issue_token(other_user)

    async def _call(name, **kwargs):
        async with Client(f"{server}/mcp", auth=token) as c:
            return payload(await c.call_tool(name, kwargs))
    return lambda name, **kw: run(_call(name, **kw))


@pytest.fixture
def today():
    return dt.datetime.now(ZoneInfo(TZ)).date()


def test_the_published_tool_surface(server, user, issue_token):
    """If a tool appears or vanishes, that is a deliberate decision and this
    test should be the thing that makes you say so."""
    token = issue_token(user)

    async def go():
        async with Client(f"{server}/mcp", auth=token) as c:
            return sorted(t.name for t in await c.list_tools())
    assert run(go()) == [
        "clear_target", "correct_meal", "get_activities", "get_day",
        "get_ignored_days", "get_range", "get_targets", "ignore_day",
        "log_meal", "query_sql", "set_target", "unignore_day",
    ]


def test_no_tool_accepts_a_user_id(server, user, issue_token):
    """The identity boundary, asserted on the published schema rather than by
    reading the code."""
    token = issue_token(user)

    async def go():
        async with Client(f"{server}/mcp", auth=token) as c:
            return {t.name: (t.input_schema or {}).get("properties", {})
                    for t in await c.list_tools()}
    for name, props in run(go()).items():
        offending = [p for p in props
                     if p in ("user_id", "user", "email", "account", "subject")]
        assert not offending, f"{name} exposes {offending}"


class TestAuth:
    def test_an_unknown_token_is_refused(self, server):
        async def go():
            async with Client(f"{server}/mcp", auth="not-a-real-token") as c:
                await c.list_tools()
        with pytest.raises(Exception):
            run(go())

    def test_a_revoked_token_stops_working(self, server, user, issue_token, owner):
        token = issue_token(user)

        async def go():
            async with Client(f"{server}/mcp", auth=token) as c:
                return await c.list_tools()
        assert run(go())
        owner.execute("UPDATE auth.api_tokens SET revoked_at = now()")
        with pytest.raises(Exception):
            run(go())


class TestMeals:
    def test_logging_a_meal_and_reading_it_back(self, mcp):
        meal = mcp("log_meal", description="toast and butter", items=MEAL)
        assert meal["items"] == 2 and meal["total_kcal"] == 316.0
        day = mcp("get_day")
        assert [m["meal_id"] for m in day["meals"]] == [meal["meal_id"]]
        assert day["energy_in_kcal"] == 316

    def test_a_correction_supersedes_rather_than_stacks(self, mcp):
        """Corrections are append-only: the original survives for comparison,
        and only the current version is shown."""
        first = mcp("log_meal", description="two slices", items=MEAL)
        fixed = mcp("correct_meal", meal_id=first["meal_id"],
                    description="actually three slices",
                    items=[dict(MEAL[0], grams=120, kcal=312, protein_g=12.0,
                                carb_g=60.0, fat_g=2.4)])
        assert fixed["supersedes"] == first["meal_id"]
        ids = [m["meal_id"] for m in mcp("get_day")["meals"]]
        assert ids == [fixed["meal_id"]]

    def test_correcting_an_already_corrected_meal_is_refused(self, mcp):
        """Pointing at a superseded meal means the caller is working from a
        stale read; silently applying it would lose the newer version."""
        first = mcp("log_meal", description="x", items=MEAL)
        mcp("correct_meal", meal_id=first["meal_id"], description="y", items=MEAL)
        with pytest.raises(Exception, match="already"):
            mcp("correct_meal", meal_id=first["meal_id"], description="z", items=MEAL)

    def test_the_schema_still_refuses_implausible_model_output(self, mcp):
        with pytest.raises(Exception, match="kcal"):
            mcp("log_meal", description="x",
                items=[{"food": "x", "grams": 100, "kcal": 99999,
                        "protein_g": 1, "carb_g": 1, "fat_g": 1, "confidence": 0.5}])

    def test_macros_that_do_not_add_up_are_refused(self, mcp):
        with pytest.raises(Exception):
            mcp("log_meal", description="x",
                items=[{"food": "x", "grams": 100, "kcal": 5000,
                        "protein_g": 1, "carb_g": 1, "fat_g": 1, "confidence": 0.5}])


class TestTargets:
    def test_set_get_and_clear(self, mcp, today):
        mcp("set_target", effective_from=today.isoformat(), kcal=1800,
            protein_g=150, note="start of cut")
        got = mcp("get_targets")
        assert got["in_force"]["kcal"] == 1800
        assert got["in_force"]["note"] == "start of cut"
        mcp("clear_target", effective_from=today.isoformat())
        assert mcp("get_targets")["in_force"] is None

    def test_clearing_a_date_with_no_target_is_refused(self, mcp, today):
        with pytest.raises(Exception):
            mcp("clear_target", effective_from=today.isoformat())

    def test_a_field_left_out_is_inherited(self, mcp, today):
        mcp("set_target", effective_from=(today - dt.timedelta(days=5)).isoformat(),
            kcal=1800, protein_g=150)
        got = mcp("set_target", effective_from=today.isoformat(), protein_g=170)
        assert got["kcal"] == 1800


class TestIgnoredDays:
    def test_ignore_list_and_undo(self, mcp, today):
        day = (today - dt.timedelta(days=2)).isoformat()
        assert mcp("ignore_day", date=day, reason="food poisoning")["reason"] \
            == "food poisoning"
        assert [d["date"] for d in mcp("get_ignored_days")["ignored"]] == [day]
        assert mcp("get_day", date=day)["ignored"] is True
        mcp("unignore_day", date=day)
        assert mcp("get_ignored_days")["ignored"] == []

    def test_a_reason_is_required(self, mcp, today):
        with pytest.raises(Exception, match="reason"):
            mcp("ignore_day", date=today.isoformat(), reason="   ")

    def test_unignoring_a_day_that_was_not_ignored_is_refused(self, mcp, today):
        """Rather than reporting success for a change it did not make."""
        with pytest.raises(Exception, match="not being ignored"):
            mcp("unignore_day", date=today.isoformat())


class TestDates:
    def test_a_date_that_is_not_a_date_is_refused(self, mcp):
        with pytest.raises(Exception, match="YYYY-MM-DD"):
            mcp("get_day", date="last tuesday")

    def test_an_inverted_range_is_refused(self, mcp):
        with pytest.raises(Exception, match="after"):
            mcp("get_activities", from_date="2026-09-30", to_date="2026-09-01")

    def test_an_unknown_metric_names_the_known_ones(self, mcp):
        with pytest.raises(Exception, match="weight_kg"):
            mcp("get_range", metrics=["vibes"])


class TestQuerySql:
    def test_it_reads_the_users_own_rows(self, mcp):
        mcp("log_meal", description="x", items=MEAL)
        got = mcp("query_sql", sql="SELECT count(*) AS n FROM diet.meals")
        assert got["rows"] == [{"n": 1}]

    @pytest.mark.parametrize("sql, because", [
        ("DELETE FROM diet.meals", "not a SELECT"),
        ("WITH x AS (UPDATE diet.meals SET description = 'o' RETURNING 1) "
         "SELECT * FROM x", "a write dressed as a CTE"),
        ("SELECT 1; SELECT 2", "two statements"),
        ("SELECT * FROM auth.users", "the identity schema"),
        ("SELECT * FROM diet.garmin_credentials", "the credential table"),
    ])
    def test_it_refuses(self, mcp, sql, because):
        with pytest.raises(Exception):
            mcp("query_sql", sql=sql)

    def test_the_read_only_transaction_is_what_stops_a_cte_write(self, mcp):
        """The SELECT-prefix check alone is not sufficient, and this is the case
        that proves it: the statement passes the prefix test and is stopped by
        the transaction instead."""
        with pytest.raises(Exception, match="read-only"):
            mcp("query_sql",
                sql="WITH x AS (UPDATE diet.meals SET description = 'o' RETURNING 1) "
                    "SELECT * FROM x")


class TestIsolation:
    def test_one_account_cannot_see_anothers_meals(self, mcp, mcp_other):
        mcp("log_meal", description="mine", items=MEAL)
        assert mcp_other("get_day")["meals"] == []
        assert mcp_other("query_sql",
                         sql="SELECT count(*) AS n FROM diet.meals")["rows"] == [{"n": 0}]

    def test_one_account_cannot_correct_anothers_meal(self, mcp, mcp_other):
        mine = mcp("log_meal", description="mine", items=MEAL)
        with pytest.raises(Exception, match="no meal"):
            mcp_other("correct_meal", meal_id=mine["meal_id"], description="theirs")

    def test_one_account_cannot_unignore_anothers_day(self, mcp, mcp_other, today):
        day = (today - dt.timedelta(days=1)).isoformat()
        mcp("ignore_day", date=day, reason="mine")
        with pytest.raises(Exception, match="not being ignored"):
            mcp_other("unignore_day", date=day)
        assert mcp("get_day", date=day)["ignored"] is True
