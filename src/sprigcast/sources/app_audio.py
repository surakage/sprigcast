"""App sound: one kind of sound (games, music, voice chat, ...) captured from just those apps.

Windows 10 2004+ can capture the sound of one process tree on its own ("process loopback",
VAD\\Process_Loopback through ActivateAudioInterfaceAsync). SprigCast uses it so each type of
sound gets its own row, volume and mute: Music catches Spotify and friends, Voice chat catches
Discord, Games follows the full-screen app in front, and so on.

Settings:
    preset     "games" | "music" | "sfx" | "tts" | "voice" | "browser" | "custom"
    apps       exe names to catch (case doesn't matter); empty = the preset's usual apps
    auto_game  also catch the full-screen app in front (the Games preset turns this on)
    volume_db, muted  as for every sound

Everything here is plain data except `_ProcessCapture`, the WASAPI part, which only runs on Windows.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from dataclasses import dataclass
from typing import Any

import numpy as np

from sprigcast.core.source import Source, register
from sprigcast.sources.audio import CHANNELS, SAMPLE_RATE, AudioRing

log = logging.getLogger(__name__)

SCAN_SECONDS = 2.0  # how often to look for the apps starting or closing


@dataclass(frozen=True)
class SoundType:
    key: str
    name: str  # what the row is called
    hint: str  # the usual apps, short, for the picker
    apps: tuple[str, ...]  # exe names, lower case
    auto_game: bool = False


SOUND_TYPES: tuple[SoundType, ...] = (
    SoundType("games", "Games", "Full-screen game in front", (), auto_game=True),
    SoundType("music", "Music", "Spotify, Apple Music, VLC…",
              ("spotify.exe", "applemusic.exe", "itunes.exe", "foobar2000.exe", "musicbee.exe", "vlc.exe",
               "tidal.exe", "amazon music.exe", "deezer.exe", "aimp.exe", "winamp.exe", "pandora.exe",
               "youtube music desktop app.exe")),
    SoundType("sfx", "SFX & alerts", "Streamer.bot, Soundpad, TikFinity…",
              ("streamer.bot.exe", "soundpad.exe", "tikfinity.exe", "mixitup.exe", "firebot v5.exe",
               "sammi panel.exe", "lioranboard.exe", "voicemeeter macrobuttons.exe")),
    SoundType("tts", "Text to speech", "Speaker.bot, TTS Voice Wizard…",
              ("speaker.bot.exe", "ttsvoicewizard.exe", "tts voice wizard.exe", "balcon.exe")),
    SoundType("voice", "Voice chat", "Discord, TeamSpeak, Mumble…",
              ("discord.exe", "discordptb.exe", "discordcanary.exe", "ts3client_win64.exe", "teamspeak.exe",
               "mumble.exe", "guilded.exe", "zoom.exe", "slack.exe", "ms-teams.exe", "skype.exe", "vesktop.exe")),
    SoundType("browser", "Browser", "Chrome, Edge, Firefox…",
              ("chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe", "vivaldi.exe", "arc.exe",
               "librewolf.exe", "zen.exe")),
)
TYPES = {t.key: t for t in SOUND_TYPES}

# Never treated as "the game in front": the desktop, SprigCast and the other kinds' apps.
NOT_GAMES = {"explorer.exe", "sprigcast.exe", "python.exe", "pythonw.exe", "searchhost.exe",
             "shellexperiencehost.exe", "startmenuexperiencehost.exe", "applicationframehost.exe",
             "textinputhost.exe", "lockapp.exe", "obs64.exe", "tiktok live studio.exe", "steamwebhelper.exe"}
for _t in SOUND_TYPES:
    NOT_GAMES.update(_t.apps)


def wanted_apps(settings: dict[str, Any]) -> set[str]:
    """The exe names (lower case) this sound catches."""
    apps = [str(a).lower() for a in settings.get("apps") or [] if str(a).strip()]
    if apps:
        return set(apps)
    kind = TYPES.get(str(settings.get("preset", "")))
    return set(kind.apps) if kind else set()


def follows_game(settings: dict[str, Any]) -> bool:
    if "auto_game" in settings:
        return bool(settings["auto_game"])
    kind = TYPES.get(str(settings.get("preset", "")))
    return bool(kind and kind.auto_game and not settings.get("apps"))


def describe(settings: dict[str, Any]) -> str:
    """'Spotify, Apple Music, VLC…' or 'Full-screen game in front' or the picked apps."""
    apps = [str(a) for a in settings.get("apps") or []]
    if apps:
        names = [a[:-4] if a.lower().endswith(".exe") else a for a in apps]
        return ", ".join(names[:3]) + ("…" if len(names) > 3 else "")
    kind = TYPES.get(str(settings.get("preset", "")))
    return kind.hint if kind else "No apps picked"


def root_processes(processes: list[tuple[int, int, str]], names: set[str]) -> dict[int, str]:
    """pid -> name for each wanted app whose parent isn't the same app (one capture covers its tree).

    `processes` is (pid, parent pid, exe name) for everything running.
    """
    by_pid = {pid: name.lower() for pid, _parent, name in processes}
    roots = {}
    for pid, parent, name in processes:
        lower = name.lower()
        if lower in names and by_pid.get(parent) not in names:
            roots[pid] = name
    return roots


def running_processes() -> list[tuple[int, int, str]]:
    import psutil

    out = []
    for proc in psutil.process_iter(["pid", "ppid", "name"]):
        info = proc.info
        if info.get("name"):
            out.append((int(info["pid"]), int(info.get("ppid") or 0), str(info["name"])))
    return out


def running_apps() -> list[str]:
    """Exe names of apps with a window open now (Windows), for "pick an app"; sorted, no duplicates."""
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    pids: set[int] = set()

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd) > 0 and not user32.GetWindow(hwnd, 4):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            pids.add(pid.value)
        return True

    user32.EnumWindows(each, 0)
    names = {}
    for pid, _parent, name in running_processes():
        if pid in pids and pid != os.getpid() and name.lower() not in {"explorer.exe", "textinputhost.exe",
                                                                      "applicationframehost.exe"}:
            names[name.lower()] = name
    return sorted(names.values(), key=str.lower)


def fullscreen_app() -> tuple[int, str] | None:
    """(pid, exe) of the full-screen window in front, if it isn't the desktop or a known non-game."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                    ("dwFlags", wintypes.DWORD)]

    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    monitor = user32.MonitorFromWindow(hwnd, 2)
    if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        return None
    m = info.rcMonitor
    if (rect.left, rect.top, rect.right, rect.bottom) != (m.left, m.top, m.right, m.bottom):
        return None
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if pid.value == os.getpid():
        return None
    name = next((n for p, _pp, n in running_processes() if p == pid.value), "")
    if not name or name.lower() in NOT_GAMES:
        return None
    return pid.value, name


