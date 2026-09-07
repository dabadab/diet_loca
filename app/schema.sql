-- Diet tracker schema. Applied at app startup under an advisory lock, so it
-- must stay idempotent: every statement here has to survive being re-run
-- against a database that already has it.
--
-- Two schemas, on purpose:
--   auth.*  identity. Read only through SECURITY DEFINER functions, never
--           directly by the app role, and never granted to the read-only role.
--   diet.*  user data. Row-level security on every table, keyed on the
--           app.user_id GUC that the transaction wrapper sets.

CREATE SCHEMA IF NOT EXISTS auth;
CREATE SCHEMA IF NOT EXISTS diet;

-- ---------------------------------------------------------------- roles ---
-- Passwords arrive as session GUCs (diet.app_password / diet.ro_password) set
-- by the migration runner from the environment, so they are never literals in
-- this file and re-running keeps the roles in step with .env.

DO $roles$
DECLARE
  app_pw text := current_setting('diet.app_password', true);
  ro_pw  text := current_setting('diet.ro_password', true);
BEGIN
  IF app_pw IS NULL OR app_pw = '' THEN
    RAISE EXCEPTION 'diet.app_password is not set; migration runner must set it';
  END IF;

  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'diet_app') THEN
    EXECUTE format('ALTER ROLE diet_app WITH LOGIN PASSWORD %L', app_pw);
  ELSE
    EXECUTE format('CREATE ROLE diet_app LOGIN PASSWORD %L', app_pw);
  END IF;

  -- Read-only role for the future MCP query_sql tool. RLS still applies to it;
  -- it simply cannot write, and cannot see the auth schema at all.
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'diet_ro') THEN
    IF ro_pw IS NOT NULL AND ro_pw <> '' THEN
      EXECUTE format('ALTER ROLE diet_ro WITH LOGIN PASSWORD %L', ro_pw);
    END IF;
  ELSIF ro_pw IS NOT NULL AND ro_pw <> '' THEN
    EXECUTE format('CREATE ROLE diet_ro LOGIN PASSWORD %L', ro_pw);
  ELSE
    -- Created without login so the grants below have somewhere to land.
    CREATE ROLE diet_ro NOLOGIN;
  END IF;
END
$roles$;

-- Belt and braces on the read-only role: even a psql session as diet_ro cannot
-- open a writing transaction, and no single query can run away.
ALTER ROLE diet_ro SET default_transaction_read_only = on;
ALTER ROLE diet_ro SET statement_timeout = '15s';
ALTER ROLE diet_ro SET idle_in_transaction_session_timeout = '30s';

-- ------------------------------------------------------------- identity ---

CREATE OR REPLACE FUNCTION diet.current_user_id() RETURNS uuid
  LANGUAGE sql STABLE
  SET search_path = pg_catalog
  AS $fn$
    -- Unset -> NULL -> every policy below is false. Fails closed.
    SELECT nullif(current_setting('app.user_id', true), '')::uuid
  $fn$;

COMMENT ON FUNCTION diet.current_user_id() IS
  'Identity for RLS. STABLE so the planner folds it into index scans.';

-- ------------------------------------------------------------ provenance ---
-- One place to extend when a new writer appears.

DO $dom$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
    WHERE t.typname = 'provenance' AND n.nspname = 'diet'
  ) THEN
    CREATE DOMAIN diet.provenance AS text
      CHECK (VALUE IN ('garmin', 'claude-estimate', 'manual'));
  END IF;
END $dom$;

-- ----------------------------------------------------------- auth.users ---

CREATE TABLE IF NOT EXISTS auth.users (
  user_id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  email         text NOT NULL UNIQUE,          -- stored lowercased by the app
  display_name  text NOT NULL CHECK (length(display_name) BETWEEN 1 AND 100),
  timezone      text NOT NULL DEFAULT 'UTC',
  password_hash text NOT NULL,
  is_active     boolean NOT NULL DEFAULT true,
  created_at    timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT users_email_lowercase CHECK (email = lower(email))
);

-- A CHECK cannot hold a subquery, so the timezone is validated by trigger.
-- It has to be validated *somewhere*: local_date is derived from it, and a
-- typo'd zone would silently misfile every row the user ever writes.
CREATE OR REPLACE FUNCTION auth.validate_user() RETURNS trigger
  LANGUAGE plpgsql AS $fn$
  BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_timezone_names z WHERE z.name = NEW.timezone) THEN
      RAISE EXCEPTION 'unknown timezone: %', NEW.timezone
        USING HINT = 'must match a name in pg_timezone_names, e.g. Europe/Budapest';
    END IF;
    RETURN NEW;
  END
  $fn$;

