# Diet tracker — backend

FastAPI + PostgreSQL, containerised. Replaces `stub_api.py`: identity now comes
from a session cookie, and every data query runs through a transaction wrapper
that sets `app.user_id`, so Postgres row-level security — not application code —
is what separates one user's rows from another's.

Design context and the reasoning behind the schema: [ARCHITECTURE.md](ARCHITECTURE.md).

## Deploy

```sh
cp .env.example .env         # then fill in two secrets:
openssl rand -hex 32         #   POSTGRES_PASSWORD
openssl rand -hex 32         #   APP_DB_PASSWORD

docker compose up -d --build
./adduser.sh you@example.com "Your Name" Europe/Budapest   # or just ./adduser.sh
```

`adduser.sh` checks what is cheap to check before a password is typed twice:
that the stack is up, that the address is free, and that the timezone is one
Postgres will accept. The last matters most — every `local_date` is derived
from it, so a typo silently misfiles every row the account ever writes.

It offers to connect Garmin afterwards, and that step can also be run on its
own for an account that already exists:

```sh
./adduser.sh --garmin you@example.com     # connect Garmin only
./adduser.sh --no-garmin you@example.com "Your Name" Europe/Budapest
```

Connecting Garmin needs an interactive terminal — it prompts for the Garmin
password and then a time-limited MFA code — so the script refuses rather than
half-running it from a pipe. It replaces an existing session only if you
confirm, and offers a 30-day backfill once the session is stored.

Then open `http://127.0.0.1:8080` and sign in.

That is the whole deploy. The app applies its own schema at startup under an
advisory lock, so there is no migration step to remember and no reliance on the
Postgres image's first-boot hooks — redeploying onto an existing volume works.

**Before exposing it:** set `COOKIE_SECURE=1` in `.env` (the generated file ships
with `0` so login works over plain http on localhost) and put TLS in front.

## Behind nginx

The app binds to loopback only. A complete server block — `location` is only
valid inside `server`, so this is the whole file, not a fragment:

```nginx
server {
    listen 80;
    listen [::]:80;
    server_name diet.example.com;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host              $host;
        proxy_set_header X-Forwarded-For   $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;

        # Required for MCP: responses are streamed, and buffering them is the most
        # common cause of a client that connects but never receives anything.
        proxy_http_version 1.1;
        proxy_set_header Connection '';
        proxy_buffering off;
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }
}
```

Then `nginx -t && systemctl reload nginx`, and `certbot --nginx -d
diet.example.com` to add TLS — it rewrites this file in place. Afterwards set
`MCP_PUBLIC_URL=https://diet.example.com` and `COOKIE_SECURE=1` in `.env` and
`docker compose up -d` to pick them up.

Do not put CDN bot-protection in front of `/mcp`. Anthropic's egress range is
`160.79.104.0/21`, and a WAF that lets the OAuth traffic through while blocking
the authenticated POSTs produces a failure that is invisible in origin logs.

`X-Forwarded-For` matters: it is what the login rate limiter counts against.
Set `TRUST_PROXY=1` and `FORWARDED_ALLOW_IPS=<the address nginx connects from>`
in `.env` so the header is believed from that proxy and nowhere else — the
limiter reads the rightmost entry, which is the one nginx wrote. Left at the
defaults the header is ignored entirely and the limiter counts the peer
address, which is safe but lumps every client behind the proxy together.

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/` | the page |
| `GET` | `/api/health` | unauthenticated; used by the container healthcheck |
| `POST` | `/api/login` | `{email, password}` → sets the session cookie |
| `POST` | `/api/logout` | revokes the session server-side |
| `GET` | `/api/me` | the signed-in user |
| `GET` | `/api/status` | the chain the page draws, every line measured |
| `GET` | `/api/days?days=7` | per-day intake, expenditure, weight |
| `POST` | `/mcp` | the MCP server, for Claude — bearer token |

No endpoint takes a user id, and neither does any MCP tool. There is no
argument by which a client — or a model driving a tool — can name anyone but
itself.

## MCP

Eight tools at `POST /mcp`: `log_meal`, `correct_meal`, `get_day`, `get_range`,
`get_targets`, `set_target`, `clear_target`, `query_sql`.

Targets are effective-dated — set from a date, applying until a later one
supersedes it — so asking Claude to "set 1800 kcal from the 1st" re-scores
exactly the days from then onwards and leaves earlier ones judged against what
you were actually aiming at. They are managed only through Claude; the page
shows the one in force and since when. Two ways to authenticate, on the same server:

**Bearer token** — for Claude Code, MCP Inspector and scripts:

```sh
docker compose exec app python -m app.manage issue-token you@example.com --label laptop
claude mcp add --transport http diet https://your-host/mcp \
  --header "Authorization: Bearer <the token>"
