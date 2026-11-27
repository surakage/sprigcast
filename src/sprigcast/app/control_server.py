"""The remote control server: one WebSocket on the UI thread with two doors.

ws://127.0.0.1:<port>/sprigcast is the SprigCast API (control/native.py, docs/control-api.md): our own
words, both shapes built in, where new features go. Any other path is the OBS-compatible door
(control/api.py) so apps made for OBS work today; it is frozen and can be switched off in
Settings → Connections. Both share the port, the password and the main window bridge below.

Light by design: Qt's own WebSocket server (no extra threads or packages), nothing runs while no app
is connected, and events come from comparing a small snapshot after each change rather than hooks
all over the app. Volume meters are only sent to apps that ask for them.
"""

from __future__ import annotations

import json
import os
import time
import logging
from collections import deque
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QObject, QSettings, QTimer, Signal, Slot
from PySide6.QtNetwork import QHostAddress, QTcpServer, QTcpSocket
from PySide6.QtWebSockets import QWebSocket, QWebSocketProtocol, QWebSocketServer

from sprigcast.control import web
from sprigcast.control.api import ControlApi
from sprigcast.control import secret_store
from sprigcast.control.native import Later, NativeApi, NativeSession
from sprigcast.control.pairing import Pairing
from sprigcast.control.protocol import Close, Session, Sleep, Step, Sub, new_password
from sprigcast.control.who import asking
from sprigcast.core.quiet import Every

if TYPE_CHECKING:
    from sprigcast.app.main_window import MainWindow

# app.stats redrawsBy: the engine's reasons for drawing a canvas that aren't a source (SC_REDRAW_* in sprigcore.h)
REDRAW_REASONS = {1: "Everything changed", 2: "Bloom switch or transition", 3: "Canvas started", 4: "Once-a-second refresh",
                  5: "Nested too deep"}

log = logging.getLogger(__name__)

_CONNECT_LOG = Every(600)

ENABLED = "control/enabled"
PORT = "control/port"
PASSWORD = "control/password"
USE_PASSWORD = "control/use_password"
WEB_DOOR = "control/web_door"  # plain web links /api/<action> (needs the password)
OBS_DOOR = "control/obs_door"  # also accept apps made for OBS (the frozen compatibility door)
DEVICES = "control/devices"  # paired devices and their keys (Pair a device), encrypted like the password
LOCAL_KEY = "control/local_key"  # the key api.json hands to apps on this PC (not the password), encrypted
LAN = "control/lan"  # let phones and other PCs connect, not just apps on this PC
DEFAULT_PORT = 4455  # OBS's port, so apps find SprigCast without changes (use another if OBS runs too)
NATIVE_PATH = "/sprigcast"  # ws://127.0.0.1:4455/sprigcast is the SprigCast API; any other path, the OBS door
WEB_TIMEOUT_MS = 5000  # a connection that hasn't said what it is by then is dropped
MAX_WAITING = 32  # messages from one connection waiting their turn; past that it's asking faster than answers come
SOON_GAP_S = 0.1  # full looks for changes at most this often: a burst of requests (StreamBridge's pages) shares one
FULL_EVERY = 6  # every this many watches (3 s) the settings are written out and compared anyway
WATCH_MS = 500  # how often stream and record states are compared while an app is connected


def _own_settings() -> QSettings:
    """SprigCast's settings without Qt's fallback places (HKLM, Slothbloom's OrganizationDefaults): a secret or a
    device list missing here must read as missing, never as an old copy left somewhere else."""
    q = QSettings()
    q.setFallbacksEnabled(False)
    return q


def _secret(key: str, make=None) -> str:
    """A secret saved encrypted for this Windows user (secret_store). A plain one from before is
    encrypted now; a missing or unreadable one is made anew when `make` is given."""
    q = _own_settings()
    saved = str(q.value(key, "") or "")
    value = secret_store.unprotect(saved)
    if value and not secret_store.is_protected(saved) and secret_store.available():
        q.setValue(key, secret_store.protect(value))  # plain text from an older SprigCast: encrypt it now
    if not value and make is not None:
        if saved:  # there was one, but it can't be read here: keep it aside instead of losing it for good
            log.warning("control: the saved %s couldn't be read (encrypted for another Windows user or PC); "
                        "made a new one and kept the old aside", key)
            q.setValue(key + "_unreadable", saved)
        value = make()
        q.setValue(key, secret_store.protect(value))
        q.sync()
    return value


def save_secret(key: str, value: str) -> bool:
    """Save a secret encrypted, written to disk now (not when Qt gets round to it, which a closed or updated
    SprigCast may never reach) and read back to be sure. False, logged, if it didn't stick."""
    q = QSettings()
    q.setValue(key, secret_store.protect(value))
    q.sync()
    back = secret_store.unprotect(str(_own_settings().value(key, "") or ""))
    if q.status() != QSettings.Status.NoError or back != value:
        log.warning("control: %s didn't save (settings status %s, read back %s)", key, q.status().name,
                    "nothing" if not back else "something else")
        return False
    return True


def load_devices() -> list:
    """Paired devices from the last run. A saved list that can't be read is kept aside (control/devices_unreadable)
    rather than written over by the next pairing, and logged, so it can still be recovered."""
    from sprigcast.control.pairing import Pairing

    q = _own_settings()
    saved = str(q.value(DEVICES, "") or "")
    text = _secret(DEVICES)
    devices = Pairing.devices_from_json(text)
    if saved and not devices and text.strip() not in ("[]",):
        log.warning("control: the saved paired devices couldn't be read (%s); kept aside",
                    "encrypted for another Windows user or PC" if not text else "not a device list")
        q.setValue(DEVICES + "_unreadable", saved)
        q.sync()
    log.info("control: %d paired device%s%s", len(devices), "" if len(devices) == 1 else "s",
             "".join(f"; {d.name} ({d.id}, key {fingerprint(d.key)})" for d in devices))
    return devices


def fingerprint(key: str) -> str:
    """A short, one-way tag for a key in the log (which key is which, never the key itself)."""
    import hashlib

    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:8] if key else "none"


def load_password() -> str:
    return _secret(PASSWORD, new_password)


def local_key() -> str:
    """What api.json hands to apps on this PC: its own key, so the file never holds the password."""
    import secrets

    return _secret(LOCAL_KEY, lambda: "sc_local_" + secrets.token_urlsafe(24))


def saved_settings() -> dict[str, Any]:
    q = QSettings()
    password = load_password()
    return {
        "enabled": q.value(ENABLED, True, type=bool),  # on: this PC's apps (Stream Deck) connect by themselves
        "port": q.value(PORT, DEFAULT_PORT, type=int),
        "password": password,
        "use_password": q.value(USE_PASSWORD, True, type=bool),
        "lan": q.value(LAN, False, type=bool),
        "obs_door": q.value(OBS_DOOR, True, type=bool),
        "web_door": q.value(WEB_DOOR, False, type=bool),
    }