DROP TRIGGER IF EXISTS users_validate ON auth.users;
CREATE TRIGGER users_validate BEFORE INSERT OR UPDATE ON auth.users
  FOR EACH ROW EXECUTE FUNCTION auth.validate_user();

CREATE TABLE IF NOT EXISTS auth.sessions (
  token_hash   bytea PRIMARY KEY,              -- sha256 of the cookie value
  user_id      uuid NOT NULL REFERENCES auth.users(user_id) ON DELETE CASCADE,
  created_at   timestamptz NOT NULL DEFAULT now(),
  last_seen_at timestamptz NOT NULL DEFAULT now(),
  expires_at   timestamptz NOT NULL,
  user_agent   text
);

CREATE INDEX IF NOT EXISTS sessions_user_idx    ON auth.sessions (user_id);
CREATE INDEX IF NOT EXISTS sessions_expires_idx ON auth.sessions (expires_at);

-- Long-lived bearer tokens for MCP clients that cannot do OAuth (Claude Code,
-- MCP Inspector, scripts). Same discipline as sessions: opaque token, only its
-- sha256 stored, revocable, never readable by the app role.
CREATE TABLE IF NOT EXISTS auth.api_tokens (
  token_hash   bytea PRIMARY KEY,
  user_id      uuid NOT NULL REFERENCES auth.users(user_id) ON DELETE CASCADE,
  label        text NOT NULL CHECK (length(label) BETWEEN 1 AND 100),
  created_at   timestamptz NOT NULL DEFAULT now(),
  last_used_at timestamptz,
  expires_at   timestamptz,          -- NULL means no expiry
  revoked_at   timestamptz,
  -- so a token can be revoked by a name a human can actually remember
  UNIQUE (user_id, label)
);

CREATE INDEX IF NOT EXISTS api_tokens_user_idx ON auth.api_tokens (user_id);

-- RLS here is a backstop: the app role reaches these tables only through the
-- SECURITY DEFINER functions below. Not FORCEd, so owner-run admin tooling
-- (creating a user, resetting a password) still works.
ALTER TABLE auth.users    ENABLE ROW LEVEL SECURITY;
ALTER TABLE auth.sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE auth.api_tokens ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS users_self ON auth.users;
CREATE POLICY users_self ON auth.users
  USING (user_id = diet.current_user_id())
  WITH CHECK (user_id = diet.current_user_id());

DROP POLICY IF EXISTS sessions_own ON auth.sessions;
CREATE POLICY sessions_own ON auth.sessions
  USING (user_id = diet.current_user_id())
  WITH CHECK (user_id = diet.current_user_id());

DROP POLICY IF EXISTS api_tokens_own ON auth.api_tokens;
CREATE POLICY api_tokens_own ON auth.api_tokens
  USING (user_id = diet.current_user_id())
  WITH CHECK (user_id = diet.current_user_id());

-- The app role never touches auth.* directly; it calls these. They are
-- SECURITY DEFINER (owner-run, so RLS does not apply inside them) and each one
-- answers exactly one question, which is why a compromised app role still
-- cannot enumerate users or steal session tokens.

DROP FUNCTION IF EXISTS auth.login_lookup(text);
CREATE FUNCTION auth.login_lookup(p_email text)
  RETURNS TABLE (user_id uuid, password_hash text, display_name text, timezone text)
  LANGUAGE plpgsql SECURITY DEFINER
  SET search_path = pg_catalog, auth
  AS $fn$
  #variable_conflict use_column
  BEGIN
    RETURN QUERY
      SELECT u.user_id, u.password_hash, u.display_name, u.timezone
      FROM auth.users u
      WHERE u.email = lower(p_email) AND u.is_active;
  END
  $fn$;

DROP FUNCTION IF EXISTS auth.session_create(uuid, bytea, interval, text);
CREATE FUNCTION auth.session_create(p_user_id uuid, p_token_hash bytea,
                                    p_ttl interval, p_user_agent text)
  RETURNS timestamptz
  LANGUAGE plpgsql SECURITY DEFINER
  SET search_path = pg_catalog, auth
  AS $fn$
  DECLARE exp timestamptz := now() + p_ttl;
  BEGIN
    INSERT INTO auth.sessions (token_hash, user_id, expires_at, user_agent)
    VALUES (p_token_hash, p_user_id, exp, left(p_user_agent, 300));
    -- Opportunistic GC; cheap enough at login rates to need no cron job.
    DELETE FROM auth.sessions WHERE expires_at < now();
    RETURN exp;
  END
  $fn$;

