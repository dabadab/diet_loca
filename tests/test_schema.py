"""
The guarantees the schema itself makes.

These are the load-bearing ones: row-level security is the entire tenant
boundary, and the CHECK constraints are the only thing standing between a
plausible-looking wrong number -- from Garmin or from a model -- and the
permanent record. If any of these fail, nothing above them can be trusted.
"""

import psycopg
import pytest

# Every table holding user data. Adding one to diet without adding it here is
# exactly the mistake this test exists to catch.
USER_TABLES = ["measurements", "activities", "meals", "meal_items",
               "sync_state", "garmin_credentials", "targets", "ignored_days"]


def test_migration_is_idempotent(owner_dsn):
    """schema.sql doubles as the migration and is applied at every startup."""
    from app import migrate
    migrate.apply(owner_dsn, app_password="testpw", readonly_password="ropw")
    migrate.apply(owner_dsn, app_password="testpw", readonly_password="ropw")


@pytest.mark.parametrize("table", USER_TABLES)
def test_every_user_table_has_rls_enabled_and_forced(owner, table):
    """FORCE matters as much as ENABLE: without it the table owner bypasses the
    policy, and the owner role is what runs migrations and the poller."""
    row = owner.execute(
        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
        "WHERE oid = %s::regclass", (f"diet.{table}",)).fetchone()
    assert row["relrowsecurity"], f"diet.{table} has RLS off"
    assert row["relforcerowsecurity"], f"diet.{table} does not FORCE RLS"


@pytest.mark.parametrize("table", USER_TABLES)
def test_every_user_table_has_an_owning_policy(owner, table):
    rows = owner.execute("SELECT polname FROM pg_policy WHERE polrelid = %s::regclass",
                         (f"diet.{table}",)).fetchall()
    assert [r["polname"] for r in rows] == [f"{table}_own"]


def test_current_user_id_fails_closed(owner):
    """It returns NULL when the GUC is unset, so a policy keyed on it matches
    nothing rather than everything. This is why forgetting user_tx is safe."""
    assert owner.execute("SELECT diet.current_user_id() AS id").fetchone()["id"] is None


class TestReadOnlyRole:
    """diet_ro backs the model-facing query_sql tool, so its grants are that
    tool's real contract."""

    def test_cannot_see_the_identity_schema_at_all(self, dsns):
        with psycopg.connect(dsns["ro"]) as c:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute("SELECT 1 FROM auth.users")

    def test_cannot_read_garmin_credentials(self, dsns):
        """A blanket schema grant once handed these over; the tool's docstring
        never claimed them and no query through it should reach them."""
        with psycopg.connect(dsns["ro"]) as c:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute("SELECT 1 FROM diet.garmin_credentials")

    @pytest.mark.parametrize("table", [
        "meals", "meal_items", "measurements", "activities", "sync_state",
        "targets", "ignored_days"])
    def test_can_read_the_tables_the_tool_advertises(self, dsns, table):
        with psycopg.connect(dsns["ro"]) as c:
            c.execute(f"SELECT 1 FROM diet.{table} LIMIT 1")

    def test_cannot_write(self, dsns, user):
        with psycopg.connect(dsns["ro"]) as c:
            with pytest.raises(psycopg.Error):
                c.execute("DELETE FROM diet.meals")


class TestAppRole:
    def test_has_no_table_access_to_identity(self, dsns):
        """Identity is reachable only through the SECURITY DEFINER functions."""
        with psycopg.connect(dsns["app"]) as c:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute("SELECT 1 FROM auth.users")


