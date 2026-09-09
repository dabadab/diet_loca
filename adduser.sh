#!/usr/bin/env bash
#
# Account setup for the diet log.
#
#   ./adduser.sh                                             prompt for everything
#   ./adduser.sh you@example.com "Your Name" Europe/Budapest create an account
#   ./adduser.sh --garmin you@example.com                    connect Garmin only
#   ./adduser.sh --no-garmin you@example.com "You" UTC       create, skip Garmin
#
# Wraps `docker compose exec app python -m app.manage`, checking the things that
# are cheap to check here rather than after a password has been typed twice, or
# after an MFA code has been spent.

set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

die()  { printf '\n%s\n' "$*" >&2; exit 1; }
note() { printf '%s\n' "$*"; }
have_tty() { [ -t 0 ] && [ -t 1 ]; }

# --- environment ----------------------------------------------------------

setup_env() {
  [ -f compose.yml ] || die "compose.yml not found next to this script."
  command -v docker >/dev/null || die "docker is not on PATH."
  # shellcheck disable=SC1091
  [ -f .env ] && set -a && . ./.env && set +a
  PGUSER_="${POSTGRES_USER:-diet_owner}"
  PGDB_="${POSTGRES_DB:-diet}"
  for svc in app db; do
    [ -n "$(docker compose ps -q "$svc" 2>/dev/null)" ] \
      || die "The $svc container is not running. Start it with:
    docker compose up -d"
  done
}

# The query arrives on stdin, not via -c: psql only expands :'var' for input it
# parses itself, and :'var' is what quotes the value safely.
psql_() { docker compose exec -T db psql -U "$PGUSER_" -d "$PGDB_" -tA "$@"; }
query() { printf '%s\n' "$2" | psql_ -v v="$1"; }

# Run a manage.py subcommand, giving it a terminal when we have one.
manage() {
  if have_tty; then docker compose exec    app python -m app.manage "$@"
  else             docker compose exec -T app python -m app.manage "$@"; fi
}

user_exists()   { [ "$(query "$1" "SELECT count(*) FROM auth.users WHERE email = :'v'")" != "0" ]; }
has_garmin()    { [ "$(query "$1" "SELECT count(*) FROM diet.garmin_credentials c
                        JOIN auth.users u USING (user_id) WHERE u.email = :'v'")" != "0" ]; }

ask() {  # ask <prompt> <default>; requires a terminal when there is no default
  local reply
  if ! have_tty; then printf '%s' "$2"; return; fi
  read -rp "$1" reply
  printf '%s' "${reply:-$2}"
}

# --- creating an account ---------------------------------------------------

add_user() {
  local email="$1" display="$2" tz="$3"

  [ -n "$email" ] || email="$(ask 'Email: ' '')"
  email="$(printf '%s' "$email" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')"
  [ -n "$email" ] || die "An email address is required."
  case "$email" in *@*.*) ;; *) die "'$email' does not look like an email address." ;; esac

  [ -n "$display" ] || display="$(ask 'Display name: ' '')"
  [ -n "$display" ] || die "A display name is required."

  [ -n "$tz" ] || tz="$(ask "Timezone [${TZ:-UTC}]: " "${TZ:-UTC}")"

  # Postgres is the authority: auth.users has a trigger rejecting anything not
  # in pg_timezone_names. Every local_date is derived from this, so a typo
  # silently misfiles every row the account ever writes.
  if [ "$(query "$tz" "SELECT count(*) FROM pg_timezone_names WHERE name = :'v'")" != "1" ]; then
    printf '\nUnknown timezone: %s\n' "$tz" >&2
    local near
    near="$(query "$tz" "SELECT string_agg(name, ', ') FROM (SELECT name FROM pg_timezone_names
       WHERE name ILIKE '%' || :'v' || '%' ORDER BY name LIMIT 8) s" || true)"
    [ -n "$near" ] && printf 'Did you mean: %s\n' "$near" >&2
    die "Pick a name from pg_timezone_names, e.g. Europe/Budapest."
  fi

  user_exists "$email" && die "$email already has an account. To change its password:
    docker compose exec app python -m app.manage passwd $email
To connect Garmin to it:
    ./adduser.sh --garmin $email"

  printf '\nCreating %s (%s, %s)\n' "$email" "$display" "$tz"
  note 'The password is asked for twice and must be at least 10 characters.'
  printf '\n'
  manage adduser "$email" "$display" "$tz"
  ACCOUNT_EMAIL="$email"
}

# --- connecting Garmin -----------------------------------------------------

garmin_login() {
  local email="$1"

  [ -n "$email" ] || email="$(ask 'Account email: ' '')"
  email="$(printf '%s' "$email" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')"
  [ -n "$email" ] || die "An account email is required."
  user_exists "$email" || die "No account for $email. Create one first:
    ./adduser.sh $email"

  # Garmin asks for a password and then an MFA code that expires in seconds.
  # Piping those is not realistic, so refuse rather than half-run it.
  have_tty || die "Connecting Garmin needs a terminal: it prompts for the Garmin
password and then an MFA code. Run this from an interactive shell:
    ./adduser.sh --garmin $email"

  if has_garmin "$email"; then
    local yn
    yn="$(ask "$email already has a stored Garmin session. Replace it? [y/N] " "n")"
    case "$yn" in [Yy]*) ;; *) note "Left the existing session in place."; return 0 ;; esac
  fi

  printf '\nConnecting Garmin for %s\n' "$email"
  note 'The Garmin password is used once and never stored; only the resulting'
  note 'session token is kept, encrypted. An MFA code will be asked for.'
  note 'Garmin rate-limits logins: if this returns 429, wait before retrying.'
  printf '\n'
  manage garmin-login "$email"

  local yn
  yn="$(ask 'Backfill the last 30 days from Garmin now? [Y/n] ' 'y')"
  case "$yn" in
    [Nn]*) note "Skipped. The poller will pick up recent days on its next cycle." ;;
    *) docker compose run --rm poller --once --days 30 --user "$email" ;;
  esac
}

# --- next steps ------------------------------------------------------------

next_steps() {
  local email="$1"
  cat <<EOF

Done. Next, as needed:

  Sign in            ${MCP_PUBLIC_URL:-http://127.0.0.1:${APP_PORT:-8080}}
  MCP bearer token   docker compose exec app python -m app.manage issue-token $email
  Connect Garmin     ./adduser.sh --garmin $email
  Garmin status      docker compose exec app python -m app.manage garmin-status
EOF
}

# --- entry -----------------------------------------------------------------

main() {
  local mode="adduser" want_garmin="ask"
  while [ $# -gt 0 ]; do
    case "$1" in
      --garmin)    mode="garmin"; shift ;;
      --no-garmin) want_garmin="no"; shift ;;
      -h|--help)   awk 'NR>1 && /^#/ {sub(/^# ?/,""); print; next} NR>1 {exit}' "$0"; exit 0 ;;
      -*)          die "Unknown option: $1  (try --help)" ;;
      *)           break ;;
    esac
  done

  setup_env

  if [ "$mode" = "garmin" ]; then
    garmin_login "${1-}"
    exit 0
  fi

  add_user "${1-}" "${2-}" "${3-}"

  if [ "$want_garmin" != "no" ]; then
    local yn
    yn="$(ask $'\nConnect a Garmin account to it now? [y/N] ' 'n')"
    case "$yn" in [Yy]*) garmin_login "$ACCOUNT_EMAIL" ;; esac
  fi

  next_steps "$ACCOUNT_EMAIL"
}

main "$@"