DROP FUNCTION IF EXISTS auth.session_lookup(bytea);
CREATE FUNCTION auth.session_lookup(p_token_hash bytea)
  RETURNS TABLE (user_id uuid, email text, display_name text,
                 timezone text, expires_at timestamptz)
  LANGUAGE plpgsql SECURITY DEFINER
  SET search_path = pg_catalog, auth
  AS $fn$
  #variable_conflict use_column
  BEGIN
    -- Throttled so a busy session is not one UPDATE per request.
    UPDATE auth.sessions s SET last_seen_at = now()
     WHERE s.token_hash = p_token_hash
       AND s.expires_at > now()
       AND s.last_seen_at < now() - interval '5 minutes';

    RETURN QUERY
      SELECT u.user_id, u.email, u.display_name, u.timezone, s.expires_at
      FROM auth.sessions s
      JOIN auth.users u ON u.user_id = s.user_id
      WHERE s.token_hash = p_token_hash
        AND s.expires_at > now()
        AND u.is_active;
  END
  $fn$;

DROP FUNCTION IF EXISTS auth.session_delete(bytea);
CREATE FUNCTION auth.session_delete(p_token_hash bytea)
  RETURNS void
  LANGUAGE sql SECURITY DEFINER
  SET search_path = pg_catalog, auth
  AS $fn$ DELETE FROM auth.sessions WHERE token_hash = p_token_hash $fn$;

DROP FUNCTION IF EXISTS auth.token_lookup(bytea);
CREATE FUNCTION auth.token_lookup(p_token_hash bytea)
  RETURNS TABLE (user_id uuid, email text, display_name text,
                 timezone text, label text)
  LANGUAGE plpgsql SECURITY DEFINER
  SET search_path = pg_catalog, auth
  AS $fn$
  #variable_conflict use_column
  BEGIN
    -- Throttled, as for sessions: a chatty MCP client is not one UPDATE per call.
    UPDATE auth.api_tokens t SET last_used_at = now()
     WHERE t.token_hash = p_token_hash
       AND t.revoked_at IS NULL
       AND (t.expires_at IS NULL OR t.expires_at > now())
       AND (t.last_used_at IS NULL OR t.last_used_at < now() - interval '5 minutes');

    RETURN QUERY
      SELECT u.user_id, u.email, u.display_name, u.timezone, t.label
      FROM auth.api_tokens t
      JOIN auth.users u ON u.user_id = t.user_id
      WHERE t.token_hash = p_token_hash
        AND t.revoked_at IS NULL
        AND (t.expires_at IS NULL OR t.expires_at > now())
        AND u.is_active;
  END
  $fn$;

-- Minting and revoking are admin operations: manage.py does them over the
-- owner connection. The app role gets lookup and nothing else, so a compromised
-- app process cannot issue itself a token.

DROP FUNCTION IF EXISTS auth.user_timezone(uuid);
CREATE FUNCTION auth.user_timezone(p_user_id uuid)
  RETURNS text
  LANGUAGE sql SECURITY DEFINER STABLE
  SET search_path = pg_catalog, auth
  AS $fn$ SELECT timezone FROM auth.users WHERE user_id = p_user_id $fn$;

-- ---------------------------------------------------------- local_date ----
-- Derived, never supplied: computed from the *user's own* stored timezone at
-- write time. The trigger overwrites whatever was passed in, which is what
-- makes "which day did this belong to" answerable years later without
-- re-deriving it from a timezone that may since have changed.

CREATE OR REPLACE FUNCTION diet.set_local_date() RETURNS trigger
  LANGUAGE plpgsql AS $fn$
  DECLARE
    tz  text;
    src timestamptz := (to_jsonb(NEW) ->> TG_ARGV[0])::timestamptz;
  BEGIN
    -- Via a definer function: the app role has no read access to auth.users,
    -- and does not need any to get this one derived field right.
    tz := auth.user_timezone(NEW.user_id);
    IF tz IS NULL THEN
      RAISE EXCEPTION 'no such user: %', NEW.user_id;
    END IF;
    NEW.local_date := (src AT TIME ZONE tz)::date;
    RETURN NEW;
  END
  $fn$;

-- -------------------------------------------------------- measurements ----
-- Measured data from Garmin. Upserted, never plain-inserted: Garmin backfills
-- and revises, so (user, source, metric, external_id) is the identity of a
-- reading and a second sight of it must overwrite, not duplicate.

