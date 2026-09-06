#!/usr/bin/env python3
"""
Placeholder backend for the diet tracker web UI. SUPERSEDED by app/main.py --
kept only as a zero-dependency way to serve the page with no database at all.
Do not deploy it: its identity comes from a client-supplied header.

Stdlib only, no dependencies. Serves index.html plus three JSON endpoints so the
browser -> nginx -> app path can be verified before Postgres, the Garmin poller
or the OAuth server exist.

    python3 stub_api.py            # http://127.0.0.1:8080
    python3 stub_api.py 0.0.0.0 8080

IDENTITY, AND WHY IT IS FAKE HERE
---------------------------------
Every response is scoped to one user. In this stub the user is taken from the
X-Debug-User header or ?as= query parameter, which exists ONLY so you can click
between users while testing. In the real service, resolve_user() must read the
subject of the session cookie / bearer token and nothing else -- never a value
the client can choose. The rest of the code is written so that swapping the body
of resolve_user() is the only change needed.
"""

import json
import sys
import time
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent

# --- fake data -------------------------------------------------------------

USERS = {
    "u_joe": {"user_id": "u_joe", "display_name": "Joe", "timezone": "Europe/Budapest"},
    "u_guest": {"user_id": "u_guest", "display_name": "Guest", "timezone": "Europe/Vienna"},
}
DEFAULT_USER = "u_joe"


def fake_days(user_id, n=7):
    """Deterministic per-user sample rows, newest first."""
    seed = sum(ord(c) for c in user_id)
    out = []
    for i in range(n):
        d = date.today() - timedelta(days=i)
        wobble = (seed + i * 7) % 11
        out.append(
            {
                "date": d.isoformat(),
                "energy_in_kcal": 1800 + wobble * 40,
                "energy_out_kcal": 2300 + wobble * 25,
                "weight_kg": round(84.0 - i * 0.08 + wobble * 0.03, 1) if seed % 3 else None,
                "meals_logged": 2 + (wobble % 3),
                # source shows which writer produced the intake figure; the UI
                # renders estimated and measured differently on purpose.
                "intake_source": "claude-estimate",
            }
        )
    return out


def fake_status(user_id):
    """One entry per stage of the chain, in data-flow order."""
    return [
        {"stage": "Web server", "state": "ok", "detail": "static files served", "ms": 2},
        {"stage": "API", "state": "ok", "detail": "stub, no database attached", "ms": 4},
        {"stage": "Sign-in", "state": "ok", "detail": f"resolved as {user_id}", "ms": 1},
        {"stage": "Database", "state": "down", "detail": "not connected yet", "ms": None},
        {"stage": "Garmin sync", "state": "stale", "detail": "last run 3 days ago", "ms": None},
        {"stage": "Claude connector", "state": "down", "detail": "not registered yet", "ms": None},
    ]


# --- server ----------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "diet-stub/0.1"

    def resolve_user(self):
        """REPLACE THIS with session/token lookup. Returns a user dict or None."""
        qs = parse_qs(urlparse(self.path).query)
        uid = self.headers.get("X-Debug-User") or (qs.get("as") or [DEFAULT_USER])[0]
        return USERS.get(uid)

    def send_json(self, obj, status=200):
        body = json.dumps(obj, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, ctype):
        try:
            body = path.read_bytes()
        except OSError:
            return self.send_json({"error": "not_found", "path": str(path.name)}, 404)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        route = urlparse(self.path).path

        if route in ("/", "/index.html"):
            return self.send_file(HERE / "web" / "index.html", "text/html; charset=utf-8")

        if not route.startswith("/api/"):
            return self.send_json({"error": "not_found"}, 404)

        user = self.resolve_user()
        if user is None:
            # Shape matters: the UI distinguishes "signed out" from "backend broken".
            return self.send_json({"error": "unauthenticated"}, 401)

        if route == "/api/me":
            return self.send_json({**user, "users": list(USERS)})
        if route == "/api/status":
            return self.send_json({"checked_at": time.time(), "stages": fake_status(user["user_id"])})
        if route == "/api/days":
            qs = parse_qs(urlparse(self.path).query)
            n = min(int((qs.get("days") or [7])[0]), 60)
            return self.send_json({"user_id": user["user_id"], "days": fake_days(user["user_id"], n)})

        return self.send_json({"error": "not_found"}, 404)

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.log_date_time_string(), fmt % args))


if __name__ == "__main__":
    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8080
    print(f"serving on http://{host}:{port}  (ctrl-c to stop)", file=sys.stderr)
    ThreadingHTTPServer((host, port), Handler).serve_forever()
