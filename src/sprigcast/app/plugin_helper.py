"""Your own plugins' sources in a helper process, so a broken or slow one can never freeze or crash SprigCast.

A plugin you wrote (a .py file in your plugins folder that didn't come from the plugin store) is still imported by
SprigCast itself: its hotkeys, menus, labels and Settings fields work as before. But each source it adds runs in a
helper process (this file's serve()): there it draws its pictures with its own Python lock, and writes them into
shared memory, where the native engine takes them onto the card by itself (as it does a browser page's) and
Python's compositor reads them when it draws. A source that raises, hangs or crashes the helper takes only the
helper down: it starts again RESTART_AFTER_S later with the same sources.

Store plugins stay in SprigCast: they are drawn by the engine already, or redraw about once a second.
SPRIGCAST_PLUGINS_IN_PROCESS=1 runs every plugin source in SprigCast, as before.

Messages: SprigCast -> helper are JSON lines on its stdin ({"op": "open", "id", "kind", "file", "settings"},
update, close, sleep, wake, detail, hidden, quit). Helper -> SprigCast are length-prefixed JSON on its stdout
(app/browser_helper.py's framing): "frames" (a source's shared memory block), "size", "status", "sound" (float32
stereo, 48 kHz) and "log".
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

from sprigcast.app.browser_helper import (
    FrameSlot,
    _attach,
    _read_message,
    _write_message,
)
from sprigcast.core.source import Source

log = logging.getLogger(__name__)

ENV_OFF = "SPRIGCAST_PLUGINS_IN_PROCESS"
RESTART_AFTER_S = 2.0
LINGER_S = 30.0  # the helper ends this long after its last source closed
FPS = 60  # how often the helper asks each source for a picture (it sends one only when there's a new one)
SOUND_MS = 20
SOUND_BLOCK = 960  # 20 ms at 48 kHz

_kinds: dict[str, tuple[type, str]] = {}  # kind -> (the plugin's class, its file), for kinds run in the helper
_host: PluginHost | None = None
_host_lock = threading.Lock()


def enabled() -> bool:
    return os.environ.get(ENV_OFF, "").strip() not in ("1", "true", "yes")


def adopt(path: Path, module_name: str) -> list[str]:
    """After a plugin file loaded: its source kinds run in the helper from now on, unless it is a store plugin (or
    the helper is off). Returns those kinds."""
    from sprigcast.core import source as source_mod

    for kind in [k for k, (_cls, file) in _kinds.items() if file == str(path)]:
        _kinds.pop(kind, None)
        source_mod.run_elsewhere(kind, None)
    if not enabled():
        return []
    try:
        from sprigcast.plugin_store import is_store_file

        if is_store_file(path):
            return []
    except Exception:
        log.debug("plugin %s: store check failed", path.name, exc_info=True)
    adopted = []
    for kind, cls in list(source_mod._REGISTRY.items()):
        if getattr(cls, "__module__", "") == module_name and isinstance(cls, type) and issubclass(cls, Source):
            _kinds[kind] = (cls, str(path))
            source_mod.run_elsewhere(kind, _factory(kind))
            adopted.append(kind)
    if adopted:
        log.info("plugin %s: %s run in the plugin helper", path.name, ", ".join(sorted(adopted)))
    return adopted


def forget(module_name: str) -> None:
    """A plugin removed: its kinds aren't run in the helper any more."""
    from sprigcast.core import source as source_mod

    for kind in [k for k, (cls, _f) in _kinds.items() if getattr(cls, "__module__", "") == module_name]:
        _kinds.pop(kind, None)
        source_mod.run_elsewhere(kind, None)


def host() -> PluginHost:
    global _host
    with _host_lock:
        if _host is None:
            _host = PluginHost()
        return _host


def shutdown() -> None:
    global _host
    with _host_lock:
        h, _host = _host, None
    if h is not None:
        h.shutdown()


def _factory(kind: str):
    def make(source_id: str, settings: dict[str, Any]) -> Source:
        cls, file = _kinds[kind]
        proxy_cls = _SoundProxy if hasattr(cls, "read_block") else HelperSource
        return proxy_cls(kind, cls, file, source_id, settings)

    return make


