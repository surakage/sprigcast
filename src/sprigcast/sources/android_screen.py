"""Android Screen: an Android phone's screen over its USB cable, for mobile gaming streams.

Phone browsers can't share their screen, so this goes over the cable. With USB debugging on (once, in the
phone's Developer options), Android's own screen recorder (screenrecord, part of every Android) sends H.264 to
the PC through Android's USB tool (adb), and FFmpeg (PyAV) decodes it here: nothing is installed on the phone.

screenrecord stops by itself after 3 minutes, so the next one starts a few seconds before that and takes over
once its first picture is in: the picture carries on without a gap. Each new one also follows the phone turning
between portrait and landscape. The phone's sound isn't sent (screenrecord has no sound).
"""

from __future__ import annotations

import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

from sprigcast.core.mailbox import Mailbox
from sprigcast.core.source import Source, register

log = logging.getLogger(__name__)

SEGMENT_S = 170.0  # screenrecord's own limit is 180 s: the next one starts before that
RETRY_S = 2.0
IDLE_S = 0.1  # adb quiet this long: the picture the parser holds is shown
SIZES = (("720p", 1280), ("1080p", 1920))
DEFAULTS: dict[str, Any] = {"serial": "", "longest": 1280, "bitrate_kbps": 8000}
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

# Google's adb isn't shipped with SprigCast: one already on the PC is used, or it's downloaded from Google when
# you ask (app/android_screen_dialog.py), into ADB_HOME (SprigCast's app data; main.py sets it).
ADB_HOME: Path | None = None
ADB_TERMS_URL = "https://developer.android.com/studio/terms"
_ZIP_NAME = {"win32": "windows", "darwin": "darwin"}.get(sys.platform, "linux")
ADB_URL = f"https://dl.google.com/android/repository/platform-tools-latest-{_ZIP_NAME}.zip"
KEEP = ("adb", "adb.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll", "NOTICE.txt", "source.properties")


def find_adb() -> str | None:
    """Google's adb: one already on the PC (on PATH, Android Studio's SDK, scrcpy's folder) or the one
    SprigCast downloaded from Google for you (ADB_HOME)."""
    name = "adb.exe" if sys.platform == "win32" else "adb"
    found = shutil.which("adb")
    if found:
        return found
    places = []
    for home in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        if os.environ.get(home):
            places.append(Path(os.environ[home]) / "platform-tools" / name)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        places.append(Path(local) / "Android" / "Sdk" / "platform-tools" / name)
        places += sorted((Path(local) / "Microsoft" / "WinGet" / "Packages").glob(f"Genymobile.scrcpy*/*/{name}"))
    for scoop in (Path.home() / "scoop" / "apps" / "scrcpy" / "current", Path.home() / "scoop" / "shims"):
        places.append(scoop / name)
    if ADB_HOME is not None:
        places.append(ADB_HOME / "platform-tools" / name)
    for place in places:
        if place.is_file():
            return str(place)
    return None


def install_adb(zip_path: Path, home: Path) -> Path:
    """Unpack adb from Google's platform-tools zip into home/platform-tools: adb, its two Windows libraries,
    Google's NOTICE.txt and the version file, nothing else. Returns adb's path. Raises OSError or ValueError."""
    import zipfile

    target = home / "platform-tools"
    target.mkdir(parents=True, exist_ok=True)
    adb = None
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            parts = Path(info.filename).parts
            if len(parts) != 2 or parts[0] != "platform-tools" or parts[1] not in KEEP:
                continue
            out = target / parts[1]
            part = out.with_name(out.name + ".part")
            with z.open(info) as src, part.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            os.replace(part, out)
            if parts[1] in ("adb", "adb.exe"):
                adb = out
                if sys.platform != "win32":
                    out.chmod(0o755)
    if adb is None:
        raise ValueError("adb wasn't in the download")
    return adb


