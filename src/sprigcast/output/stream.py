"""Live streaming over RTMP: one Streamer per output, each with its own encoder.

A Streamer is a Recorder whose file is an RTMP connection. When the connection drops it
waits (2 s, then longer, up to 30 s) and connects again, until you stop it.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from sprigcast.core import privacy
from sprigcast.output.autopilot import Autopilot
from sprigcast.output.recorder import Recorder, rate_limits, video_options

log = logging.getLogger(__name__)

RETRY_FIRST, RETRY_MAX = 2.0, 30.0
REFUSED_SAY = 3  # tries in a row the server ends as they start before the streamer is told plainly
REFUSED_MAX = 300.0  # then the tries slow to one every 5 minutes
REFUSED_GRACE = 90.0  # after a place was live (or the PC offline), refusals don't count for this long
GRACE_RETRY = 10.0  # and the tries come at least this often
OFFLINE_RETRY = 5.0  # the PC can't look the server up (its internet is down): ask again this often, not slower
CONNECT_TIMEOUT, SEND_TIMEOUT = 10.0, 20.0  # seconds: connecting, and any one send stalled on the network
GRACE_SECONDS = 5.0  # right after connecting, frames queued during the handshake are still going out
HOLD_SECONDS = 10.0  # a warning stays on screen this long so it can be read
SEVERITY = {"good": 0, "warning": 1, "bad": 2}
# Encoded packets wait in a send buffer so a slow network never stalls the encoder. Up to
# MAX_BACKLOG seconds of video can queue up (a few MB); past that, new frames are skipped until
# the backlog is back under RESUME_BACKLOG.
MAX_BACKLOG, RESUME_BACKLOG = 4.0, 2.0


@dataclass(frozen=True)
class Service:
    label: str
    server: str  # ingest URL without the key; empty means the user must paste one
    max_kbps: int  # what the platform accepts for video
    canvas: str  # which canvas suits it
    help: str = ""
    # Preset applied when you pick the service: longest side of the encoded picture, fps, bitrate.
    long_side: int = 1920
    fps: int = 60
    kbps: int = 6000
    bandwidth_test: bool = False  # supports a test mode that never shows the stream to viewers


SERVICES: dict[str, Service] = {
    "twitch": Service("Twitch", "rtmp://live.twitch.tv/app", 6000, "horizontal",
                      "Key: Twitch Creator Dashboard → Settings → Stream → Primary Stream key.",
                      1920, 60, 6000, bandwidth_test=True),
    # RTMPS on 443, as Kick and Facebook (which never dropped) go: plain RTMP on 1935 is the port networks shape.
    "youtube": Service("YouTube", "rtmps://a.rtmps.youtube.com:443/live2", 20000, "horizontal",
                       "Key: YouTube Studio → Go live → Stream → Stream key.", 1920, 60, 9000),
    "kick": Service("Kick", "", 8000, "horizontal",
                    "Server and key: Kick dashboard → Settings → Stream URL & Key.", 1920, 60, 8000),
    "tiktok": Service("TikTok", "", 6000, "vertical",
                      "Server and key: TikTok LIVE Producer (needs LIVE access), copy Server URL and Stream key.",
                      1280, 30, 4000),
    "x": Service("X", "", 9000, "horizontal",
                 "Server and key: X Media Studio → Producer → Sources → Create source.", 1920, 30, 6000),
    # Places beyond the everyday ones (the "Show all" list). Each was checked to still take streams from
    # streaming software in 2026, and each gives its own server address and key to paste, so none is guessed
    # here. Left out on purpose: sites for adults, services that have closed (Trovo and KakaoTV in June 2026,
    # Nimo TV's global site in 2022) and video hosting sold to companies. Starting quality is a safe one where
    # the site's own numbers weren't found; the limit is then left open (50000) rather than made up.
    "facebook": Service("Facebook", "", 50000, "horizontal",
                        "Server and key: Facebook Live Producer → Go live → Streaming software.", 1920, 60, 6000),
    # BEAM and SharePlay: asked for by name (user, 2026-11-20). Their pages could not be read for the menu path,
    # so the line only says the site gives both.
    "beam": Service("BEAM", "", 50000, "horizontal", "Server and key: from your BEAM stream settings.",
                    1920, 60, 6000),
    "chzzk": Service("CHZZK", "", 50000, "horizontal",
                     "Server and key: CHZZK Creator Dashboard → Broadcasting settings → Stream key.", 1920, 60, 6000),
    "mixcloud": Service("Mixcloud", "", 50000, "horizontal",
                        "Server and key: Mixcloud → Go live → Streaming software (needs Mixcloud Pro).",
                        1920, 30, 3500),
    "niconico": Service("Niconico", "", 50000, "horizontal",
                        "Server and key: Niconico Live → Broadcast → Start broadcast, copy URL and Stream key.",
                        1280, 30, 3000),
    "restream": Service("Restream", "", 50000, "horizontal",
                        "Server and key: Restream → New stream → Encoder | RTMP.", 1920, 60, 6000),
    "shareplay": Service("SharePlay", "", 50000, "horizontal",
                         "Server and key: from your SharePlay stream settings.", 1920, 60, 6000),
    "showroom": Service("SHOWROOM", "", 50000, "horizontal",
                        "Server and key: SHOWROOM → Go live → External encoder settings.", 1280, 30, 2500),
    "soop": Service("SOOP", "", 50000, "horizontal",
                    "Server and key: SOOP BJ Center → Broadcasting settings → Stream key.", 1920, 60, 6000),
    "vimeo": Service("Vimeo", "", 50000, "horizontal",
                     "Server and key: Vimeo event → Use an external encoder (needs a paid plan).", 1920, 30, 6000),
    "whowatch": Service("Whowatch", "", 50000, "horizontal",
                        "Server and key: Whowatch → Go live → Use an external encoder.", 1280, 30, 2500),
    "custom": Service("Custom RTMP", "", 50000, "horizontal", "Any RTMP or RTMPS server URL plus its stream key.",
                      1920, 60, 6000),
}

# The five shown first wherever a place is picked; the rest sit behind "Show all…" (user, 2026-11-20).
TOP_SERVICES = ("twitch", "youtube", "kick", "tiktok", "facebook")
SHOW_ALL = "Show all…"


def all_services() -> list[tuple[str, str]]:
    """Every place by name, A to Z, Custom RTMP last: (key, label)."""
    named = sorted(((k, s.label) for k, s in SERVICES.items() if k != "custom"), key=lambda item: item[1].lower())
    return [*named, ("custom", SERVICES["custom"].label)]

# Encoded sizes offered for each canvas shape.
HORIZONTAL_SIZES = [(1920, 1080), (1600, 900), (1280, 720), (854, 480)]
VERTICAL_SIZES = [(1080, 1920), (720, 1280), (540, 960)]


def sizes_for(canvas) -> list[tuple[int, int]]:
    """Output sizes that keep the canvas shape, never larger than the canvas."""
    options = HORIZONTAL_SIZES if canvas.width >= canvas.height else VERTICAL_SIZES
    fitting = [(w, h) for w, h in options if w <= canvas.width and h <= canvas.height]
    return fitting or [(canvas.width, canvas.height)]


def preset_size(service: Service, canvas) -> tuple[int, int]:
    """The largest offered size whose longest side fits the service's preset."""
    options = sizes_for(canvas)
    return next(((w, h) for w, h in options if max(w, h) <= service.long_side), options[-1])