def _outcome(out: dict | None) -> str | dict | None:
    """_start_streaming / _start_recording's answer as the API's hosts give it."""
    out = out or {}
    if out.get("problem"):
        return str(out["problem"])
    if out.get("warnings"):
        return {"warnings": list(out["warnings"]), "started": bool(out.get("started"))}
    return None


class WindowHost:
    """ControlApi's view of the main window (see control/api.py Host)."""

    def __init__(self, window: MainWindow | None):
        self.w = window  # None while SprigCast starts (open_early): apps can sign in, their requests wait

    @property
    def collection(self):
        return self.w.collection

    def commit_settings(self, sources) -> None:
        """Only these sources' settings changed (a plugin's own state): see MainWindow._settings_changed."""
        self.w._settings_changed(sources)

    def commit(self, what: str) -> None:
        w = self.w
        w._changed()
        if what == "program":
            w._refresh_scenes()
            w._refresh_previews()
        elif what == "items":
            w._refresh_sources()
        elif what == "sound":
            w._refresh_audio()
        elif what == "states":
            w.live_panel.show_scenes(w.collection)
        elif what == "transition":
            w._transition_edited()
        elif what == "places":
            w._refresh_stream_boxes()
        elif what in ("sources", "filters"):
            w._refresh_sources()
        elif what == "scenes":  # blooms made or removed
            w._refresh_scenes()
            w._refresh_sources()
            w._refresh_previews()

    def stream_key(self) -> int:
        """Which stream this is (0 offline): a stop and a new start between two looks still say stream.changed
        off, then on (StreamBridge counts on the stream's end, 2026-11-21)."""
        session = self.w.streaming
        if session is None:
            return 0
        key = getattr(session, "_control_key", None)
        if key is None:
            WindowHost._streams = key = getattr(WindowHost, "_streams", 0) + 1
            try:
                session._control_key = key
            except AttributeError:  # a session that takes no attributes: each look counts as the same stream
                return 1
        return key

    def stream_status(self) -> dict[str, Any] | None:
        session = self.w.streaming
        if session is None:
            return None
        streamers = list({id(st): st for _, st in session.streamers}.values())
        # Every place sends the same pictures: "frames" is one place's count (the most any sent) and "skipped"
        # the most any place skipped, as one output would report them. A sum counted each frame once per place
        # (35,885 frames at 302 s for two places on the PC, 2026-11-18).
        return {
            "seconds": session.elapsed,
            "frames": max((getattr(st, "frames", 0) for st in streamers), default=0),
            "skipped": max((getattr(st, "dropped", 0) for st in streamers), default=0),
            "bytes": sum(getattr(st, "bytes_out", 0) for st in streamers),
            "reconnecting": any(not getattr(st, "connected", True) and not getattr(st, "delay_left", lambda: 0.0)()
                                for st in streamers),
        }

    def stream_places(self) -> dict[str, dict[str, Any]]:
        """Each place being sent to while live, by output id: on, reconnecting, seconds, skipped, frames, bytes."""
        session = self.w.streaming
        out: dict[str, dict[str, Any]] = {}
        if session is None:
            return out
        now = time.monotonic()
        for _, st in session.streamers:
            connected = getattr(st, "connected", True)
            tries = int(getattr(st, "reconnects", 0))
            out[getattr(st, "output_id", None) or st.name] = {
                "on": True,
                "state": "live" if connected else "reconnecting" if tries else "connecting",
                "reconnects": tries,  # how often it dropped so far (each new try counts one more)
                "reconnecting": not connected and not getattr(st, "delay_left", lambda: 0.0)(),
                "seconds": round(now - getattr(st, "started_at", session.started_at), 1),
                "skipped": getattr(st, "dropped", 0),
                "frames": getattr(st, "frames", 0),
                "bytes": getattr(st, "bytes_out", 0),
                "congestion": min(1.0, max(0.0, float(getattr(st, "backlog_seconds", lambda: 0.0)()) / 5)),
            }
        return out

    def place_problem(self, output_id: str) -> str | None:
        """Why this place can't go live yet ("Needs a stream key"), or None when it's ready."""
        from sprigcast.app.destinations import problem

        output = next((o for o in self.w.collection.outputs if o.id == output_id), None)
        return problem(output, self.w.collection, self.w._has_key) if output is not None else None

    def start_place(self, output_id: str) -> str | None:
        return self.w.start_place(output_id)

    def stop_place(self, output_id: str) -> str | None:
        return self.w.stop_place(output_id)

    def record_status(self) -> dict[str, Any] | None:
        session = self.w.recording
        if session is None:
            return None
        paths = getattr(session, "paths", [])
        return {"seconds": session.elapsed, "path": str(paths[0]) if paths else "",
                "paused": bool(getattr(session, "paused", False))}

    def pause_record(self, pause: bool | None) -> str | None:
        """Pause (True), resume (False) or switch (None). A problem, or None."""
        return self.w.pause_recording(pause)

    def mark_chapter(self, name: str = "") -> str | None:
        return self.w.mark_chapter(name)

    def clips_ready(self) -> bool:
        return self.w.replay is not None

    # Going live or recording from an app never waits on a pop-up (the Stream doctor's warnings, low space...):
    # problems and warnings come back in the answer, and force goes ahead past the warnings.

    def start_stream(self, force: bool = False) -> str | dict | None:
        """None: live now. A problem (text), or {"warnings": [...]} when the doctor says wait (nothing started).
        {"warnings": [...], "started": True} when force went live past them."""
        if not any(o.type == "stream" and o.enabled for o in self.w.collection.outputs):
            return "No place to stream to is switched on (Live → Destinations)."
        if self.w.streaming is not None:
            return None
        out = self.w._start_streaming(remote=True, force=force)
        return _outcome(out)

    def stop_stream(self) -> None:
        QTimer.singleShot(0, self.w._stop_streaming)

    def start_record(self, force: bool = False) -> str | dict | None:
        """Like start_stream, for recording (the warning is low space on the drive)."""
        col = self.w.collection
        if not any(c.enabled and c.id in col.recording.canvases for c in col.canvases):
            return "No canvas is ticked for recording (Settings → Recording)."
        if self.w.recording is not None:
            return None
        return _outcome(self.w._start_recording(remote=True, force=force))

    def stop_record(self) -> None:
        QTimer.singleShot(0, self.w._stop_recording)

    def set_clips(self, on: bool) -> str | None:
        if on and not self.w._clip_canvases():
            return "Turn on a canvas first (Settings → Video)."
        QTimer.singleShot(0, lambda: self.w._toggle_replay(on))
        return None

    def save_clip(self) -> list[str]:
        return [str(p) for p in self.w._save_replay()]

    def source_sizes(self) -> dict[str, tuple[int, int]]:
        return self.w.source_sizes

    def screenshot(self, shapes: list[str], width: int | None, save: bool, as_data: bool) -> list[dict[str, Any]]:
        return self.w._screenshot(shapes, width, save, as_data)

    def source_screenshot(self, source_id: str, name: str, width: int | None, save: bool,
                          as_data: bool) -> dict[str, Any] | None:
        return self.w._source_screenshot(source_id, name, width, save, as_data)

    def undo(self) -> bool:
        """Ctrl+Z for apps (edit.undo). False: nothing to undo."""
        if not self.w._undo:
            return False
        self.w.undo()
        return True

    def redo(self) -> bool:
        if not self.w._redo:
            return False
        self.w.redo()
        return True

    def garden_replaced(self) -> None:
        """Every bloom, sprig and item was put in again in place (an app's batch): the lists are read again, as
        after an undo."""
        self.w._refresh_all()

    def new_undo_step(self) -> None:
        """The next change is an undo step of its own, however soon after the last (a batch from an app)."""
        self.w._undo_at = 0.0

    def bloom_screenshot(self, scene_id: str, name: str, width: int | None, save: bool,
                         as_data: bool) -> dict[str, Any] | None:
        return self.w._bloom_shot_for_apps(scene_id, name, width, save, as_data)

    # Later versions (control/native.Later): the picture is made on a worker, the window never waits for it.

    def screenshot_later(self, shapes: list[str], width: int | None, save: bool, as_data: bool) -> Later:
        return self.w._screenshot_later(shapes, width, save, as_data)

    def source_screenshot_later(self, source_id: str, name: str, width: int | None, save: bool,
                                as_data: bool) -> Later:
        return self.w._source_shot_later(source_id, name, width, save, as_data)

    def bloom_screenshots_later(self, scene_ids: list[str], name: str, width: int | None, save: bool,
                                as_data: bool) -> Later:
        return self.w._bloom_shots_later(scene_ids, name, width, save, as_data)

    def source_instance(self, source_id: str):
        """The running source (from the engine), for plugins; None until it's loaded."""
        return self.w.engine._sources.get(source_id)

    def hold(self, source_ids, seconds: float, token: object | None = None) -> object:
        """Freeze sources and sprig placements on the next frame drawn (for plugins: Snap). See Engine.hold."""
        return self.w.engine.hold(source_ids, seconds, token)

    def let_go(self, token: object) -> None:
        self.w.engine.let_go(token)

    def grab_picture(self, source_id: str | None, callback) -> None:
        """The next picture of a source, or of what's on air (None), handed to callback on the render thread."""
        self.w.engine.grab_source(source_id, callback)

    def media_info(self, source_id: str) -> dict[str, Any] | None:
        source = self.w.media_source(source_id)
        return source.info() if source is not None else None

    def media_state(self, source_id: str) -> str | None:
        """A media source's state for the half-second watch: as the render thread last read it, when that is recent."""
        source = self.w.media_source(source_id)
        return source.recent_state() if source is not None else None

    def source_now(self, source_id: str) -> dict[str, Any] | None:
        """What a loaded source is doing: its state as the Sources list words it ("capturing 1920x1080", "camera
        not found, retrying"), the size of its picture, and whether it rests. None: not loaded yet."""
        source = self.w.engine._sources.get(source_id)
        if source is None:
            return None
        out: dict[str, Any] = {"state": str(getattr(source, "status", "") or ""), "asleep": bool(source.asleep)}
        size = getattr(source, "_size", None)  # (never `size`: a text would be drawn on the window's thread for it)
        if isinstance(size, tuple) and len(size) == 2 and min(size) > 0:
            out["size"] = [int(size[0]), int(size[1])]
        feed = getattr(source, "_feed", None)
        # What the device really delivers (a card fed 1440p gives 2560x1440 for a 720p source: PC 2026-11-21, where
        # this said the size it was shrunk to as it was read).
        picture = getattr(feed, "given", None) or getattr(feed, "picture", None)
        if picture:
            out["cameraSize"] = [int(picture[0]), int(picture[1])]
        if feed is not None and getattr(feed, "status", ""):
            out["state"] = str(feed.status)
        return out

    def media_control(self, source_id: str, what: str, seconds: float | None = None) -> dict[str, Any] | None:
        return self.w.media_control(source_id, what, seconds)

    def stats(self) -> dict[str, Any]:
        out: dict[str, Any] = {"recordDirectory": str(self.w._recordings_folder())}
        stats = getattr(self.w, "_last_stats", None)
        if stats is not None:
            out.update({"activeFps": stats.fps, "averageFrameRenderTime": stats.render_ms,
                        "renderSkippedFrames": stats.dropped_ticks})
        try:
            import psutil

            if getattr(self, "_process", None) is None:
                self._process = psutil.Process()
                self._process.cpu_percent()  # the first reading is always 0: it starts the count
            out["memoryUsage"] = self._process.memory_info().rss / (1024 * 1024)
            out["cpuUsage"] = self._process.cpu_percent() / (psutil.cpu_count() or 1)  # of the whole PC, like OBS
            out["availableDiskSpace"] = psutil.disk_usage(out["recordDirectory"]).free / (1024 * 1024)
        except Exception:
            pass
        native = getattr(getattr(self.w, "engine", None), "_native", None)
        if native is not None and not getattr(native, "failed", False):
            try:  # the native engine's own counts: frames, and canvases really drawn (a still bloom isn't redrawn)
                counts = native.engine.stats()
                out["engineTicks"], out["canvasDraws"] = int(counts["ticks"]), int(counts["canvas_draws"])
                take = getattr(native.engine, "redraws_take", None)
                if take is not None:  # which sources kept a bloom being redrawn since the last ask, by name
                    from sprigcast import sprigcore

                    names = {sprigcore.key(s.id): s.name for s in getattr(self.w.collection, "sources", [])}
                    names.update(REDRAW_REASONS)
                    by: dict[str, int] = {}
                    for k, n in take().items():
                        by[names.get(k, "Other")] = by.get(names.get(k, "Other"), 0) + n
                    out["redrawsBy"] = by
            except Exception:  # noqa: BLE001
                pass
        status = self.stream_status()
        if status:
            out["outputSkippedFrames"], out["outputTotalFrames"] = status["skipped"], status["frames"] + status["skipped"]
        return out

    def shelf_lists(self) -> dict[str, Any]:
        """The garden and path in use, and every one of each, A-Z (by name)."""
        return self.w.gardens.lists()

    def shelf_do(self, kind: str, op: str, name: str, new_name: str = "") -> str | None:
        """kind garden or path; op switch, new, duplicate, rename or remove. A problem, or None."""
        return self.w.gardens.do(kind, op, name, new_name)

    def camera_on(self) -> bool:
        return self.w.camera is not None

    def set_camera(self, on: bool) -> str | None:
        """SprigCast Camera on or off, like Live's button. A reason it couldn't, or None."""
        if on and self.w.camera is None:
            from sprigcast.output import virtualcam

            if not (virtualcam.supported()[0] and virtualcam.installed()):  # never a setup prompt from an app
                return "Set up SprigCast Camera first: Settings → Video → Virtual camera."
        if (self.w.camera is not None) != on:
            self.w._toggle_camera()
        if (self.w.camera is not None) != on:
            return getattr(self.w, "_camera_error", "") or "SprigCast Camera couldn't start."
        return None

    def hotkeys(self) -> list[tuple[str, str]]:
        from sprigcast.app.hotkeys import actions

        return actions(self.w.collection)

    def trigger_hotkey(self, action: str) -> str | None:
        """Do what the hotkey does. Returns its note when it couldn't ("Not in this bloom: Facecam")."""
        from sprigcast.app.hotkeys import TOGGLE_RECORD, TOGGLE_STREAM

        start = {TOGGLE_STREAM: ("streaming", self.start_stream), TOGGLE_RECORD: ("recording", self.start_record)}.get(action)
        if start is not None and getattr(self.w, start[0], None) is None:  # going live or recording from an app: no pop-ups
            out = start[1]()
            if isinstance(out, dict):
                return "Not started: " + " ".join(out["warnings"])
            return out
        self.w._on_hotkey(action)
        return getattr(self.w, "_last_hotkey_note", None)


