# Diet tracker — architecture notes

Context for whoever (human or Claude Code) picks this project up next. This is
the output of a design conversation, not a spec handed down from elsewhere —
treat the decisions below as defaults worth reconsidering if they don't hold up
once real code exists, but they're not arbitrary either.

## Shape of the system

```
Garmin Connect ──(poller, systemd timer)──┐
                                          ├──> PostgreSQL <── MCP server ──(HTTPS+OAuth2.1)── Claude web chat
Browser (custom HTML/JS) ──(HTTPS+session cookie)── App server ──┘
```

One Postgres database. Two writers (Garmin poller, Claude via MCP tools).
Two readers (the browser frontend, ad-hoc SQL via a read-only MCP tool).
The MCP server is the *only* thing Claude talks to — no generic SQL write tool.

Multi-user from the start: every table carries `user_id`, enforced by Postgres
row-level security, not by application-layer filtering.

## What exists now

The real backend is built and containerised — see [README.md](README.md) for
the deploy. `docker compose up -d --build` brings up Postgres and the app; the
app applies `app/schema.sql` itself at startup under an advisory lock, so there
is no separate migration step.

- `app/` — FastAPI service. `db.py` holds the pool and the two transaction
  wrappers, and is the only module that can produce a cursor. `auth.py` is the
  real `resolve_user()`: session cookie in, identity out. `queries.py` holds
  the reads, none of which filter by user id because RLS already has.
- `web/index.html` — the frontend, now with a sign-in form. The `?as=` user
  switch is gone; the shape of every API response is unchanged from the stub
  except that `energy_out_kcal` may be null (see below).
- `stub_api.py` — superseded, kept only as a no-database way to serve the page.
  Do not deploy it; its identity still comes from a client-supplied header.

## Schema

Implemented in `app/schema.sql`, which is the whole database: roles, tables,
policies, grants. It is idempotent, so it doubles as the migration.

Three core tables, deliberately separating *measured* data from *estimated* data
rather than sharing columns:

- `measurements(user_id, ts_utc, local_date, source, metric, value, unit, external_id)`
  — Garmin metrics (weight, resting HR, sleep, steps, active kcal). Unique on
  `(user_id, source, metric, external_id)`; upsert, never plain insert, because
  Garmin backfills and revises.
- `meals(user_id, eaten_at, local_date, description, raw_analysis jsonb, source)`
  — `raw_analysis` stores Claude's full output verbatim so reparsing later
    doesn't require re-asking the user.
- `meal_items(meal_id, food, grams, kcal, protein_g, carb_g, fat_g, confidence)`
  — parsed per-item breakdown of a meal.

Details settled on:
- Every row stores both `ts_utc` and a materialized `local_date`, computed at
  write time from the *user's own* stored timezone (not a hardcoded one — this
  matters once there's more than one user).
- Provenance is explicit per row (`garmin`, `claude-estimate`, `manual`).
  Corrections are common ("actually it was two slices") — prefer an
  append-only revision table or `superseded_by` over plain UPDATE, so history
  isn't lost.
- Constraints live in the schema, not the prompt: kcal ranges, non-null units,
  a `CHECK` on the source enum. A model will occasionally write a plausible
  but wrong number; the schema is what catches it reliably.

## Multi-user / auth

- **Identity comes from the token, never from a request argument.** No MCP
  tool or API endpoint takes a `user_id` parameter — it's resolved server-side
  from the OAuth access token / session cookie. This is the one rule not worth
  bending; it's what keeps a confused model or an injected meal description
  from writing into someone else's data.
- **Single Postgres role, RLS-enforced**, not one role per user:
  ```sql
  ALTER TABLE meals ENABLE ROW LEVEL SECURITY;
  CREATE POLICY meals_own ON meals
    USING (user_id = current_setting('app.user_id')::uuid);
  ```
  `SET LOCAL app.user_id = '...'` at the start of every transaction — `SET
  LOCAL` is transaction-scoped so it can't leak across a pooled connection.
  Make the transaction wrapper the only way to get a cursor. This also makes
  the read-only ad-hoc SQL MCP tool safe without inspecting queries.
- **One OAuth 2.1 authorization server serving two clients**: the Claude.ai
  custom connector (authorization-code + PKCE; Claude.ai only supports OAuth
  for custom connectors, no bearer/header option) and the browser (session
  cookie after the same login). Same accounts either way.