class TestSanityConstraints:
    """A wrong-by-a-factor-of-ten reading is the failure mode worth catching in
    the schema, because neither Garmin nor a model will flag it."""

    @pytest.mark.parametrize("metric, value", [
        ("weight_kg", 8400.0),      # grams misread as kilos
        ("resting_hr", 400.0),
        ("sleep_minutes", 2000.0),  # more minutes than a day has
        ("body_fat_pct", 0.5),
        ("steps", -1.0),
    ])
    def test_an_implausible_measurement_is_refused(self, owner, user, metric, value):
        with pytest.raises(psycopg.errors.CheckViolation):
            owner.execute(
                """INSERT INTO diet.measurements
                   (user_id, ts_utc, source, metric, value, unit, external_id)
                   VALUES (%s, now(), 'garmin', %s, %s, 'x', 'e')""",
                (user, metric, value))

    def test_an_unknown_metric_is_refused(self, owner, user):
        """Adding a metric should be a deliberate migration, not a typo that
        silently creates a new series nothing reads."""
        with pytest.raises(psycopg.errors.CheckViolation):
            owner.execute(
                """INSERT INTO diet.measurements
                   (user_id, ts_utc, source, metric, value, unit, external_id)
                   VALUES (%s, now(), 'garmin', 'vibes', 5, 'x', 'e')""", (user,))

    def test_an_ignored_day_must_carry_a_reason(self, owner, user):
        """An unexplained exclusion cannot answer "why is this week different"
        later, which is the question excluding a day creates."""
        with pytest.raises(psycopg.errors.CheckViolation):
            owner.execute(
                "INSERT INTO diet.ignored_days (user_id, local_date, reason) "
                "VALUES (%s, '2026-09-10', '')", (user,))

    @pytest.mark.parametrize("kcal, protein", [(50, 150), (99999, 150), (2000, 900)])
    def test_an_implausible_target_is_refused(self, owner, user, kcal, protein):
        with pytest.raises(psycopg.errors.CheckViolation):
            owner.execute(
                "INSERT INTO diet.targets (user_id, effective_from, kcal, protein_g) "
                "VALUES (%s, '2026-09-01', %s, %s)", (user, kcal, protein))

    def test_macros_must_account_for_the_calories(self, owner, user):
        """A model will occasionally produce a confident item whose numbers do
        not add up; the schema is what catches it reliably."""
        meal = owner.execute(
            """INSERT INTO diet.meals (user_id, eaten_at, local_date, description, source)
               VALUES (%s, now(), current_date, 'lunch', 'claude-estimate')
               RETURNING meal_id""", (user,)).fetchone()["meal_id"]
        with pytest.raises(psycopg.errors.CheckViolation):
            owner.execute(
                """INSERT INTO diet.meal_items
                   (meal_id, user_id, food, grams, kcal, protein_g, carb_g, fat_g, confidence)
                   VALUES (%s, %s, 'x', 100, 5000, 1, 1, 1, 0.5)""", (meal, user))


def test_emails_are_stored_lowercase(owner, user):
    """Identity lookups go through auth.login_lookup, which lowercases its
    argument. A mixed-case row would be a user who could never sign in."""
    import psycopg
    with pytest.raises(psycopg.errors.CheckViolation):
        owner.execute(
            """INSERT INTO auth.users (email, display_name, password_hash, timezone)
               VALUES ('Mixed@Example.com', 'x', 'x', 'UTC')""")


class TestLocalDateTrigger:
    """Every row stores both the instant and the calendar day it belongs to, in
    the user's own timezone -- not a hardcoded one."""

    @pytest.mark.parametrize("tz, expected", [
        ("Europe/Budapest", "2026-09-09"),   # 23:30 UTC is already tomorrow here
        ("America/Denver", "2026-09-08"),
    ])
    def test_the_day_is_resolved_in_the_users_own_timezone(
            self, owner, make_user, tz, expected):
        uid = make_user(f"{tz.replace('/', '-').lower()}@example.com", timezone=tz)
        row = owner.execute(
            """INSERT INTO diet.measurements
               (user_id, ts_utc, source, metric, value, unit, external_id)
               VALUES (%s, '2026-09-08 23:30+00', 'garmin', 'steps', 1, 'count', 'e')
               RETURNING local_date""", (uid,)).fetchone()
        assert str(row["local_date"]) == expected
