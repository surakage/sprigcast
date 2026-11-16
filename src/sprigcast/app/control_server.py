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
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QObject, QSettings, QTimer
from PySide6.QtNetwork import QHostAddress, QTcpServer, QTcpSocket
from PySide6.QtWebSockets import QWebSocket, QWebSocketProtocol, QWebSocketServer

from sprigcast.control import web
from sprigcast.control.api import ControlApi
from sprigcast.control import secret_store
from sprigcast.control.native import NativeApi, NativeSession
from sprigcast.control.pairing import Pairing
from sprigcast.control.protocol import Close, Session, Sleep, Step, Sub, new_password
from sprigcast.core.quiet import Every

if TYPE_CHECKING:
    from sprigcast.app.main_window import MainWindow

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
WATCH_MS = 500  # how often stream and record states are compared while an app is connected


def _secret(key: str, make=None) -> str:
    """A secret saved encrypted for this Windows user (secret_store). A plain one from before is
    encrypted now; a missing or unreadable one is made anew when `make` is given."""
    q = QSettings()
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
    back = secret_store.unprotect(str(QSettings().value(key, "") or ""))
    if q.status() != QSettings.Status.NoError or back != value:
        log.warning("control: %s didn't save (settings status %s, read back %s)", key, q.status().name,
                    "nothing" if not back else "something else")
        return False
    return True


