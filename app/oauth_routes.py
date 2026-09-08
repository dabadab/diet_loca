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
:root { --paper:#edf0f1; --card:#f7f8f9; --ink:#16232b; --ink-dim:#5d6d76;
        --line:#cfd6da; --ok:#2b6b5f; --down:#9a3a2c; --focus:#1f5f8b; }
@media (prefers-color-scheme: dark) {
  :root { --paper:#151b1f; --card:#1c2429; --ink:#e2e8ea; --ink-dim:#94a3aa;
          --line:#2f3a41; --ok:#6fb6a4; --down:#d98274; --focus:#7fb6da; } }
* { box-sizing:border-box; }
body { margin:0; padding:3rem 1.25rem; background:var(--paper); color:var(--ink);
       font-family:"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif; font-size:15px;
       line-height:1.5; }
.card { max-width:30rem; margin:0 auto; background:var(--card); border:1px solid var(--line);
        border-radius:5px; padding:1.5rem 1.6rem; }
h1 { font-size:1.15rem; margin:0 0 0.4rem; }
p { margin:0.5rem 0; color:var(--ink-dim); }
strong { color:var(--ink); }
ul { margin:0.5rem 0 0; padding-left:1.1rem; color:var(--ink-dim); font-size:0.9rem; }
.who { font-size:0.85rem; color:var(--ink-dim); margin-top:1.2rem;
       border-top:1px solid var(--line); padding-top:0.8rem; }
form.actions { display:flex; gap:0.6rem; margin-top:1.4rem; }
label { display:grid; gap:0.2rem; font-size:0.85rem; color:var(--ink-dim); margin-bottom:0.7rem; }
input { font:inherit; color:var(--ink); background:var(--paper); border:1px solid var(--line);
        border-radius:3px; padding:0.4rem 0.5rem; }
button { font:inherit; font-size:0.92rem; border-radius:3px; padding:0.45rem 1.1rem;
         cursor:pointer; border:1px solid var(--line); background:var(--paper); color:var(--ink); }
button.primary { background:var(--ok); border-color:var(--ok); color:var(--paper); font-weight:500; }
button:hover { filter:brightness(1.06); }
.err { color:var(--down); font-size:0.85rem; min-height:1.2em; }
code { font-family:ui-monospace,Consolas,monospace; font-size:0.85em; word-break:break-all; }
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
        f"<title>{html.escape(title)}</title><style>{_STYLE}</style></head>"
        f"<body><div class=card>{body}</div></body></html>", status_code=status,
        # Set explicitly so the middleware's setdefault leaves it alone.
        headers={"Content-Security-Policy": _csp(redirect_uri)})


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
        ({html.escape(user.email)}). Redirects to <code>{html.escape(str(req['redirect_uri']))}</code>.</div>
    """)


@router.post("/oauth/consent", include_in_schema=False)
async def consent_submit(request: Request, pending: str = Form(...),
                         decision: str = Form(...)):
    user = auth.resolve(request.cookies.get(settings.cookie_name))
    if user is None:
        return _problem("Your session expired before you decided. Start again.", 401)
    try:
        target = (await oauth_provider.approve(pending, user.user_id)
                  if decision == "allow" else await oauth_provider.deny(pending))
    except AuthorizeError as exc:
        return _problem(exc.error_description or exc.error)
    # 303, never 307/308: a method-preserving redirect here would make the
    # browser POST to Claude's callback, which only answers GET. That is a
    # documented way to break the handshake.
    return RedirectResponse(target, status_code=303,
                            headers={"Cache-Control": "no-store",
                                     "Content-Security-Policy": _csp(target)})