def _to_rgba(frame: np.ndarray, order: str) -> np.ndarray:
    """A plugin's picture in straight R, G, B, A, as the engine reads a shared block."""
    frame = np.ascontiguousarray(frame)
    if frame.ndim != 3 or frame.shape[2] not in (3, 4):
        raise ValueError(f"a picture is H x W x 4, not {frame.shape}")
    if frame.shape[2] == 3:
        frame = np.concatenate([frame, np.full(frame.shape[:2] + (1,), 255, np.uint8)], axis=-1)
    order = order or "RGBA"
    premultiplied = order.startswith("p")
    order = order.lstrip("p").upper()
    if order.startswith("BGR"):
        frame = frame[..., [2, 1, 0, 3]]
    if order.endswith("X"):
        frame = frame.copy()
        frame[..., 3] = 255
    elif premultiplied:
        alpha = frame[..., 3:4].astype(np.uint16)
        rgb = frame[..., :3].astype(np.uint16) * 255 // np.maximum(alpha, 1)
        frame = np.concatenate([np.minimum(rgb, 255).astype(np.uint8), frame[..., 3:4]], axis=-1)
    return np.ascontiguousarray(frame, dtype=np.uint8)


class HelperSource(Source):
    """A plugin's source, run in the helper: same kind, label, defaults and Settings as the plugin's own class."""

    channel_order = "RGBA"
    stop_may_wait = False

    def __init__(self, kind: str, cls: type, file: str, source_id: str, settings: dict[str, Any]):
        super().__init__(source_id, settings)
        self.kind, self._cls, self._file = kind, cls, file
        self.label = getattr(cls, "label", kind)
        self.defaults = getattr(cls, "defaults", {})
        self.sleeps = bool(getattr(cls, "sleeps", False))
        self.sharp = bool(getattr(cls, "sharp", False))
        self.keeps_picture = bool(getattr(cls, "keeps_picture", False))
        self.first_picture = False
        self._open = False
        try:
            width, height = int(settings.get("width", 0) or 0), int(settings.get("height", 0) or 0)
        except (TypeError, ValueError):
            width = height = 0
        self._size = (width, height) if width > 0 and height > 0 else (2, 2)
        self.status = "starting"

    @property
    def size(self) -> tuple[int, int]:
        return self._size

    def start(self) -> None:
        self._open = True
        host().open(self)

    def stop(self) -> None:
        if self._open:
            self._open = False
            host().close(self.id)

    def update(self, settings: dict[str, Any]) -> None:
        super().update(settings)
        if self._open:
            host().send({"op": "update", "id": self.id, "settings": dict(self.settings)})

    def sleep(self) -> None:
        if self._open:
            host().send({"op": "sleep", "id": self.id})

    def wake(self) -> None:
        if self._open:
            host().send({"op": "wake", "id": self.id})
        else:
            self.start()

    def set_detail(self, detail: float) -> None:
        super().set_detail(detail)
        if self._open:
            host().send({"op": "detail", "id": self.id, "detail": float(detail)})

    def set_hidden(self, hidden: bool) -> None:
        if self._open:
            host().send({"op": "hidden", "id": self.id, "hidden": bool(hidden)})

    # from the helper (any thread)
    def heard(self, header: dict) -> None:
        kind = header.get("kind")
        if kind == "size":
            w, h = int(header.get("w", 0)), int(header.get("h", 0))
            if w > 0 and h > 0:
                self._size = (w, h)
        elif kind == "status":
            self.status = str(header.get("status", ""))

    @property
    def engine_player(self) -> str | None:
        """The shared memory block its pictures are in: the native engine takes them from there itself."""
        h = _host
        return h.shared_name(self.id) if h is not None and self._open else None

    def engine_tick(self) -> None:
        h = _host
        if h is not None and h.painted(self.id):
            self.first_picture = True

    def video_frame(self) -> np.ndarray | None:
        h = _host
        frame = h.take(self.id) if h is not None and self._open else None
        if frame is not None:
            self.first_picture = True
        return frame


class _SoundProxy(HelperSource):
    """A plugin source that makes sound: heard through the mixer like any other (read_block and gain)."""

    @property
    def gain(self) -> float:
        g = getattr(self._cls, "gain", None)
        if isinstance(g, property):
            try:
                return float(g.fget(self))  # its own rule, read from these settings
            except Exception:  # noqa: BLE001
                return 1.0
        return 1.0

    def read_block(self, n: int) -> np.ndarray:
        h = _host
        block = h.read_audio(self.id, n) if h is not None and self._open else None
        return block if block is not None else np.zeros((n, 2), dtype=np.float32)


