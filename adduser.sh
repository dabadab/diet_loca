#!/usr/bin/env bash
#
# Add an account to the diet log.
#
#   ./adduser.sh                                            # prompts for everything
#   ./adduser.sh you@example.com "Your Name" Europe/Budapest
#
# Wraps `docker compose exec app python -m app.manage adduser`, checking the
# things that are cheap to check here rather than after you have typed a
# password twice: that the stack is up, that the address is not already taken,
# and that the timezone is one Postgres will accept. That last one matters more
# than it looks -- every local_date is derived from it, so a typo silently
# misfiles every row the account ever writes.

set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

die() { printf '\n%s\n' "$*" >&2; exit 1; }

[ -f compose.yml ] || die "compose.yml not found next to this script."
command -v docker >/dev/null || die "docker is not on PATH."

# POSTGRES_USER / POSTGRES_DB, defaulted the same way compose.yml defaults them.
# shellcheck disable=SC1091
[ -f .env ] && set -a && . ./.env && set +a
PGUSER_="${POSTGRES_USER:-diet_owner}"
PGDB_="${POSTGRES_DB:-diet}"

running() { [ -n "$(docker compose ps -q "$1" 2>/dev/null)" ]; }
running app || die "The app container is not running. Start it with:
    docker compose up -d"
running db  || die "The database container is not running. Start it with:
    docker compose up -d"

# The query comes in on stdin, not via -c: psql only expands :'var' for input
# it parses itself, and :'var' is what quotes the value safely.
psql_() { docker compose exec -T db psql -U "$PGUSER_" -d "$PGDB_" -tA "$@"; }

email="${1-}"; display="${2-}"; tz="${3-}"

if [ -z "$email" ]; then read -rp "Email: " email; fi
email="$(printf '%s' "$email" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')"
[ -n "$email" ] || die "An email address is required."
case "$email" in
  *@*.*) ;;
  *) die "'$email' does not look like an email address." ;;
esac

if [ -z "$display" ]; then read -rp "Display name: " display; fi
[ -n "$display" ] || die "A display name is required."

if [ -z "$tz" ]; then
  read -rp "Timezone [${TZ:-UTC}]: " tz
  tz="${tz:-${TZ:-UTC}}"
fi

# Postgres is the authority here: auth.users has a trigger that rejects
# anything not in pg_timezone_names, so ask the same table.
known="$(printf '%s\n' "SELECT count(*) FROM pg_timezone_names WHERE name = :'tz'" \
  | psql_ -v tz="$tz")"
if [ "$known" != "1" ]; then
  printf '\nUnknown timezone: %s\n' "$tz" >&2
  near="$(printf '%s\n' "SELECT string_agg(name, ', ') FROM (SELECT name FROM pg_timezone_names
     WHERE name ILIKE '%' || :'tz' || '%' ORDER BY name LIMIT 8) s" | psql_ -v tz="$tz" || true)"
  [ -n "$near" ] && printf 'Did you mean: %s\n' "$near" >&2
  die "Pick a name from pg_timezone_names, e.g. Europe/Budapest."
fi

taken="$(printf '%s\n' "SELECT count(*) FROM auth.users WHERE email = :'e'" | psql_ -v e="$email")"
if [ "$taken" != "0" ]; then
  die "$email already has an account. To change its password instead:
    docker compose exec app python -m app.manage passwd $email"
fi

printf '\nCreating %s (%s, %s)\n' "$email" "$display" "$tz"
printf 'The password is asked for twice and must be at least 10 characters.\n\n'

# adduser prompts for the password, so give it a TTY when we have one. Without
# one (a pipe, CI) fall back to -T and let it read the two lines from stdin.
if [ -t 0 ] && [ -t 1 ]; then
  docker compose exec app python -m app.manage adduser "$email" "$display" "$tz"
else
  docker compose exec -T app python -m app.manage adduser "$email" "$display" "$tz"
fi

cat <<EOF

Done. Next, as needed:

  Sign in            ${MCP_PUBLIC_URL:-http://127.0.0.1:${APP_PORT:-8080}}
  MCP bearer token   docker compose exec app python -m app.manage issue-token $email
  Connect Garmin     docker compose exec app python -m app.manage garmin-login $email
  Backfill Garmin    docker compose run --rm poller --once --days 30 --user $email
EOF