@register("app_audio")
class AppAudioSource(Source):
    """The sound of a few apps (by exe name), or of the full-screen game in front."""

    def __init__(self, source_id: str, settings: dict[str, Any]):
        super().__init__(source_id, settings)
        self._captures: dict[int, _ProcessCapture] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._rescan = threading.Event()
        self._thread: threading.Thread | None = None
        self._game: tuple[int, str] | None = None
        self.status = "starting"

    @property
    def size(self) -> tuple[int, int]:
        return (0, 0)

    @property
    def gain(self) -> float:
        if self.settings.get("muted"):
            return 0.0
        return float(10 ** (float(self.settings.get("volume_db", 0.0)) / 20))

    def read_block(self, n: int) -> np.ndarray:
        with self._lock:
            captures = list(self._captures.values())
        if not captures:
            return np.zeros((n, CHANNELS), dtype=np.float32)
        out = captures[0].ring.read(n)
        for capture in captures[1:]:
            out += capture.ring.read(n)
        return out

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"app-audio-{self.id}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._rescan.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
        self._close_all()

    def update(self, settings: dict[str, Any]) -> None:
        changed = any(settings.get(k) != self.settings.get(k) for k in ("apps", "preset", "auto_game"))
        super().update(settings)
        if changed:
            self._rescan.set()

    def targets(self, processes: list[tuple[int, int, str]]) -> dict[int, str]:
        """pid -> exe of what to capture now (the apps' root processes, plus the game in front)."""
        found = root_processes(processes, wanted_apps(self.settings))
        if follows_game(self.settings):
            alive = {pid for pid, _p, _n in processes}
            front = fullscreen_app()
            if front is not None:
                self._game = front
            if self._game is not None and self._game[0] in alive:  # keep the game while you alt-tab
                found[self._game[0]] = self._game[1]
            else:
                self._game = None
        return found

    def _run(self) -> None:
        if sys.platform != "win32" or sys.getwindowsversion().build < 19041:
            self.status = "unavailable: needs Windows 10 (2004) or newer"
            return
        _com_init()
        try:
            while not self._stop.is_set():
                self._rescan.clear()
                try:
                    self._sync(self.targets(running_processes()))
                except Exception as exc:
                    log.info("%s: app sound scan failed: %s", self.id, exc)
                self._rescan.wait(SCAN_SECONDS)
        finally:
            self._close_all()

    def _sync(self, wanted: dict[int, str]) -> None:
        with self._lock:
            current = dict(self._captures)
        for pid in set(current) - set(wanted):
            current[pid].close()
            with self._lock:
                self._captures.pop(pid, None)
        for pid, name in wanted.items():
            capture = current.get(pid)
            if capture is not None and capture.alive:
                continue
            if capture is not None:
                capture.close()
            try:
                capture = _ProcessCapture(pid, name, self.id)
            except Exception as exc:  # the app closed meanwhile, or Windows refused
                log.info("%s: can't catch %s (%d): %s", self.id, name, pid, exc)
                continue
            with self._lock:
                self._captures[pid] = capture
        with self._lock:
            names = sorted({c.name for c in self._captures.values()}, key=str.lower)
        if names:
            self.status = "capturing " + ", ".join(n[:-4] if n.lower().endswith(".exe") else n for n in names)
        elif follows_game(self.settings) and not wanted_apps(self.settings):
            self.status = "waiting for a full-screen game"
        else:
            self.status = "waiting for " + describe(self.settings)

    def _close_all(self) -> None:
        with self._lock:
            captures, self._captures = list(self._captures.values()), {}
        for capture in captures:
            capture.close()


