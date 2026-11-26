"""Android Screen: an Android phone's screen over its USB cable, for mobile gaming streams.

Phone browsers can't share their screen, so this goes over the cable. With USB debugging on (once, in the
phone's Developer options), Android's own screen recorder (screenrecord, part of every Android) sends H.264 to
the PC through Android's USB tool (adb), and FFmpeg (PyAV) decodes it here: nothing is installed on the phone.

screenrecord stops by itself after 3 minutes, so the next one starts a few seconds before that and takes over
once its first picture is in: the picture carries on without a gap. The phone's turn is checked every
TURN_CHECK_S too: screenrecord keeps the shape it started with (a landscape game came in squeezed into a portrait
picture, PC 2026-11-26), so a turn starts the next one at once, in the new shape.

The phone's sound (user 2026-11-26): screenrecord has none, so SprigCast copies its own small helper
(android/sound, built with the installer) to the phone's temporary folder and runs it as the USB debugging shell:
it sends what the phone plays as 48 kHz stereo PCM over the same cable, and the mixer reads it like a mic
(read_block). Android 11 or newer; nothing is installed, and the phone plays through SprigCast while it runs.
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
TURN_CHECK_S = 0.7  # how often the phone's turn is looked at while recording
HOLD_MOST_S = 3.0  # a turned phone's last good picture stays at most this long while the next recording starts
BOXED = 0.3  # dark this far in from both ends of the long side: the other shape boxed inside this one
_TURN_WORDS = "'SurfaceOrientation|orientation=|mCurrentRotation|mRotation='"  # grep on the phone: a short answer
SIZES = (("720p", 1280), ("1080p", 1920))
DEFAULTS: dict[str, Any] = {"serial": "", "longest": 1280, "bitrate_kbps": 8000, "sound": True, "trim": True, "fill": True,
                            "monitor": True}  # you hear the phone through the PC (Android plays it here, not there)
TRIM_EVERY_S = 1.0
TRIM_SURE = 3
TRIM_MOST = 0.08  # of a side: more than that is the picture being dark, not a bar
DARK = 28  # luma (16 is video black)
SOUND_JAR = "sprigcast-sound.jar"
SOUND_ON_PHONE = "/data/local/tmp/sprigcast-sound.jar"
SOUND_CLASS = "com.slothbloom.sprigcast.Sound"
SOUND_START = b"SPRIGCAST-SOUND-START\n"  # the helper's line just before its sound
SOUND_RATE = 48000
SOUND_KEEP_S = 0.25  # more than this waiting (the mixer fell behind): the oldest goes, so it never lags
SOUND_RETRY_S = 3.0
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


def sound_on(settings: dict[str, Any]) -> bool:
    """Whether the leaf brings the phone's sound (listed in Sound, mixed like a mic)."""
    return bool(settings.get("sound", True))


def sound_helper() -> Path | None:
    """The phone sound helper SprigCast ships (built into the installed package by packaging/build_windows.ps1;
    from source, android/sound/build), or None."""
    here = Path(__file__).resolve()
    for path in (here.with_name(SOUND_JAR), here.parents[3] / "android" / "sound" / "build" / SOUND_JAR):
        if path.is_file():
            return path
    return None


def _helper_notes(text: bytes) -> list[str]:
    """The helper's "sprigcast-sound: ..." lines (short, nothing personal in them)."""
    lines = text.decode("utf-8", "replace").splitlines()
    return [ln.split(":", 1)[1].strip()[:200] for ln in lines if ln.startswith("sprigcast-sound:")]


def pcm_to_float(data: bytes) -> np.ndarray:
    """48 kHz stereo 16-bit PCM -> N x 2 float32 (an odd byte or half a pair at the end is left out)."""
    usable = len(data) // 4 * 4
    return (np.frombuffer(data[:usable], dtype="<i2").astype(np.float32) / 32768.0).reshape(-1, 2)