def helper_command() -> list[str]:
    code = ("import sys; sys.path[:0] = [p for p in sys.argv[1:] if p]; "
            "from sprigcast.app.plugin_helper import serve; serve()")
    return [sys.executable, "-I", "-c", code, *[p for p in sys.path if p and Path(p).is_dir()]]


class PluginHost:
    """The helper process seen from SprigCast: starts it with the first source, starts it again if it stops."""

    def __init__(self, command: list[str] | None = None):
        self.command = command or helper_command()
        self._sources: dict[str, HelperSource] = {}
        self._frames: dict[str, FrameSlot] = {}
        self._sounds: dict[str, Any] = {}
        self._lock = threading.RLock()
        self._send_lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self._shut = False
        self._linger: threading.Timer | None = None
        self.starts = 0

    def _ensure(self) -> bool:
        with self._lock:
            if self._shut:
                return False
            if self.proc is not None and self.proc.poll() is None:
                return True
            flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            try:
                self.proc = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                             stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True)
            except OSError as exc:
                log.warning("plugin helper didn't start: %s", exc)
                self.proc = None
                return False
            self.starts += 1
            log.info("plugin helper started (pid %s)", self.proc.pid)
            threading.Thread(target=self._read, args=(self.proc,), name="sprigcast-plugin-pipe", daemon=True).start()
            return True

    def send(self, message: dict) -> None:
        proc = self.proc
        if proc is None or proc.stdin is None:
            return
        try:
            with self._send_lock:
                proc.stdin.write((json.dumps(message) + "\n").encode())
                proc.stdin.flush()
        except (OSError, ValueError):
            pass  # it stopped: the pipe reader starts it again

    def _open_message(self, source: HelperSource) -> dict:
        return {"op": "open", "id": source.id, "kind": source.kind, "file": source._file,
                "settings": dict(source.settings), "detail": float(source.detail)}

    def open(self, source: HelperSource) -> None:
        with self._lock:
            self._sources[source.id] = source
            if self._linger is not None:
                self._linger.cancel()
                self._linger = None
            if hasattr(source, "read_block"):
                from sprigcast.sources.audio import AudioRing

                self._sounds[source.id] = AudioRing()
            if not self._ensure():
                source.status = "plugin helper didn't start"
                return
        self.send(self._open_message(source))

    def close(self, source_id: str) -> None:
        with self._lock:
            self._sources.pop(source_id, None)
            self._sounds.pop(source_id, None)
            slot = self._frames.pop(source_id, None)
            empty = not self._sources
        if slot is not None:
            slot.close()
        self.send({"op": "close", "id": source_id})
        if empty:
            with self._lock:
                if self._linger is None and not self._shut:
                    self._linger = threading.Timer(LINGER_S, self._end_if_idle)
                    self._linger.daemon = True
                    self._linger.start()

    def _end_if_idle(self) -> None:
        with self._lock:
            self._linger = None
            if self._sources:
                return
            proc, self.proc = self.proc, None
        if proc is not None:
            log.info("plugin helper ended (no plugin sources for %.0f s)", LINGER_S)
            self._quit(proc)

    def _read(self, proc: subprocess.Popen) -> None:
        try:
            while True:
                header, payload = _read_message(proc.stdout)
                self._on_message(header, payload)
        except (EOFError, OSError, ValueError):
            pass
        proc.wait()
        for pipe in (proc.stdout, proc.stdin):
            try:
                pipe.close()
            except (OSError, ValueError, AttributeError):
                pass
        with self._lock:
            if self.proc is not proc:
                return
            self.proc = None
            for slot in self._frames.values():
                slot.close()
            self._frames.clear()
            again = bool(self._sources) and not self._shut
            for source in self._sources.values():
                source.status = "plugin helper stopped, starting it again"
        if again:
            log.warning("plugin helper stopped (code %s): starting it again", proc.returncode)
            time.sleep(RESTART_AFTER_S)
            with self._lock:
                sources = list(self._sources.values())
                if not sources or self._shut or not self._ensure():
                    return
            for source in sources:
                self.send(self._open_message(source))

    def _on_message(self, header: dict, payload: bytes) -> None:
        kind, source_id = header.get("kind"), header.get("id", "")
        if kind == "frames":
            try:
                slot = FrameSlot(_attach(header["name"]), owner=False)
            except (OSError, ValueError, KeyError) as exc:
                log.warning("plugin source %s: can't read its pictures (%s)", source_id, exc)
                return
            with self._lock:
                old = self._frames.pop(source_id, None)
                if source_id in self._sources:
                    self._frames[source_id] = slot
                else:
                    slot.close()
            if old is not None:
                old.close()
        elif kind == "sound":
            ring = self._sounds.get(source_id)
            if ring is not None and payload:
                ring.write(np.frombuffer(payload, dtype=np.float32).reshape(-1, 2).copy())
        elif kind in ("size", "status"):
            source = self._sources.get(source_id)
            if source is not None:
                source.heard(header)
        elif kind == "log":
            log.log(int(header.get("level", logging.INFO)), "plugin helper: %s", header.get("text", ""))

    def shared_name(self, source_id: str) -> str | None:
        slot = self._frames.get(source_id)
        return slot.shm.name if slot is not None and slot.buf is not None else None

    def painted(self, source_id: str) -> bool:
        slot = self._frames.get(source_id)
        try:
            return slot is not None and slot.painted()
        except (TypeError, ValueError):
            return False

    def take(self, source_id: str) -> np.ndarray | None:
        slot = self._frames.get(source_id)
        try:
            return slot.read() if slot is not None else None
        except (TypeError, ValueError):
            return None

    def read_audio(self, source_id: str, n: int) -> np.ndarray | None:
        ring = self._sounds.get(source_id)
        if ring is None or len(ring) < n:
            return None
        return ring.read(n)

    def _quit(self, proc: subprocess.Popen) -> None:
        try:
            proc.stdin.write(b'{"op": "quit"}\n')
            proc.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)

    def shutdown(self) -> None:
        with self._lock:
            self._shut = True
            proc, self.proc = self.proc, None
            for slot in self._frames.values():
                slot.close()
            self._frames.clear()
            self._sources.clear()
            if self._linger is not None:
                self._linger.cancel()
        if proc is not None:
            self._quit(proc)