CREATE TABLE IF NOT EXISTS diet.measurements (
  measurement_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  user_id     uuid NOT NULL REFERENCES auth.users(user_id) ON DELETE CASCADE,
  ts_utc      timestamptz NOT NULL,
  local_date  date NOT NULL,
  source      diet.provenance NOT NULL,
  metric      text NOT NULL,
  value       double precision NOT NULL,
  unit        text NOT NULL,
  external_id text NOT NULL,
  ingested_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (user_id, source, metric, external_id),
  CONSTRAINT measurements_metric_known CHECK (metric IN (
    'weight_kg', 'resting_hr', 'sleep_minutes', 'steps',
    'active_kcal', 'total_kcal', 'body_fat_pct'
  )),
  -- A wrong-by-a-factor-of-ten reading is the failure mode worth catching in
  -- the schema, since neither Garmin nor a model will flag it.
  CONSTRAINT measurements_value_sane CHECK (
    value >= 0 AND CASE metric
      WHEN 'weight_kg'     THEN value BETWEEN 20 AND 400
      WHEN 'resting_hr'    THEN value BETWEEN 20 AND 200
      WHEN 'sleep_minutes' THEN value BETWEEN 0 AND 1440
      WHEN 'steps'         THEN value BETWEEN 0 AND 200000
      WHEN 'active_kcal'   THEN value BETWEEN 0 AND 20000
      WHEN 'total_kcal'    THEN value BETWEEN 0 AND 25000
      WHEN 'body_fat_pct'  THEN value BETWEEN 1 AND 75
      ELSE true END)
);

CREATE INDEX IF NOT EXISTS measurements_lookup_idx
  ON diet.measurements (user_id, metric, local_date DESC);

DROP TRIGGER IF EXISTS measurements_local_date ON diet.measurements;
CREATE TRIGGER measurements_local_date BEFORE INSERT OR UPDATE ON diet.measurements
  FOR EACH ROW EXECUTE FUNCTION diet.set_local_date('ts_utc');

-- --------------------------------------------------------------- meals ----
-- Estimated data, deliberately not sharing columns with measurements.
-- Corrections are append-only: a correction inserts a new meal and points the
-- old one at it via superseded_by, so "what did the model originally say"
-- survives. Every read of current state filters superseded_by IS NULL.

CREATE TABLE IF NOT EXISTS diet.meals (
  meal_id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id       uuid NOT NULL REFERENCES auth.users(user_id) ON DELETE CASCADE,
  eaten_at      timestamptz NOT NULL,
  local_date    date NOT NULL,
  description   text NOT NULL CHECK (length(description) BETWEEN 1 AND 4000),
  raw_analysis  jsonb,          -- the model's full output, kept verbatim
  source        diet.provenance NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  superseded_by uuid,
  -- Target of the composite FK from meal_items, which is what keeps an item
  -- from ever being attached to another user's meal.
  CONSTRAINT meals_id_user_key UNIQUE (meal_id, user_id),
  CONSTRAINT meals_superseded_fk FOREIGN KEY (superseded_by, user_id)
    REFERENCES diet.meals (meal_id, user_id),
  CONSTRAINT meals_not_self_superseded
    CHECK (superseded_by IS NULL OR superseded_by <> meal_id)
);

CREATE INDEX IF NOT EXISTS meals_day_idx
  ON diet.meals (user_id, local_date) WHERE superseded_by IS NULL;

DROP TRIGGER IF EXISTS meals_local_date ON diet.meals;
CREATE TRIGGER meals_local_date BEFORE INSERT OR UPDATE ON diet.meals
  FOR EACH ROW EXECUTE FUNCTION diet.set_local_date('eaten_at');

-- user_id is carried here too, against the design note's original sketch: it
-- makes the RLS predicate a plain indexed comparison instead of a subquery on
-- meals, and the composite FK below makes the denormalisation unfalsifiable.
CREATE TABLE IF NOT EXISTS diet.meal_items (
  item_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  meal_id    uuid NOT NULL,
  user_id    uuid NOT NULL,
  food       text NOT NULL CHECK (length(food) BETWEEN 1 AND 200),
  grams      numeric(9,2) CHECK (grams IS NULL OR (grams > 0 AND grams <= 20000)),
  kcal       numeric(8,1) NOT NULL CHECK (kcal >= 0 AND kcal <= 10000),
  protein_g  numeric(7,2) CHECK (protein_g IS NULL OR protein_g BETWEEN 0 AND 1000),
  carb_g     numeric(7,2) CHECK (carb_g   IS NULL OR carb_g   BETWEEN 0 AND 1000),
  fat_g      numeric(7,2) CHECK (fat_g    IS NULL OR fat_g    BETWEEN 0 AND 1000),
  confidence real CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
  CONSTRAINT meal_items_meal_fk FOREIGN KEY (meal_id, user_id)
    REFERENCES diet.meals (meal_id, user_id) ON DELETE CASCADE,
  -- Loose 4/4/9 sanity check. Wide enough for alcohol, fibre and rounding;
  -- narrow enough to catch a model that put the kcal of the whole meal on one
  -- item, or moved a decimal point.
  CONSTRAINT meal_items_macros_plausible CHECK (
    protein_g IS NULL OR carb_g IS NULL OR fat_g IS NULL
    OR kcal BETWEEN 0.5 * (4*protein_g + 4*carb_g + 9*fat_g) - 50
                AND 1.6 * (4*protein_g + 4*carb_g + 9*fat_g) + 50)
);