def black_edges(y: np.ndarray) -> tuple[int, int, int, int]:
    """A picture's luma -> the black bars on its (left, top, right, bottom), even numbers, each at most TRIM_MOST
    of its side (none where more is dark)."""
    h, w = y.shape
    cols = y[::8, :].max(axis=0) < DARK
    rows = y[:, ::8].max(axis=1) < DARK

    def run(flags: np.ndarray, most: int) -> int:
        n = int(np.argmin(flags)) if not flags.all() else len(flags)
        return n // 2 * 2 if n <= most else 0

    return (run(cols, int(w * TRIM_MOST)), run(rows, int(h * TRIM_MOST)),
            run(cols[::-1], int(w * TRIM_MOST)), run(rows[::-1], int(h * TRIM_MOST)))


def boxed(y: np.ndarray) -> bool:
    """True when a picture's luma looks like the phone turned under a recording still in the old shape: the
    screen shrunk into the middle, with wide black on both ends of the long side and something lit between."""
    h, w = y.shape
    along = y[::8, :] if w >= h else y[:, ::8].T  # the long side across
    n = along.shape[1]
    ends = int(n * BOXED)
    if ends < 2:
        return False
    return bool(along[:, :ends].max() < DARK and along[:, n - ends:].max() < DARK
                and along[:, ends:n - ends].max() >= DARK * 2)