def _run(args: list[str], timeout: float = 5.0) -> str:
    try:
        done = subprocess.run(args, capture_output=True, timeout=timeout, creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.decode("utf-8", errors="replace")


def parse_devices(text: str) -> list[dict[str, str]]:
    """`adb devices -l` -> [{"serial", "state", "model"}]: state is "device" (ready), "unauthorized" (allow USB
    debugging on the phone) or "offline"."""
    found = []
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2 or parts[0].startswith("*"):
            continue
        model = next((p.split(":", 1)[1] for p in parts[2:] if p.startswith("model:")), "")
        found.append({"serial": parts[0], "state": parts[1], "model": model.replace("_", " ")})
    return found


def list_phones(adb: str | None = None) -> list[dict[str, str]]:
    adb = adb or find_adb()
    return parse_devices(_run([adb, "devices", "-l"])) if adb else []


def parse_size(text: str) -> tuple[int, int] | None:
    """`wm size` -> the size the screen draws at now (an override wins over the physical size)."""
    sizes = dict(re.findall(r"(Physical|Override) size:\s*(\d+x\d+)", text))
    found = sizes.get("Override") or sizes.get("Physical")
    if not found:
        return None
    w, h = (int(v) for v in found.split("x"))
    return w, h


def parse_turned(text: str) -> bool:
    """`dumpsys input` -> True when the screen is turned a quarter (landscape on a phone)."""
    found = re.search(r"SurfaceOrientation:\s*(\d)", text)
    return bool(found) and found.group(1) in ("1", "3")


def record_size(screen: tuple[int, int], turned: bool, longest: int) -> tuple[int, int]:
    """The size to record: the screen as it's shown now, its longest side at most `longest`, both sides a
    multiple of 16 (what phone encoders take)."""
    w, h = screen
    if turned:
        w, h = h, w
    scale = min(1.0, longest / max(w, h))
    return max(16, round(w * scale / 16) * 16), max(16, round(h * scale / 16) * 16)


@register("android_screen")
class AndroidScreenSource(Source):
    """Settings: serial ("" = the first phone ready), longest (1280 or 1920), bitrate_kbps."""

    label = "Android Screen"
    defaults = DEFAULTS
    channel_order = "BGR"

    def __init__(self, source_id: str, settings: dict[str, Any]):
        super().__init__(source_id, settings)
        self._frames: Mailbox = Mailbox()
        self._size = (1280, 720)
        self.status = "starting"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._current = 0  # the segment whose pictures show
        self._procs: dict[int, subprocess.Popen] = {}
        self.adb_path = None  # tests set it

    # Source ---------------------------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"android-{self.id}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._end_all()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=3)
        self._thread = None

    def update(self, settings: dict[str, Any]) -> None:
        before = dict(self.settings)
        super().update(settings)
        if before != self.settings and self._thread is not None:
            self.stop()
            self.start()

    @property
    def size(self) -> tuple[int, int]:
        return self._size

    def video_frame(self) -> np.ndarray | None:
        item = self._frames.take()
        if item is None:
            return None
        frame, self.channel_order = item
        return frame

    # The phone -----------------------------------------------------------------------------------

    def _adb(self) -> str | None:
        return self.adb_path or find_adb()

    def _phone(self, adb: str) -> str | None:
        phones = parse_devices(_run([adb, "devices", "-l"]))
        want = str(self.settings.get("serial") or "")
        ready = [p for p in phones if p["state"] == "device" and (not want or p["serial"] == want)]
        if ready:
            return ready[0]["serial"]
        if any(p["state"] == "unauthorized" for p in phones):
            self.status = "allow USB debugging on your phone"
        elif want and phones:
            self.status = "that phone isn't connected"
        else:
            self.status = "no Android phone connected by USB"
        return None

    def _command(self, adb: str, serial: str) -> list[str] | None:
        screen = parse_size(_run([adb, "-s", serial, "shell", "wm", "size"]))
        if screen is None:
            return None
        turned = parse_turned(_run([adb, "-s", serial, "shell", "dumpsys", "input"], timeout=8))
        w, h = record_size(screen, turned, int(self.settings.get("longest") or 1280))
        bitrate = max(1000, min(40_000, int(self.settings.get("bitrate_kbps") or 8000))) * 1000
        return [adb, "-s", serial, "exec-out", "screenrecord", "--output-format=h264", "--size", f"{w}x{h}",
                "--bit-rate", str(bitrate), "--time-limit", "180", "-"]

    def _run(self) -> None:
        segment = 0
        while not self._stop.is_set():
            adb = self._adb()
            if adb is None:
                self.status = "needs Android's USB tool (adb)"
                self._stop.wait(RETRY_S * 5)
                continue
            serial = self._phone(adb)
            command = self._command(adb, serial) if serial else None
            if command is None:
                self._stop.wait(RETRY_S)
                continue
            segment += 1
            mine = segment
            reader = threading.Thread(target=self._segment, args=(mine, command), name=f"android-{self.id}-{mine}",
                                      daemon=True)
            reader.start()
            began = time.monotonic()
            while not self._stop.is_set() and reader.is_alive() and time.monotonic() - began < SEGMENT_S:
                self._stop.wait(0.25)
            if not reader.is_alive() and self._current == mine and not self._stop.is_set():
                self._stop.wait(RETRY_S)  # it ended early (unplugged, the screen recorder refused): again soon

    def _segment(self, mine: int, command: list[str]) -> None:
        """One screenrecord: its pictures show from its first one on (the one before then ends)."""
        import av

        from sprigcast.sources.camera import camera_picture

        try:
            proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    creationflags=_NO_WINDOW, bufsize=0)
        except OSError as exc:
            self.status = f"couldn't start: {exc}"
            return
        with self._lock:
            self._procs[mine] = proc
        try:
            # Our own parser rather than av.open(): FFmpeg's h264 parser hands a picture out only once the next
            # one starts, and a still screen sends no next one (screenrecord sends a picture when the screen
            # changes): the leaf stayed on "starting" for minutes (PC 2026-11-26). Here, once adb has sent
            # nothing for IDLE_S, the parser is flushed and the picture it holds shows.
            codec = av.CodecContext.create("h264", "r")
            codec.thread_count = 1
            try:  # each picture out of the decoder as soon as it's in
                from av.codec.context import Flags

                codec.flags |= Flags.low_delay
            except (ImportError, AttributeError, TypeError, ValueError):
                pass
            chunks: queue.Queue = queue.Queue(maxsize=256)
            threading.Thread(target=_read_into, args=(proc.stdout, chunks), name=f"android-{self.id}-read",
                             daemon=True).start()
            held = ended = False
            while not ended and not self._stop.is_set():
                try:
                    data = chunks.get(timeout=IDLE_S)
                except queue.Empty:
                    if not held:
                        continue
                    data = None
                ended = data is _EOF
                packets = codec.parse(None if data is None or ended else data)
                held = not (data is None or ended)
                if self._show(mine, (f for packet in packets for f in codec.decode(packet)), camera_picture):
                    break
        except Exception as exc:  # noqa: BLE001 (unplugged mid-stream, a phone that can't record: tried again)
            if not self._stop.is_set():
                log.info("%s: the phone's screen stopped: %s", self.id, exc)
        finally:
            _end(proc)
            with self._lock:
                self._procs.pop(mine, None)

    def _show(self, mine: int, frames, camera_picture) -> bool:
        """Show these pictures; True when a newer recording has taken over (this one ends)."""
        for frame in frames:
            if self._stop.is_set():
                return True
            with self._lock:
                if self._current < mine:  # the newest one's first picture: it takes over
                    old = [p for n, p in self._procs.items() if n < mine]
                    self._current = mine
                else:
                    old = []
                behind = self._current > mine
            for p in old:
                _end(p)
            if behind:
                return True
            picture, order, size = camera_picture(frame, None)
            self._size = size
            self.status = f"capturing {size[0]}x{size[1]}"
            self._frames.put((picture, order))
        return False

    def _end_all(self) -> None:
        with self._lock:
            procs = list(self._procs.values())
        for p in procs:
            _end(p)


_EOF = b""


def _read_into(pipe, chunks: queue.Queue) -> None:
    """adb's output into `chunks` as it comes, then _EOF."""
    try:
        while True:
            data = pipe.read(65536)
            if not data:
                break
            chunks.put(data)
    except (OSError, ValueError):
        pass
    finally:
        chunks.put(_EOF)


def _end(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except (OSError, subprocess.SubprocessError):
            try:
                proc.kill()
            except OSError:
                pass
