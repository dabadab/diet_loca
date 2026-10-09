# Diet tracker — architecture notes

Context for whoever (human or Claude Code) picks this project up next. It began
as the output of a design conversation rather than a spec handed down from
elsewhere, and it is now a record of what was actually built and what the
building taught — the sections below say where the implementation departed from
the original sketch, and why. Treat the decisions as defaults worth
reconsidering, but not as arbitrary.

## Shape of the system

```
Garmin Connect ──(poller sidecar, hourly)──┐
                                           ├──> PostgreSQL <── MCP server ──(HTTPS+OAuth2.1)── Claude
Browser (custom HTML/JS) ──(HTTPS+session cookie)── App server ──┘
```

All of this is built and running. Three containers — `app`, `db`, `poller` —
behind nginx with TLS.

One Postgres database. Two writers (Garmin poller, Claude via MCP tools).
Two readers (the browser frontend, ad-hoc SQL via a read-only MCP tool).
The MCP server is the *only* thing Claude talks to — no generic SQL write tool.

Multi-user from the start: every table carries `user_id`, enforced by Postgres
row-level security, not by application-layer filtering.

## What exists now

Deployed and in use — see [README.md](README.md). `docker compose up -d --build`
brings up Postgres, the app and the Garmin poller; the app applies
`app/schema.sql` itself at startup under an advisory lock, so there is no
separate migration step.

Live as of September 2026: real Garmin measurements flowing in hourly, meals
logged and corrected through Claude — both the Claude.ai custom connector and
Claude Code, over this service's own OAuth — and both readable in the browser.

- `app/` — FastAPI service. `db.py` holds the pool and the two transaction
  wrappers, and is the only module that can produce a cursor. `auth.py` is the
  real `resolve_user()`: session cookie in, identity out. `queries.py` holds
  the reads, none of which filter by user id because RLS already has.
- `web/index.html` — the frontend, now with a sign-in form. The `?as=` user
  switch is gone; the shape of every API response is unchanged from the stub
  except that `energy_out_kcal` may be null (see below).
- `app/mcp_server.py` — the five MCP tools, mounted at `/mcp`. Identity comes
  from a bearer token verified against `auth.api_tokens`; no tool takes a user
  id, and every tool body runs inside one of the transaction wrappers.
- `app/writes.py` — the first write paths in the project. Rows are stamped with
  `diet.current_user_id()` in SQL rather than with a value passed from Python,
  so there is no argument anywhere by which a caller could name a user.
- `app/oauth_provider.py` / `app/oauth_routes.py` — the authorization server and
  its consent screen, so Claude connects over OAuth against the same
  `auth.users` rows the browser signs in with.
- `app/garmin.py` / `app/poller.py` / `app/secretbox.py` — the Garmin sidecar
  and the encryption for its stored sessions.
- `stub_api.py` — superseded, kept only as a no-database way to serve the page.
  Do not deploy it; its identity still comes from a client-supplied header.

## Schema

Implemented in `app/schema.sql`, which is the whole database: roles, tables,
policies, grants. It is idempotent, so it doubles as the migration.

Four core tables, deliberately separating *measured* data from *estimated* data
rather than sharing columns:

- `measurements(user_id, ts_utc, local_date, source, metric, value, unit, external_id)`
  — Garmin metrics (weight, resting HR, sleep, steps, active kcal). Unique on
  `(user_id, source, metric, external_id)`; upsert, never plain insert, because
  Garmin backfills and revises.
- `activities(user_id, started_at, local_date, source, external_id, activity_type,
  duration_s, distance_m, kcal, avg_hr, hr_zones jsonb, raw jsonb, …)`
  — Garmin workouts. Not rows in `measurements`: a measurement is one scalar at
  one instant, an activity is an interval whose dozen fields are only meaningful
  together. Same upsert identity rule, because Garmin reprocesses HR and lets
  people rename a workout afterwards.
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
  per-person sync staleness, not a global figure. All implemented; see Garmin
  ingest below.
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

No usable free official API — the Health API needs partner approval — so this
rides the same undocumented endpoints the mobile app uses, via
`garminconnect` 0.3.11. (That library dropped garth for `curl_cffi`, so the
earlier "garth-based" note here was out of date.)

Built, in `app/garmin.py`, `app/poller.py` and `app/secretbox.py`:

- **Parsing is declarative and defensive.** `MetricSpec` lists candidate JSON
  paths per metric; a field that has moved yields nothing rather than a guess,
  and `describe_payload()` prints the numeric keys that did arrive so the spec
  can be corrected. Raw payloads are archived *before* parsing, so when the
  shape shifts the evidence is already on disk.
- **Readings are timestamped noon local, not midnight.** Midnight UTC puts
  every reading for a user west of Greenwich on the previous day. Verified
  against America/Denver.
- **Upsert on `(user_id, source, metric, external_id)`** with the date as the
  external id, over a deliberately overlapping trailing window, because sleep
  lands late and Garmin revises figures afterwards.
- **A rejected reading costs only that reading**: each upsert runs in a
  savepoint, so a value caught by `measurements_value_sane` does not take the
  rest of the day with it.
- **A failed account costs only that account.** Outcomes land per user in
  `diet.sync_state`, which is what the status panel already reads.
- **Activity calories are never summed into expenditure.** Garmin's daily
  summary already counts them inside `active_kcal`, so adding them would
  double-count exactly the days with training on them — the same class of error
  as scoring a Garmin-only day as zero intake, or comparing every logged day
  against targets over only the days that had one. Activities are *context for*
  the day's energy figures, never an *input to* them. The rule is written into
  the schema comment, `queries.day_detail`, and both tool docstrings, because it
  is the thing a later change will get wrong. Enforced structurally too:
  `queries._DAYS_SQL`, which backs the whole energy view, does not reference
  `diet.activities` at all.
- **The session, not the password, is stored.** `manage.py garmin-login` uses
  the password once, interactively, handles MFA, and keeps only the token blob,
  Fernet-encrypted with the key in a file outside the database. A dump of
  `diet.garmin_credentials` is therefore not a set of working sessions.

Two cadences, because the two things the window is for have different tempos:
today changes hour to hour and is fetched every 15 minutes at one day wide,
while the trailing window exists only to catch Garmin's late arrivals and
revisions and is fetched every two hours. Fetching the full window at the short
interval would multiply requests against an API that has already rate-limited
this deployment; a 429 now stops all polling for a backoff period rather than
retrying into it. A conditional UPDATE of `sync_state.last_attempt_at` is the
claim that stops the sidecar and the web page's **Sync now** button colliding.

Activities ride the same two cadences at **exactly one extra request per pass**:
`get_activities_by_date(start, end)` returns a whole window in a single call,
however many workouts are in it, so the small cycle asks for today and the large
one for the configured window.

Time-in-zone looked like the one thing the list response did not carry, and
there is a `/hrTimeInZones` endpoint costing a request *per activity*. It was
built the careful way — fetch once per activity ever, large cycle only, under a
per-pass budget, with an attempt counter so transient failures could not become
a retry loop against an endpoint that rate-limits. Then the first real run
showed `hrTimeInZone_1..5` sitting in the list item all along, on all eleven
activities.

So the endpoint, the budget, the backlog index and the `zones_fetched_at` /
`zone_attempts` bookkeeping were all deleted, and `extract_activity` reads the
five figures out of the payload it already has. The lesson is the one this
codebase keeps relearning: look at a real response before building machinery to
go and fetch what might be in it. Power zones went with it — no
`powerTimeInZone_*` appears inline, and `raw` keeps the option open if a power
meter ever shows up.

Deeper per-activity endpoints — splits, exercise sets, weather, gear, and the
per-second chart and GPS polyline behind `get_activity_details` — are
deliberately not fetched: each costs a request per activity and none of them
answers a question a diet log asks. The full summary is kept verbatim in `raw`,
so that decision is reversible without re-fetching. Coordinates are stripped
from `raw` on the way in: they are a location trace with no bearing on diet, and
`diet_ro` — the role behind the model-facing `query_sql` — can read that column.

It runs as a sidecar container rather than the systemd timer originally planned.
That trade needed compensating for: a restarting container has no exit code for
anyone to read, so each cycle writes a status file the container healthcheck
reads, and a failed or stale cycle makes the container unhealthy.
`docker compose run --rm poller --once --days 30` still does a one-shot backfill.

### What the first real run established

The field mapping was right on first contact: six of seven metrics landed, and
the arithmetic checks out — `bmrKilocalories` (2153) + `activeKilocalories`
(512) = `totalKilocalories` (2665), so `total_kcal` is the right field and is
not double-counting activity.