def load_devices() -> list:
    """Paired devices from the last run. A saved list that can't be read is kept aside (control/devices_unreadable)
    rather than written over by the next pairing, and logged, so it can still be recovered."""
    from sprigcast.control.pairing import Pairing

    q = QSettings()
    saved = str(q.value(DEVICES, "") or "")
    text = _secret(DEVICES)
    devices = Pairing.devices_from_json(text)
    if saved and not devices and text.strip() not in ("[]",):
        log.warning("control: the saved paired devices couldn't be read (%s); kept aside",
                    "encrypted for another Windows user or PC" if not text else "not a device list")
        q.setValue(DEVICES + "_unreadable", saved)
        q.sync()
    log.info("control: %d paired device%s", len(devices), "" if len(devices) == 1 else "s")
    return devices


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

    def __init__(self, window: MainWindow):
        self.w = window

    @property
    def collection(self):
        return self.w.collection

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

    def stream_status(self) -> dict[str, Any] | None:
        session = self.w.streaming
        if session is None:
            return None
        streamers = [st for _, st in session.streamers]
        return {
            "seconds": session.elapsed,
            "frames": sum(getattr(st, "frames", 0) for st in streamers),
            "skipped": sum(getattr(st, "dropped", 0) for st in streamers),
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
            out[getattr(st, "output_id", None) or st.name] = {
                "on": True,
                "reconnecting": not connected and not getattr(st, "delay_left", lambda: 0.0)(),
                "seconds": round(now - getattr(st, "started_at", session.started_at), 1),
                "skipped": getattr(st, "dropped", 0),
                "frames": getattr(st, "frames", 0),
                "bytes": getattr(st, "bytes_out", 0),
                "congestion": min(1.0, max(0.0, float(getattr(st, "backlog_seconds", lambda: 0.0)()) / 5)),
            }
        return out

    def start_place(self, output_id: str) -> str | None:
        return self.w.start_place(output_id)

    def stop_place(self, output_id: str) -> str | None:
        return self.w.stop_place(output_id)

    def record_status(self) -> dict[str, Any] | None:
        session = self.w.recording
        if session is None:
            return None
        paths = getattr(session, "paths", [])
        return {"seconds": session.elapsed, "path": str(paths[0]) if paths else ""}

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
            return "No canvas is ticked for recording (Settings → Output)."
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
                return "Set up SprigCast Camera first: Settings → Output → Virtual camera."
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


class ControlServer(QObject):
    """Owns the listening socket and one session per connected app.

    Apps on ws://host:port/sprigcast speak the SprigCast API (control/native.py); any other path is the
    OBS-compatible door (control/api.py), which can be switched off and gets no new features.
    """

    def __init__(self, api: ControlApi, parent: QObject | None = None, native: NativeApi | None = None):
        super().__init__(parent)
        self.api = api
        self.native = native if native is not None else NativeApi(api.host)
        api.custom_event = lambda data: self.broadcast("CustomEvent", Sub.GENERAL, data)
        self.native.custom_event = lambda data: self.native_broadcast("custom", {"data": data})
        self.native.plugin_event = self.native_broadcast
        self.server: QWebSocketServer | None = None
        self.tcp: QTcpServer | None = None
        self.web_door = False  # plain web links (/api/<action>); Settings → Connections
        self.sessions: dict[QWebSocket, Session | NativeSession] = {}
        self.password = ""
        self.obs_door = True  # also accept apps made for OBS (Settings → Connections)
        self.frame_seconds = 1 / 60
        self.error = ""
        self.watch = QTimer(self)
        self.watch.setInterval(WATCH_MS)
        self.watch.timeout.connect(self.notify)
        self._notify_queued = False
        self._running: tuple | None = None  # (port, password, lan, obs door) it listens with
        self.info_path: Path | None = None  # where apps on this PC find the port and password (api.json)
        self.connection_path: Path | None = None  # %APPDATA%\SprigCast\connection.json: port and doors, no secrets
        self.on_clients_changed = lambda count: None
        self.pairing = Pairing(load_devices(), on_change=lambda: save_secret(DEVICES, self.pairing.to_json()))
        self.local_key = ""  # set by start() while a password is in use

    def drop_key(self, key: str) -> None:
        """A paired device was removed: close its open connections too."""
        for socket, session in self._natives():
            if key and session.key == key:
                socket.close(QWebSocketProtocol.CloseCode(4009), "This device was removed in SprigCast.")

    def keys(self) -> list[str]:
        """Everything that works like the password: paired devices' keys and this PC's api.json key."""
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
        from sprigcast import __version__

        info = {"app": "SprigCast", "version": __version__, "port": self.port, "path": NATIVE_PATH,
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
        if self.tcp is not None:
            self.tcp.close()
            self.tcp.deleteLater()
            self.tcp = None
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
        while self.tcp is not None and self.tcp.hasPendingConnections():
            sock = self.tcp.nextPendingConnection()
            sock.readyRead.connect(lambda s=sock: self._sniff(s))
            sock.disconnected.connect(sock.deleteLater)
            QTimer.singleShot(WEB_TIMEOUT_MS, lambda s=sock: self._too_slow(s))

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
            sock.readyRead.disconnect()
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
        status, body = web.handle(self.native, request, enabled=self.web_door, password=self.password,
                                  keys=self.keys() if self.password else [], pairing=self.pairing)
        log.info("control: web link %s %s -> %d", request.method, request.path, status)
        sock.write(web.response(status, body))
        sock.disconnectFromHost()
        self.soon()

    def _accept(self) -> None:
        while self.server is not None and self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
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
            self.sessions[socket] = session
            socket.textMessageReceived.connect(lambda text, s=socket: self._received(s, text))
            socket.binaryMessageReceived.connect(lambda _data, s=socket: s.close(
                QWebSocketProtocol.CloseCode(4002), "Only JSON text messages are understood."))
            socket.disconnected.connect(lambda s=socket: self._gone(s))
            socket.sendTextMessage(session.hello())
            if _CONNECT_LOG.allow("connect"):  # Streamer.bot and friends reconnect often: 129 lines in a day
                log.info("control: %s app connected from %s%s", "a SprigCast API" if native else "an OBS-style",
                         socket.peerAddress().toString(), _CONNECT_LOG.skipped("connect"))
            if first:  # start comparing from now
                (self.native if native else self.api).changes()
            self.watch.start()
            self.on_clients_changed(len(self.sessions))

    def _gone(self, socket: QWebSocket) -> None:
        if self.sessions.pop(socket, None) is not None:
            if _CONNECT_LOG.allow("gone"):
                log.info("control: an app disconnected%s", _CONNECT_LOG.skipped("gone"))
        try:
            socket.deleteLater()
            if not self.sessions:
                self.watch.stop()
        except RuntimeError:  # Qt already freed it (the app is closing): nothing left to tidy
            return
        self.on_clients_changed(len(self.sessions))

    def _received(self, socket: QWebSocket, text: str) -> None:
        session = self.sessions.get(socket)
        if session is not None:
            self._pump(socket, session.receive(text))
            self.soon()  # a request may have changed something: tell everyone (and the asker) once it settles

    def _pump(self, socket: QWebSocket, steps: Iterator[Step]) -> None:
        for step in steps:
            if socket not in self.sessions:
                return
            if isinstance(step, Sleep):
                QTimer.singleShot(int(step.seconds * 1000), lambda: self._pump(socket, steps))
                return
            if isinstance(step, Close):
                log.info("control: closing a connection: %s", step.reason)
                socket.close(QWebSocketProtocol.CloseCode(step.code), step.reason)
                return
            socket.sendTextMessage(step)
            self.soon()  # a step list changes things between waits

    # Events ----------------------------------------------------------------------------

    def soon(self) -> None:
        """Something may have changed: compare once the current work is done (many edits, one look)."""
        if self.sessions and not self._notify_queued:
            self._notify_queued = True
            QTimer.singleShot(0, self.notify)

    def notify(self) -> None:
        self._notify_queued = False
        try:
            if self._obs():
                for event_type, intent, data in self.api.changes():
                    self.broadcast(event_type, intent, data)
            if self._natives():
                for name, data in self.native.changes():
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


def start_for(window: MainWindow) -> ControlServer:
    """Make the window's control server and start it if Settings → Connections has it on."""
    server = ControlServer(ControlApi(WindowHost(window)), window)
    apply_settings(server, window)
    return server


def apply_settings(server: ControlServer, window: MainWindow | None = None) -> None:
    """Start, restart or stop the server to match the saved settings."""
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