def stream_url(server: str, key: str, bandwidth_test: bool = False) -> str:
    """Twitch's bandwidth test sends the stream to Twitch without showing it to anyone."""
    server = server.strip().rstrip("/")
    parts = urlsplit(server)
    if parts.hostname and parts.hostname.endswith(".live-video.net") and not parts.path.strip("/"):
        server += "/app"  # Kick and other IVS ingests need the app name; a bare host makes the key the app
    return f"{server}/{key}" + ("?bandwidthtest=true" if bandwidth_test else "")


def ingest_name(url: str) -> str:
    """Where a stream URL goes, without the key: rtmps://host:443/app."""
    parts = urlsplit(url)
    path = parts.path.strip("/").split("/")
    app = "/".join(path[:-1])
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{parts.hostname or '?'}{port}/{app}"


def open_ingest(url: str):
    """An FLV container for an RTMP(S) ingest. PyAV connects at the first packet (start_encoding), so wrap that
    in explain_connect. Bounded: a server that never answers must not hold the link (or a stop) forever."""
    import av

    return av.open(url, "w", format="flv", timeout=(CONNECT_TIMEOUT, SEND_TIMEOUT))


_PLAIN_REASONS = (
    ("cannot open connection", "the server couldn't be reached"),
    ("i/o error", "the server ended the connection as it was starting"),
    ("immediate exit", "the server didn't answer in time"),
    ("timed out", "the server didn't answer in time"),
    ("connection refused", "the server refused the connection"),
    ("connection reset", "the server dropped the connection"),
    ("aborted", "the server dropped the connection"),
    ("broken pipe", "the server dropped the connection"),
)


def plain_reason(why: str) -> str:
    """Why a connect failed, in plain words with the network's own after them: "I/O error" alone told nobody
    anything (YouTube, live 2026-11-21). What isn't known is left as it was."""
    low = why.lower()
    if offline(why):
        return why  # retry_plan says the PC is offline
    for words, plain in _PLAIN_REASONS:
        if words in low:
            return f"{plain} ({why})"
    return why


@contextmanager
def explain_connect(url: str):
    """Around the connect: a failure names the server (never the key) and adds what FFmpeg itself logged, which
    is where the real reason is ("[Errno 5] I/O error" alone says nothing; Kick, 2026-11-18)."""
    import av.logging as avlog

    old = avlog.get_level()
    louder = old is None or old > avlog.WARNING
    if louder:
        avlog.set_level(avlog.WARNING)
    try:
        with avlog.Capture(True) as logs:  # this thread only: the connect happens here
            try:
                yield
            except Exception as exc:
                said = [str(line[2]).strip() for line in logs if str(line[2]).strip()]
                hint = "" if urlsplit(url).path.strip("/").count("/") else " (the server address has no app part)"
                detail = " | FFmpeg: " + " / ".join(said[-4:]) if said else ""
                why = plain_reason(str(exc) or exc.__class__.__name__)
                if any("already publishing" in line.lower() for line in said):
                    # The server's own words: "[Errno 1] Operation not permitted" alone said nothing (sim PC).
                    why = ("this stream key is already live there (the site is still closing the last "
                           f"connection, or another app is using the key) ({why})")
                elif "operation not permitted" in why.lower():
                    # What FFmpeg makes of a server turning the publish away, when its own words weren't caught
                    # (five of six places at once got only this, sim PC 2026-11-28).
                    why = ("the server turned the stream away; often the stream key is still live there (the "
                           f"site is closing the last connection, or another app is using the key) ({why})")
                raise ConnectionError(f"couldn't connect to {ingest_name(url)}{hint}: {why}{detail}") from exc
    finally:
        if louder:
            avlog.set_level(old)


def offline(message: str) -> bool:
    """The server's name didn't resolve: this PC's internet or DNS is down, not the server or the key."""
    m = message.lower()
    return "resolve hostname" in m or "name does not resolve" in m or "getaddrinfo" in m


def retry_plan(message: str, bytes_out: int, delay: float) -> tuple[str, float, float]:
    """After a failed (re)connect: (hint for the status, seconds to wait now, the next backoff). With the PC
    offline the wait stays short, so the stream is back within seconds of the internet (light-PC test
    2026-11-19: Twitch's name stopped resolving at minute 28 and tries went out at 4, 19, 16, 30, 30 s)."""
    if offline(message):
        wait = min(delay, OFFLINE_RETRY)
        return " (this PC can't reach the internet right now)", wait, min(wait * 2, OFFLINE_RETRY)
    hint = "" if bytes_out else " (check the server and stream key)"
    return hint, delay, min(delay * 2, RETRY_MAX)


_ended_at: dict[int, float] = {}  # a server and key (hashed) -> when a stream to it was last stopped here


def note_ended(st) -> None:
    """A stream to this server and key was just stopped: for REFUSED_GRACE the site may still hold the old
    connection and turn the same key away ("Already publishing"). Going live again at once was refused 3 times
    in 6 s, then not tried again for a minute and a half though the server was free seconds later (sim PC,
    2026-11-28)."""
    url = getattr(st, "_url", None)
    if url:
        _ended_at[hash(url)] = time.monotonic()


def note_refusal(st, message: str, sent: bool, delay: float, after: float) -> tuple[float, float]:
    """After a failed try: count the tries in a row that sent nothing (the server ended them as they started).
    From the REFUSED_SAY-th the streamer is told plainly (st.refused, health.notice), it is logged once, and the
    tries slow down towards REFUSED_MAX. Facebook ended a stream after 2 h and then turned away 80 tries every
    30 s for 40 minutes with only "reconnecting" to show for it (stream review 2026-11-23). A try that sent
    something, or a PC that is offline, starts the count again, and for REFUSED_GRACE after it refusals aren't
    counted. Returns (wait now, next backoff)."""
    now = time.monotonic()
    if sent or offline(message):
        st.refused, st.live_lost = 0, now
        return delay, after
    lost = getattr(st, "live_lost", None)
    if lost is None:
        lost = _ended_at.get(hash(getattr(st, "_url", None) or id(st)))  # stopped a moment ago, live again now
    if lost is not None and now - lost < REFUSED_GRACE:
        # It was live a moment ago: a site turns the same key away until it has closed the old connection
        # (Twitch and Kick for about 45 s after the user's internet came back, 2026-11-27, by when the tries
        # had slowed to one every 30 s). Keep asking briskly instead.
        st.refused = 0
        wait = min(delay, GRACE_RETRY)
        return wait, min(wait * 2, GRACE_RETRY)
    st.refused = getattr(st, "refused", 0) + 1
    if st.refused < REFUSED_SAY:
        return delay, after
    if st.refused == REFUSED_SAY:
        log.warning("stream %s: the server ended %d tries in a row as they started; the stream may have ended on "
                    "the site or its stream key changed. Trying less often now", st.name, st.refused)
    wait = max(delay, RETRY_MAX)
    return wait, min(wait * 2, REFUSED_MAX)


