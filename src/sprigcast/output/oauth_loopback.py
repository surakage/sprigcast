"""Browser sign-in for a desktop app: OAuth 2 authorization code with PKCE and a loopback redirect.

SprigCast opens the service's sign-in page in your browser; after you approve, the browser comes back
to a tiny one-shot web server on 127.0.0.1 (a random free port), which hands SprigCast the code. The
code plus the PKCE verifier buys the tokens. Nothing listens once sign-in ends.

Used by YouTube (Google) and Kick. Twitch uses its device code instead (output/twitch_auth.py).
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import logging
import secrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

TIMEOUT = 10


def _user_agent() -> str:
    try:
        from sprigcast import __version__
    except Exception:  # noqa: BLE001
        __version__ = "0"
    return f"SprigCast/{__version__} (+https://slothbloom.com)"


# Who's asking, on every sign-in request. Python's own "Python-urllib/3.x" is on Cloudflare's list of
# abused clients: Browser Integrity Check answered it with Error 1010 (2026-11-16). X-SprigCast lets a
# Cloudflare rule on signin.slothbloom.com recognise the app.
HEADERS = {"User-Agent": _user_agent(), "X-SprigCast": _user_agent().split()[0].split("/")[1]}


def problem_text(reply: dict) -> str:
    """A refused reply's error, in words: never the raw dict (Cloudflare answers with a JSON problem)."""
    error = reply.get("error")
    if isinstance(error, dict):  # Cloudflare's own error page, as JSON: {"title": "Error 1010: Access denied"...}
        title = str(error.get("title") or error.get("detail") or "refused")
        return f"the sign-in service turned SprigCast away ({title}). Try again later, or paste the stream key instead"
    if error == "slow_down":
        return "too many sign-ins in a minute. Wait a minute and try again"
    detail = str(reply.get("error_description") or "").strip()
    if detail:
        return detail
    if error in (None, ""):
        return "no answer"
    return f"{error}" + (f" ({reply['status']})" if reply.get("status") else "")