- **`body_fat_pct` is absent, correctly.** `get_stats_and_body` merges the body
  composition response's `totalAverage` into the stats dict; `weight` arrives
  from there, so the call works — Garmin simply has no `bodyFat` without a
  scale that measures impedance. The spec is left in place: if such a scale
  ever appears the metric starts populating with no code change. This is the
  defensive parsing behaving as intended — a missing field produced no reading
  rather than a fabricated one.
- **`bmrKilocalories` is in the payload and unused.** For a diet log it is
  arguably the most useful field available: it splits energy out into the part
  that is not negotiable and the part that is. Adding it is not a one-line
  change; see Open / deferred.
- The stats payload also carries `sleepingSeconds`, which disagrees with the
  `dailySleepDTO.sleepTimeSeconds` we store (512 vs 462 minutes). They measure
  different things — time in the sleep window versus measured sleep — and the
  one we store is the better of the two.

### The credentials key

The container runs as uid 10001, so the key file must be readable by that uid
rather than by the host user who created it. Two failures came out of getting
this wrong, both now designed against: Docker materialises a missing bind-mount
source as a *directory*, so the secrets **directory** is mounted rather than the
key file itself; and `garmin-login` verifies the key is usable **before**
prompting, because a Garmin login costs an MFA code and a slot against an IP
rate limit that answers 429 for a while afterwards. Discovering an unreadable
key after spending both is the wrong order, and was how it was first written.

## MCP tools (surface kept intentionally small)

Built, on FastMCP 4, and verified against Postgres 13 and 17:

- `log_meal(description, items[], eaten_at?)` — the user's words and the model's
  parse are both kept; `raw_analysis` holds the estimate verbatim
- `correct_meal(meal_id, ...)` — append-only; returns the new id, refuses a meal
  that has already been corrected and points at the current head
- `get_day(date?)` — meals with items, that day's measurements, and its activities
- `get_range(from?, to?, metrics[]?)` — measured series only
- `get_activities(from?, to?, activity_type?)` — workouts with their heart-rate
  zone breakdown; the docstring says outright that their calories are already
  inside `active_kcal` and must not be added to expenditure
- `ignore_day(date, reason)` / `unignore_day(date)` / `get_ignored_days()` —
  leave a day out of the summaries, with the reason recorded
- `query_sql(sql)` — separate **read-only** Postgres role, RLS still applies

Two things the testing changed. The `SELECT`-prefix check on `query_sql` is not
sufficient on its own — `WITH x AS (UPDATE ... RETURNING 1) SELECT * FROM x`
passes it — so the wrapper also opens a read-only transaction, and that is what
actually refuses the write. And the status panel's "Claude connector" line now
reads `sync_state`, which only a real tool call writes; it used to infer from
`claude-estimate` meals, which `seed-demo` also produces, so it would have
reported a connector that had never existed.

Auth is a bearer token in `auth.api_tokens`, built like sessions: opaque, stored
only as a digest, revocable, expiring. Minting is an owner-connection operation
in `manage.py`, so the app role can look a token up but cannot issue one. It
stays after OAuth arrives, composed under FastMCP's `MultiAuth`, so scripts and
Claude Code keep working against the same server Claude.ai talks to.

## OAuth (implemented)

One authorization server, two clients, the same `auth.users` rows — which was
the point of the original design and is now literally true. FastMCP's
`OAuthProvider` supplies `/authorize`, `/token`, `/register`, `/revoke` and the
RFC 8414 / RFC 9728 metadata documents; `app/oauth_provider.py` supplies
storage, and `app/oauth_routes.py` supplies the consent screen, which is the
one thing no library can provide: deciding *who* is granting access.

That decision comes from the existing session cookie. Approving the connector
is therefore one screen with two buttons for someone already signed in, and a
sign-in on that same screen for someone who isn't. There is no second account
and no second login.

**The Claude.ai custom connector connects against this successfully**, which
was the step flagged from the outset as most likely to eat an evening — there
is a cluster of open reports of that handshake failing against otherwise
correct self-hosted servers. It worked first time, and the things complied with
up front are the ones those reports blame: the endpoint sits at exactly `/mcp`,
one path segment deep; a bare `POST /mcp` is served rather than redirected to
`/mcp/`; the consent screen redirects with 303 and not 307/308; `/token` accepts
form-urlencoded; there is no CDN bot protection in front of the path; and the
`resource` in the protected-resource metadata matches the typed URL exactly.

