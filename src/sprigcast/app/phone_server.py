"""The Phone Camera: a phone on the same Wi-Fi sends its camera (and mic) to SprigCast, with nothing to install.

You add a Phone Camera leaf and scan its QR code. The phone opens a page SprigCast serves on your home network
(HTTPS, core/selfsigned.py: phones only give a page the camera over a secure connection), and the page sends
the camera to SprigCast over WebRTC. On the PC the leaf is a web page (sources/browser.py) that SprigCast serves
to itself on 127.0.0.1: it answers the phone and shows its video, and its sound goes to the mixer like any page.

Nothing leaves your network: no outside servers are used (not even to find a route), the page is only served to
addresses on a private network, and each leaf has its own secret code in its address. The server runs only
while the garden has a Phone Camera leaf.

The two pages agree through this server (no trickle: each sends its whole description once its local routes are
known, which on a home network is right away):
  phone   POST /phone/<code>/offer          -> {"id": n}
  page    GET  /view/<code>/offer?after=n   -> {"id": m, "sdp": ...} once a newer offer is in, else 204
  page    POST /view/<code>/answer?id=m
  phone   GET  /phone/<code>/answer?id=n    -> the answer once it's in, else 204
  phone   POST /phone/<code>/state          "live" / "ended" (for the leaf's pop-up)
"""

from __future__ import annotations

import ipaddress
import json
import logging
import secrets
import socket
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

log = logging.getLogger(__name__)

PHONE_PORT = 4466  # the phone's page (HTTPS, home network); the next free one when taken
VIEW_PORT = 4467  # the leaf's own page (HTTP, this PC only)
MAX_SDP = 64 * 1024
SEEN_FOR_S = 6.0  # a phone not heard from in this long counts as gone


def new_code() -> str:
    return secrets.token_urlsafe(12)


def lan_address() -> str | None:
    """This PC's address on the home network (the one its default route goes out of). Sends nothing."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # a documentation address: only picks the route
            address = s.getsockname()[0]
        return address if ipaddress.ip_address(address).is_private else None
    except OSError:
        return None


def is_home(address: str) -> bool:
    """A client on the home network or this PC (never a public address)."""
    try:
        ip = ipaddress.ip_address(address.split("%")[0])
    except ValueError:
        return False
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return ip.is_private or ip.is_loopback or ip.is_link_local


class Session:
    def __init__(self) -> None:
        self.offer_id = 0
        self.offer = ""
        self.answer_id = 0
        self.answer = ""
        self.state = ""
        self.seen = 0.0  # when the phone last asked anything


class PhoneServer:
    """Both pages and the meeting point. start() and stop() from the window; requests on the server's threads."""

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.codes: set[str] = set()
        self.sessions: dict[str, Session] = {}
        self.lock = threading.Lock()
        self.phone_port = 0
        self.view_port = 0
        self.address = None
        self._servers: list[ThreadingHTTPServer] = []

    # The window's side -----------------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return bool(self._servers)

    def use(self, codes) -> None:
        """The codes of the garden's Phone Camera leaves: the server runs while there is one."""
        with self.lock:
            self.codes = set(codes)
            for gone in [c for c in self.sessions if c not in self.codes]:
                del self.sessions[gone]
        if self.codes and not self.running:
            self.start()
        elif not self.codes and self.running:
            self.stop()

    def start(self) -> None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.address = lan_address()
        cert, key = self._certificate()
        context.load_cert_chain(cert, key)
        phone = self._listen("0.0.0.0", PHONE_PORT, context)
        view = self._listen("127.0.0.1", VIEW_PORT, None)
        self.phone_port, self.view_port = phone.server_address[1], view.server_address[1]
        for server, name in ((phone, "phone page"), (view, "phone view")):
            threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.5}, name=name,
                             daemon=True).start()
        log.info("phone camera: ready (phone page on port %d, view on port %d%s)", self.phone_port, self.view_port,
                 "" if self.address else "; no home network found")

    def stop(self) -> None:
        servers, self._servers = self._servers, []
        for server in servers:
            threading.Thread(target=lambda s=server: (s.shutdown(), s.server_close()), daemon=True).start()
        if servers:
            log.info("phone camera: stopped")

    def phone_url(self, code: str) -> str:
        """What the QR code says: this PC's page for the phone (empty without a home network)."""
        return f"https://{self.address}:{self.phone_port}/phone/{code}" if self.address and self.phone_port else ""

    def view_url(self, code: str) -> str:
        return f"http://127.0.0.1:{self.view_port or VIEW_PORT}/view/{code}"

    def connected(self, code: str) -> bool:
        with self.lock:
            s = self.sessions.get(code)
            return s is not None and s.state == "live" and time.monotonic() - s.seen < SEEN_FOR_S

    def _listen(self, host: str, port: int, context: ssl.SSLContext | None) -> ThreadingHTTPServer:
        handler = _handler(self, phone=context is not None)
        last: OSError | None = None
        for p in [port, *range(port + 2, port + 40, 2), 0]:  # the phone's and the view's ports stay apart
            try:
                server = ThreadingHTTPServer((host, p), handler)
            except OSError as exc:
                last = exc
                continue
            server.daemon_threads = True
            if context is not None:
                server.socket = context.wrap_socket(server.socket, server_side=True)
            self._servers.append(server)
            return server
        raise last or OSError("no free port")

    def _certificate(self) -> tuple[Path, Path]:
        """The kept certificate, made again when it's missing, near its end, or for another home address."""
        from sprigcast.core import selfsigned

        self.folder.mkdir(parents=True, exist_ok=True)
        cert, key, note = self.folder / "phone.crt", self.folder / "phone.key", self.folder / "phone.json"
        want = self.address or ""
        try:
            about = json.loads(note.read_text("utf-8"))
            fresh = about.get("address") == want and about.get("until", 0) > time.time() + 7 * 86400
        except (OSError, ValueError):
            fresh = False
        if not (fresh and cert.is_file() and key.is_file()):
            pem, private = selfsigned.make([want] if want else [])
            cert.write_text(pem, "ascii")
            key.write_text(private, "ascii")
            note.write_text(json.dumps({"address": want, "until": time.time() + 390 * 86400}), "utf-8")
            log.info("phone camera: made the page's certificate")
        return cert, key

    # Requests (server threads) ------------------------------------------------------------------------

    def session(self, code: str) -> Session | None:
        if code not in self.codes:
            return None
        return self.sessions.setdefault(code, Session())


