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
               "sync_state", "garmin_credentials", "bosch_credentials",
               "targets", "ignored_days"]


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

    @pytest.mark.parametrize("table", ["garmin_credentials", "bosch_credentials"])
    def test_cannot_read_any_stored_credential(self, dsns, table):
        """A credential table is never readable through the model-facing tool,
        whichever connector it belongs to."""
        with psycopg.connect(dsns["ro"]) as c:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute(f"SELECT 1 FROM diet.{table}")

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


class TestProvenance:
    """The domain is created inside a guard on its own absence, so a changed
    list would never reach a database that already had it. The constraint is
    replaced outright every run instead -- idempotent *and* evolvable."""

    @pytest.mark.parametrize("source", ["garmin", "bosch", "claude-estimate",
                                        "manual"])
    def test_every_writer_is_accepted(self, owner, user, source):
        owner.execute(
            """INSERT INTO diet.activities
               (user_id, source, external_id, started_at, activity_type)
               VALUES (%s, %s, %s, now(), 'x')""", (user, source, source))

    def test_an_unknown_writer_is_still_refused(self, owner, user):
        with pytest.raises(psycopg.errors.CheckViolation):
            owner.execute(
                """INSERT INTO diet.activities
                   (user_id, source, external_id, started_at, activity_type)
                   VALUES (%s, 'strava', 'x', now(), 'x')""", (user,))


class TestBoschColumnsAreConstrained:
    """These columns are added by ALTER on a database that predates them, and
    ALTER COLUMN alone carries no CHECK -- a migrated database would accept
    anything while a fresh one refused it. The constraints are therefore named
    and replaced, not inline."""

    @pytest.mark.parametrize("column, value", [
        ("avg_cadence_rpm", 900),
        ("avg_rider_power_w", 9999),
        ("motor_energy_share", 1.5),
        ("motor_energy_share", -0.1),
    ])
    def test_an_implausible_value_is_refused(self, owner, user, column, value):
        with pytest.raises(psycopg.errors.CheckViolation):
            owner.execute(
                f"""INSERT INTO diet.activities
                    (user_id, source, external_id, started_at, activity_type, {column})
                    VALUES (%s, 'bosch', 'x', now(), 'ebike_ride', %s)""",
                (user, value))

    def test_plausible_values_are_accepted(self, owner, user):
        owner.execute(
            """INSERT INTO diet.activities
               (user_id, source, external_id, started_at, activity_type,
                avg_cadence_rpm, avg_rider_power_w, motor_energy_share)
               VALUES (%s, 'bosch', 'ok', now(), 'ebike_ride', 68, 112, 0.667)""",
            (user,))


class TestMigratingAnOlderDatabase:
    """
    Applying the schema to a database that predates a change is the case
    production actually takes, and the one a fresh-database test cannot see.

    Simulated by removing what the older version lacked rather than by checking
    out old SQL, so the test keeps working as the schema moves on.
    """

    def _make_it_old(self, owner):
        owner.execute("ALTER DOMAIN diet.provenance DROP CONSTRAINT provenance_check")
        owner.execute("""ALTER DOMAIN diet.provenance ADD CONSTRAINT provenance_check
                         CHECK (VALUE IN ('garmin', 'claude-estimate', 'manual'))""")
        for name in ("activities_cadence_sane", "activities_rider_power_sane",
                     "activities_motor_share_sane"):
            owner.execute(f"ALTER TABLE diet.activities DROP CONSTRAINT {name}")
        for col in ("avg_cadence_rpm", "avg_rider_power_w", "motor_energy_share"):
            owner.execute(f"ALTER TABLE diet.activities DROP COLUMN {col}")
        owner.execute("DROP TABLE diet.bosch_credentials")
        owner.execute("ALTER TABLE diet.sync_state "
                      "DROP CONSTRAINT sync_state_connector_check")
        owner.execute("""ALTER TABLE diet.sync_state ADD CONSTRAINT
                         sync_state_connector_check
                         CHECK (connector IN ('garmin', 'claude-mcp'))""")

    def _migrate(self, owner_dsn):
        from app import migrate
        migrate.apply(owner_dsn, app_password="testpw", readonly_password="ropw")

    def test_the_data_already_there_survives(self, owner, owner_dsn, user):
        self._make_it_old(owner)
        owner.execute(
            """INSERT INTO diet.activities
               (user_id, source, external_id, started_at, activity_type, kcal)
               VALUES (%s, 'garmin', 'old', '2026-10-12 06:00+00', 'cycling', 540)""",
            (user,))
        self._migrate(owner_dsn)
        row = owner.execute("SELECT source, kcal FROM diet.activities").fetchone()
        assert row["source"] == "garmin" and row["kcal"] == 540

    def test_the_new_writer_becomes_acceptable(self, owner, owner_dsn, user):
        self._make_it_old(owner)
        with pytest.raises(psycopg.errors.CheckViolation):
            owner.execute("""INSERT INTO diet.activities
                (user_id, source, external_id, started_at, activity_type)
                VALUES (%s, 'bosch', 'b', now(), 'ebike_ride')""", (user,))
        self._migrate(owner_dsn)
        owner.execute("""INSERT INTO diet.activities
            (user_id, source, external_id, started_at, activity_type)
            VALUES (%s, 'bosch', 'b', now(), 'ebike_ride')""", (user,))

    def test_the_added_columns_arrive_constrained(self, owner, owner_dsn, user):
        """ADD COLUMN carries no CHECK. Without naming and replacing the
        constraints, a migrated database would quietly accept a motor share of
        1.5 that a fresh one refuses -- which is how this was first written."""
        self._make_it_old(owner)
        self._migrate(owner_dsn)
        with pytest.raises(psycopg.errors.CheckViolation):
            owner.execute("""INSERT INTO diet.activities
                (user_id, source, external_id, started_at, activity_type,
                 motor_energy_share)
                VALUES (%s, 'bosch', 'b', now(), 'ebike_ride', 1.5)""", (user,))

    def test_the_new_credential_table_comes_up_protected(self, owner, owner_dsn, dsns):
        self._make_it_old(owner)
        self._migrate(owner_dsn)
        row = owner.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE oid = 'diet.bosch_credentials'::regclass").fetchone()
        assert row["relrowsecurity"] and row["relforcerowsecurity"]
        with psycopg.connect(dsns["ro"]) as c:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute("SELECT 1 FROM diet.bosch_credentials")

    def test_the_new_connector_can_record_its_syncs(self, owner, owner_dsn, user):
        """sync_state has its own connector list, with the same trap: an inline
        CHECK never reaches a database that already had the table."""
        self._make_it_old(owner)
        with pytest.raises(psycopg.errors.CheckViolation):
            owner.execute("INSERT INTO diet.sync_state (user_id, connector) "
                          "VALUES (%s, 'bosch')", (user,))
        self._migrate(owner_dsn)
        owner.execute("INSERT INTO diet.sync_state (user_id, connector) "
                      "VALUES (%s, 'bosch')", (user,))

    def test_migrating_twice_changes_nothing(self, owner, owner_dsn):
        self._make_it_old(owner)
        self._migrate(owner_dsn)
        self._migrate(owner_dsn)