Things worth knowing:

- **`authorize()` cannot approve anything by itself.** The reference
  implementation does, because it is a test double. At the point it runs,
  nothing has established which user is granting access — so it parks the
  request and redirects to consent, and identity is resolved there the same way
  it is everywhere else in this system.
- **Tokens are opaque and stored as digests**, like sessions and api_tokens.
  Access and refresh are issued as a pair that revoke together, since half a
  pair is never useful. Refresh rotates on use.
- **An authorization code is consumed by the UPDATE that validates it**, so a
  replayed code loses a race rather than being checked twice.
- **`client_name` comes from dynamic registration**, i.e. from whoever
  registered, and is rendered on the consent screen. It is escaped; a client
  registering as `<script>alert(1)</script>` displays as text.
- **The app role holds ordinary DML on `auth.oauth_*`**, unlike every other
  table in that schema. This process *is* the authorization server, so it can
  already mint a token for any user through its own code path — gating its own
  token store behind definer functions would buy nothing. Password hashes,
  sessions and api_tokens remain function-only, and the token lookup reaches
  user profile fields through `auth.user_profile()` rather than by reading
  `auth.users`.
- **`/revoke` requires a `client_secret` field even from public clients** — the
  SDK's request model declares it without a default, so a client that omits it
  gets a 400. Sending it empty works. Because revocation should not depend on
  the client choosing to call it, `manage.py revoke-connection` kills tokens
  from this side.

## Targets (implemented)

Calorie and protein targets are **effective-dated**: a row in `diet.targets` is
set from a date and applies until a later one supersedes it. Resolution is a
lateral join picking the greatest `effective_from <= the day`.

They began as two numbers in the browser's `localStorage`, which was wrong in a
way worth recording. Not because it was per-browser — because it was a single
*current* value applied to all of history. The weekly bar computed
`goal x days`, so changing the target re-scored every past day against it: a
maintenance week became a failure the moment a cut started. A target is what
you were aiming at *then*, and the schema now says so.

- **Retroactive editing is free**, not a feature bolted on. Insert a row with a
  past date and exactly the days from there until the next one re-score,
  because nothing stores a per-day copy.
- **Rows are complete, never deltas.** Per-field effective dating would make
  "what applied on day D" ambiguous. `set_target` fills anything omitted from
  whatever was in force on that date, so a stored row answers on its own.
- **Days before the first row have no target**, reported as null rather than
  defaulted. A day before you set one was not a missed target, and the UI shows
  the value with no bar rather than inventing a denominator.
- **Both sides of a summary bar cover the same days.** Summing intake over
  every logged day against targets over only the days that had one compares
  different sets — the same error as counting a Garmin-only day as zero
  calories, and it reappeared here during the UI work.
- Management is MCP-only: `get_targets`, `set_target`, `clear_target`. The page
  displays the target in force and when it started, and has no editing surface,
  so there is no second dateless way to change one.
- `diet_ro` is granted `SELECT`, so `query_sql` can answer "which days did I
  miss protein". Deliberate: that grant list has been opt-in since the security
  review.

## Ignored days (implemented)

A day can be left out of the summaries: illness, travel, a day the tracker was
not worn, a deliberate one-off that would otherwise drag every average through
it. Managed only through MCP, like targets.

`diet.ignored_days` has no `ignored = false` state — presence of the row is the
flag, so a day nobody has said anything about is simply counted. `reason` is
`NOT NULL`: an excluded day is precisely what makes someone ask "why is this
week different" later, and an unexplained exclusion cannot answer it. Nothing is
deleted or hidden; the day keeps its meals, measurements and activities and
still appears in the day list.

The page marks it, and this is the part worth getting right: **a dimmed row that
is actually being averaged in, or a normal-looking row that is not, is worse
than no marking at all.** So `uncountedKind(d)` is the single definition of
"this day is out", and everything else derives from it: `uncountedReason(d)`
turns the kind into the row's tooltip, the Summary tab's day selection keeps the
days with no kind, and the day list dims the rest. Four kinds — `ignored`,
`today`, `empty`, `pregoal` — and only a deliberate exclusion also gets a ✕, so
"set aside on purpose" stays distinguishable from "not yet".