def _handler(server: PhoneServer, phone: bool):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "SprigCast"
        sys_version = ""

        def log_message(self, *args) -> None:  # never the address or code in the log
            pass

        def _send(self, code: int, body: bytes = b"", kind: str = "text/plain; charset=utf-8") -> None:
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _route(self):
            if not is_home(self.client_address[0]):
                self._send(403)
                return None
            url = urlparse(self.path)
            parts = [p for p in url.path.split("/") if p]
            want = "phone" if phone else "view"
            if len(parts) < 2 or parts[0] != want:
                self._send(404)
                return None
            with server.lock:
                session = server.session(parts[1])
            if session is None:
                self._send(404)
                return None
            return session, parts[1], parts[2] if len(parts) > 2 else "", parse_qs(url.query)

        def _body(self) -> str | None:
            size = int(self.headers.get("Content-Length") or 0)
            if size > MAX_SDP:
                self._send(413)
                return None
            return self.rfile.read(size).decode("utf-8", errors="replace")

        def do_GET(self) -> None:  # noqa: N802
            route = self._route()
            if route is None:
                return
            session, code, what, query = route
            after = int((query.get("after") or query.get("id") or ["0"])[0] or 0)
            if phone:
                with server.lock:
                    session.seen = time.monotonic()
                    ready = what == "answer" and session.answer_id == after and session.answer
                    answer = session.answer
                if what == "":
                    self._send(200, PHONE_PAGE.encode("utf-8"), "text/html; charset=utf-8")
                elif ready:
                    self._send(200, json.dumps({"sdp": answer}).encode(), "application/json")
                else:
                    self._send(204)
                return
            if what == "":
                self._send(200, VIEW_PAGE.encode("utf-8"), "text/html; charset=utf-8")
                return
            with server.lock:
                newer = what == "offer" and session.offer_id > after and session.offer
                body = json.dumps({"id": session.offer_id, "sdp": session.offer}) if newer else ""
            self._send(200, body.encode(), "application/json") if newer else self._send(204)

        def do_POST(self) -> None:  # noqa: N802
            route = self._route()
            if route is None:
                return
            session, code, what, query = route
            text = self._body()
            if text is None:
                return
            with server.lock:
                if phone and what == "offer":
                    session.offer_id += 1
                    session.offer, session.answer, session.answer_id = text, "", 0
                    session.state, session.seen = "connecting", time.monotonic()
                    reply = json.dumps({"id": session.offer_id}).encode()
                elif phone and what == "state":
                    session.state, session.seen = text.strip()[:16], time.monotonic()
                    reply = b"{}"
                elif not phone and what == "answer":
                    session.answer_id = int((query.get("id") or ["0"])[0] or 0)
                    session.answer = text
                    reply = b"{}"
                else:
                    reply = None
            if reply is None:
                self._send(404)
            else:
                self._send(200, reply, "application/json")
            if phone and what == "offer":
                log.info("phone camera: a phone is connecting")

    return Handler


