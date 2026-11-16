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
import secrets
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

TIMEOUT = 10
DONE_PAGE = """<!doctype html><meta charset="utf-8"><title>SprigCast</title>
<body style="background:#0e1311;color:#e6ede8;font:16px system-ui;display:grid;place-items:center;height:90vh">
<div style="text-align:center"><h2 style="color:#4fd88f">{title}</h2><p>{text}</p></div>"""


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
                page = DONE_PAGE.format(title="You're signed in" if ok else "Sign-in didn't finish",
                                        text="You can close this tab and go back to SprigCast." if ok
                                        else "Go back to SprigCast and try again.")
                body = page.encode()
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

    def wait(self, timeout: float, cancelled: threading.Event | None = None) -> str:
        """The code, once the browser comes back. Raises SignInError/Cancelled."""
        try:
            step = 0.25
            waited = 0.0
            while not self._done.wait(step):
                waited += step
                if cancelled is not None and cancelled.is_set():
                    raise Cancelled("Sign-in cancelled.")
                if waited >= timeout:
                    raise SignInError("Sign-in took too long. Try again.")
            query = self.result or {}
            if query.get("state") != self.state:
                raise SignInError("Sign-in didn't match. Try again.")
            if "error" in query:
                if query["error"] == "access_denied":
                    raise SignInError("Sign-in was declined.")
                raise SignInError(f"Sign-in failed: {query.get('error_description') or query['error']}")
            if not query.get("code"):
                raise SignInError("Sign-in didn't return a code. Try again.")
            return query["code"]
        finally:
            self.stop()

    def stop(self) -> None:
        for server in self._servers:
            server.shutdown()
            server.server_close()
        self._servers = []


def post_form(url: str, fields: dict, who: str) -> dict:
    """POST a form; the JSON reply, or {"error": ...} with the status on an HTTP error."""
    request = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode(), method="POST",
                                     headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read() or b"{}")
        except ValueError:
            body = {}
        return {"error": body.get("error", body) or exc.code, "status": exc.code,
                "error_description": body.get("error_description", "")}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SignInError(f"Couldn't reach {who}: {exc}") from exc


def get_json(url: str, token: str, who: str, headers: dict | None = None) -> dict:
    """GET with a bearer token. Raises PermissionError on 401 (refresh and retry)."""
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json",
                                                   **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise PermissionError("token rejected") from exc
        try:
            detail = json.loads(exc.read() or b"{}")
        except ValueError:
            detail = {}
        message = (detail.get("error") or {}).get("message") if isinstance(detail.get("error"), dict) \
            else detail.get("message")
        raise SignInError(f"{who} said {exc.code}" + (f": {message}" if message else "")) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SignInError(f"Couldn't reach {who}: {exc}") from exc