# The helper process -----------------------------------------------------------------------------


def serve() -> None:
    """The helper: plugin sources drawn here, each picture into its shared memory block."""
    from PySide6.QtCore import QObject, Qt, QTimer, Signal
    from PySide6.QtGui import QGuiApplication

    from sprigcast.app.browser_helper import _PipeLog

    out = sys.stdout.buffer
    sys.stdout = sys.stderr  # nothing else may write into the message pipe
    out_lock = threading.Lock()

    def emit(header: dict, payload: bytes = b"") -> None:
        try:
            _write_message(out, out_lock, header, payload)
        except (OSError, ValueError):
            pass

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(_PipeLog(emit))
    app = QGuiApplication([sys.argv[0] if sys.argv else "sprigcast-plugins"])
    app.setOrganizationName("Slothbloom")
    app.setApplicationName("SprigCast")
    try:
        from sprigcast.core.painted import brand_fonts

        brand_fonts()
    except Exception:
        log.debug("brand fonts not loaded", exc_info=True)
    import sprigcast.sources  # noqa: F401  (built-in kinds a plugin builds on)
    from sprigcast import plugins
    from sprigcast.core.source import _REGISTRY

    plugins._IN_HELPER = True  # here the plugins' sources are the real ones
    load_plugin_file = plugins.load_plugin_file

    loaded: dict[str, str | None] = {}  # file -> error
    sources: dict[str, Source] = {}
    slots: dict[str, FrameSlot] = {}
    sizes: dict[str, tuple[int, int]] = {}
    statuses: dict[str, str] = {}
    asleep: set[str] = set()
    made = [0]

    def say_status(source_id: str, text: str) -> None:
        if statuses.get(source_id) != text:
            statuses[source_id] = text
            emit({"kind": "status", "id": source_id, "status": text})

    def store(source_id: str, frame: np.ndarray) -> None:
        h, w = frame.shape[:2]
        slot = slots.get(source_id)
        if slot is None or w * h * 4 > slot.capacity:
            if slot is not None:
                slot.close()
            made[0] += 1
            name = f"sprigcast-plugin-{os.getpid()}-{made[0]}"
            slot = slots[source_id] = FrameSlot.create(name, w * h)
            emit({"kind": "frames", "id": source_id, "name": name})
        slot.write(frame)

    def close(source_id: str) -> None:
        source = sources.pop(source_id, None)
        asleep.discard(source_id)
        sizes.pop(source_id, None)
        statuses.pop(source_id, None)
        if source is not None:
            try:
                source.stop()
            except Exception:
                log.warning("plugin source %s: stop failed", source_id, exc_info=True)
        slot = slots.pop(source_id, None)
        if slot is not None:
            slot.close()

    def call(source_id: str, what: str, fn, *args) -> None:
        try:
            fn(*args)
        except Exception as exc:  # noqa: BLE001 - a plugin's mistake stays its own
            log.warning("plugin source %s: %s failed: %s", source_id, what, exc)
            say_status(source_id, f"error: {exc}")

    def handle(message: dict) -> None:
        op, source_id = message.get("op"), message.get("id", "")
        if op == "quit":
            app.quit()
            return
        if op == "open":
            close(source_id)
            file = message.get("file", "")
            if file not in loaded:
                loaded[file] = load_plugin_file(Path(file))
            cls = _REGISTRY.get(message.get("kind", ""))
            if loaded[file] is not None or cls is None:
                say_status(source_id, f"error: {loaded[file] or 'kind not found'}")
                return
            try:
                source = cls(source_id, message.get("settings") or {})
                source.set_detail(float(message.get("detail", 1.0)))
                source.start()
            except Exception as exc:  # noqa: BLE001
                log.warning("plugin source %s: start failed: %s", source_id, exc)
                say_status(source_id, f"error: {exc}")
                return
            sources[source_id] = source
            return
        source = sources.get(source_id)
        if source is None:
            return
        if op == "close":
            close(source_id)
        elif op == "update":
            call(source_id, "update", source.update, message.get("settings") or {})
        elif op == "sleep":
            asleep.add(source_id)
            call(source_id, "sleep", source.sleep)
        elif op == "wake":
            asleep.discard(source_id)
            call(source_id, "wake", source.wake)
        elif op == "detail":
            call(source_id, "detail", source.set_detail, float(message.get("detail", 1.0)))
        elif op == "hidden":
            call(source_id, "hidden", source.set_hidden, bool(message.get("hidden")))

    def tick() -> None:
        for source_id, source in list(sources.items()):
            if source_id in asleep:
                continue
            try:
                frame = source.video_frame()
                size = tuple(int(v) for v in source.size)
                if size != sizes.get(source_id) and size[0] > 0 and size[1] > 0:
                    sizes[source_id] = size
                    emit({"kind": "size", "id": source_id, "w": size[0], "h": size[1]})
                if frame is not None:
                    store(source_id, _to_rgba(frame, getattr(source, "channel_order", "RGBA")))
                say_status(source_id, str(getattr(source, "status", "ok")))  # drew without error: its own word
            except Exception as exc:  # noqa: BLE001
                log.warning("plugin source %s: drawing failed: %s", source_id, exc)
                say_status(source_id, f"error: {exc}")

    def send_sound() -> None:
        for source_id, source in list(sources.items()):
            if source_id in asleep or not hasattr(source, "read_block"):
                continue
            try:
                block = np.ascontiguousarray(source.read_block(SOUND_BLOCK), dtype=np.float32)
            except Exception as exc:  # noqa: BLE001
                log.debug("plugin source %s: sound failed: %s", source_id, exc)
                continue
            if block.size and np.any(block):
                emit({"kind": "sound", "id": source_id}, block.tobytes())

    class Inbox(QObject):
        got = Signal(object)

    inbox = Inbox()
    inbox.got.connect(handle, Qt.ConnectionType.QueuedConnection)
    frames = QTimer()
    frames.setTimerType(Qt.TimerType.PreciseTimer)
    frames.timeout.connect(tick)
    frames.start(max(1, int(1000 / FPS)))
    sound = QTimer()
    sound.timeout.connect(send_sound)
    sound.start(SOUND_MS)

    def listen() -> None:
        for line in sys.stdin.buffer:
            try:
                inbox.got.emit(json.loads(line))
            except ValueError:
                continue
        inbox.got.emit({"op": "quit"})  # SprigCast closed (or stopped): the sources go with it

    threading.Thread(target=listen, name="plugin-helper-inbox", daemon=True).start()
    try:
        app.exec()
    finally:
        frames.stop()
        sound.stop()
        for source_id in list(sources):
            close(source_id)