CREATE INDEX IF NOT EXISTS meal_items_meal_idx ON diet.meal_items (meal_id);
CREATE INDEX IF NOT EXISTS meal_items_user_idx ON diet.meal_items (user_id);

-- ---------------------------------------------------- connector plumbing ---

-- Per-user, so one person's expired Garmin token cannot make the whole poll
-- run look failed, and the UI can show whose sync is stale.
CREATE TABLE IF NOT EXISTS diet.sync_state (
  user_id         uuid NOT NULL REFERENCES auth.users(user_id) ON DELETE CASCADE,
  connector       text NOT NULL CHECK (connector IN ('garmin', 'claude-mcp')),
  last_attempt_at timestamptz,
  last_success_at timestamptz,
  last_error      text,
  PRIMARY KEY (user_id, connector)
);

-- Ciphertext only. The key lives outside the database, so a dump of this
-- table is not a set of Garmin logins.
CREATE TABLE IF NOT EXISTS diet.garmin_credentials (
  user_id           uuid PRIMARY KEY REFERENCES auth.users(user_id) ON DELETE CASCADE,
  secret_ciphertext bytea NOT NULL,
  key_id            text NOT NULL,
  updated_at        timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- RLS -----
-- Applied by loop rather than by hand: adding a table to the list is the only
-- thing anyone has to remember, and no table can quietly end up unprotected.

DO $rls$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['measurements', 'meals', 'meal_items',
                           'sync_state', 'garmin_credentials'] LOOP
    EXECUTE format('ALTER TABLE diet.%I ENABLE ROW LEVEL SECURITY', t);
    -- FORCE so the table owner is bound by the policy too.
    EXECUTE format('ALTER TABLE diet.%I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS %I ON diet.%I', t || '_own', t);
    EXECUTE format(
      'CREATE POLICY %I ON diet.%I USING (user_id = diet.current_user_id()) '
      'WITH CHECK (user_id = diet.current_user_id())', t || '_own', t);
  END LOOP;
END
$rls$;

-- -------------------------------------------------------------- grants ----

GRANT USAGE ON SCHEMA diet TO diet_app, diet_ro;
GRANT USAGE ON SCHEMA auth TO diet_app;          -- for the functions only

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA diet TO diet_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA diet TO diet_app;
GRANT SELECT ON ALL TABLES IN SCHEMA diet TO diet_ro;

-- The app role gets no table privileges in auth at all: identity is reachable
-- only through the definer functions. The read-only role cannot even see the
-- schema, which is what makes an ad-hoc SQL tool safe to expose to a model.
REVOKE ALL ON ALL TABLES IN SCHEMA auth FROM diet_app, diet_ro;
REVOKE ALL ON SCHEMA auth FROM diet_ro;

REVOKE ALL ON FUNCTION auth.login_lookup(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION auth.session_create(uuid, bytea, interval, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION auth.session_lookup(bytea) FROM PUBLIC;
REVOKE ALL ON FUNCTION auth.session_delete(bytea) FROM PUBLIC;
REVOKE ALL ON FUNCTION auth.token_lookup(bytea) FROM PUBLIC;
REVOKE ALL ON FUNCTION auth.user_timezone(uuid) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION auth.login_lookup(text) TO diet_app;
GRANT EXECUTE ON FUNCTION auth.session_create(uuid, bytea, interval, text) TO diet_app;
GRANT EXECUTE ON FUNCTION auth.session_lookup(bytea) TO diet_app;
GRANT EXECUTE ON FUNCTION auth.session_delete(bytea) TO diet_app;
GRANT EXECUTE ON FUNCTION auth.token_lookup(bytea) TO diet_app;
GRANT EXECUTE ON FUNCTION auth.user_timezone(uuid) TO diet_app;
GRANT EXECUTE ON FUNCTION diet.current_user_id() TO diet_app, diet_ro;

-- Tables added by a later migration inherit the same shape.
ALTER DEFAULT PRIVILEGES IN SCHEMA diet
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO diet_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA diet
  GRANT SELECT ON TABLES TO diet_ro;
