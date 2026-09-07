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
docker compose exec app python -m app.manage adduser you@example.com "Your Name" Europe/Budapest
```

Then open `http://127.0.0.1:8080` and sign in.

That is the whole deploy. The app applies its own schema at startup under an
advisory lock, so there is no migration step to remember and no reliance on the
Postgres image's first-boot hooks — redeploying onto an existing volume works.

**Before exposing it:** set `COOKIE_SECURE=1` in `.env` (the generated file ships
with `0` so login works over plain http on localhost) and put TLS in front.

## Behind nginx

The app binds to loopback only. A minimal front:

```nginx
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
}
```

Do not put CDN bot-protection in front of `/mcp`. Anthropic's egress range is
`160.79.104.0/21`, and a WAF that lets the OAuth traffic through while blocking
the authenticated POSTs produces a failure that is invisible in origin logs.

`X-Forwarded-For` matters: it is what the login rate limiter counts against.

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

Five tools at `POST /mcp`: `log_meal`, `correct_meal`, `get_day`, `get_range`,
`query_sql`. Two ways to authenticate, on the same server:

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

## Admin

```sh
docker compose exec app python -m app.manage adduser <email> <name> [timezone]
docker compose exec app python -m app.manage passwd <email>      # also revokes sessions
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
app/manage.py    admin CLI
web/index.html   the frontend
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