```

**OAuth** — for Claude.ai custom connectors, which cannot use a static header.
Set `MCP_PUBLIC_URL` to the public https root (no trailing slash) and the
authorization server turns itself on: `/authorize`, `/token`, `/register`
(dynamic client registration), `/revoke`, and the two metadata documents. Then
add `https://your-host/mcp` as a custom connector.

It is *this* service's authorization server, over the accounts already in
`auth.users` — there is no third party in the loop. Approving the connector
uses the session cookie you already have, so it is one screen with two buttons,
not a second login. Manage what is connected:

```sh
docker compose exec app python -m app.manage list-connections
docker compose exec app python -m app.manage revoke-connection <email> [client_id]
docker compose exec app python -m app.manage generate-key
docker compose exec app python -m app.manage garmin-login <email>
docker compose exec app python -m app.manage garmin-status
```

`revoke-connection` works from this side and does not depend on the client
calling `/revoke`, which matters because disconnecting something should not
require its cooperation.

`query_sql` connects as `diet_ro` — SELECT only, row-level security still
applies, and it cannot see the `auth` schema at all. It additionally runs in a
read-only transaction with a statement timeout, which is what stops
`WITH x AS (UPDATE ...) SELECT * FROM x` from sneaking past the SELECT check.
If `READONLY_DB_PASSWORD` is empty the tool simply isn't offered; everything
else still works.

**The path must stay exactly `/mcp`.** Claude.ai has a documented failure where
a connector completes the OAuth handshake and then sends no traffic at all when
the endpoint sits deeper than one path segment. For the same family of reasons
the mount is arranged so that a bare `POST /mcp` is served directly rather than
redirected to `/mcp/` — a method-preserving redirect there breaks the handshake.

## Garmin

A sidecar polls Garmin Connect on an interval and upserts into
`diet.measurements`. Set it up once:

```sh
# Make the key first — before the containers start. It is a 32-byte urlsafe
# base64 value, so nothing from this repo is needed to generate one:
mkdir -p secrets
openssl rand -base64 32 | tr '+/' '-_' > secrets/credentials.key

# The container runs as uid 10001, not as you, so a 0600 file you own is
# unreadable inside it. Hand the key to that uid and nobody else:
sudo chown 10001:10001 secrets/credentials.key
sudo chmod 400 secrets/credentials.key

docker compose up -d
docker compose exec app python -m app.manage garmin-login you@example.com
docker compose run --rm poller --once --days 30         # backfill
```

(`docker compose exec app python -m app.manage generate-key` prints one too,
but only once the container is running — which is why the key comes first.)

`garmin-login` checks the key is readable before it prompts for anything: a
Garmin login costs an MFA code and a slot against an IP rate limit that answers
429 for a while afterwards, so it fails on cheap problems first.

`garmin-login` prompts for the Garmin password and MFA code, uses them once,
and stores only the resulting session tokens — encrypted, with the key in
`secrets/credentials.key` rather than in the database. **Back that file up.**
Losing it means re-authenticating every stored session; leaking it makes the
encrypted column pointless. `secrets/` is gitignored.

