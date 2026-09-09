"""
The consent screen.

FastMCP provides every OAuth endpoint except this one, because deciding who is
granting access and whether they meant to is not something a library can do for
you. It is the only place in the OAuth flow where a human is involved.

Identity here comes from the ordinary session cookie, so approving the Claude
connector is not a second login and not a second account -- the same rows in
auth.users back both. Someone already signed in sees one screen with two
buttons; someone signed out signs in first, on this page, and stays on it.
"""

from __future__ import annotations

import html
import logging
from urllib.parse import urlsplit

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from mcp.server.auth.provider import AuthorizeError

from . import auth, config, oauth_provider

log = logging.getLogger("diet.oauth.consent")
settings = config.load()
router = APIRouter()

_STYLE = """
:root {
  --paper:#F6F1E4; --paper-line:#DCD4BC; --ink:#2B2A24; --ink-soft:#6B6553;
  --tomato:#B5452B; --leaf:#4A6B3D; --leaf-dark:#33492A; --card:#FFFDF6;
}
* { box-sizing:border-box; }
body {
  margin:0; padding:44px 16px 60px; background:var(--paper);
  background-image:repeating-linear-gradient(var(--paper-line) 0 1px, transparent 1px 32px);
  color:var(--ink); font-family:'Karla','Segoe UI',system-ui,sans-serif; font-size:15px;
  line-height:1.55;
}
.card {
  max-width:30rem; margin:0 auto; background:var(--card); border:1.5px solid var(--ink);
  box-shadow:4px 4px 0 var(--ink); padding:24px 26px;
}
h1 {
  font-family:'Fraunces',Georgia,serif; font-weight:600; font-size:1.35rem;
  margin:0 0 10px; letter-spacing:-0.01em;
}
p { margin:8px 0; color:var(--ink-soft); }
strong { color:var(--ink); font-weight:700; }
ul {
  margin:10px 0 0; padding-left:1.1rem; color:var(--ink-soft);
  font-family:'IBM Plex Mono',ui-monospace,monospace; font-size:0.8rem;
}
.who {
  font-family:'IBM Plex Mono',ui-monospace,monospace; font-size:0.76rem; color:var(--ink-soft);
  margin-top:20px; border-top:1px dotted var(--paper-line); padding-top:12px; line-height:1.7;
}
label {
  display:grid; gap:0.2rem; margin-bottom:0.8rem;
  font-family:'IBM Plex Mono',ui-monospace,monospace; font-size:0.76rem; color:var(--ink-soft);
}
input {
  font-family:'Karla',sans-serif; font-size:0.92rem; padding:9px 10px;
  border:1.5px solid var(--ink); border-radius:3px; background:var(--paper); color:var(--ink);
}
input:focus { outline:2px solid var(--leaf); outline-offset:1px; }
form.actions { display:flex; gap:10px; margin-top:22px; }
button {
  font-family:'IBM Plex Mono',ui-monospace,monospace; font-size:0.85rem; font-weight:600;
  border-radius:3px; padding:10px 18px; cursor:pointer;
  border:1.5px solid var(--ink); background:var(--paper); color:var(--ink);
}
button:hover { background:var(--card); }
button.primary { background:var(--leaf-dark); border-color:var(--leaf-dark); color:var(--card); }
button.primary:hover { background:var(--leaf); border-color:var(--leaf); }
.err {
  color:var(--tomato); font-size:0.8rem; min-height:1.2em; margin-bottom:6px;
  font-family:'IBM Plex Mono',ui-monospace,monospace;
}
code {
  font-family:'IBM Plex Mono',ui-monospace,monospace; font-size:0.85em; color:var(--ink);
  /* anywhere, not break-all: only break when the URL genuinely overflows,
     rather than splitting "https" across lines. */
  overflow-wrap:anywhere; display:block; margin-top:2px;
}
"""


def _origin(url: str) -> str | None:
    """Scheme and authority of a redirect URI, for use in a CSP form-action."""
    try:
        parts = urlsplit(str(url))
    except ValueError:
        return None
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else None


def _csp(redirect_uri: str | None = None) -> str:
    """
    The app-wide policy sets form-action 'self', which is right for the SPA and
    completely wrong here: approving is a form submission whose entire purpose
    is to redirect to the client's callback, and form-action governs the
    redirect chain as well as the initial POST. With 'self' alone the browser
    silently refuses to navigate and the approval appears to do nothing.
    So the client's origin is added, per request, from the redirect URI the
    authorization server already validated against the registration.
    """
    origin = _origin(redirect_uri) if redirect_uri else None
    form_action = "'self'" + (f" {origin}" if origin else "")
    return ("default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            f"form-action {form_action}; frame-ancestors 'none'; base-uri 'none'")


def _page(title: str, body: str, status: int = 200,
          redirect_uri: str | None = None) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><html lang=en><head><meta charset=utf-8>"
        f"<meta name=viewport content='width=device-width, initial-scale=1'>"
        f"<title>{html.escape(title)}</title>"
        f"<link rel=stylesheet href='/static/fonts.css'>"
        f"<style>{_STYLE}</style></head>"
        f"<body><div class=card>{body}</div></body></html>", status_code=status,
        # Set explicitly so the middleware's setdefault leaves it alone.
        headers={"Content-Security-Policy": _csp(redirect_uri),
                 # This page prints the account's name and email and the client
                 # asking for access; it must not sit in a shared cache.
                 "Cache-Control": "no-store"})