# --- Windows: process loopback through WASAPI (ctypes COM) ---------------------------------------

def _com_init() -> None:
    import ctypes

    ctypes.windll.ole32.CoInitializeEx(None, 0x0)  # multithreaded; S_FALSE if already done is fine


def _guid(text: str):
    import ctypes
    import uuid

    class GUID(ctypes.Structure):
        _fields_ = [("data", ctypes.c_ubyte * 16)]

    g = GUID()
    ctypes.memmove(g.data, uuid.UUID(text).bytes_le, 16)
    return g


IID_IUNKNOWN = "00000000-0000-0000-C000-000000000046"
IID_IAGILE = "94ea2b94-e9cc-49e0-c0ff-ee64ca8f5b90"
IID_HANDLER = "41D949AB-9862-444A-80F6-C261334DA5EB"
IID_IAUDIOCLIENT = "1CB9AD4C-DBFA-4c32-B178-C2F568A703B2"
IID_CAPTURE = "C8ADBD64-E71E-48a0-A4DE-185C395CD317"
LOOPBACK_PATH = "VAD\\Process_Loopback"
FLAGS = 0x00020000 | 0x00040000 | 0x80000000 | 0x08000000  # loopback, event, auto-convert, good resampling
SILENT = 0x2


def _method(ptr, index: int, *argtypes):
    """A COM method from the object's vtable, called with the object as `this`. Raises on failure."""
    import ctypes

    vtable = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    proto = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)
    fn = proto(vtable[index])
    return lambda *args: fn(ptr, *args)


def _release(ptr) -> None:
    import ctypes

    if ptr:
        vtable = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
        ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])(ptr)


_LATE: list = []  # handlers Windows never called back in time (rare); freeing them could crash


def _activate(pid: int):
    """IAudioClient for one process tree's sound (blocks up to 5 s for Windows to hand it over)."""
    import ctypes
    from ctypes import wintypes

    class LoopbackParams(ctypes.Structure):
        _fields_ = [("activation_type", ctypes.c_int), ("pid", wintypes.DWORD), ("mode", ctypes.c_int)]

    class Blob(ctypes.Structure):
        _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.c_void_p)]

    class PropVariant(ctypes.Structure):
        _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort),
                    ("r3", ctypes.c_ushort), ("blob", Blob)]

    params = LoopbackParams(1, pid, 0)  # PROCESS_LOOPBACK, include the process tree
    prop = PropVariant(vt=65, blob=Blob(ctypes.sizeof(params), ctypes.cast(ctypes.pointer(params), ctypes.c_void_p)))
    done = threading.Event()
    result: dict[str, Any] = {}

    HRESULT = ctypes.HRESULT
    QI = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
    REF = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
    COMPLETED = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p)

    class VTable(ctypes.Structure):
        _fields_ = [("qi", QI), ("add", REF), ("release", REF), ("completed", COMPLETED)]

    class Handler(ctypes.Structure):
        _fields_ = [("vtable", ctypes.POINTER(VTable))]

    known = {bytes(_guid(i).data) for i in (IID_IUNKNOWN, IID_IAGILE, IID_HANDLER)}

    def qi(this, riid, out):
        if bytes(ctypes.string_at(riid, 16)) in known:
            out[0] = this
            return 0
        out[0] = None
        return -2147467262  # E_NOINTERFACE

    def completed(_this, operation):
        try:
            hr = ctypes.c_long()
            unknown = ctypes.c_void_p()
            _method(operation, 3, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(
                ctypes.byref(hr), ctypes.byref(unknown))
            if hr.value < 0:
                result["error"] = OSError(f"Windows refused the app's sound (0x{hr.value & 0xFFFFFFFF:08X})")
            else:
                result["client"] = unknown.value
        except Exception as exc:
            result["error"] = exc
        done.set()
        return 0

    vtable = VTable(QI(qi), REF(lambda _t: 1), REF(lambda _t: 1), COMPLETED(completed))
    handler = Handler(ctypes.pointer(vtable))
    operation = ctypes.c_void_p()
    activate = ctypes.windll.mmdevapi.ActivateAudioInterfaceAsync
    activate.restype = HRESULT
    activate.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.POINTER(ctypes.c_void_p)]
    iid = _guid(IID_IAUDIOCLIENT)
    activate(LOOPBACK_PATH, ctypes.byref(iid), ctypes.byref(prop), ctypes.byref(handler), ctypes.byref(operation))
    finished = done.wait(5)
    if not finished:  # Windows may still call the handler later: keep it alive for good
        _LATE.append((handler, vtable, params, prop))
    _release(operation.value)
    if not finished:
        raise TimeoutError("Windows didn't hand over the app's sound")
    if "error" in result:
        raise result["error"]
    if not result.get("client"):
        raise RuntimeError("no audio client")
    return result["client"]