# The pages ------------------------------------------------------------------------------------------------

PHONE_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#121816">
<title>SprigCast Phone Camera</title>
<style>
:root { color-scheme: dark; --bg: #121816; --card: #1b2420; --edge: #2c3a33; --text: #e8f0ec; --soft: #9fb3a9;
        --accent: #4fd68b; --live: #ff5c8a; }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text); font: 16px/1.4 system-ui, -apple-system, sans-serif;
       min-height: 100vh; padding: max(16px, env(safe-area-inset-top)) 16px max(16px, env(safe-area-inset-bottom)); }
h1 { font-size: 20px; margin: 4px 4px 14px; }
.card { background: var(--card); border: 1px solid var(--edge); border-radius: 18px; padding: 16px; margin-bottom: 14px; }
video { width: 100%; border-radius: 12px; background: #000; display: block; aspect-ratio: 16 / 9; object-fit: cover; }
.state { display: flex; align-items: center; gap: 10px; padding: 4px 2px 12px; color: var(--soft); }
.dot { width: 10px; height: 10px; border-radius: 50%; background: var(--soft); flex: none; }
.live .dot { background: var(--live); box-shadow: 0 0 10px var(--live); }
.live { color: var(--text); }
.row { display: flex; gap: 10px; flex-wrap: wrap; }
button { flex: 1 1 140px; min-height: 48px; border-radius: 14px; border: 1px solid var(--edge); background: #24302a;
         color: var(--text); font: inherit; font-weight: 600; padding: 10px 16px; }
button.go { background: var(--accent); color: #0c1a12; border-color: var(--accent); }
button.on { border-color: var(--accent); }
label { display: flex; justify-content: space-between; align-items: center; gap: 12px; padding: 8px 2px; }
select { background: #0e1412; color: var(--text); border: 1px solid var(--edge); border-radius: 12px;
         padding: 10px 14px; font: inherit; }
p { color: var(--soft); margin: 8px 2px 0; }
</style></head>
<body>
<h1>SprigCast Phone Camera</h1>
<div class="card">
  <div class="state" id="state"><span class="dot"></span><span id="words">Not sending yet</span></div>
  <video id="me" autoplay playsinline muted></video>
</div>
<div class="card">
  <div class="row">
    <button class="go" id="go">Start</button>
    <button id="flip">Switch camera</button>
  </div>
  <label>Microphone <select id="mic"><option value="off">Off</option><option value="on">On</option></select></label>
  <label>Quality <select id="quality"><option value="720">720p</option><option value="1080">1080p</option>
    <option value="480">480p</option></select></label>
  <p>Keep this page open while you stream. Your phone talks to your PC directly over your Wi-Fi.</p>
</div>
<script>
const base = location.pathname.replace(/\\/$/, "");
const $ = (id) => document.getElementById(id);
let pc = null, stream = null, facing = "user", wake = null, offerId = 0, running = false;
const say = (text, live) => { $("words").textContent = text; $("state").classList.toggle("live", !!live); };
const post = (what, body) => fetch(`${base}/${what}`, {method: "POST", body, cache: "no-store"});
async function media() {
  const h = +$("quality").value, w = Math.round(h * 16 / 9);
  return navigator.mediaDevices.getUserMedia({
    video: {facingMode: facing, width: {ideal: w}, height: {ideal: h}, frameRate: {ideal: 30, max: 30}},
    audio: $("mic").value === "on" ? {echoCancellation: false, noiseSuppression: false, autoGainControl: false} : false,
  });
}
function gathered(peer) {
  return new Promise((done) => {
    if (peer.iceGatheringState === "complete") return done();
    const check = () => { if (peer.iceGatheringState === "complete") done(); };
    peer.addEventListener("icegatheringstatechange", check);
    setTimeout(done, 2500);
  });
}
async function start() {
  if (!navigator.mediaDevices) { say("This page needs a secure connection. Open the address from the QR code again."); return; }
  running = true;
  $("go").textContent = "Stop";
  say("Starting the camera…");
  try {
    if (stream) stream.getTracks().forEach((t) => t.stop());
    stream = await media();
  } catch (e) {
    say("The camera couldn't start: allow the camera for this page, then press Start.");
    running = false; $("go").textContent = "Start"; return;
  }
  $("me").srcObject = stream;
  if (pc) pc.close();
  pc = new RTCPeerConnection({iceServers: []});
  for (const track of stream.getTracks()) {
    const sender = pc.addTrack(track, stream);
    if (track.kind === "video") {
      track.contentHint = "motion";
      const p = sender.getParameters();
      p.encodings = [{maxBitrate: +$("quality").value >= 1080 ? 6000000 : 3500000, maxFramerate: 30}];
      p.degradationPreference = "maintain-framerate";
      sender.setParameters(p).catch(() => {});
    }
  }
  pc.onconnectionstatechange = () => {
    const s = pc.connectionState;
    if (s === "connected") { say("Live in SprigCast", true); post("state", "live"); }
    else if (s === "failed" || s === "disconnected") { say("Lost SprigCast. Trying again…"); post("state", "ended"); retry(); }
  };
  await pc.setLocalDescription(await pc.createOffer());
  await gathered(pc);
  say("Waiting for SprigCast…");
  const r = await post("offer", pc.localDescription.sdp).catch(() => null);
  if (!r || !r.ok) { say("SprigCast didn't answer. Is it open on your PC?"); retry(); return; }
  offerId = (await r.json()).id;
  const mine = offerId;
  for (let i = 0; i < 120 && running && mine === offerId; i++) {
    const a = await fetch(`${base}/answer?id=${mine}`, {cache: "no-store"}).catch(() => null);
    if (a && a.status === 200) {
      await pc.setRemoteDescription({type: "answer", sdp: (await a.json()).sdp});
      return;
    }
    await new Promise((ok) => setTimeout(ok, 500));
  }
  if (running && mine === offerId) { say("SprigCast didn't answer. Is the Phone Camera leaf in your bloom?"); retry(); }
}
let again = null;
function retry() { clearTimeout(again); if (running) again = setTimeout(start, 3000); }
function stop() {
  running = false; clearTimeout(again);
  if (pc) pc.close(); pc = null;
  if (stream) stream.getTracks().forEach((t) => t.stop()); stream = null;
  $("me").srcObject = null;
  post("state", "ended");
  say("Not sending"); $("go").textContent = "Start";
}
async function keepAwake() {
  try { if ("wakeLock" in navigator) wake = await navigator.wakeLock.request("screen"); } catch (e) {}
}
$("go").onclick = () => { if (running) stop(); else { keepAwake(); start(); } };
$("flip").onclick = () => { facing = facing === "user" ? "environment" : "user"; if (running) start(); };
$("mic").onchange = $("quality").onchange = () => { if (running) start(); };
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible" && running) keepAwake(); });
setInterval(() => { if (running && pc && pc.connectionState === "connected") post("state", "live"); }, 2000);
</script>
</body></html>
"""

VIEW_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Phone Camera</title>
<style>html, body { margin: 0; height: 100%; background: transparent; overflow: hidden; }
video { width: 100%; height: 100%; object-fit: contain; display: block; }</style></head>
<body><video id="v" autoplay playsinline></video>
<script>
const base = location.pathname.replace(/\\/$/, "");
let pc = null, have = 0;
function gathered(peer) {
  return new Promise((done) => {
    if (peer.iceGatheringState === "complete") return done();
    peer.addEventListener("icegatheringstatechange", () => { if (peer.iceGatheringState === "complete") done(); });
    setTimeout(done, 2500);
  });
}
async function answer(offer) {
  if (pc) pc.close();
  pc = new RTCPeerConnection({iceServers: []});
  pc.ontrack = (e) => {
    const v = document.getElementById("v");
    if (v.srcObject !== e.streams[0]) v.srcObject = e.streams[0];
    for (const r of pc.getReceivers()) { try { r.playoutDelayHint = 0; r.jitterBufferTarget = 0; } catch (x) {} }
    v.play().catch(() => {});
  };
  await pc.setRemoteDescription({type: "offer", sdp: offer.sdp});
  await pc.setLocalDescription(await pc.createAnswer());
  await gathered(pc);
  await fetch(`${base}/answer?id=${offer.id}`, {method: "POST", body: pc.localDescription.sdp});
}
async function look() {
  try {
    const r = await fetch(`${base}/offer?after=${have}`, {cache: "no-store"});
    if (r.status === 200) { const offer = await r.json(); have = offer.id; await answer(offer); }
  } catch (e) {}
  setTimeout(look, 500);
}
look();
</script></body></html>
"""