def refused_status(st, message: str, delay: float) -> str:
    """The status once note_refusal has given up retrying quickly."""
    return (f"disconnected: {message}; {st.name} has turned away {st.refused} tries in a row. Check the stream "
            f"key, or whether the stream ended there; trying again in {_wait(delay)}")


def _wait(seconds: float) -> str:
    return f"{seconds / 60:.0f} min" if seconds >= 90 else f"{seconds:.0f} s"


def same_key_warnings(targets) -> list[str]:
    """Before going live: places that would send to the same server with the same stream key. A service takes one
    stream per key, so all but one would fail to connect with nothing clearer than "I/O error" (YouTube horizontal
    and vertical on one key, live 2026-11-21). Names only, never the key."""
    by: dict[tuple, list[str]] = {}
    for t in targets:
        server, key = getattr(t, "server", None), getattr(t, "key", None)
        if server and key:
            by.setdefault((str(server).rstrip("/").lower(), key), []).append(t.name)
    return [f"{_and(names)} use the same stream key. A stream key takes one stream at a time, so they get in "
            "each other's way: one may not connect or may keep dropping. Give each place its own key in Stream…."
            for names in by.values() if len(names) > 1]


def _and(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def mark_same_keys(streamers) -> None:
    """Each stream learns which others send to its very address (server and key): a failed connect then says so
    (dropped_status)."""
    by: dict[str, list] = {}
    for st in streamers:
        url = getattr(st, "_url", None)
        if isinstance(url, str) and url:
            by.setdefault(url, []).append(st)
    for st in streamers:
        url = getattr(st, "_url", None)
        others = [o.name for o in by.get(url, []) if o is not st] if isinstance(url, str) else []
        st.same_key_as = _and(others) if others else ""


def dropped_status(st, message: str, hint: str, delay: float, sent: bool = False) -> str:
    """The status of a stream that dropped and will try again. When another place sends with the very same stream
    key and this try sent nothing (sent False), that is said too: it is the likeliest reason, though not a
    known one."""
    other = getattr(st, "same_key_as", "")
    if other and not sent:
        if not getattr(st, "_same_key_said", False):
            st._same_key_said = True
            log.warning("stream %s can't get through and %s uses the same stream key: a stream key takes one "
                        "stream at a time", st.name, other)
        return f"disconnected: {message} ({other} uses the same stream key); retrying in {delay:.0f} s"
    return f"disconnected: {message}{hint}; retrying in {delay:.0f} s"


@dataclass
class StreamTarget:
    name: str  # shown in the status, e.g. "Twitch (horizontal)"
    canvas: object  # core.scene.Canvas
    server: str
    key: str
    bitrate_kbps: int
    size: tuple[int, int] | None = None  # encoded size; None = canvas size
    fps: int | None = None  # None = canvas fps
    bandwidth_test: bool = False
    enhanced: object | None = None  # output.enhanced.EnhancedConfig: several qualities over one connection
    canvases: list | None = None  # with enhanced: the canvases its tracks come from (index = canvas_index)
    output_id: str | None = None  # the scene collection Output this target came from
    auto_bitrate: bool = False  # lower the bitrate when the upload struggles (output/autopilot.py)
    max_kbps: int = 0  # the most its service accepts (Service.max_kbps); 0 = no limit


class Streamer(Recorder):
    eases = False  # a stream steers its bitrate instead (auto-pilot, backlog)
    allow_writer = False  # reconnects, bitrate steering and the backlog are its own (Recorder.uses_writer)

    def __init__(
        self,
        name: str,
        server: str,
        key: str,
        width: int,
        height: int,
        fps: int,
        bitrate_kbps: int,
        codec: str,
        start_time: float,
        out_size: tuple[int, int] | None = None,
        bandwidth_test: bool = False,
        encoder=None,
        auto_bitrate: bool = False,
    ):
        super().__init__(
            Path(f"stream {name}"), width, height, fps, bitrate_kbps, codec, start_time, "flv", out_size, encoder
        )
        self.name = name
        self.bandwidth_test = bandwidth_test
        self._url = stream_url(server, key, bandwidth_test)
        self._key = key
        privacy.remember_secret(key, self._url)  # never in a log line
        self.connected = False
        self.reconnects = 0
        self.refused = 0  # tries in a row the server ended as they started (note_refusal)
        self.status = "connecting"
        self._health: tuple | None = None
        self._held: tuple[str, str, float] | None = None  # (level, reason, shown until)
        self._send_q: queue.Queue = queue.Queue()
        self._send_error: Exception | None = None
        from sprigcast.output.link_watch import WATCH

        WATCH.add(self)  # a quiet stream can't tell a dead connection by itself (link_watch.py)
        self._backlog_bytes = 0
        self._congested = False
        self._sender: threading.Thread | None = None
        self.delay_s = 0.0  # Settings → Stream → Quality → Stream delay: packets wait this long before they're sent
        self._delay_line = None
        self._connected_at = 0.0
        self._first_live_at = 0.0  # the first time it went live: the summary averages from here, reconnects or not
        # Auto-pilot needs a target bitrate to steer; constant-quality encoding has none.
        self.autopilot = Autopilot(bitrate_kbps) if auto_bitrate and self.encoder.rate_control != "cqp" else None
        self._want_kbps = self._applied_kbps = bitrate_kbps
        self._autopilot_at = 0.0

    def _live_text(self) -> str:
        return f"live, {self.delay_s:g} s delay" if self.delay_s > 0 else "live"

    def delay_left(self) -> float:
        """Seconds until the first delayed packets go out: nothing is wrong while they wait, so this isn't
        "connecting". 0 with no delay, once something has been sent, and after a drop (that's a reconnect)."""
        if self.delay_s <= 0 or self.connected or self.reconnects or self.bytes_out:
            return 0.0
        return max(0.0, self.started_at + self.delay_s - time.monotonic())

    def waiting_text(self) -> str:
        """What a not-yet-connected stream is doing, for cards and the stream report."""
        left = self.delay_left()
        return f"going out in {left:.0f} s (stream delay)" if left > 0 else self.status

    def health(self) -> tuple[str, str]:
        """("good" | "warning" | "bad", short reason), comparing against the previous call (about 1 s ago).

        Connecting shows as a warning, a drop in connection as bad. A warning or worse stays shown
        for HOLD_SECONDS even after things recover, unless something more severe replaces it.
        """
        now = time.monotonic()
        level, reason = self._measure_health(now)
        held = self._held
        if held and now < held[2] and SEVERITY[held[0]] >= SEVERITY[level] and self.connected:
            return held[0], held[1]
        if level != "good" and self.connected:  # connection states speak for themselves, no need to hold them
            self._held = (level, reason, now + HOLD_SECONDS)
        return level, reason

    def _measure_health(self, now: float) -> tuple[str, str]:
        sample = (now, self.frames, self.dropped, self.mux_seconds)
        last, self._health = self._health, sample
        if not self.connected:
            if self.delay_left() > 0:
                return "good", self.waiting_text()
            return ("warning" if self.status.startswith(("connecting", "reconnecting")) else "bad"), self.status
        if last is None or now <= last[0] or now - self._connected_at < GRACE_SECONDS:
            return "good", self._live_text()
        sent, lost = sample[1] - last[1], sample[2] - last[2]
        waiting_on_network = (sample[3] - last[3]) / (now - last[0]) > 0.5
        backlog = self._video.qsize() / self._video.maxsize
        behind = self.backlog_seconds()
        loss = lost / max(1, sent + lost)
        why = "upload too slow for this bitrate" if waiting_on_network else "encoder overloaded"
        if loss > 0.05:
            return "bad", f"dropping {loss:.0%} of frames: {why}"
        if lost or backlog > 0.5:
            return "warning", f"falling behind: {why}"
        if behind > 1.0:
            return "warning", f"network slow, {behind:.1f} s behind"
        return "good", self._live_text()

    def redact(self, text: str) -> str:
        """Error text from FFmpeg can contain the URL; never let the key reach the screen or a log."""
        return privacy.mask(text, [self._key])

    def _network_share(self) -> tuple[float, str]:
        """(share of the time waiting on the network, how the summary says it): time inside the send call."""
        waiting = self.mux_seconds / max(1e-6, time.monotonic() - self.started_at)
        return waiting, f"{waiting:.0%} of the time"

    def summary(self) -> str:
        """One line for after the stream ends: how much was dropped and what was slow."""
        total = self.frames + self.dropped
        if not total:
            return f"{self.name}: never connected ({self.status})"
        loss = self.dropped / total
        encode_ms = 1000 * self.encode_seconds / max(1, self.frames)
        budget_ms = 1000 / self.fps
        waiting, waits = self._network_share()
        # From the first time it went live: since the last reconnect only, a stream that reconnected 8 minutes
        # before the end read 2127 kbps where it had carried about 390 (Mikedog151, 2026-11-24)
        live_s = max(1e-6, time.monotonic() - (self._first_live_at or self.started_at))
        average = f"averaged {self.bytes_out * 8 / 1000 / live_s:.0f} kbps (set {self.bitrate_kbps})"
        line = (
            f"{self.name}: {self.dropped} of {total} frames dropped ({loss:.1%}), {average}. "
            f"Encoding took {encode_ms:.1f} ms per frame (budget {budget_ms:.1f} ms); "
            f"waiting on the network {waits}, longest stall {self.longest_stall:.1f} s"
        )
        if self.reconnects:
            line += f"; {self.reconnects} reconnects"
        if self.autopilot and self.autopilot.steps_down:
            line += (f"; auto-pilot lowered the bitrate {self.autopilot.steps_down} times"
                     f" (lowest {self.autopilot.lowest_kbps} kbps)")
        if loss > 0.01:
            if waiting > 0.5:
                line += ". Your upload can't keep up: lower this output's bitrate."
            elif encode_ms > 0.6 * budget_ms:
                line += ". Your PC can't prepare frames fast enough: lower this output's resolution or frame rate."
            else:
                line += ". Neither looks maxed out, so the drops were probably brief hiccups."
        return line + ("" if line.endswith(".") else ".")

    # Recorder hooks ------------------------------------------------------------

    def _counts_drops(self) -> bool:
        return self.connected  # frames lost while (re)connecting are expected, not an encoder problem

    def push_audio(self, block) -> None:
        if self._stop.is_set():
            return
        try:
            self._audio.put_nowait(block)
        except queue.Full:
            pass

    def _open_container(self):
        return open_ingest(self._url)

    @staticmethod
    def default_encoder():
        from sprigcast.core.scene import EncoderSettings

        return EncoderSettings()  # constant bitrate, what Twitch and others ask for

    # Sending: the encoder thread queues packets, a sender thread writes them to the network ----------

    @property
    def current_kbps(self) -> int:
        return self.autopilot.current_kbps if self.autopilot else self.bitrate_kbps

    def backlog_seconds(self) -> float:
        # Against the bitrate set, not the lowered one: the same bytes waiting read as more seconds after each step
        # down, and that alone asked for the next (PC live 2026-11-21).
        return self._backlog_bytes * 8 / (max(1, self.bitrate_kbps) * 1000)

    def _video_options(self) -> dict[str, str]:
        return video_options(self.codec, self.encoder, self.bitrate_kbps, hrd=self.autopilot is None)

    def _steer(self, backlog: float) -> None:
        """About once a second: let auto-pilot pick the bitrate; the encoder thread applies it."""
        now = time.monotonic()
        if self.autopilot is None or now - self._autopilot_at < 1.0 or now - self._connected_at < GRACE_SECONDS:
            return
        self._autopilot_at = now
        kbps = self.autopilot.update(now, backlog)
        if kbps is not None:
            log.info("stream %s: auto-pilot bitrate %d kbps", self.name, kbps)
            self._want_kbps = kbps

    def _write_video(self, container, stream, frame, pts: int | None = None) -> None:
        if self._want_kbps != self._applied_kbps:
            k = self._want_kbps
            if self._direct is not None:  # SprigCast's own NVENC: reconfigured in place
                try:
                    self._direct.set_bitrate(k)
                except Exception as exc:  # keep streaming at the old bitrate rather than stop
                    log.warning("stream %s: couldn't change the bitrate to %d kbps: %s", self.name, k, exc)
                self._applied_kbps = k
        if self._direct is None:
            from sprigcast.output.recorder import next_rate

            if getattr(self, "_applied_limits", None) is None:
                self._applied_limits = rate_limits(self.codec, self.encoder, self._applied_kbps)
            step = next_rate(self.codec, self.encoder, (self._applied_kbps, *self._applied_limits), self._want_kbps)
            if step is not None:  # going down: the bitrate first, its limits on the next picture (see next_rate)
                k, peak, buffer = step
                ctx = stream.codec_context
                ctx.bit_rate = k * 1000
                if peak:
                    ctx.max_bit_rate, ctx.rc_buffer_size = peak * 1000, buffer * 1000
                self._applied_kbps, self._applied_limits = k, (peak, buffer)
        super()._write_video(container, stream, frame, pts)

    def push_frame(self, frame) -> None:
        if self.connected:
            backlog = self.backlog_seconds()
            self._steer(backlog)
            if self._congested and backlog < RESUME_BACKLOG:
                self._congested = False
            elif backlog > MAX_BACKLOG:
                self._congested = True
            if self._congested:
                if self._assign_pts(frame) is not None:  # a frame we would have sent
                    self.dropped += 1
                return
        super().push_frame(frame)

    def _open(self):
        container, video, audio = super()._open()
        self._applied_kbps = self.bitrate_kbps  # a fresh encoder starts at the set bitrate; _write_video steers it
        self._applied_limits = None
        self._send_q = queue.Queue()
        self._send_error = None
        self._backlog_bytes = 0
        self._delay_line = self._new_delay_line(self._send_q)
        self._sender = threading.Thread(
            target=self._send_loop, args=(container,), name=f"sprigcast-send-{self.name}", daemon=True
        )
        self._sender.start()
        return container, video, audio

    def _new_delay_line(self, send_q):
        """With a stream delay, packets wait in a DelayLine and only then join the send queue (and count as
        backlog, so the wait isn't mistaken for a slow network)."""
        if self.delay_s <= 0:
            return None
        from sprigcast.output.delay import DelayLine

        def release(packet):
            self._backlog_bytes += packet.size
            send_q.put(packet)
        return DelayLine(self.delay_s, release, f"delay-{self.name}")

    def _mux(self, container, packets) -> None:
        if self._send_error is not None:
            raise self._send_error  # the connection failed: end this session so _run reconnects
        line = self._delay_line
        for packet in packets:
            if line is not None:
                line.put(packet)
                continue
            self._backlog_bytes += packet.size
            self._send_q.put(packet)

    def link_lost(self, reason: str) -> None:
        """The server stopped answering (link_watch): end this connection so _run makes a new one."""
        if self.connected and self._send_error is None:
            self._send_error = ConnectionError(reason)

    def _send_loop(self, container) -> None:
        from sprigcast.core.pacing import ABOVE_NORMAL, raise_thread_priority

        raise_thread_priority(ABOVE_NORMAL)  # a starved sender looks like a slow network
        while True:
            packet = self._send_q.get()
            if packet is None:
                return
            if self._send_error is not None:
                self._backlog_bytes -= packet.size  # connection already failed: discard
                continue
            try:
                if self.connected or self.bytes_out:
                    Recorder._mux(self, container, [packet])  # timing, bytes_out and longest_stall
                else:
                    with explain_connect(self._url):  # the first packet connects
                        Recorder._mux(self, container, [packet])
            except Exception as exc:
                self._send_error = exc
            finally:
                self._backlog_bytes -= packet.size
            if not self.connected and self.bytes_out and self._send_error is None:
                self.connected = True
                self._connected_at = time.monotonic()
                self._first_live_at = self._first_live_at or self._connected_at
                self.status = "live"
                log_live(self)

    def stop(self, timeout: float = 15.0) -> None:
        """Stop sending. With a stream delay the delayed part still goes out first (like OBS), in the
        background, so ending a stream never freezes the window for the length of the delay."""
        note_ended(self)
        if self.delay_s > 0 and self._delay_line is not None and self._send_error is None:
            self.status = f"ending: sending the last {self.delay_s:g} s"
            threading.Thread(target=super().stop, args=(self.delay_s + timeout,),
                             name=f"sprigcast-end-{self.name}", daemon=True).start()
            return
        super().stop(timeout)

    def _before_close(self, container) -> bool:
        """Let the sender finish what is queued (briefly), then stop it before the container closes."""
        line, self._delay_line = self._delay_line, None
        if line is not None:
            if self._stop.is_set() and self._send_error is None:
                line.finish(self.delay_s + 5)  # ending: the delayed part goes out on time, then we stop
            else:
                line.close()  # the connection failed: what's delayed belongs to it and goes with it
        self._send_q.put(None)
        self._sender.join(timeout=3.0 if self._send_error is None else 0.5)
        if self._sender.is_alive():
            log.warning("stream %s: sender still blocked on the network; abandoning the connection", self.name)
            return False  # closing now would free the connection under the sender's feet
        if self._send_error is not None:
            raise self._send_error
        return True

    # Stream thread -------------------------------------------------------------

    def _drain(self) -> None:
        for q in (self._video, self._audio):
            while True:
                try:
                    q.get_nowait()
                except queue.Empty:
                    break

    def _run(self) -> None:
        delay = RETRY_FIRST
        while not self._stop.is_set():
            self._drain()  # anything queued while disconnected would arrive late and out of sync
            self.start_time = time.perf_counter()
            self._last_pts, self._last_queued, self._samples = -1, -1, 0
            self._congested = False
            self.connected = False
            self.status = "connecting" if not self.reconnects else f"reconnecting (try {self.reconnects})"
            began, sent = time.monotonic(), self.bytes_out
            try:
                self._session()
                self.status = "stopped"
                return
            except Exception as exc:
                message = self.redact(str(exc) or exc.__class__.__name__)
                log.warning("stream %s dropped: %s", self.name, message)
                if time.monotonic() - began > 60:
                    delay = RETRY_FIRST  # it had been running fine: start the backoff again
                self.reconnects += 1
                hint, delay, after = retry_plan(message, self.bytes_out, delay)
                # "Was live in this try" counts like "sent something": six places that had only just come back
                # were reset before sending anything new, so the refusals after it weren't seen as following a
                # live stream, and the waits grew to 40 s and 60 s with the server free (sim PC, 2026-11-28).
                was_live = self.bytes_out > sent or getattr(self, "_connected_at", 0.0) >= began
                delay, after = note_refusal(self, message, was_live, delay, after)
                self.status = (refused_status(self, message, delay) if self.refused >= REFUSED_SAY else
                               dropped_status(self, message, hint, delay, self.bytes_out > sent))
                if self._stop.wait(delay):
                    self.status = "stopped"
                    return
                delay = after


def log_live(st) -> None:
    """One line when a destination starts going out, and when it's back after dropping; the first time also
    which server it went to and how far away that is (looked up on a thread of its own)."""
    if st.reconnects:
        st.back_live_at = time.monotonic()  # Live says "Back live" for a moment (app/health.py notice)
        log.info("stream %s back live after %d reconnect%s", st.name, st.reconnects,
                 "" if st.reconnects == 1 else "s")
    else:
        log.info("stream %s live", st.name)
        url = getattr(st, "_url", "")
        if isinstance(url, str) and "://" in url:
            threading.Thread(target=log_server, args=(st.name, url), name="sprigcast-ingest-rtt", daemon=True).start()


def round_trip_ms(host: str, port: int, timeout: float = 3.0) -> float | None:
    """How long a TCP connection to the server takes to open (about one round trip), or None."""
    import socket

    began = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return (time.perf_counter() - began) * 1000
    except OSError:
        return None


def log_server(name: str, url: str, measure=round_trip_ms) -> str:
    """Which server a place goes to (no key), over what, and the round trip to it. Live on the PC (2026-11-21)
    both YouTube links fell seconds behind while the others were fine, and the log couldn't say where they went."""
    parts = urlsplit(url)
    port = parts.port or {"rtmps": 443, "rtmp": 1935, "srt": 0}.get(parts.scheme, 0)
    rtt = measure(parts.hostname, port) if parts.hostname and port and parts.scheme.startswith("rtmp") else None
    line = (f"stream {name} goes to {ingest_name(url)} over {parts.scheme.upper()}"
            + (f", {rtt:.0f} ms away" if rtt is not None else ""))
    log.info(line)
    return line


def engine_encodes_streams() -> bool:
    """With the native engine, every stream goes through a shared encoder (output/shared_stream.py), which the
    engine's writer runs on its own thread (packets mode): Python only sends the packets. SPRIGCAST_NO_WRITER or
    SPRIGCAST_NO_PACKET_WRITER keeps a lone stream on its own Python encoder (Streamer)."""
    import os

    from sprigcast import sprigcore

    if os.environ.get("SPRIGCAST_NO_WRITER") or os.environ.get("SPRIGCAST_NO_PACKET_WRITER"):
        return False
    try:
        return sprigcore.use_native_media()
    except Exception:  # noqa: BLE001
        return False


def is_youtube(target: StreamTarget) -> bool:
    return "youtube.com" in (target.server or "").lower()


def is_vertical(target: StreamTarget) -> bool:
    w, h = target.size or (getattr(target.canvas, "width", 0), getattr(target.canvas, "height", 0))
    return h > w


def dual_stream_order(targets: list[StreamTarget]) -> list[StreamTarget]:
    """YouTube's Dual stream: the horizontal feed connects before the vertical one, so YouTube has the stream
    the vertical one joins. Everything else keeps its order."""
    return sorted(targets, key=lambda t: is_youtube(t) and is_vertical(t))


class StreamSession:
    """Every live output at once. Each output encodes its own canvas; all share the mixed audio."""

    def __init__(self, engine, streamers: list[tuple[str, Streamer]]):
        self.engine = engine
        self.streamers = streamers  # (canvas id, streamer)
        self.started_at = time.monotonic()

    @classmethod
    def start(cls, engine, targets: list[StreamTarget], codec: str, encoder=None,
              delay_s: float = 0.0, share_all: bool = False) -> "StreamSession":
        """share_all: a destination on its own gets a shared encoder too, so a recording set to "Same as
        stream" can write its packets (Settings → Recording)."""
        from sprigcast.output.shared_stream import SharedStreamEncoder, StreamLink, bitrate_groups, share_key

        start_time = time.perf_counter()
        share_all = share_all or engine_encodes_streams()
        targets = dual_stream_order(targets)
        streamers, taps, shared = [], [], []
        owned: dict[int, list[tuple]] = {}  # id(streamer or shared encoder) -> its frame taps (drop() takes them off)
        for t in targets:  # no size of its own: the canvas's output (scaled) size
            if t.size is None and hasattr(t.canvas, "output_size"):
                size = t.canvas.output_size()
                if size != (t.canvas.width, t.canvas.height):
                    t.size = size
        # Destinations that want the same picture (canvas, size, fps) at close bitrates share one encoder.
        pictures: dict[tuple, list[StreamTarget]] = {}
        for t in targets:
            key = share_key(t)
            if key is not None:
                pictures.setdefault(key, []).append(t)
        groups: dict[tuple, list[StreamTarget]] = {}
        for (canvas_id, size, fps), alike in pictures.items():
            for kbps, auto, members in bitrate_groups(alike):
                groups[(canvas_id, size, fps, kbps, auto, len(groups))] = members
        done: set[int] = set()
        # Twitch Enhanced Broadcasting first: a destination wanting one of its qualities' pictures (YouTube or Kick
        # at the same canvas, size and frame rate) sends that quality's packets, with no encoder of its own.
        enhanced: list = []
        for t in targets:
            if t.enhanced is not None:
                st, mine = cls._one(t, codec, encoder, delay_s, start_time)
                taps += mine
                owned[id(st)] = mine
                enhanced.append((t.canvas.id, st))
                done.add(id(t))
        qualities = {(tr.canvas.id, (tr.cfg.width, tr.cfg.height), tr.fps): tr.encoder
                     for _, st in enhanced for tr in getattr(st, "tracks", ()) if getattr(st, "native", False)}
        borrowed_encoders: list = []
        for (canvas_id, size, fps, kbps, auto, _n), group in groups.items():
            borrowed = qualities.get((canvas_id, size, fps))
            if borrowed is not None:
                for t in group:
                    link = StreamLink(t.name, t.server, t.key, borrowed, t.bandwidth_test)
                    link.output_id = t.output_id or t.name
                    link.delay_s = delay_s
                    borrowed.links.append(link)
                    streamers.append((canvas_id, link))
                    done.add(id(t))
                    link.start()
                if all(borrowed is not b for b in borrowed_encoders):
                    borrowed_encoders.append(borrowed)  # its links need sound: it encodes the stream's mix too
                log.info("%s send%s Twitch's %dx%d quality (%d kbps): no encoder of their own",
                         ", ".join(t.name for t in group), "" if len(group) > 1 else "s", size[0], size[1],
                         borrowed.bitrate_kbps)
                continue
            if len(group) < 2 and not share_all:
                continue
            c = group[0].canvas
            enc = SharedStreamEncoder(c, fps, kbps, codec, start_time, group[0].size, encoder, auto_bitrate=auto)
            enc.share_picture = (canvas_id, size, fps)  # a place started later may join it (add)
            for t in group:
                link = StreamLink(t.name, t.server, t.key, enc, t.bandwidth_test)
                link.output_id = t.output_id or t.name
                link.delay_s = delay_s
                enc.links.append(link)
                streamers.append((c.id, link))
                done.add(id(t))
            if len(group) > 1:
                log.info("streams %s share one encoder at %d kbps (%s)", ", ".join(t.name for t in group), kbps,
                         ", ".join(f"{t.name} set {t.bitrate_kbps}" for t in group))
            enc.start()
            for link in enc.links:
                link.start()
            taps.append((c.id, enc.push_frame, enc.gpu_size))
            owned[id(enc)] = [taps[-1]]
            shared.append(enc)
        for t in targets:
            if id(t) in done:
                continue
            st, mine = cls._one(t, codec, encoder, delay_s, start_time)
            taps += mine
            owned[id(st)] = mine
            streamers.append((t.canvas.id, st))
        streamers += enhanced  # last: stopping, the links sending its qualities end before its encoders do
        session = cls(engine, streamers)
        session.taps = taps
        session.shared = shared
        session.borrowed = borrowed_encoders
        session.owned = owned
        for canvas_id, callback, *size in taps:
            engine.add_frame_tap(canvas_id, callback, *size)
        for _, st in streamers:
            engine.mixer.add_tap(st.push_audio, ("stream",))
        for enc in [*shared, *borrowed_encoders]:
            engine.mixer.add_tap(enc.push_audio, ("stream",))
        mark_same_keys([st for _, st in streamers])
        return session

    @staticmethod
    def _one(t: StreamTarget, codec: str, encoder, delay_s: float, start_time: float):
        """Start one destination with its own encoder. Returns it and its frame taps (not added yet)."""
        from sprigcast.output.enhanced_stream import EnhancedStreamer

        c = t.canvas
        if t.enhanced is not None:
            st = EnhancedStreamer(t.name, t.enhanced, t.canvases or [c], start_time, user_key=t.key)
            taps = st.frame_taps()
        else:
            fps = min(t.fps or c.fps, c.fps)
            st = Streamer(t.name, t.server, t.key, c.width, c.height, fps, t.bitrate_kbps, codec, start_time,
                          t.size, t.bandwidth_test, encoder, auto_bitrate=t.auto_bitrate)
            taps = [(c.id, st.push_frame, st.gpu_size)]
        st.output_id = t.output_id or t.name
        st.delay_s = delay_s
        st.start()
        return st, taps

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    def find(self, output_id: str):
        return next((st for _, st in self.streamers if getattr(st, "output_id", None) == output_id), None)

    def add(self, target: StreamTarget, codec: str, encoder=None, delay_s: float = 0.0):
        """Start one more destination while live (an app's place.start, or a place switched back on): as going live
        would have placed it. It sends one of Twitch's qualities when that is its picture, else joins a running
        shared encoder of the same picture at a close bitrate it may take (output/shared_stream.py
        bitrate_groups), else gets an encoder of its own: with the native engine a shared one the engine sends
        from, as at go-live. (It used to get a Python encoder and sender of its own even beside the quality it
        had been sending: YouTube switched off and on, live 2026-11-21, then the PC at 100% and 110 frames
        dropped.)"""
        from sprigcast.output.shared_stream import SHARE_WITHIN, SharedStreamEncoder, StreamLink, share_key

        if target.size is None and hasattr(target.canvas, "output_size"):
            size = target.canvas.output_size()
            if size != (target.canvas.width, target.canvas.height):
                target.size = size
        picture = share_key(target)
        ceiling = int(target.max_kbps or 0) or int(target.bitrate_kbps)

        def linked(enc):
            link = StreamLink(target.name, target.server, target.key, enc, target.bandwidth_test)
            link.output_id = target.output_id or target.name
            link.delay_s = delay_s
            enc.links.append(link)
            self.streamers.append((target.canvas.id, link))
            return link

        quality = self._twitch_quality(picture)
        if quality is not None:
            link = linked(quality)
            link.start()
            borrowed = self.__dict__.setdefault("borrowed", [])
            if all(quality is not b for b in borrowed):
                borrowed.append(quality)  # its links need sound: it encodes the stream's mix too
                self.engine.mixer.add_tap(quality.push_audio, ("stream",))
            log.info("%s sends Twitch's %dx%d quality (%d kbps): no encoder of its own", target.name,
                     picture[1][0], picture[1][1], quality.bitrate_kbps)
            self.engine.mixer.add_tap(link.push_audio, ("stream",))
            return link
        for enc in getattr(self, "shared", []):
            kbps = int(getattr(enc, "bitrate_kbps", 0))
            if picture is None or getattr(enc, "share_picture", None) != picture or kbps > ceiling or \
                    abs(kbps - int(target.bitrate_kbps)) > SHARE_WITHIN * max(kbps, int(target.bitrate_kbps)):
                continue
            link = StreamLink(target.name, target.server, target.key, enc, target.bandwidth_test)
            link.output_id = target.output_id or target.name
            link.delay_s = delay_s
            enc.links.append(link)
            self.streamers.append((target.canvas.id, link))
            link.start()
            log.info("stream %s joins the shared encoder at %d kbps (set %d)", target.name, kbps, target.bitrate_kbps)
            self.engine.mixer.add_tap(link.push_audio, ("stream",))
            mark_same_keys([st for _, st in self.streamers])
            return link
        if not hasattr(self, "taps"):
            self.taps = []
        if picture is not None and engine_encodes_streams():
            c = target.canvas
            enc = SharedStreamEncoder(c, picture[2], min(int(target.bitrate_kbps), ceiling), codec, time.perf_counter(),
                                      target.size, encoder, auto_bitrate=target.auto_bitrate)
            enc.share_picture = picture
            link = linked(enc)
            enc.start()
            link.start()
            tap = (c.id, enc.push_frame, enc.gpu_size)
            self.taps.append(tap)
            self.__dict__.setdefault("owned", {})[id(enc)] = [tap]
            self.__dict__.setdefault("shared", []).append(enc)
            self.engine.add_frame_tap(*tap)
            self.engine.mixer.add_tap(link.push_audio, ("stream",))
            self.engine.mixer.add_tap(enc.push_audio, ("stream",))
            return link
        st, mine = self._one(target, codec, encoder, delay_s, time.perf_counter())
        self.streamers.append((target.canvas.id, st))
        self.taps += mine
        self.__dict__.setdefault("owned", {})[id(st)] = mine
        for canvas_id, callback, *size in mine:
            self.engine.add_frame_tap(canvas_id, callback, *size)
        self.engine.mixer.add_tap(st.push_audio, ("stream",))
        mark_same_keys([st for _, st in self.streamers])
        return st

    def _twitch_quality(self, picture):
        """The running encoder of one of Twitch Enhanced Broadcasting's qualities with this picture (canvas, size,
        frame rate), also one Twitch left running for the places sending it; None when there is none."""
        if picture is None:
            return None
        tracks = [tr for _, st in self.streamers if getattr(st, "native", False) for tr in getattr(st, "tracks", ())]
        tracks += [enc.track for enc in getattr(self, "borrowed", []) if hasattr(enc, "track")]
        for tr in tracks:
            enc = getattr(tr, "encoder", None)
            if enc is not None and getattr(enc, "running", False) and \
                    (tr.canvas.id, (tr.cfg.width, tr.cfg.height), tr.fps) == tuple(picture):
                return enc
        return None

    def drop(self, output_id: str) -> bool:
        """Stop one destination while the rest stay live (an app's place.stop). False if it isn't streaming."""
        st = self.find(output_id)
        if st is None:
            return False
        self.streamers = [(c, s) for c, s in self.streamers if s is not st]
        mark_same_keys([s for _, s in self.streamers])  # the place left may now be the only one on its key
        owned = self.__dict__.setdefault("owned", {})
        gone = list(owned.pop(id(st), []))
        # Twitch Enhanced Broadcasting whose qualities other places send: Twitch goes, those qualities' encoders
        # stay for the places sending them (each ends with its last place, or with the stream).
        lent = [tr.encoder for tr in getattr(st, "tracks", ())
                if getattr(tr, "encoder", None) is not None and getattr(tr.encoder, "links", None)
                and any(tr.encoder is b for b in getattr(self, "borrowed", []))]
        if lent:
            st.hand_over(lent)
            for enc in lent:
                owned[id(enc)] = [tap for tap in gone if tap[1] == enc.push_frame]
            gone = [tap for tap in gone if not any(tap[1] == enc.push_frame for enc in lent)]
            log.info("%s stopped; %s keep sending its qualities", st.name,
                     ", ".join(link.name for enc in lent for link in enc.links))
        enc = getattr(st, "shared", None)
        if enc is not None and hasattr(enc, "links"):
            enc.links[:] = [link for link in enc.links if link is not st]
            owner = getattr(enc, "owner", None)  # one of Twitch's qualities: it carries on while Twitch is live
            if not enc.links and owner is not None and any(s is owner for _, s in self.streamers):
                enc = None
            elif not enc.links:  # the last place on this encoder: the encoder goes too
                self.shared = [e for e in getattr(self, "shared", []) if e is not enc]
                enc_taps = owned.pop(id(enc), [])
                if self._keep_for_recording(enc, enc_taps):
                    enc = None  # a recording still writes from it: it ends with the recording
                    self.taps = [t for t in getattr(self, "taps", []) if all(t is not k for k in enc_taps)]
                else:
                    gone += enc_taps
                    self.engine.mixer.remove_tap(enc.push_audio)
            else:
                enc = None
        else:
            enc = None
        for canvas_id, callback, *_ in gone:
            self.engine.remove_frame_tap(canvas_id, callback)
        self.taps = [t for t in getattr(self, "taps", []) if all(t is not g for g in gone)]
        self.engine.mixer.remove_tap(st.push_audio)

        left = self.__dict__.setdefault("left", [])  # (seconds in, its summary): for the end of the stream
        try:  # now, while its clock still runs: its averages are over the time it was live
            left.append((int(getattr(self, "elapsed", 0) or 0), st.summary()))
        except Exception:  # the place still stops
            log.exception("%s: no summary for the place that stopped", output_id)

        def finish():
            st.stop(timeout=5)
            if enc is not None:
                enc.stop(timeout=5)

        threading.Thread(target=finish, name=f"sprigcast-drop-{output_id}", daemon=True).start()
        return True

    def stop(self, wait: bool = True) -> None:
        """Take the outputs off the picture and sound, then let each finish. wait=False (the window): they
        finish on a thread of their own, so ending a stream never holds the window (637 ms on the PC,
        2026-11-16, joining an encoder); finished() then waits, if anything needs to."""
        taps = getattr(self, "taps", None)
        if taps is None:
            taps = [(canvas_id, st.push_frame) for canvas_id, st in self.streamers]
        owned = getattr(self, "owned", {})
        kept = [enc for enc in getattr(self, "shared", []) if self._keep_for_recording(enc, owned.get(id(enc), []))]
        kept_taps = [tap[1] for enc in kept for tap in owned.get(id(enc), [])]
        shared = [enc for enc in getattr(self, "shared", []) if all(enc is not k for k in kept)]
        for canvas_id, callback, *_ in taps:
            if not any(callback == k for k in kept_taps):
                self.engine.remove_frame_tap(canvas_id, callback)
        if kept:  # the recording owns those now: a later stop() mustn't touch them
            self.taps = [t for t in taps if not any(t[1] == k for k in kept_taps)]
            self.shared = list(shared)
        for _, st in self.streamers:
            self.engine.mixer.remove_tap(st.push_audio)
        for enc in [*shared, *getattr(self, "borrowed", [])]:
            self.engine.mixer.remove_tap(enc.push_audio)

        def finish():
            for _, st in self.streamers:
                st.stop(timeout=5)
            for enc in kept:
                enc.links[:] = []  # the recording carries on alone
            for enc in shared:  # after the links: nothing is waiting on its packets any more
                enc.stop(timeout=5)
            for enc in getattr(self, "borrowed", []):  # Twitch's qualities it left running for other places
                if getattr(enc, "running", False):
                    enc.stop(timeout=5)

        if wait:
            finish()
            return
        # Not a daemon: closing SprigCast right after still lets every connection end properly.
        self._finishing = threading.Thread(target=finish, name="sprigcast-stream-end")
        self._finishing.start()

    def _keep_for_recording(self, enc, taps: list) -> bool:
        """The stream is done with a shared encoder. If a recording ("Same as stream") still writes from it,
        it keeps running and ends when the recording lets go. True when it was kept."""
        lock = getattr(enc, "_hold_lock", None)
        if lock is None:
            return False
        engine = self.engine

        def retire():
            for canvas_id, callback, *_ in taps:
                engine.remove_frame_tap(canvas_id, callback)
            engine.mixer.remove_tap(enc.push_audio)
            # Not a daemon: closing SprigCast right after still lets the encoder finish.
            threading.Thread(target=enc.stop, kwargs={"timeout": 5}, name="sprigcast-stream-encoder-end").start()

        with lock:
            if not enc.holders - {"stream"}:
                return False
            enc.holders.discard("stream")
            enc.on_last_let_go = retire
        log.info("stream ended; its encoder keeps going for the %s (Same as stream)",
                 " and ".join({"replay": "clips"}.get(h, h) for h in sorted(enc.holders)) or "recording")
        return True

    def encoders_by_canvas(self) -> dict:
        """Canvas id -> a running shared encoder of this stream: what a "Same as stream" recording writes from.
        The best one when a canvas has several (Enhanced Broadcasting's qualities, places of different sizes): the
        biggest picture, then the highest bitrate, so the recording is never the small quality."""
        def worth(enc) -> tuple:
            return (getattr(enc, "out_width", 0) * getattr(enc, "out_height", 0), getattr(enc, "bitrate_kbps", 0))

        found: dict = {}
        for enc in getattr(self, "shared", []):
            if enc.running and "stream" in enc.holders:
                had = found.get(enc.source_id)
                if had is None or worth(enc) > worth(had):
                    found[enc.source_id] = enc
        return found

    def finished(self, timeout: float | None = None) -> bool:
        """Wait for a stop(wait=False) to be done. True when it is."""
        thread = getattr(self, "_finishing", None)
        if thread is not None:
            thread.join(timeout)
            return not thread.is_alive()
        return True