The summary header accounts for all of them, because it has to add up. It used
to read `31 days · 31 with meals · today excluded · 7 ignored`, which left the
reader unable to tell whether the seven were inside the thirty-one — they were
not — and counted ignored days over the whole fetched window rather than over
the days that were ever candidates, so neither reading closed. It now says `31
days counted` and, on its own line, `not counted: 7 ignored · today`, built by
tallying `uncountedKind` over the window. Counted plus excluded equals the
window, and a day excluded for two reasons at once is reported under one.

## The summary covers the current goal

The summary starts the day the goal in force took effect, not N days ago. A
summary over "the last four weeks" that straddles a switch from maintenance to a
cut averages two different intentions into one meaningless number — the same
objection that made targets effective-dated in the first place. With a cut 13
days old inside a 28-day window, the difference is 2,164 kcal/day over 27 days
against 1,801 over 13: the first figure describes nothing that was ever aimed
at.

That start date is `effective_from`, not `created_at` — a target entered
retroactively took effect when it says it did, which is the whole point of
backdating one.

Two consequences:

- **The window has to reach back that far.** `/api/days` resolves the target
  first and stretches `n` to cover it, so the page still boots on one request
  rather than fetching days, discovering the goal, and fetching again. The
  ceiling is `MAX_DAYS` (400).
- **A clamped window must say so.** If the goal predates the ceiling the
  summary is covering less than its own heading claims, so the page checks
  whether the oldest day actually reached the goal start and says plainly that
  earlier days are missing. Silently under-reporting would be the same class of
  error as the days-with-a-target denominator mismatch.

With no target set at all, none of this applies: the heading stays "Complete
days" and the summary covers the whole window, as before.

## Frontend

Custom HTML/JS (not Grafana) for the human-facing side, since the user wants
direct control over it rather than a dashboarding tool. Grafana can still be
pointed at Postgres later for the user's own ad-hoc charts if wanted — that's
free given the existing Grafana/VictoriaMetrics setup — but it's not the
primary UI.

## The weight chart

On the Summary tab, over the same window as everything else there: since the
goal took effect. No backend change was needed — `/api/days` already stretches
its window to the goal start and already carries `weight_kg` per day.

**The trend is three steps, and the order is the point.** Fit a straight line by
least squares; smooth the *residuals* with an exponential filter; add the line
back.

Step two alone is the classic weight-trend filter and handles **noise** — a kilo
of any single reading is water, gut contents and salt. Its alpha comes from
elapsed days rather than a sample count, which is what handles **gaps**:
`alpha = 1 - exp(-dt / tau)`, tau = 10 days. A plain EWMA counts "yesterday" and
"three weeks ago" as one step each, so a reading after a holiday barely moves the
line; elapsed time gives each reading exactly the weight its distance earns.

Steps one and three exist because of what smoothing does to a *slope*. Run
forwards an exponential filter lags, backwards it leads, and averaging the two
cancels the lag in the middle but not at the ends, where one direction has no
history to have lagged. Measured on a steady 0.1 kg/day decline that pulled both
ends 0.44 kg toward the centre and flattened the reported rate from 0.70 kg/week
to 0.47 — a third off the number someone is actually watching. Detrending first
leaves the filter nothing but noise, where it has no lag to cancel: the same
test then gives 0.000 kg of end error and exactly 0.70 kg/week. Curvature still
rides in the residuals and is still smoothed, so a plateau followed by a drop
keeps its shape.

Two smaller decisions, both measured rather than guessed:

- Each pass is **seeded at the fitted line, not at the first reading**. The
  residuals are zero-mean by construction, so zero is the right prior for a
  point with no history; seeding with that point's own noise started each pass
  half a kilo out and took a tau to relax — five times the wobble at the ends.
- The **rate is the fitted slope**, not the smoothed line's endpoints, for the
  same end-effect reason. It is withheld under a week, where slope is mostly
  noise.

Weights on an **ignored** day are plotted (dimmed, as the day list dims them):
ignoring a day says "do not average my calories", not "I did not weigh this
much", and dropping them would put a hole in the trend for exactly the fortnight
someone was away. The old "weight A → B over N weigh-ins" line was removed from
the summary card when this landed — it counted only counted days, so it sat
beside the chart advertising a different number of weigh-ins.