def _logo() -> str:
    """SprigCast's app icon (data/logo.svg), inline, or "" if it isn't there."""
    from importlib import resources

    try:
        svg = resources.files("sprigcast.data").joinpath("logo.svg").read_text(encoding="utf-8")
    except (OSError, ModuleNotFoundError):
        return ""
    return svg.replace('width="512" height="512"', 'width="56" height="56" aria-hidden="true"', 1)


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{title} · SprigCast</title>
<style>
:root {{ color-scheme: dark; }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; padding: 24px;
  background: radial-gradient(1200px 600px at 50% -10%, #16261e 0%, #0e1311 60%) #0e1311;
  color: #e6ede8; font: 15px/1.5 "Segoe UI Variable Text", "Segoe UI", system-ui, sans-serif; }}
.card {{ width: 100%; max-width: 420px; background: #151c19; border: 1px solid #1f2825; border-radius: 22px;
  padding: 36px 32px 30px; text-align: center; box-shadow: 0 24px 60px rgba(0, 0, 0, .45); }}
.brand {{ display: flex; align-items: center; justify-content: center; gap: 10px; margin-bottom: 28px;
  color: #8b9a92; font-size: 13px; letter-spacing: .04em; }}
.brand svg {{ width: 28px; height: 28px; }}
.mark {{ width: 64px; height: 64px; margin: 0 auto 18px; border-radius: 50%; display: grid; place-items: center;
  background: {mark_bg}; color: {mark_fg}; }}
.mark svg {{ width: 30px; height: 30px; }}
h1 {{ margin: 0 0 8px; font-size: 22px; font-weight: 600; color: #f2f7f3; }}
p {{ margin: 0; color: #a9b7b0; }}
.reason {{ margin-top: 14px; padding: 10px 14px; border-radius: 12px; background: #1c2420; color: #e6ede8;
  font-size: 14px; }}
.next {{ margin-top: 26px; padding-top: 18px; border-top: 1px solid #1f2825; color: #8b9a92; font-size: 13px; }}
</style></head><body><main class="card">
<div class="brand">{logo}<span>SprigCast</span></div>
<div class="mark">{icon}</div>
<h1>{title}</h1><p>{text}</p>{reason}
<div class="next">You can close this tab and go back to SprigCast.</div>
</main></body></html>"""

TICK = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" '
        'stroke-linejoin="round"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>')
CROSS = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round">'
         '<path d="M7 7l10 10M17 7L7 17"/></svg>')


def result_page(ok: bool, label: str = "", reason: str = "") -> str:
    """The page the browser shows when it comes back to SprigCast: signed in, or why not."""
    import html

    where = html.escape(label) if label else ""
    if ok:
        title = f"Signed in to {where}" if where else "You're signed in"
        text = "SprigCast is finishing your sign-in now."  # not every sign-in fetches a key (TikTok, Stream details)
        mark = ("#1d3428", "#4fd88f", TICK)
    else:
        title = "Sign-in didn't finish"
        text = "Nothing was changed. Go back to SprigCast and try again, or paste the stream key instead."
        mark = ("#3a2a1c", "#f0b35a", CROSS)
    box = f'<div class="reason">{html.escape(reason)}</div>' if reason and not ok else ""
    return PAGE.format(title=title, text=text, reason=box, logo=_logo(), icon=mark[2], mark_bg=mark[0],
                       mark_fg=mark[1])



class SignInError(Exception):
    """Something the user has to act on, worded for them."""


class Cancelled(SignInError):
    pass


def pkce_pair() -> tuple[str, str]:
    """(verifier, S256 challenge)."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class Waiter:
    """The one-shot loopback server. `url` is the redirect URI to register and send."""

    port: int
    path: str = "/callback"
    host: str = "127.0.0.1"  # in the redirect URL; Kick wants "localhost" (its sign-in page rewrites 127.0.0.1)
    state: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    label: str = ""  # "YouTube", "Kick": named on the page the browser comes back to
    result: dict | None = None
    _done: threading.Event = field(default_factory=threading.Event)
    _servers: list = field(default_factory=list)

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}{self.path}"

    def start(self) -> None:
        waiter = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path != waiter.path:
                    self.send_error(404)
                    return
                query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
                ok = "code" in query and query.get("state") == waiter.state
                reason = ""
                if query.get("error") == "access_denied":
                    reason = f"{waiter.label or 'The service'} didn't allow the sign-in."
                elif query.get("error"):
                    reason = query.get("error_description") or query["error"]
                body = result_page(ok, waiter.label, reason).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                if waiter.result is None:
                    waiter.result = query
                    waiter._done.set()

            def log_message(self, *_args):  # quiet
                return

        self._servers = [http.server.HTTPServer(("127.0.0.1", self.port), Handler)]
        if self.host == "localhost":  # the browser may try ::1 first
            class V6(http.server.HTTPServer):
                address_family = socket.AF_INET6

            try:
                self._servers.append(V6(("::1", self.port), Handler))
            except OSError:
                pass
        for server in self._servers:
            threading.Thread(target=server.serve_forever, name="sign-in", daemon=True).start()

    def wait(self, timeout: float, cancelled: threading.Event | None = None, who: str = "The service") -> str:
        """The code, once the browser comes back. Raises SignInError/Cancelled. Stops the server either way:
        only the thread that waits may stop it (a server stopped before it serves would hang)."""
        try:
            step = 0.25
            waited = 0.0
            while not self._done.wait(step):
                waited += step
                if cancelled is not None and cancelled.is_set():
                    raise Cancelled("Sign-in canceled.")
                if waited >= timeout:
                    raise SignInError("Sign-in took too long. Try again.")
            query = self.result or {}
            log.info("sign-in: browser came back (%s)", "error=" + str(query["error"]) if "error" in query
                     else "with a code" if query.get("code") else "without a code")
            if query.get("state") != self.state:
                raise SignInError("Sign-in didn't match. Try again.")
            if "error" in query:
                if query["error"] == "access_denied":
                    raise SignInError(f"{who} didn't allow the sign-in. Try again, or paste the stream key instead.")
                raise SignInError(f"Sign-in failed: {query.get('error_description') or query['error']}")
            if not query.get("code"):
                raise SignInError("Sign-in didn't return a code. Try again.")
            return query["code"]
        finally:
            self.stop()

    def stop(self) -> None:
        servers, self._servers = self._servers, []  # a second stop finds nothing to do
        for server in servers:
            server.shutdown()
            server.server_close()


def refusal_reason(raw: bytes) -> str:
    """Why a service refused, from its reply body: its own message, Cloudflare's error title, or an HTML
    page's title. "" when the body says nothing."""
    import re

    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        body = None
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])  # Google: {"error": {"message": ...}}
        for name in ("message", "error_description", "title", "detail"):  # Kick, OAuth, Cloudflare
            if isinstance(body.get(name), str) and body[name].strip():
                text = body[name].strip()
                return f"{text} (Cloudflare)" if name == "title" and "Error 1" in text else text
        if isinstance(error, str) and error:
            return error
        return ""
    text = raw.decode(errors="replace")
    match = re.search(r"<title>(.*?)</title>", text, re.IGNORECASE | re.DOTALL)
    if match:
        return " ".join(match.group(1).split())[:120]
    return " ".join(text.split())[:120]


HELPER_TIMEOUT = 30  # SprigCast's sign-in helper: a cold start plus the service's own answer can take a while


def post_form(url: str, fields: dict, who: str, timeout: float = TIMEOUT, tries: int = 1) -> dict:
    """POST a form; the JSON reply, or {"error": ...} with the status on an HTTP error.
    tries > 1: when nothing came back (no answer, timed out), ask again, after a short wait."""
    for attempt in range(1, tries + 1):
        try:
            return _post_form_once(url, fields, who, timeout)
        except SignInError as exc:
            if attempt == tries or not isinstance(exc.__cause__, (urllib.error.URLError, TimeoutError, OSError)):
                raise
            log.info("sign-in: no answer from %s (%s), asking again", who, exc.__cause__)
            time.sleep(1)
    raise SignInError(f"Couldn't reach {who}.")  # not reached


def _post_form_once(url: str, fields: dict, who: str, timeout: float) -> dict:
    request = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode(), method="POST",
                                     headers={"Accept": "application/json", **HEADERS})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read() or b"{}")
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        log.info("sign-in: %s answered %d (%s)", who, exc.code,
                 body.get("title") or body.get("error") or "no detail")
        return {"error": body.get("error", body) or exc.code, "status": exc.code,
                "error_description": body.get("error_description", "") if "error" in body else ""}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SignInError(f"Couldn't reach {who}: {exc}") from exc


def get_json(url: str, token: str, who: str, headers: dict | None = None) -> dict:
    """GET with a bearer token. Raises PermissionError on 401 (refresh and retry)."""
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json",
                                                   **HEADERS, **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise PermissionError("token rejected") from exc
        raw = exc.read() or b""
        message = refusal_reason(raw)
        log.info("sign-in: %s answered %d: %s", who, exc.code, message or raw[:200].decode(errors="replace"))
        raise SignInError(f"{who} said {exc.code}" + (f": {message}" if message else "")) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SignInError(f"Couldn't reach {who}: {exc}") from exc
