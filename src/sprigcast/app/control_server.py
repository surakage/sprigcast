"""The remote control server: a WebSocket on the UI thread that speaks obs-websocket 5.

Streamer.bot, Stream Deck, Speaker.bot, SAMMI, Touch Portal and anything else written for OBS's
WebSocket connect to it as if SprigCast were OBS (Settings → Connections has the port and password).

Light by design: Qt's own WebSocket server (no extra threads or packages), nothing runs while no app
is connected, and events come from comparing a small snapshot after each change rather than hooks
all over the app. Volume meters are only sent to apps that ask for them.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QObject, QSettings, QTimer
from PySide6.QtNetwork import QHostAddress
from PySide6.QtWebSockets import QWebSocket, QWebSocketProtocol, QWebSocketServer

from sprigcast.control.api import ControlApi
from sprigcast.control.protocol import Close, Session, Sleep, Step, Sub, new_password

if TYPE_CHECKING:
    from sprigcast.app.main_window import MainWindow

log = logging.getLogger(__name__)

ENABLED = "control/enabled"
PORT = "control/port"
PASSWORD = "control/password"
USE_PASSWORD = "control/use_password"
LAN = "control/lan"  # let phones and other PCs connect, not just apps on this PC
DEFAULT_PORT = 4455  # OBS's port, so apps find SprigCast without changes (use another if OBS runs too)
WATCH_MS = 500  # how often stream/record/Studio states are compared while an app is connected


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

    def studio(self) -> bool:
        return bool(self.w.studio)

    def set_studio(self, on: bool) -> None:
        self.w.live_panel.studio.setChecked(on)

    def staged(self) -> dict[str, str]:
        return dict(self.w.staged)

    def line_up(self, scene_id: str) -> None:
        w = self.w
        staged = dict(w.staged)
        for scene in w.collection.linked_with(scene_id):
            if scene.id == w.collection.active_scenes.get(scene.canvas):
                staged.pop(scene.canvas, None)
            else:
                staged[scene.canvas] = scene.id
        w._stage(staged)

    def bloom_it(self) -> None:
        self.w._studio_go()

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
            "reconnecting": any(not getattr(st, "connected", True) for st in streamers),
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

    def hotkeys(self) -> list[tuple[str, str]]:
        from sprigcast.app.hotkeys import actions

        return actions(self.w.collection)

    def trigger_hotkey(self, action: str) -> None:
        self.w._on_hotkey(action)


class ControlServer(QObject):
    """Owns the listening socket and one Session per connected app."""

    def __init__(self, api: ControlApi, parent: QObject | None = None):
        super().__init__(parent)
        self.api = api
        api.custom_event = lambda data: self.broadcast("CustomEvent", Sub.GENERAL, data)
        self.server: QWebSocketServer | None = None
        self.sessions: dict[QWebSocket, Session] = {}
        self.password = ""
        self.frame_seconds = 1 / 60
        self.error = ""
        self.watch = QTimer(self)
        self.watch.setInterval(WATCH_MS)
        self.watch.timeout.connect(self.notify)
        self._notify_queued = False
        self._running: tuple | None = None  # (port, password, lan) it listens with
        self.on_clients_changed = lambda count: None

    # Listening -------------------------------------------------------------------------

    def start(self, port: int, password: str = "", lan: bool = False) -> bool:
        self.stop()
        self.password = password
        self.server = QWebSocketServer("SprigCast", QWebSocketServer.SslMode.NonSecureMode, self)
        address = QHostAddress(QHostAddress.SpecialAddress.Any if lan else QHostAddress.SpecialAddress.LocalHost)
        if not self.server.listen(address, port):
            self.error = self.server.errorString() or f"port {port} is in use"
            log.warning("control: could not listen on port %d: %s", port, self.error)
            self.server = None
            return False
        self.error = ""
        self.server.newConnection.connect(self._accept)
        log.info("control: listening on %s:%d%s", "all addresses" if lan else "localhost", port,
                 " with a password" if password else " without a password")
        return True

    def stop(self) -> None:
        if self.server is None:
            return
        self.broadcast("ExitStarted", Sub.GENERAL, None)
        for socket in list(self.sessions):
            socket.close(QWebSocketProtocol.CloseCode.CloseCodeGoingAway, "SprigCast stopped the server")
        self.sessions.clear()
        self.server.close()
        self.server.deleteLater()
        self.server = None
        self.watch.stop()
        self.on_clients_changed(0)

    @property
    def listening(self) -> bool:
        return self.server is not None and self.server.isListening()

    @property
    def port(self) -> int:
        return self.server.serverPort() if self.server is not None else 0

    # Connections -----------------------------------------------------------------------

    def _accept(self) -> None:
        while self.server is not None and self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            session = Session(handler=self.api.handle, password=self.password, frame_seconds=self.frame_seconds)
            self.sessions[socket] = session
            socket.textMessageReceived.connect(lambda text, s=socket: self._received(s, text))
            socket.binaryMessageReceived.connect(lambda _data, s=socket: s.close(
                QWebSocketProtocol.CloseCode(4002), "Only JSON text messages are understood."))
            socket.disconnected.connect(lambda s=socket: self._gone(s))
            socket.sendTextMessage(session.hello())
            log.info("control: an app connected from %s", socket.peerAddress().toString())
            if len(self.sessions) == 1:
                self.api.changes()  # start comparing from now
            self.watch.start()
            self.on_clients_changed(len(self.sessions))

    def _gone(self, socket: QWebSocket) -> None:
        if self.sessions.pop(socket, None) is not None:
            log.info("control: an app disconnected")
        socket.deleteLater()
        if not self.sessions:
            self.watch.stop()
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

    # Events ----------------------------------------------------------------------------

    def soon(self) -> None:
        """Something may have changed: compare once the current work is done (many edits, one look)."""
        if self.sessions and not self._notify_queued:
            self._notify_queued = True
            QTimer.singleShot(0, self.notify)

    def notify(self) -> None:
        self._notify_queued = False
        if not self.sessions:
            return
        try:
            events = self.api.changes()
        except Exception:
            log.exception("control: working out events")
            return
        for event_type, intent, data in events:
            self.broadcast(event_type, intent, data)

    def broadcast(self, event_type: str, intent: int, data: dict[str, Any] | None) -> None:
        for socket, session in list(self.sessions.items()):
            message = session.event(event_type, intent, data)
            if message is not None:
                socket.sendTextMessage(message)

    def wants_meters(self) -> bool:
        return any(s.wants(Sub.INPUT_VOLUME_METERS) for s in self.sessions.values())

    def meters(self, levels: dict[str, tuple[float, float]]) -> None:
        if self.wants_meters():
            self.broadcast("InputVolumeMeters", Sub.INPUT_VOLUME_METERS, self.api.meters(levels))

    def replay_saved(self, paths: list) -> None:
        if paths:
            self.broadcast("ReplayBufferSaved", Sub.OUTPUTS, {"savedReplayPath": str(paths[0])})


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
    wanted = (settings["port"], password, settings["lan"])
    if not settings["enabled"]:
        server.stop()
        server._running = None
        return
    if server.listening and getattr(server, "_running", None) == wanted:
        return
    server._running = wanted if server.start(*wanted) else None
