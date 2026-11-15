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

import logging
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QObject, QSettings, QTimer
from PySide6.QtNetwork import QHostAddress, QTcpServer, QTcpSocket
from PySide6.QtWebSockets import QWebSocket, QWebSocketProtocol, QWebSocketServer

from sprigcast.control import web
from sprigcast.control.api import VENDOR, ControlApi

OBS_QUIET = ("tiktok.chat", "tiktok.like")  # too many for the OBS door's vendor events (the SprigCast API has them)
from sprigcast.control.native import NativeApi, NativeSession
from sprigcast.control.protocol import Close, Session, Sleep, Step, Sub, new_password

if TYPE_CHECKING:
    from sprigcast.app.main_window import MainWindow

log = logging.getLogger(__name__)

ENABLED = "control/enabled"
PORT = "control/port"
PASSWORD = "control/password"
USE_PASSWORD = "control/use_password"
WEB_DOOR = "control/web_door"  # plain web links /api/<action> (needs the password)
OBS_DOOR = "control/obs_door"  # also accept apps made for OBS (the frozen compatibility door)
LAN = "control/lan"  # let phones and other PCs connect, not just apps on this PC
DEFAULT_PORT = 4455  # OBS's port, so apps find SprigCast without changes (use another if OBS runs too)
NATIVE_PATH = "/sprigcast"  # ws://127.0.0.1:4455/sprigcast is the SprigCast API; any other path, the OBS door
WEB_TIMEOUT_MS = 5000  # a connection that hasn't said what it is by then is dropped
WATCH_MS = 500  # how often stream and record states are compared while an app is connected


def saved_settings() -> dict[str, Any]:
    q = QSettings()
    password = str(q.value(PASSWORD, "") or "")
    if not password:
        password = new_password()
        q.setValue(PASSWORD, password)
    return {
        "enabled": q.value(ENABLED, False, type=bool),
        "port": q.value(PORT, DEFAULT_PORT, type=int),
        "password": password,
        "use_password": q.value(USE_PASSWORD, True, type=bool),
        "lan": q.value(LAN, False, type=bool),
        "obs_door": q.value(OBS_DOOR, True, type=bool),
        "web_door": q.value(WEB_DOOR, False, type=bool),
    }


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

    def record_status(self) -> dict[str, Any] | None:
        session = self.w.recording
        if session is None:
            return None
        paths = getattr(session, "paths", [])
        return {"seconds": session.elapsed, "path": str(paths[0]) if paths else ""}

    def clips_ready(self) -> bool:
        return self.w.replay is not None

    # Starting things can ask a question (stream doctor) or show an error: do it just after replying,
    # so the app gets its answer at once and the question never blocks the socket.

    def start_stream(self) -> str | None:
        if not any(o.type == "stream" and o.enabled for o in self.w.collection.outputs):
            return "No place to stream to is switched on (Live → Destinations)."
        QTimer.singleShot(0, lambda: self.w.streaming is None and self.w._start_streaming())
        return None

    def stop_stream(self) -> None:
        QTimer.singleShot(0, self.w._stop_streaming)

    def start_record(self) -> str | None:
        col = self.w.collection
        if not any(c.enabled and c.id in col.recording.canvases for c in col.canvases):
            return "No canvas is ticked for recording (Settings → Output)."
        QTimer.singleShot(0, lambda: self.w.recording is None and self.w._start_recording())
        return None

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

    def source_instance(self, source_id: str):
        """The running source (from the engine), for plugins; None until it's loaded."""
        return self.w.engine._sources.get(source_id)

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

            out["memoryUsage"] = psutil.Process().memory_info().rss / (1024 * 1024)
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

    def hotkeys(self) -> list[tuple[str, str]]:
        from sprigcast.app.hotkeys import actions

        return actions(self.w.collection)

    def trigger_hotkey(self, action: str) -> str | None:
        """Do what the hotkey does. Returns its note when it couldn't ("Not in this bloom: Facecam")."""
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
        self.on_clients_changed = lambda count: None
        self.on_tiktok_wanted = lambda wanted: None  # an app asked for "tiktok" events by name (or stopped)
        self._tiktok_wanted = False

    def _obs(self) -> list[tuple[QWebSocket, Session]]:
        return [(k, s) for k, s in self.sessions.items() if isinstance(s, Session)]

    def _natives(self) -> list[tuple[QWebSocket, NativeSession]]:
        return [(k, s) for k, s in self.sessions.items() if isinstance(s, NativeSession)]

    # Listening -------------------------------------------------------------------------

    def start(self, port: int, password: str = "", lan: bool = False) -> bool:
        self.stop()
        self.password = password
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
        return True

    def stop(self) -> None:
        if self.server is None:
            return
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
        status, body = web.handle(self.native, request, enabled=self.web_door, password=self.password)
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
                session: Session | NativeSession = NativeSession(api=self.native, password=self.password)
                first = not self._natives()
            else:
                session = Session(handler=self.api.handle, password=self.password, frame_seconds=self.frame_seconds)
                first = not self._obs()
            self.sessions[socket] = session
            socket.textMessageReceived.connect(lambda text, s=socket: self._received(s, text))
            socket.binaryMessageReceived.connect(lambda _data, s=socket: s.close(
                QWebSocketProtocol.CloseCode(4002), "Only JSON text messages are understood."))
            socket.disconnected.connect(lambda s=socket: self._gone(s))
            socket.sendTextMessage(session.hello())
            log.info("control: %s app connected from %s", "a SprigCast API" if native else "an OBS-style",
                     socket.peerAddress().toString())
            if first:  # start comparing from now
                (self.native if native else self.api).changes()
            self.watch.start()
            self.on_clients_changed(len(self.sessions))

    def _gone(self, socket: QWebSocket) -> None:
        if self.sessions.pop(socket, None) is not None:
            log.info("control: an app disconnected")
        try:
            socket.deleteLater()
            if not self.sessions:
                self.watch.stop()
        except RuntimeError:  # Qt already freed it (the app is closing): nothing left to tidy
            return
        self.on_clients_changed(len(self.sessions))
        self._check_tiktok()

    def _check_tiktok(self) -> None:
        wanted = any(s.wants("tiktok") for _, s in self._natives())
        if wanted != self._tiktok_wanted:
            self._tiktok_wanted = wanted
            self.on_tiktok_wanted(wanted)

    def _received(self, socket: QWebSocket, text: str) -> None:
        session = self.sessions.get(socket)
        if session is not None:
            self._pump(socket, session.receive(text))
            if isinstance(session, NativeSession):
                self._check_tiktok()
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

    def app_event(self, name: str, data: dict[str, Any]) -> None:
        """An event from another part of SprigCast (e.g. "tiktok.gift" from the Chat panel): to SprigCast
        API apps by its group, and to OBS-door apps as a "sprigcast" VendorEvent (eventType = name)."""
        self.native_broadcast(name, data)
        if self._obs() and name not in OBS_QUIET:
            self.broadcast("VendorEvent", Sub.VENDORS, {"vendorName": VENDOR, "eventType": name, "eventData": data})

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
        return
    server._running = wanted if server.start(*wanted) else None
