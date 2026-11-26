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
SIZES = (("720p", 1280), ("1080p", 1920))
DEFAULTS: dict[str, Any] = {"serial": "", "longest": 1280, "bitrate_kbps": 8000}
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def find_adb() -> str | None:
    """Android's USB tool: next to SprigCast, on PATH, or in the Android SDK's usual places."""
    name = "adb.exe" if sys.platform == "win32" else "adb"
    here = Path(sys.executable).resolve().parent
    places = [here / "adb" / name, here / name]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        places.append(Path(local) / "Android" / "Sdk" / "platform-tools" / name)
    for home in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        if os.environ.get(home):
            places.append(Path(os.environ[home]) / "platform-tools" / name)
    for place in places:
        if place.is_file():
            return str(place)
    return shutil.which("adb")


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
            container = av.open(proc.stdout, format="h264", mode="r",
                                options={"probesize": "2048", "analyzeduration": "0", "fflags": "nobuffer"})
            stream = container.streams.video[0]
            stream.codec_context.thread_count = 1
            for frame in container.decode(stream):
                if self._stop.is_set():
                    break
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
                    break
                picture, order, size = camera_picture(frame, None)
                self._size = size
                self.status = f"capturing {size[0]}x{size[1]}"
                self._frames.put((picture, order))
            container.close()
        except Exception as exc:  # noqa: BLE001 (unplugged mid-stream, a phone that can't record: tried again)
            if not self._stop.is_set():
                log.info("%s: the phone's screen stopped: %s", self.id, exc)
        finally:
            _end(proc)
            with self._lock:
                self._procs.pop(mine, None)

    def _end_all(self) -> None:
        with self._lock:
            procs = list(self._procs.values())
        for p in procs:
            _end(p)


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