def crop_yuv(frame, left: int, top: int, right: int, bottom: int):
    """A 4:2:0 frame with its edges cut off (the size stays even across, and a multiple of 4 down)."""
    import av

    w, h = frame.width, frame.height
    a = frame.to_ndarray(format="yuv420p")
    nw = (w - left - right) // 2 * 2
    nh = (h - top - bottom) // 4 * 4
    if nw < 16 or nh < 16 or h % 2 or w % 2:
        raise ValueError("too small to trim")
    y = a[:h]
    u = a[h:h + h // 4].reshape(h // 2, w // 2)
    v = a[h + h // 4:h + h // 2].reshape(h // 2, w // 2)
    l2, t2 = left // 2, top // 2
    out = np.concatenate([y[top:top + nh, left:left + nw],
                          np.ascontiguousarray(u[t2:t2 + nh // 2, l2:l2 + nw // 2]).reshape(nh // 4, nw),
                          np.ascontiguousarray(v[t2:t2 + nh // 2, l2:l2 + nw // 2]).reshape(nh // 4, nw)])
    return av.VideoFrame.from_ndarray(out, format="yuv420p")


def parse_size(text: str) -> tuple[int, int] | None:
    """`wm size` -> the size the screen draws at now (an override wins over the physical size)."""
    sizes = dict(re.findall(r"(Physical|Override) size:\s*(\d+x\d+)", text))
    found = sizes.get("Override") or sizes.get("Physical")
    if not found:
        return None
    w, h = (int(v) for v in found.split("x"))
    return w, h


def parse_turned(text: str) -> bool | None:
    """`dumpsys input` (or `dumpsys window displays`) -> True when the screen is turned a quarter (landscape on a
    phone), None when it doesn't say. Older Androids write "SurfaceOrientation: 1"; newer ones dropped that line
    and write "orientation=ROTATION_90" on the display's viewport (the phone was always taken as portrait, PC
    2026-11-26), or "mCurrentRotation=ROTATION_90" for the window manager."""
    for pattern in (r"SurfaceOrientation:\s*(?:ROTATION_)?(\d+)", r"Viewport INTERNAL\b[^\n]*?orientation=(?:ROTATION_)?(\d+)",
                    r"mCurrentRotation=(?:ROTATION_)?(\d+)", r"\bmRotation=(?:ROTATION_)?(\d+)"):
        found = re.search(pattern, text)
        if found:
            return found.group(1) in ("1", "3", "90", "270")
    return None


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
        self.sound_jar: Path | None = None  # tests set it
        self.sound_note = ""  # why the phone's sound isn't coming ("" while it is, or before it's tried)
        self._sound_lock = threading.Lock()
        self._sound: list[np.ndarray] = []
        self._sound_frames = 0
        self._sound_thread: threading.Thread | None = None
        self._sound_proc: subprocess.Popen | None = None
        self._sound_given_up = False
        self._where: tuple[str, str] | None = None
        self._trim = None  # ((width, height), (left, top, right, bottom)) taken off each picture
        self._trim_seen = None
        self._trims: dict[tuple[int, int], tuple[int, int, int, int]] = {}  # each shape's trim, kept for turning back
        self._trim_count = 0
        self._trim_at = 0.0
        self._hold: int | None = None  # this recording's pictures wait: the phone turned under it
        self._hold_at = 0.0
        self._not_turned: int | None = None  # this recording looked boxed but the phone hadn't turned
        self._look_now = threading.Event()  # a boxed picture: look at the phone's turn at once

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
        proc = self._sound_proc
        if proc is not None:
            _end(proc)
        if self._sound_thread is not None and self._sound_thread is not threading.current_thread():
            self._sound_thread.join(timeout=3)
        self._sound_thread = None
        self._sound_given_up = False
        with self._sound_lock:
            self._sound, self._sound_frames = [], 0
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=3)
        self._thread = None

    VIDEO_KEYS = ("serial", "longest", "bitrate_kbps")  # "trim" is read as each picture comes

    def update(self, settings: dict[str, Any]) -> None:
        """Only a change to the picture's settings starts the recording again: the Sound card's volume, mute and
        the rest are read from the settings as they are."""
        before = dict(self.settings)
        super().update(settings)
        if self._thread is None:
            return
        if any(before.get(k) != self.settings.get(k) for k in self.VIDEO_KEYS):
            self.stop()
            self.start()
        elif bool(before.get("sound", True)) != bool(self.settings.get("sound", True)):
            if not self.settings.get("sound", True):
                proc = self._sound_proc
                if proc is not None:
                    _end(proc)
                self.sound_note = ""
            self._sound_given_up = False
            if self.settings.get("sound", True) and self._where is not None:
                self._start_sound(*self._where)

    @property
    def size(self) -> tuple[int, int]:
        return self._size

    # Sound: read by the mixer like a mic (read_block, gain) -------------------------------------------

    @property
    def gain(self) -> float:
        if not sound_on(self.settings) or self.settings.get("muted"):
            return 0.0
        return float(10 ** (float(self.settings.get("volume_db", 0.0)) / 20))

    def read_block(self, n: int) -> np.ndarray:
        out = np.zeros((n, 2), dtype=np.float32)
        filled = 0
        with self._sound_lock:
            while filled < n and self._sound:
                chunk = self._sound[0]
                take = min(n - filled, len(chunk))
                out[filled:filled + take] = chunk[:take]
                filled += take
                if take == len(chunk):
                    self._sound.pop(0)
                else:
                    self._sound[0] = chunk[take:]
            self._sound_frames -= filled
        return out

    def _heard(self, samples: np.ndarray) -> None:
        with self._sound_lock:
            self._sound.append(samples)
            self._sound_frames += len(samples)
            while self._sound_frames > SOUND_KEEP_S * SOUND_RATE and len(self._sound) > 1:
                self._sound_frames -= len(self._sound.pop(0))

    def _start_sound(self, adb: str, serial: str) -> None:
        if not sound_on(self.settings) or self._sound_given_up:
            return
        if self._sound_thread is not None and self._sound_thread.is_alive():
            return
        jar = self.sound_jar or sound_helper()
        if jar is None:
            self.sound_note = "this copy of SprigCast has no phone sound helper"
            return
        self._sound_thread = threading.Thread(target=self._sound_run, args=(adb, serial, jar),
                                              name=f"android-{self.id}-sound", daemon=True)
        self._sound_thread.start()

    def _sound_run(self, adb: str, serial: str, jar: Path) -> None:
        """Copy the helper to the phone and read what it sends, again after a few seconds if it stops."""
        while not self._stop.is_set():
            if not self.settings.get("sound", True):
                return  # switched off (update ended the helper)
            pushed = _run([adb, "-s", serial, "push", str(jar), SOUND_ON_PHONE], timeout=15)
            command = [adb, "-s", serial, "exec-out", f"CLASSPATH={SOUND_ON_PHONE}", "app_process", "/",
                       SOUND_CLASS]
            try:
                proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        creationflags=_NO_WINDOW, bufsize=0)
            except OSError as exc:
                self.sound_note = f"couldn't start: {exc}"
                return
            self._sound_proc = proc
            # adb's exec-out sends what the helper writes to its error output down the same pipe: everything
            # before its SOUND_START line is text (notes, or why it couldn't), and only after it comes sound.
            # The sound is read in whole 4-byte frames, the odd bytes kept for the next read: dropping them
            # shifted every sample after and the phone's sound came as loud noise (meter at 0 dB, PC 2026-11-26).
            prelude, rest, started = b"", b"", False
            try:
                while not self._stop.is_set():
                    data = proc.stdout.read(SOUND_RATE // 50 * 4)  # 20 ms
                    if not data:
                        break
                    if not started:
                        prelude += data
                        at = prelude.find(SOUND_START)
                        if at < 0:
                            if len(prelude) > 64 * 1024:
                                prelude = prelude[-4096:]
                            continue
                        data, prelude = prelude[at + len(SOUND_START):], prelude[:at]
                        started = True
                        self.sound_note = ""
                        notes = _helper_notes(prelude)
                        log.info("%s: the phone's sound is coming%s", self.id,
                                 f" ({'; '.join(notes)})" if notes else "")
                    data = rest + data
                    usable = len(data) // 4 * 4
                    rest = data[usable:]
                    if usable:
                        self._heard(pcm_to_float(data[:usable]))
            except (OSError, ValueError):
                pass
            finally:
                _end(proc)
                self._sound_proc = None
            if self._stop.is_set():
                return
            said = b"" if started else prelude
            try:
                said += proc.stderr.read() or b""
            except (OSError, ValueError):
                pass
            line = next((n for n in _helper_notes(said) if "trying anyway" not in n), "")
            line = line or (pushed.strip().splitlines() or ["it stopped"])[-1][:200]
            self.sound_note = line
            if "Android 11" in line or "refused" in line:
                log.info("%s: no phone sound: %s", self.id, line)
                self._sound_given_up = True  # the phone can't: not tried again until the leaf restarts
                return
            log.info("%s: the phone's sound stopped (%s); trying again", self.id, line)
            self._stop.wait(SOUND_RETRY_S)

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

    def _turned(self, adb: str, serial: str) -> bool:
        for what in (["input"], ["window", "displays"]):
            turned = parse_turned(_run([adb, "-s", serial, "shell", "dumpsys", *what, "|", "grep", "-E",
                                        _TURN_WORDS], timeout=8))
            if turned is not None:
                return turned
        return False

    def _command(self, adb: str, serial: str) -> tuple[list[str], bool] | None:
        screen = parse_size(_run([adb, "-s", serial, "shell", "wm", "size"]))
        if screen is None:
            return None
        turned = self._turned(adb, serial)
        w, h = record_size(screen, turned, int(self.settings.get("longest") or 1280))
        bitrate = max(1000, min(40_000, int(self.settings.get("bitrate_kbps") or 8000))) * 1000
        return [adb, "-s", serial, "exec-out", "screenrecord", "--output-format=h264", "--size", f"{w}x{h}",
                "--bit-rate", str(bitrate), "--time-limit", "180", "-"], turned

    def _run(self) -> None:
        segment = 0
        while not self._stop.is_set():
            adb = self._adb()
            if adb is None:
                self.status = "needs Android's USB tool (adb)"
                self._stop.wait(RETRY_S * 5)
                continue
            serial = self._phone(adb)
            made = self._command(adb, serial) if serial else None
            if made is None:
                self._stop.wait(RETRY_S)
                continue
            command, turned = made
            self._where = (adb, serial)
            self._start_sound(adb, serial)
            segment += 1
            mine = segment
            reader = threading.Thread(target=self._segment, args=(mine, command), name=f"android-{self.id}-{mine}",
                                      daemon=True)
            reader.start()
            began = looked = time.monotonic()
            while not self._stop.is_set() and reader.is_alive() and time.monotonic() - began < SEGMENT_S:
                asked = self._look_now.wait(0.25)
                self._look_now.clear()
                if (asked or time.monotonic() - looked >= TURN_CHECK_S) and not self._stop.is_set():
                    looked = time.monotonic()
                    if self._turned(adb, serial) != turned:
                        log.info("%s: the phone turned to %s", self.id, "portrait" if turned else "landscape")
                        self._hold_from(mine)
                        break  # the next recording, in the new shape, takes over once its first picture is in
                    if asked:
                        self._hold, self._not_turned = None, mine  # only a dark picture: it shows again
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

    def _hold_from(self, mine: int) -> None:
        """The phone turned under recording `mine`: its pictures (squeezed or boxed in the old shape) don't show;
        the last good one stays until the next recording's first picture, so the turn is one clean cut."""
        if self._hold != mine:
            self._hold, self._hold_at = mine, time.monotonic()

    def _held(self, mine: int, frame) -> bool:
        if self._hold == mine and time.monotonic() - self._hold_at < HOLD_MOST_S:
            return True
        if self._not_turned == mine or self._hold == mine:  # held too long (no next recording): show it anyway
            return False
        try:
            plane = frame.planes[0]
            y = np.frombuffer(plane, np.uint8).reshape(frame.height, plane.line_size)[:, :frame.width]
            looks_turned = boxed(y)
        except (ValueError, TypeError):
            looks_turned = False
        if looks_turned:
            self._hold_from(mine)
            self._look_now.set()
        return looks_turned

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
            if self._held(mine, frame):
                continue
            if self.settings.get("trim", True):
                frame = self._trimmed(frame)
            picture, order, size = camera_picture(frame, None)
            self._size = size
            note = self.sound_note if sound_on(self.settings) else ""
            self.status = f"capturing {size[0]}x{size[1]}" + (f"; no phone sound: {note}" if note else "")
            self._frames.put((picture, order))
        return False

    def _trimmed(self, frame):
        """The picture without the black bars the phone draws itself (in landscape, the strip beside the camera
        cutout: PC 2026-11-26). Looked at once a second; a change is used once seen TRIM_SURE times running,
        so a dark moment in a game doesn't trim it."""
        dims = (frame.width, frame.height)
        now = time.monotonic()
        if self._trim is not None and self._trim[0] != dims:  # turned: this shape's trim, if it had one
            self._trims[self._trim[0]] = self._trim[1]
            self._trim = (dims, self._trims[dims]) if dims in self._trims else None
            self._trim_at = 0.0  # and look at the new shape now, not up to a second later
        elif self._trim is None and dims in self._trims:
            self._trim = (dims, self._trims[dims])
        every = TRIM_EVERY_S if self._trim is not None else TRIM_EVERY_S / 3  # a new shape settles sooner
        if now - self._trim_at >= every:
            self._trim_at = now
            try:
                plane = frame.planes[0]
                y = np.frombuffer(plane, np.uint8).reshape(frame.height, plane.line_size)[:, :frame.width]
                edges = black_edges(y)
            except (ValueError, TypeError):
                edges = (0, 0, 0, 0)
            seen = (dims, edges)
            self._trim_count = self._trim_count + 1 if seen == self._trim_seen else 1
            self._trim_seen = seen
            if self._trim_count >= TRIM_SURE and seen != self._trim:
                self._trim = seen
                self._trims[dims] = edges
                log.info("%s: black edges trimmed: %s", self.id, edges)
        if self._trim is None or self._trim[0] != dims or not any(self._trim[1]):
            return frame
        try:
            return crop_yuv(frame, *self._trim[1])
        except (ValueError, TypeError) as exc:
            log.info("%s: couldn't trim the picture: %s", self.id, exc)
            self._trim = (dims, (0, 0, 0, 0))
            return frame

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