Drawing notes: it is an **emphasis** chart, not two series — the trend is the
subject and the readings are the cloud it runs through, so one hue carries the
line and a recessive warm gray the dots. That pair was checked rather than
eyeballed; the page's own `--ink-soft` against `--leaf` came out at dE 6.6,
genuinely hard to tell apart, so the dots are a lighter step at dE 21.4. The
viewBox is sized to the card's measured width so one unit is one pixel —
stretching a fixed box to fit shrank the axis labels to about five pixels on a
phone. The y-span has a 1.5 kg floor, because a flat fortnight has a real range
of about 300 g and scaling to the extremes turns that into a mountain profile.

## Tests

`tests/`, pytest, against a real cluster rather than a mocked cursor — almost
everything worth testing here *is* the database, and a fake cursor agrees with
whatever the code believes. A fresh database per test, because these assert on
counts and averages over whole tables and cross-test residue would make the
numbers untrustworthy.

Two choices worth recording. The **Garmin seam is `garmin.fetch_*`**: above it
everything is real including the writes, below it is their HTTP and is faked, so
the suite never depends on someone's actual sleep or spends requests against a
rate limit. And `tests/web_harness.js` **lifts the page's functions out of
`index.html` by name** instead of reimplementing them — the browser decides
which days the summary counts and what today's projection is, every one of those
has been wrong at some point, and a reimplementation could not catch a change to
the original.

The suite was validated by mutation, not by passing: reverting
`energy_out_partial` to its old definition, dropping a table from the `DO $rls$`
loop, removing the pre-goal exclusion, and letting activity calories into the
energy view each fail exactly the test written for them.

## Open / deferred

- **Adding a measurement metric needs a real migration.** `bmr_kcal` is the
  first one wanted, and it exposes a gap: a new metric must pass
  `measurements_metric_known`, and `CREATE TABLE IF NOT EXISTS` cannot alter a
  CHECK on a table that already exists. `schema.sql` being *idempotent* does
  not make it *evolvable*. This needs an explicit
  `ALTER TABLE ... DROP CONSTRAINT / ADD CONSTRAINT` step, a range in
  `measurements_value_sane`, and an entry in `queries.KNOWN_METRICS` — done
  once, properly, since every later metric follows the same path.
- **DCR has a stated end-of-life.** The current MCP spec revision deprecates
  Dynamic Client Registration in favour of Client ID Metadata Documents, while
  Claude.ai's working path and FastMCP's base `OAuthProvider` are both still
  DCR. `fastmcp/server/auth/cimd.py` exists but is beta and is not wired into
  `OAuthProvider.get_routes()`, so moving is not a flag flip.
- **`auth.oauth_clients` grows without bound.** Claude registers a fresh client
  on every new connection — Anthropic warns about this — and the prune only
  removes registrations that were *never* used (`last_seen_at IS NULL`). A
  client that connected once and was then abandoned keeps its row for ever.
  Nothing is broken; it accumulates. Pruning on `last_seen_at` age instead
  would fix it, at the cost of forcing a re-consent for a connector that goes
  quiet for a while.
- Anthropic documents a `static_headers` connector auth type in beta. If the
  org has it, a bearer token from `manage.py issue-token` may be enough on its
  own; there is at least one report of the beta ignoring the header and falling
  back to OAuth anyway.
- **Credential key rotation is unimplemented.** `secretbox` stores a `key_id`
  per row so a second key could be introduced, but nothing re-encrypts. Losing
  `secrets/credentials.key` means re-authenticating every stored Garmin
  session, so it needs backing up somewhere off the box.
- We do not emit the RFC 9207 `iss` parameter on authorization responses — a
  SHOULD in the current spec. Consistently not advertised, so a gap rather than
  a violation.
- Nothing writes `manual` provenance yet. The domain allows it and the UI
  distinguishes it from `claude-estimate`, but there is no path to enter a meal
  by hand without going through Claude.
- Login is single-factor with an in-process rate limiter. That limiter is
  per-container, so it stops counting correctly the moment there is more than
  one app replica.
- **`/register` and `/authorize` have no rate limit.** They are unauthenticated
  by necessity — Claude.ai needs dynamic client registration — and the login
  throttle does not reach them; it is wired only to `POST /api/login`. Abandoned
  client rows are now aged out, so the growth is bounded, but the endpoints
  themselves are open. A limiter here would have to not block Claude, whose
  egress is a shared range, so an nginx `limit_req` zone tuned generously is a
  better fit than the in-process throttle.