- **Garmin credentials are per-user**, stored encrypted (key outside the
  database — e.g. a file on disk / existing secret management), refreshed by
  a poller that iterates users and doesn't let one expired token abort the
  run for everyone. Track last-success per user so the frontend can show
  per-person sync staleness, not a global figure.
- Shared/global reference data (e.g. a future foods table) should use
  `user_id NULL` for global rows with per-user overrides, decided before rows
  exist.

### Where the implementation departed from the sketch above

All small, all in the same direction — moving a guarantee out of application
code and into Postgres:

- **`meal_items` carries `user_id` too**, against the original signature. It
  makes the RLS predicate a plain indexed comparison instead of a subquery
  against `meals`, and a composite foreign key to `meals(meal_id, user_id)`
  makes the denormalised column impossible to falsify.
- **Policies got `WITH CHECK`, not just `USING`.** `USING` alone would let a
  user *write* a row stamped with someone else's id while being unable to read
  it back. Verified: the insert is now refused.
- **`local_date` is set by trigger**, never by the writer, from the user's own
  stored timezone. It was going to be computed at every call site, which is a
  thing that gets forgotten once.
- **Identity lives in a separate `auth` schema** that the app role has *no*
  table privileges on — it reaches identity only through four SECURITY DEFINER
  functions, one question each. The read-only role cannot see the schema at
  all, which is what makes exposing `query_sql` to a model reasonable.
- **`provenance` is a domain**, so the source enum is one edit rather than one
  per table.
- Corrections use `superseded_by` (append-only) rather than a separate
  revision table; every read filters `superseded_by IS NULL`.
- Metrics gained `total_kcal` and `body_fat_pct`. Expenditure prefers
  `total_kcal`, falls back to `active_kcal`, and flags the fallback so the UI
  never shows a partial figure as if it were a whole-day total. When nothing is
  known the API returns null rather than zero — a missing sync should not read
  as a 2000 kcal deficit.

## Garmin ingest

No usable free official API — Health API needs partner approval. Plan is
`python-garminconnect` (garth-based) on a systemd timer, pulling the trailing
2–3 days each run (sleep/body-battery land late) and upserting. Log raw JSON
to disk before parsing (Garmin's undocumented API shifts occasionally), and
have the poller exit non-zero loudly so an existing monitoring setup — there is
already a Grafana/VictoriaMetrics stack to hang it off — catches it.

## MCP tools (surface kept intentionally small)

- `log_meal(description, eaten_at, items[])`
- `correct_meal(meal_id, ...)`
- `get_day(date)`
- `get_range(from, to, metrics[])`
- `query_sql(sql)` — separate **read-only** Postgres role, RLS still applies

## Frontend

Custom HTML/JS (not Grafana) for the human-facing side, since the user wants
direct control over it rather than a dashboarding tool. Grafana can still be
pointed at Postgres later for the user's own ad-hoc charts if wanted — that's
free given the existing Grafana/VictoriaMetrics setup — but it's not the
primary UI.

## Open / deferred

- Which OAuth 2.1 implementation to build on: FastMCP (has auth support
  built in — probably least effort), a supergateway-style stdio→HTTP wrapper
  with a hand-rolled auth server behind nginx, or leaning on Auth0/similar as
  the authorization server. Not decided — flagged as the step most likely to
  eat an evening, since there are several open reports of the Claude.ai
  connector OAuth flow failing against otherwise-correct self-hosted servers.
  Suggested to validate MCP tools first against Claude Code (accepts a static
  header) before adding OAuth into the mix.
- The Garmin poller. The schema is ready for it: `diet.measurements` upserts
  on `(user_id, source, metric, external_id)`, `diet.sync_state` records
  per-user last-success (the status panel already reads it), and
  `diet.garmin_credentials` holds ciphertext with the key outside the database.
  No poller code exists yet, and no encryption code either.
- The MCP server. `diet_ro` exists and is proven to be read-only and
  RLS-scoped; nothing connects as it yet. Setting `MCP_PUBLIC_URL` is what
  turns the status panel's last line green.
- Login is single-factor with an in-process rate limiter. That limiter is
  per-container, so it stops counting correctly the moment there is more than
  one app replica.
- Sessions are opaque tokens in Postgres, not the OAuth 2.1 server the
  connector will need. The two will share `auth.users`, not this cookie path.