class _ProcessCapture:
    """One app's sound (its whole process tree) as 48 kHz stereo float, read by its own thread."""

    def __init__(self, pid: int, name: str, owner: str):
        import ctypes
        from ctypes import wintypes

        self.pid, self.name, self.owner = pid, name, owner
        self.ring = AudioRing()
        self._stop = threading.Event()
        self.alive = True
        self._client = _activate(pid)
        self._capture = None
        self._event = None
        try:
            class WaveFormat(ctypes.Structure):
                _pack_ = 1
                _fields_ = [("tag", ctypes.c_ushort), ("channels", ctypes.c_ushort), ("rate", ctypes.c_uint),
                            ("bytes_per_sec", ctypes.c_uint), ("block", ctypes.c_ushort),
                            ("bits", ctypes.c_ushort), ("extra", ctypes.c_ushort)]

            fmt = WaveFormat(3, CHANNELS, SAMPLE_RATE, SAMPLE_RATE * 4 * CHANNELS, 4 * CHANNELS, 32, 0)
            _method(self._client, 3, ctypes.c_int, wintypes.DWORD, ctypes.c_longlong, ctypes.c_longlong,
                    ctypes.c_void_p, ctypes.c_void_p)(0, FLAGS, 200_000, 0, ctypes.byref(fmt), None)
            kernel32 = ctypes.windll.kernel32
            kernel32.CreateEventW.restype = wintypes.HANDLE
            self._event = kernel32.CreateEventW(None, False, False, None)
            _method(self._client, 13, wintypes.HANDLE)(self._event)
            capture = ctypes.c_void_p()
            iid = _guid(IID_CAPTURE)
            _method(self._client, 14, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(
                ctypes.byref(iid), ctypes.byref(capture))
            self._capture = capture.value
            _method(self._client, 10)()  # Start
        except Exception:
            self.close()
            raise
        self._thread = threading.Thread(target=self._read, name=f"app-audio-{owner}-{pid}", daemon=True)
        self._thread.start()

    def _read(self) -> None:
        import ctypes
        from ctypes import wintypes

        _com_init()
        kernel32 = ctypes.windll.kernel32
        get_buffer = _method(self._capture, 3, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_uint32),
                             ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p, ctypes.c_void_p)
        release = _method(self._capture, 4, ctypes.c_uint32)
        next_size = _method(self._capture, 5, ctypes.POINTER(ctypes.c_uint32))
        data, frames, flags, packet = ctypes.c_void_p(), ctypes.c_uint32(), wintypes.DWORD(), ctypes.c_uint32()
        try:
            while not self._stop.is_set():
                kernel32.WaitForSingleObject(self._event, 100)
                next_size(ctypes.byref(packet))
                while packet.value and not self._stop.is_set():
                    get_buffer(ctypes.byref(data), ctypes.byref(frames), ctypes.byref(flags), None, None)
                    count = frames.value
                    if count:
                        if flags.value & SILENT or not data.value:
                            block = np.zeros((count, CHANNELS), dtype=np.float32)
                        else:
                            raw = ctypes.string_at(data.value, count * 4 * CHANNELS)
                            block = np.frombuffer(raw, dtype=np.float32).reshape(count, CHANNELS).copy()
                        self.ring.write(block)
                    release(count)
                    next_size(ctypes.byref(packet))
        except Exception as exc:  # the app closed: its stream goes invalid
            log.info("%s: %s (%d) stopped: %s", self.owner, self.name, self.pid, exc)
        self.alive = False

    def close(self) -> None:
        import ctypes

        self._stop.set()
        thread = getattr(self, "_thread", None)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1)
        try:
            if self._client:
                _method(self._client, 11)()  # Stop
        except Exception:
            pass
        _release(self._capture)
        _release(self._client)
        self._capture = self._client = None
        if self._event:
            ctypes.windll.kernel32.CloseHandle(self._event)
            self._event = None
        self.alive = False
