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
}
```

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

No endpoint takes a user id. There is no argument by which a client — or a
model on a future MCP tool — can name anyone but itself.

## Admin

```sh
docker compose exec app python -m app.manage adduser <email> <name> [timezone]
docker compose exec app python -m app.manage passwd <email>      # also revokes sessions
docker compose exec app python -m app.manage seed-demo <email>   # sample rows for the UI
docker compose exec db psql -U diet_owner diet
```

## Layout

```
app/schema.sql   the whole database: roles, tables, RLS policies, grants
app/db.py        the pool and the two transaction wrappers (the only cursors)
app/auth.py      passwords, sessions, the login throttle
app/queries.py   read queries — none of them filters by user, RLS already did
app/main.py      routes
app/manage.py    admin CLI
web/index.html   the frontend
stub_api.py      superseded; kept only as a no-database way to serve the page
```

## Roles

| Role | Used by | Can |
| --- | --- | --- |
| `diet_owner` | migrations, admin CLI | everything; the app never connects as it |
| `diet_app` | the running app | DML on `diet.*` under RLS; `auth.*` only via four definer functions |
| `diet_ro` | the future MCP `query_sql` tool | `SELECT` on `diet.*` under RLS; cannot see `auth.*` at all |

Passwords for the latter two come from `.env` and are re-applied on every
startup, so rotating one is an edit and a restart.

## Local development, without Docker

```sh
.venv/bin/python -m uvicorn app.main:app --reload --port 8080
```

with `DATABASE_URL` and `MIGRATION_DATABASE_URL` pointed at any Postgres 13+.