NAMELESS = "an app that doesn't say its name"
SLOW_ACCEPT_MS = 50.0  # taking a connection (hello sent, lists read) is a few ms: longer is said in the log


def _client_label(socket) -> str:
    """Who an app says it is, from its connection request: its User-Agent and the page origin it came from (an
    origin has no path, so nothing personal), cut short."""
    parts = []
    try:
        agent = bytes(socket.request().rawHeader(b"User-Agent")).decode("utf-8", "replace").strip()
    except Exception:  # noqa: BLE001 - a socket Qt already let go
        agent = ""
    try:
        origin = str(socket.origin() or "").strip()
    except Exception:  # noqa: BLE001
        origin = ""
    if agent:
        parts.append(f"says it is {agent[:60]!r}")
    if origin and origin != "null":
        parts.append(f"from the page {origin[:60]}")
    return ", ".join(parts) or NAMELESS


class ControlServer(QObject):
    """Owns the listening socket and one session per connected app.

    Apps on ws://host:port/sprigcast speak the SprigCast API (control/native.py); any other path is the
    OBS-compatible door (control/api.py), which can be switched off and gets no new features.
    """

    _later_done = Signal(object)  # (socket, steps, Later, future) from a worker: answered on the window's thread
    ready = True  # False while it was opened before the window (open_early)

    def __init__(self, api: ControlApi, parent: QObject | None = None, native: NativeApi | None = None):
        super().__init__(parent)
        self._later_done.connect(self._finish_later)
        self._later_busy = False  # a Later answer's work is running: the inbox waits for it
        self.api = api
        self.native = native if native is not None else NativeApi(api.host)
        api.custom_event = lambda data: self.broadcast("CustomEvent", Sub.GENERAL, data)
        self.native.custom_event = lambda data: self.native_broadcast("custom", {"data": data})
        self.native.plugin_event = self.native_broadcast
        self.server: QWebSocketServer | None = None
        self.tcp: QTcpServer | None = None
        self.web_door = False  # plain web links (/api/<action>); Settings → Connections
        self.sessions: dict[QWebSocket, Session | NativeSession] = {}
        self._labels: dict[QWebSocket, tuple[str, float]] = {}  # who each app says it is, and since when (the log)
        self.password = ""
        self.obs_door = True  # also accept apps made for OBS (Settings → Connections)
        self.frame_seconds = 1 / 60
        self.error = ""
        self.watch = QTimer(self)
        self.watch.setInterval(WATCH_MS)
        self.watch.timeout.connect(lambda: self.notify(full=False))
        self._notify_queued = False
        self._edited = True  # something was edited or asked since the settings were last written out
        self._running: tuple | None = None  # (port, password, lan, obs door) it listens with
        self.info_path: Path | None = None  # where apps on this PC find the port and password (api.json)
        self.connection_path: Path | None = None  # %APPDATA%\SprigCast\connection.json: port and doors, no secrets
        self.on_clients_changed = lambda count: None
        self.pairing = Pairing(load_devices(), on_change=self._devices_changed)
        self._devices_raw = str(_own_settings().value(DEVICES, "") or "")  # the saved value this copy last matched
        self._devices_seen = {d.id for d in self.pairing.devices}
        self.pairing.on_seen = self.keep_devices  # a paired key signed in or left: last_seen saved (throttled there)
        self._signed_in: set[int] = set()  # sessions whose sign-in was already marked on their device
        self.local_key = ""  # set by start() while a password is in use
        # Everything apps ask is answered here, one at a time, in the order it came (_queue).
        self._inbox: deque[tuple[object | None, Callable[[], None]]] = deque()
        self._answering = False
        self._drain_queued = False
        self._waiting: dict[object, int] = {}  # connection -> its messages in the inbox
        # Open before the window (open_early): apps connect and sign in at once, and what they ask waits in _held
        # until the window is there (ready). Streamer.bot tried while SprigCast was still starting and, refused,
        # waited 30 s to try again (PC, 2026-11-26: port open 8.2 s after start, its attempt at 6.1 s).
        self.ready = True
        self._held: list[tuple[QWebSocket, str]] = []

    def _stored_devices(self) -> tuple[str, list | None]:
        """The saved value as it is now, and its devices (None if it can't be read here)."""
        raw = str(_own_settings().value(DEVICES, "") or "")
        if not raw:
            return raw, []
        text = secret_store.unprotect(raw)
        if not text:
            return raw, None
        return raw, Pairing.devices_from_json(text)

    def sync_devices(self, save: bool = False) -> None:
        """Bring this copy's paired devices and the saved list together, and save the result when it differs.

        Each change used to save this copy's whole list, so a second SprigCast with the same settings (a test tool,
        or two copies at once) wrote its own list over the first one's: a device paired in one was lost, or came
        back with the other's key (PC test 2026-11-16: StreamBridge paired but gone after the update, the harness
        listed but its key refused). Now the saved list is read first and merged."""
        raw, stored = self._stored_devices()
        if raw == self._devices_raw and not save:
            return
        if stored is None:  # can't be decrypted here: only a change made here writes over it (kept aside at start)
            if not save:
                return
            log.warning("control: the saved paired devices couldn't be read; saving this copy's list")
            stored = []
        merged = self.pairing.merged(stored, self._devices_seen)
        self.pairing.devices = merged
        if merged != stored:
            names = ", ".join(f"{d.name} ({d.id}, key {fingerprint(d.key)})" for d in merged) or "none"
            try:
                saved = save_secret(DEVICES, self.pairing.to_json())
            except Exception:
                log.exception("control: the paired devices couldn't be saved: %s", names)
                return
            if saved:
                log.info("control: paired devices saved: %s", names)
            raw, stored = self._stored_devices()
        self._devices_raw = raw
        self._devices_seen = {d.id for d in stored or []}

    def _devices_changed(self) -> None:
        """A device was paired or removed here: saved at once, merged with the saved list."""
        self.sync_devices(save=True)

    def keep_devices(self) -> None:
        """Settings → Save and closing: what's paired is saved (and anything another copy saved is kept)."""
        self.sync_devices(save=True)

    def drop_key(self, key: str) -> None:
        """A paired device was removed: close its open connections too."""
        for socket, session in self._natives():
            if key and session.key == key:
                socket.close(QWebSocketProtocol.CloseCode(4009), "This device was removed in SprigCast.")

    def keys(self) -> list[str]:
        """Everything that works like the password: paired devices' keys and this PC's api.json key. Devices another
        SprigCast paired since are picked up first (a registry read; decrypted only when it changed)."""
        self.sync_devices()
        return [*self.pairing.keys(), *([self.local_key] if self.local_key else [])]

    def _obs(self) -> list[tuple[QWebSocket, Session]]:
        return [(k, s) for k, s in self.sessions.items() if isinstance(s, Session)]

    def _natives(self) -> list[tuple[QWebSocket, NativeSession]]:
        return [(k, s) for k, s in self.sessions.items() if isinstance(s, NativeSession)]

    # Listening -------------------------------------------------------------------------

    def start(self, port: int, password: str = "", lan: bool = False) -> bool:
        self.stop()
        self.password = password
        self.local_key = local_key() if password else ""
        # One port for both: a plain TCP server looks at each new connection's first lines and hands
        # WebSocket upgrades to the WebSocket server (which never listens itself) and web links to web.py.
        self.tcp = QTcpServer(self)
        address = QHostAddress(QHostAddress.SpecialAddress.Any if lan else QHostAddress.SpecialAddress.LocalHost)
        if not self.tcp.listen(address, port):
            self.error = self.tcp.errorString() or f"port {port} is in use"
            log.warning("control: could not listen on port %d: %s", port, self.error)
            self.tcp = None
            return False
        self.error = ""
        self.server = QWebSocketServer("SprigCast", QWebSocketServer.SslMode.NonSecureMode, self)
        self.server.newConnection.connect(self._accept)
        self.tcp.newConnection.connect(self._incoming)
        # "localhost" is ::1 first on Windows: with only 127.0.0.1 open, an app that connects by that name waited
        # about 2 s for the refusal before it tried 127.0.0.1 (PC, 2026-11-21: 2,028 / 2,025 / 2,016 ms against
        # 4 / 15 / 0). So this PC's other own address is open too. A PC without it just goes without.
        self.tcp6 = None
        if not lan:
            tcp6 = QTcpServer(self)
            if tcp6.listen(QHostAddress(QHostAddress.SpecialAddress.LocalHostIPv6), self.tcp.serverPort()):
                tcp6.newConnection.connect(self._incoming)
                self.tcp6 = tcp6
            else:
                log.info("control: not listening on ::1 (%s): apps should connect to 127.0.0.1", tcp6.errorString())
                tcp6.deleteLater()
        log.info("control: listening on %s:%d%s", "all addresses" if lan else "localhost", port,
                 " with a password" if password else " without a password")
        self._write_info(self.local_key)
        return True

    def _write_info(self, key: str) -> None:
        """api.json next to SprigCast's settings (this Windows user's own folder): the port and a key, so an app on
        this PC (the Stream Deck plugin) connects without anyone typing them, like Wave Link's ws-info.json. The key
        is this PC's own, not the password, and changes with the password. Gone again when the API stops."""
        info = {"app": "SprigCast", "port": self.port, "path": NATIVE_PATH, "password": key}
        self._write(self.info_path, info)
        self.write_connection()

    def write_connection(self) -> None:
        """connection.json: where SprigCast is listening and which doors are open, for apps like THSV StreamBridge
        that find it by themselves. Never the password or a key (apps ask the user for the password)."""
        if self.connection_path is None or not self.listening:
            return
        from sprigcast import __version__, build_label

        info = {"app": "SprigCast", "version": __version__, "build": build_label(), "port": self.port, "path": NATIVE_PATH,
                "obsCompatible": bool(self.obs_door), "passwordSet": bool(self.password), "pid": os.getpid()}
        self._write(self.connection_path, info)

    @staticmethod
    def _write(path: Path | None, info: dict[str, Any]) -> None:
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(info, indent=2), encoding="utf-8")
        except OSError as exc:
            log.warning("control: couldn't write %s: %s", path, exc)

    def _remove_info(self) -> None:
        for path in (self.info_path, self.connection_path):
            if path is not None:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass

    def stop(self) -> None:
        self.keep_devices()  # closing or restarting: what's paired now is what the next start finds
        if self.server is None:
            return
        self._remove_info()
        self.broadcast("ExitStarted", Sub.GENERAL, None)
        self.native_broadcast("app.closing", {})
        for socket in list(self.sessions):
            socket.close(QWebSocketProtocol.CloseCode.CloseCodeGoingAway, "SprigCast stopped the server")
        self.sessions.clear()
        self.server.close()
        self.server.deleteLater()
        self.server = None
        for name in ("tcp", "tcp6"):
            tcp = getattr(self, name, None)
            if tcp is not None:
                tcp.close()
                tcp.deleteLater()
                setattr(self, name, None)
        self.watch.stop()
        self.on_clients_changed(0)

    @property
    def listening(self) -> bool:
        return self.tcp is not None and self.tcp.isListening()

    @property
    def port(self) -> int:
        return self.tcp.serverPort() if self.tcp is not None else 0

    # Connections -----------------------------------------------------------------------

    def _incoming(self) -> None:
        for tcp in (self.tcp, getattr(self, "tcp6", None)):  # 127.0.0.1 and ::1 (what "localhost" tries first)
            while tcp is not None and tcp.hasPendingConnections():
                sock = tcp.nextPendingConnection()
                # Bound slots, not lambdas: a lambda makes PySide watch the socket's destroyed signal, and Qt's
                # WebSocket code then warns "wildcard call disconnects from destroyed signal" when it lets it go.
                sock.readyRead.connect(self._ready_read)
                sock.disconnected.connect(sock.deleteLater)
                self._note_arrival(sock.peerPort(), "tcp")
                QTimer.singleShot(WEB_TIMEOUT_MS, lambda s=sock: self._too_slow(s))

    def _note_arrival(self, port: int, step: str) -> None:
        """When a connection got this far (by the asker's port), for the slow-connection line in _accept."""
        arrivals = self.__dict__.setdefault("_arrivals", {})
        if step == "tcp":
            if len(arrivals) > 64:  # ones that never became a WebSocket (web links, port checks)
                arrivals.clear()
            arrivals[port] = {}
        arrivals.setdefault(port, {})[step] = time.perf_counter()

    @Slot()
    def _ready_read(self) -> None:
        sock = self.sender()
        if isinstance(sock, QTcpSocket):
            self._sniff(sock)

    def _too_slow(self, sock: QTcpSocket) -> None:
        try:
            if sock.property("sprigcast_door") is None and sock.state() == QTcpSocket.SocketState.ConnectedState:
                sock.abort()
        except RuntimeError:
            pass  # already gone

    def _sniff(self, sock: QTcpSocket) -> None:
        """WebSocket upgrade or web link? Peek without taking the bytes so the WebSocket server reads them."""
        if sock.property("sprigcast_door") is not None:
            return
        raw = bytes(sock.peek(web.MAX_REQUEST + 1))
        if web.header_end(raw) < 0 and len(raw) <= web.MAX_REQUEST:
            return  # wait for the rest of the headers
        if web.header_end(raw) >= 0 and web.is_websocket(raw):
            sock.setProperty("sprigcast_door", "ws")
            self._note_arrival(sock.peerPort(), "request")
            sock.readyRead.disconnect(self._ready_read)
            if self.server is not None:
                self.server.handleConnection(sock)
            return
        try:
            request = web.parse(raw)
        except ValueError:
            sock.setProperty("sprigcast_door", "bad")
            sock.write(web.response(400, {"ok": False, "error": {"code": "bad_request", "message": "Not HTTP."}}))
            sock.disconnectFromHost()
            return
        if request is None:
            return  # the body is still arriving
        sock.setProperty("sprigcast_door", "web")
        sock.readAll()
        self._queue(None, lambda: self._answer_web(sock, request))  # outside the socket's signal: see _arrived

    def _answer_web(self, sock: QTcpSocket, request) -> None:
        status, body = web.handle(self.native, request, enabled=self.web_door, password=self.password,
                                  keys=self.keys() if self.password else [], pairing=self.pairing)
        log.info("control: web link %s %s -> %d", request.method, request.path, status)
        try:
            sock.write(web.response(status, body))
            sock.disconnectFromHost()
        except RuntimeError:
            pass  # the asker hung up while the answer was being made: Qt already let the socket go
        self.soon()

    def _accept(self) -> None:
        while self.server is not None and self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            came = self.__dict__.get("_arrivals", {}).pop(socket.peerPort(), {})
            now = time.perf_counter()
            # Where this connection's time goes, said when it's slow: waiting for the asker's request after it
            # connected, the WebSocket handshake, then each step here.
            marks = [("start", came.get("tcp", now)), ("its request", came.get("request", came.get("tcp", now))),
                     ("handshake", now)]
            native = socket.requestUrl().path().rstrip("/").lower() == NATIVE_PATH
            if not native and not self.obs_door:
                socket.close(QWebSocketProtocol.CloseCode(4000),
                             f"Connect to {NATIVE_PATH} (apps made for OBS are switched off in SprigCast)")
                socket.deleteLater()
                continue
            if native:
                session: Session | NativeSession = NativeSession(api=self.native, password=self.password,
                                                                 keys=self.keys(), pairing=self.pairing)
                first = not self._natives()
            else:
                session = Session(handler=self.api.handle, password=self.password, keys=self.keys(),
                                  frame_seconds=self.frame_seconds)
                first = not self._obs()
            marks.append(("keys", time.perf_counter()))
            self.sessions[socket] = session
            socket.textMessageReceived.connect(lambda text, s=socket: self._arrived(s, text))
            socket.binaryMessageReceived.connect(lambda _data, s=socket: s.close(
                QWebSocketProtocol.CloseCode(4002), "Only JSON text messages are understood."))
            socket.disconnected.connect(lambda s=socket: self._gone(s))
            socket.sendTextMessage(session.hello())
            marks.append(("hello", time.perf_counter()))
            self._labels[socket] = (_client_label(socket), time.monotonic())
            if _CONNECT_LOG.allow("connect"):  # Streamer.bot and friends reconnect often: 129 lines in a day
                log.info("control: %s app connected from %s (%s)%s", "a SprigCast API" if native else "an OBS-style",
                         socket.peerAddress().toString(), self._labels[socket][0], _CONNECT_LOG.skipped("connect"))
            marks.append(("log", time.perf_counter()))
            if first and self.ready:  # start comparing from now (before the window: when it's ready)
                (self.native if native else self.api).changes()
            marks.append(("first look", time.perf_counter()))
            self.watch.start()
            self.on_clients_changed(len(self.sessions))
            marks.append(("window", time.perf_counter()))
            took = 1000 * (marks[-1][1] - marks[0][1])
            if took >= SLOW_ACCEPT_MS:
                # A new connection waited 4.4 s for its hello on the PC (2026-11-21) and 7 to 35 ms here: this says
                # whether the wait is in here (and in which step) or before the connection ever reaches SprigCast.
                log.info("control: taking a new connection took %.0f ms (%s)", took, ", ".join(
                    f"{name} {1000 * (at - marks[i][1]):.0f}" for i, (name, at) in enumerate(marks[1:])))

    def _mark_seen(self, session, leaving: bool = False) -> None:
        """A paired device's key signed in (once per connection) or its connection closed: its last_seen is now."""
        key = getattr(session, "key", "")
        if not key:
            return
        signed_in = getattr(session, "welcomed", False) or getattr(session, "identified", False)
        if leaving:
            self._signed_in.discard(id(session))
        elif not signed_in or id(session) in self._signed_in:
            return
        else:
            self._signed_in.add(id(session))
        try:
            self.pairing.seen(key)
        except Exception:
            log.exception("control: couldn't note when a paired device was last seen")

    def _gone(self, socket: QWebSocket) -> None:
        gone = self.sessions.get(socket)
        if gone is not None:
            self._mark_seen(gone, leaving=True)
        label, since = self._labels.pop(socket, ("an unnamed app", time.monotonic()))
        if gone is not None and (getattr(gone, "key", "") or getattr(gone, "name", "")):
            # Signed in with a key: its paired name says more than what it sent as it connected (Streamer.bot
            # sends no name: it was "an app that doesn't say its name" all through the PC's log, 2026-11-21).
            try:
                who = self._who(socket)
                label = who if label == NAMELESS else f"{who}, {label}"
            except Exception:  # noqa: BLE001 - only for the log
                pass
        if self.sessions.pop(socket, None) is not None:
            if _CONNECT_LOG.allow("gone"):
                # Which app and after how long: one came and went every ~20 s all night, unnamed (PC 2026-11-21)
                try:
                    code = int(socket.closeCode())
                except (RuntimeError, TypeError, ValueError):
                    code = 0
                log.info("control: %s disconnected after %.0f s (close code %d)%s", label, time.monotonic() - since,
                         code, _CONNECT_LOG.skipped("gone"))
        try:
            socket.deleteLater()
            if not self.sessions:
                self.watch.stop()
        except RuntimeError:  # Qt already freed it (the app is closing): nothing left to tidy
            return
        self.on_clients_changed(len(self.sessions))

    def _arrived(self, socket: QWebSocket, text: str) -> None:
        """A message, answered once the socket's own signal has returned.

        Answering inside the signal left Qt's socket code waiting under ours. Some answers keep the window
        answering while they work (a bloom screenshot for StreamBridge: workers.wait_answering), and anything
        that runs meanwhile can close the connection; the socket was then deleted under that waiting Qt code,
        which crashed in Qt6Core once the answer came back (PC, build 44779596950, 2026-11-20 17:22 UTC, Settings
        opened and closed during the wait). Queued, a closed connection is simply skipped: _gone drops its
        session first, and _pump answers only sessions still here."""
        if self._waiting.get(socket, 0) >= MAX_WAITING:
            if socket in self.sessions:
                log.info("control: closing a connection: %d messages waiting for an answer", MAX_WAITING)
                socket.close(QWebSocketProtocol.CloseCode(4008),
                             "Too many messages waiting for an answer: wait for each answer before asking again.")
            return
        self._waiting[socket] = self._waiting.get(socket, 0) + 1
        self._queue(socket, lambda: self._received(socket, text))

    def _queue(self, socket, answer: Callable[[], None]) -> None:
        """Answer one thing apps asked, after everything asked before it, never inside another answer.

        A screenshot's answer keeps the window answering while its picture is made (workers.wait_answering), and
        every message that arrived meanwhile was answered right there, inside it: each next screenshot nested in
        the one before, until the window's thread ran out of stack (PC 2026-11-20 12:05, build 44785036541,
        asked twice a second with each picture taking 1.7 s). Now what arrives meanwhile waits its turn."""
        self._inbox.append((socket, answer))
        if not self._answering and not self._drain_queued:
            self._drain_queued = True
            QTimer.singleShot(0, self._drain)

    def _drain(self) -> None:
        self._drain_queued = False
        if self._answering or self._later_busy:
            return  # inside an answer (or its work still running): it carries on with the inbox when it's done
        if not self._inbox:
            return
        socket, answer = self._inbox.popleft()
        if socket is not None:
            left = self._waiting.get(socket, 1) - 1
            if left > 0:
                self._waiting[socket] = left
            else:
                self._waiting.pop(socket, None)
        self._answering = True
        try:
            answer()
        except Exception:  # noqa: BLE001  one answer going wrong never stops the ones after it
            log.exception("control: answering an app failed")
        finally:
            self._answering = False
            if self._inbox and not self._drain_queued:
                # The next one a turn of the event loop later: what was held back for the end of this answer's
                # wait (Settings from the Sound gear, workers.when_free) goes first, not after the whole inbox.
                self._drain_queued = True
                QTimer.singleShot(0, self._drain)

    def _received(self, socket: QWebSocket, text: str) -> None:
        session = self.sessions.get(socket)
        if session is not None and not self.ready and (_signed_in(session) or '"do"' in text or '"steps"' in text):
            self.__dict__.setdefault("_held", []).append((socket, text))  # asked before the window was there: answered once it is
            return
        if session is not None:
            self._pump(socket, session.receive(text))
            self._mark_seen(session)
            if getattr(session, "may_change", True):
                self.soon()  # a request may have changed something: tell everyone (and the asker) once it settles

    def _who(self, socket: QWebSocket) -> str:
        """The asking app, for the log: its paired name, else what kind of app it is (never its key)."""
        session = self.sessions.get(socket)
        what = "a SprigCast API app" if isinstance(session, NativeSession) else "an OBS-style app"
        key = getattr(session, "key", "") or ""
        pairing = getattr(self, "pairing", None)  # only for the log: never in a request's way
        device = next((d for d in (pairing.devices if pairing else ()) if key and d.key == key), None)
        if device is not None:
            return f"{device.name} ({what})"
        said = getattr(session, "name", "")  # "name" in its greeting: its own word, so it's quoted as that
        if key and key == getattr(self, "local_key", ""):
            # api.json: Streamer.bot and other local tools read it
            return f"{said!r} ({what} with this PC's key)" if said else f"{what} with this PC's key"
        return f"{said!r} ({what}, by its own word)" if said else what

    def _pump(self, socket: QWebSocket, steps: Iterator[Step]) -> None:
        with asking(self._who(socket)):
            self._pump_steps(socket, steps)

    def _pump_steps(self, socket: QWebSocket, steps: Iterator[Step]) -> None:
        for step in steps:
            if socket not in self.sessions:
                return
            if isinstance(step, Sleep):
                QTimer.singleShot(int(step.seconds * 1000),
                                  lambda: self._queue(None, lambda: self._pump(socket, steps)))
                return
            if isinstance(step, Later):
                # Its work on a worker; the answer, and anything after it, once that's done (_finish_later). The
                # inbox waits meanwhile, so answers stay in order and two pictures never share the engine's view.
                from sprigcast.core import workers

                self._later_busy = True
                future = workers.run(step.job, name="control answer")
                future.add_done_callback(lambda f, s=socket, st=steps, later=step: self._later_done.emit((s, st, later, f)))
                return
            if isinstance(step, Close):
                # Which door and which app, on every refusal: the "app connected" line is folded after the first,
                # so six wrong passwords in a row named nobody (PC 2026-11-22, it was StreamBridge's OBS default).
                who = ""
                if step.code == 4009:
                    door = "SprigCast API" if isinstance(self.sessions.get(socket), NativeSession) else "OBS-style"
                    who = (f" ({door} app, {self._labels.get(socket, (NAMELESS, 0))[0]}; "
                           f"{len(self.pairing.devices)} paired device key(s), the password"
                           f"{' and the api.json key' if self.local_key else ''} checked)")
                log.info("control: closing a connection: %s%s", step.reason, who)
                socket.close(QWebSocketProtocol.CloseCode(step.code), step.reason)
                return
            socket.sendTextMessage(step)
            if getattr(self.sessions.get(socket), "may_change", True):
                self.soon()  # a step list changes things between waits

    @Slot(object)
    def _finish_later(self, done) -> None:
        socket, steps, later, future = done
        self._later_busy = False
        try:
            if socket in self.sessions:
                try:
                    text = later.then(future)
                except Exception:  # noqa: BLE001
                    log.exception("control: finishing an answer failed")
                    text = None
                if isinstance(text, str):
                    socket.sendTextMessage(text)
                self._pump(socket, steps)
            else:
                try:  # the asker hung up: what the work left (a saved file's note) is still tidied
                    later.then(future)
                except Exception:  # noqa: BLE001
                    pass
        finally:
            if self._inbox and not self._drain_queued and not self._later_busy:
                self._drain_queued = True
                QTimer.singleShot(0, self._drain)

    # Events ----------------------------------------------------------------------------

    def become_ready(self) -> None:
        """The window is there: apps that connected while SprigCast started get their first look and answers."""
        if self.ready:
            return
        self.ready = True
        if self._obs():
            self.api.changes()
        if self._natives():
            self.native.changes()
        held, self._held = self.__dict__.get("_held", []), []
        if held:
            log.info("control: answering %d request(s) asked while SprigCast was starting", len(held))
        for socket, text in held:
            if socket in self.sessions:
                self._waiting[socket] = self._waiting.get(socket, 0) + 1
                self._queue(socket, lambda s=socket, t=text: self._received(s, t))

    def soon(self) -> None:
        """Something may have changed: compare once the current work is done (many edits, one look)."""
        self._edited = True
        if self.sessions and not self._notify_queued:
            self._notify_queued = True
            # Each full look writes every source's settings out to compare: a burst of requests from many pages,
            # each with its own look, held the window up while live (PC 2026-11-21). One look per SOON_GAP_S.
            wait = SOON_GAP_S - (time.monotonic() - getattr(self, "_full_at", -SOON_GAP_S))
            QTimer.singleShot(max(0, round(1000 * wait)), self.notify)

    def notify(self, full: bool = True) -> None:
        """full=False (the half-second watch): only what changes by itself (stream, recording, media, states)
        is looked at again, unless something was edited or asked since the last full look."""
        self._light = 0 if full else getattr(self, "_light", 0) + 1
        if not full and (self._edited or self._light >= FULL_EVERY):
            full, self._light = True, 0  # a plugin may change its own settings: looked at fully now and then
        if not self.ready:
            self._notify_queued = False
            return  # no window yet: nothing to compare (become_ready takes the first look)
        if full:
            self._notify_queued, self._edited = False, False
            self._full_at = time.monotonic()
        try:
            if self._obs():
                for event_type, intent, data in self.api.changes():
                    self.broadcast(event_type, intent, data)
            if self._natives():
                for name, data in self.native.changes(full):
                    self.native_broadcast(name, data)
        except Exception:
            log.exception("control: working out events")

    def broadcast(self, event_type: str, intent: int, data: dict[str, Any] | None) -> None:
        """An OBS-style event to the apps on the OBS door."""
        for socket, session in self._obs():
            message = session.event(event_type, intent, data)
            if message is not None:
                socket.sendTextMessage(message)

    def native_broadcast(self, name: str, data: dict[str, Any]) -> None:
        """A SprigCast API event to the apps that asked for its group."""
        for socket, session in self._natives():
            message = session.event(name, data)
            if message is not None:
                socket.sendTextMessage(message)

    def wants_meters(self) -> bool:
        return any(s.wants(Sub.INPUT_VOLUME_METERS) for _, s in self._obs()) or \
            any(s.wants("meters") for _, s in self._natives())

    def meters(self, levels: dict[str, tuple[float, float]]) -> None:
        if not self.sessions or not self.wants_meters():
            return
        if any(s.wants(Sub.INPUT_VOLUME_METERS) for _, s in self._obs()):
            self.broadcast("InputVolumeMeters", Sub.INPUT_VOLUME_METERS, self.api.meters(levels))
        if any(s.wants("meters") for _, s in self._natives()):
            self.native_broadcast("meters", self.native.meters(levels))

    def replay_saved(self, paths: list) -> None:
        if paths:
            self.broadcast("ReplayBufferSaved", Sub.OUTPUTS, {"savedReplayPath": str(paths[0])})
            self.native_broadcast("clip.saved", {"files": [str(p) for p in paths]})