```sh
docker compose exec app python -m app.manage garmin-status    # freshness, last error
docker compose exec app python -m app.manage garmin-forget <email>
docker compose logs -f poller
```

The poller re-fetches a trailing window (`GARMIN_POLL_DAYS`, default 3) every
`GARMIN_POLL_INTERVAL` seconds. The overlap is deliberate: sleep lands late and
Garmin revises figures, and measurements upsert so re-fetching costs nothing.

Because a sidecar has no exit code for anyone to read, a failed cycle makes the
container **unhealthy** — `docker compose ps` shows it — and per-account
failures also land in `diet.sync_state`, which drives the Garmin line on the
status page.

**The System tab is the first place to look when nothing arrives.** It names
which of the four failure points you are at — no stored session, a poller that
never runs, a poller that runs and fails, or syncs that succeed while Garmin
returns nothing new — and gives the command for that case. Underneath it lists
every metric with when it was last seen, so a field that has moved shows up as
one row falling behind the others.

If a metric stops arriving, Garmin has moved a field. Raw payloads are archived
before parsing:

```sh
docker compose run --rm poller --once --days 1     # logs the numeric keys it saw
docker compose exec poller ls /var/log/diet/garmin/
```

Correct `DAILY_SPECS` / `SLEEP_SPECS` in [app/garmin.py](app/garmin.py) to match.

## Admin

```sh
./adduser.sh [email] [name] [timezone]      # create an account (wraps the next line)
./adduser.sh --garmin <email>               # connect Garmin to an existing one
docker compose exec app python -m app.manage adduser <email> <name> [timezone]
docker compose exec app python -m app.manage passwd <email>      # revokes every credential
docker compose exec app python -m app.manage seed-demo <email>   # sample rows for the UI
docker compose exec app python -m app.manage issue-token <email> [--label L] [--days N]
docker compose exec app python -m app.manage list-tokens
docker compose exec app python -m app.manage revoke-token <email> <label>
docker compose exec app python -m app.manage list-connections
docker compose exec app python -m app.manage revoke-connection <email> [client_id]
docker compose exec db psql -U diet_owner diet
```

## Layout

```
app/schema.sql   the whole database: roles, tables, RLS policies, grants
app/db.py        the pool and the two transaction wrappers (the only cursors)
app/auth.py      passwords, sessions, the login throttle
app/queries.py   read queries — none of them filters by user, RLS already did
app/writes.py    the write paths; rows are stamped by diet.current_user_id()
app/mcp_server.py  the five MCP tools
app/mcp_auth.py  bearer-token verification against auth.api_tokens
app/oauth_provider.py  the OAuth 2.1 authorization server, over Postgres
app/oauth_routes.py    the consent screen — the one part FastMCP cannot supply
app/main.py      routes, and the /mcp mount
app/garmin.py    Garmin session and the defensive field mapping
app/poller.py    the sync loop; one shot or sidecar
app/secretbox.py credential encryption, key outside the database
app/manage.py    admin CLI
web/index.html   the frontend
adduser.sh       account creation and Garmin connection, with the checks
                 worth doing before a password or an MFA code is spent
stub_api.py      superseded; kept only as a no-database way to serve the page
```

## Roles

| Role | Used by | Can |
| --- | --- | --- |
| `diet_owner` | migrations, admin CLI | everything; the app never connects as it |
| `diet_app` | the running app | DML on `diet.*` under RLS; identity only via definer functions; DML on `auth.oauth_*` because it *is* the authorization server |
| `diet_ro` | the MCP `query_sql` tool | `SELECT` on `diet.*` under RLS; cannot see `auth.*` at all |

Passwords for the latter two come from `.env` and are re-applied on every
startup, so rotating one is an edit and a restart.

## Local development, without Docker

```sh
.venv/bin/python -m uvicorn app.main:app --reload --port 8080
```

with `DATABASE_URL` and `MIGRATION_DATABASE_URL` pointed at any Postgres 13+.