def _problem(message: str, status: int = 400) -> HTMLResponse:
    return _page("Authorisation problem",
                 f"<h1>That did not work</h1><p>{html.escape(message)}</p>"
                 "<p>Close this window and start the connection again.</p>", status)


def _login_form(pending_id: str, client_name: str) -> HTMLResponse:
    # Signed out: sign in here rather than bouncing to the app and losing the
    # pending request. Posts to the same /api/login the page uses.
    return _page("Sign in", f"""
      <h1>Sign in to continue</h1>
      <p><strong>{html.escape(client_name)}</strong> is asking for access to your diet log.
         Sign in to decide.</p>
      <form id=f style="margin-top:1.2rem">
        <label>Email<input type=email name=email autocomplete=username required></label>
        <label>Password<input type=password name=password autocomplete=current-password required></label>
        <div class=err id=e></div>
        <button class=primary type=submit>Sign in</button>
      </form>
      <script>
      document.getElementById('f').addEventListener('submit', async (ev) => {{
        ev.preventDefault();
        const d = new FormData(ev.target);
        const r = await fetch('/api/login', {{
          method: 'POST', headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify({{email: d.get('email'), password: d.get('password')}})
        }});
        if (r.ok) location.reload();
        else document.getElementById('e').textContent =
          r.status === 401 ? 'That email and password did not match.'
                           : 'Sign-in failed (' + r.status + ').';
      }});
      </script>""")


@router.get("/oauth/consent", include_in_schema=False)
async def consent_page(request: Request, pending: str = ""):
    req = await oauth_provider.pending_request(pending)
    if req is None:
        return _problem("This authorisation request has expired or was already used.")

    user = auth.resolve(request.cookies.get(settings.cookie_name))
    # client_name arrives from dynamic client registration, so it is written by
    # whoever registered. Escaped everywhere it is shown.
    name = html.escape(req["client_name"])
    if user is None:
        return _login_form(pending, req["client_name"])

    # Bind this pending request to the viewing session before rendering, so the
    # POST can require the same one back.
    await oauth_provider.bind_to_session(
        pending, auth.token_digest(request.cookies.get(settings.cookie_name, "")))

    scopes = req["scopes"] or []
    scope_list = ("<ul>" + "".join(f"<li>{html.escape(s)}</li>" for s in scopes) + "</ul>"
                  if scopes else "")
    return _page("Authorise access", redirect_uri=str(req["redirect_uri"]), body=f"""
      <h1>Allow {name} to use your diet log?</h1>
      <p>It will be able to read your meals and measurements, and to log and
         correct meals on your behalf. It cannot see your password, your
         sessions, or anyone else's data.</p>
      {scope_list}
      <form class=actions method=post action="/oauth/consent">
        <input type=hidden name=pending value="{html.escape(req['pending_id'])}">
        <button class=primary type=submit name=decision value=allow>Allow</button>
        <button type=submit name=decision value=deny>Deny</button>
      </form>
      <div class=who>Signed in as {html.escape(user.display_name)}
        ({html.escape(user.email)}). Redirects to
        <code>{html.escape(str(req['redirect_uri']))}</code></div>
    """)


@router.post("/oauth/consent", include_in_schema=False)
async def consent_submit(request: Request, pending: str = Form(...),
                         decision: str = Form(...)):
    user = auth.resolve(request.cookies.get(settings.cookie_name))
    if user is None:
        return _problem("Your session expired before you decided. Start again.", 401)
    session_hash = auth.token_digest(request.cookies.get(settings.cookie_name, ""))
    try:
        target = (await oauth_provider.approve(pending, user.user_id, session_hash)
                  if decision == "allow" else await oauth_provider.deny(pending))
    except AuthorizeError as exc:
        return _problem(exc.error_description or exc.error)
    # 303, never 307/308: a method-preserving redirect here would make the
    # browser POST to Claude's callback, which only answers GET. That is a
    # documented way to break the handshake.
    return RedirectResponse(target, status_code=303,
                            headers={"Cache-Control": "no-store",
                                     "Content-Security-Policy": _csp(target)})