_EARLY: ControlServer | None = None  # opened by open_early, taken over by start_for


def _signed_in(session) -> bool:
    return bool(getattr(session, "identified", False) or getattr(session, "welcomed", False))


def open_early(parent: QObject) -> None:
    """Listen as soon as the app is made, before the garden loads and the window is built, so an app that connects
    while SprigCast starts (Streamer.bot does, then waits 30 s after a refusal) signs in at once. Requests wait
    until the window is there (start_for). Never stops SprigCast from opening."""
    global _EARLY
    try:
        if not saved_settings()["enabled"]:
            return
        server = ControlServer(ControlApi(WindowHost(None)), parent)
        server.ready = False
        apply_settings(server)
        if server.listening:
            _EARLY = server
            log.info("control: open early, while SprigCast starts")
        else:
            server.stop()
            server.deleteLater()
    except Exception:  # noqa: BLE001
        log.exception("control: couldn't open early; it opens with the window")


def start_for(window: MainWindow) -> ControlServer:
    """Make the window's control server and start it if Settings → Connections has it on. One opened early
    (open_early) is handed the window and answers what apps asked meanwhile."""
    global _EARLY
    server, _EARLY = _EARLY, None
    if server is not None:
        server.api.host.w = window
        server.setParent(window)
        apply_settings(server, window)
        server.become_ready()
        return server
    server = ControlServer(ControlApi(WindowHost(window)), window)
    apply_settings(server, window)
    return server


def apply_settings(server: ControlServer, window: MainWindow | None = None) -> None:
    """Start, restart or stop the server to match the saved settings."""
    server.keep_devices()  # Settings → Save: the devices paired while it was open stay paired
    settings = saved_settings()
    if server.info_path is None:
        from sprigcast.app.main import config_dir

        server.info_path = config_dir() / "api.json"
    if server.connection_path is None and os.environ.get("APPDATA"):
        server.connection_path = Path(os.environ["APPDATA"]) / "SprigCast" / "connection.json"
    if window is not None and window.collection.canvases:
        server.frame_seconds = 1 / max(1, window.collection.canvases[0].fps)
    password = settings["password"] if settings["use_password"] else ""
    server.obs_door = settings["obs_door"]
    server.web_door = settings["web_door"]
    wanted = (settings["port"], password, settings["lan"])
    if not settings["enabled"]:
        server.stop()
        server._running = None
        return
    if not server.obs_door:  # switched off just now: say goodbye to apps already on that door
        for socket, _ in server._obs():
            socket.close(QWebSocketProtocol.CloseCode(4000), "Apps made for OBS are switched off in SprigCast")
    if server.listening and server._running == wanted:
        server.write_connection()  # Compatibility mode may have changed
        return
    if server.start(*wanted):
        server._running = wanted
    elif settings["port"] == DEFAULT_PORT:  # OBS holds 4455: take the next free port (api.json tells apps)
        for port in range(DEFAULT_PORT + 1, DEFAULT_PORT + 6):
            if server.start(port, password, settings["lan"]):
                server._running = wanted
                break
        else:
            server._running = None
    else:
        server._running = None
