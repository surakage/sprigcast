"""The logbook: everything worth checking later, in one folder (Documents\\SprigCast\\Logs).

* ``sprigcast YYYY-MM-DD.log``: every message SprigCast logs that day (encoder choices, stream
  summaries, the Optimize wizard's findings, warnings and errors).
* ``stream YYYY-MM-DD HH-MM-SS.txt``: one per stream. What went live and how (destinations, sizes,
  bitrates, encoder path, shared encoders, the PC), then every 5 seconds a line per destination
  (bitrate actually sent, frames, drops, upload backlog, auto-pilot), then the end-of-stream summary.
* ``crash.log``: where a native library crashed, if one ever does (a section per run, by process id),
  and on the next start what Windows recorded about it (app/crash_report.py).

Day logs and stream reports older than 14 days are deleted at start-up (Settings → General → Logs:
7, 14, 30 or 90 days, or Forever). SPRIGCAST_LOG_DIR puts the folder somewhere else.

Nothing personal reaches these files: every handler, every stream report line and crash.log go through
core/privacy.py (keys, tokens, passwords, addresses past their host, the user and PC name, e-mails, public IPs,
device serials, channel names). What faulthandler writes straight to a file is scrubbed at the next start.
"""

from __future__ import annotations

import contextlib
import logging
import os
import queue
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from sprigcast.core import privacy

log = logging.getLogger(__name__)

KEEP_DAYS = 14
KEEP_SETTING = "logs/keep_days"  # QSettings: 0 = keep forever
KEEP_CHOICES = (7, 14, 30, 90, 0)


def keep_days() -> int:
    """How many days of logs and stream reports to keep (Settings → Recording → File names); 0 keeps them all."""
    try:
        from PySide6.QtCore import QSettings

        days = int(QSettings().value(KEEP_SETTING, KEEP_DAYS) or 0)
    except Exception:
        return KEEP_DAYS
    return max(0, days)
EVERY_S = 5.0
FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_handler: logging.Handler | None = None
_crash_file = None


def crash_log() -> Path | None:
    return Path(_crash_file.name) if _crash_file is not None else None


def log_dir() -> Path:
    custom = os.environ.get("SPRIGCAST_LOG_DIR", "").strip()
    if custom:
        return Path(custom)
    try:
        from PySide6.QtCore import QStandardPaths

        docs = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
    except Exception:
        docs = ""
    return Path(docs or Path.home() / "Documents") / "SprigCast" / "Logs"


def _prune(folder: Path, now: datetime, days: int | None = None) -> int:
    """Delete day logs (sprigcast *.log) and stream reports (stream *.txt) older than `days`. Returns how many."""
    days = keep_days() if days is None else days
    if days <= 0:
        return 0
    cutoff = (now - timedelta(days=days)).timestamp()
    removed = 0
    for path in [*folder.glob("sprigcast *.log"), *folder.glob("stream *.txt")]:
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            pass
    return removed


def setup(now: datetime | None = None) -> Path | None:
    """Send every log message to today's file too (call once at start). Returns the file, or None."""
    global _handler, _crash_file
    now = now or datetime.now()
    folder = log_dir()
    try:
        folder.mkdir(parents=True, exist_ok=True)
        removed = _prune(folder, now)
        remember_personal()
        path = folder / f"sprigcast {now:%Y-%m-%d}.log"
        _sweep(folder, path)
        handler = logging.FileHandler(path, encoding="utf-8")
    except OSError as exc:
        log.warning("no log file (%s)", exc)
        return None
    handler.setFormatter(privacy.ScrubbingFormatter(_Formatter(FORMAT)))
    handler.setLevel(logging.INFO)
    handler.addFilter(Floods())
    quiet_live_flv()
    root = logging.getLogger()
    if _handler is not None:
        root.removeHandler(_handler)
        _handler.close()
    root.addHandler(handler)
    privacy.guard_root()  # the console too
    if root.level > logging.INFO or root.level == logging.NOTSET:
        root.setLevel(logging.INFO)
    _handler = handler
    try:
        import faulthandler

        if _crash_file is None:
            _crash_file = open(folder / "crash.log", "a", encoding="utf-8")  # noqa: SIM115 (open for the run)
            from sprigcast.app.crash_report import section_header

            _crash_file.write(section_header(now=now, version=_version()))  # whose crash it is, if one comes
            _crash_file.flush()
            faulthandler.enable(file=_crash_file, all_threads=crash_dumps_all_threads())
    except (OSError, RuntimeError):
        pass
    logging.getLogger("sprigcast").info("SprigCast %s started; logs in %s%s", _version(), folder,
                                        f" ({removed} older than {keep_days()} days removed)" if removed else "")
    return path


def _sweep(folder: Path, today: Path) -> None:
    """What faulthandler wrote straight to crash.log or today's log last run is scrubbed now, before this run
    writes to them; older logs (anything an older SprigCast wrote) on a side thread that rests as it goes."""
    first = [p for p in (folder / "crash.log", today) if p.exists()]
    privacy.sweep(folder, first)
    rest = [p for p in privacy.log_files(folder) if p not in first]
    if rest:
        import threading

        threading.Thread(target=privacy.sweep, args=(folder, rest), kwargs={"pause": 0.01}, name="log-sweep",
                         daemon=True).start()


def remember_personal() -> None:
    """The chat channels named in Settings: hidden from the log even before a chat reader starts."""
    try:
        import json

        from PySide6.QtCore import QSettings

        settings = QSettings()
        names = json.loads(str(settings.value("chat/names", "") or "{}"))
        old = str(settings.value("chat/twitch_channel", "") or "")
    except Exception:
        return
    if isinstance(names, dict):
        privacy.remember_name(old, *[n for n in names.values() if isinstance(n, str)])


@contextlib.contextmanager
def crash_notes_paused(platform: str | None = None):
    """Windows: no faulthandler notes while a message box waits on its own (the safe-mode question).

    faulthandler notes every exception with an error code, also the ones COM raises and handles itself: a
    message box's input call answering UI Automation logged "Windows fatal exception: code 0x8001010d" at
    safe_mode.py:47 on every start after a crash (PC, 2026-11-18). faulthandler can't be told to skip a code.
    A real crash meanwhile is still caught by SprigCast.exe's crash guard, which writes the dump."""
    import faulthandler

    paused = (platform or sys.platform) == "win32" and faulthandler.is_enabled()
    if paused:
        faulthandler.disable()
    try:
        yield paused
    finally:
        if paused:
            try:
                if _crash_file is not None:
                    faulthandler.enable(file=_crash_file, all_threads=crash_dumps_all_threads())
                else:
                    faulthandler.enable(all_threads=crash_dumps_all_threads())
            except (OSError, RuntimeError, ValueError):
                pass


def crash_dumps_all_threads(platform: str | None = None) -> bool:
    """Whether a crash note lists every thread's stack, or only the crashing thread's (Windows).

    On Windows faulthandler writes a note for every exception with an error code, also ones Windows handles
    and carries on from, like 0x8001010d, which COM raises when UI Automation (a screen reader, the PC's
    pop-up sweep) asks the window something during a menu's input call. Listing every thread then reads the
    stacks of threads that are still running; the render thread's changed under it, the note was cut off
    mid-line with "access violation" and SprigCast died (2026-11-17 20:57, live, Heavy test, menu sweep).
    The thread the note is about is the one that's stopped, so reading only its own stack is safe."""
    return (platform or sys.platform) != "win32"


class Floods(logging.Filter):
    """The same message more than LIMIT times a minute (an error raised every frame, say) is held back;
    the next one written after that says how many were held. Each kind of problem still shows up, early,
    without one of them filling the log."""

    LIMIT = 10
    WINDOW_S = 60.0

    def __init__(self, clock=time.monotonic):
        super().__init__()
        self._clock = clock
        self._seen: dict[tuple, list] = {}  # (logger, level, message template) -> [window start, count, held]

    def filter(self, record: logging.LogRecord) -> bool:
        key = (record.name, record.levelno, str(record.msg)[:200])
        now = self._clock()
        seen = self._seen.get(key)
        if seen is None or now - seen[0] >= self.WINDOW_S:
            held = seen[2] if seen else 0
            self._seen[key] = [now, 1, 0]
            if held:
                record.sprig_held = held
            if len(self._seen) > 2000:  # a long run: forget the quiet ones
                self._seen = {k: v for k, v in self._seen.items() if now - v[0] < self.WINDOW_S}
            return True
        seen[1] += 1
        if seen[1] <= self.LIMIT:
            return True
        seen[2] += 1
        return False


class LiveFlvQuiet(logging.Filter):
    """FFmpeg's FLV muxer can't go back to write the duration and size into a live stream's header when it closes
    ("Failed to update header with correct duration/filesize"): normal for every stream that stops, so it's logged
    as information, not a warning (PC test 2026-11-20)."""

    NORMAL = (
        ("Failed to update header with correct", "normal when a live stream ends"),
        # Twitch Enhanced Broadcasting sends several video tracks in one FLV on purpose (E-RTMP multitrack)
        ("more than one video stream", "normal for Twitch Enhanced Broadcasting: one track per quality"),
        # The AAC encoder closed with its last few frames (a fraction of a second at the very end) not flushed
        ("frames left in the queue on closing", "normal when a stream or recording stops: the last moment's sound"),
    )

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno == logging.WARNING:
            text = str(record.msg)
            for found, why in self.NORMAL:
                if found in text:
                    record.levelno, record.levelname = logging.INFO, "INFO"
                    record.msg = f"{record.msg} ({why})"
                    break
        return True


def quiet_live_flv() -> None:
    for name in ("libav.flv", "libav.aac"):
        logger = logging.getLogger(name)
        if not any(isinstance(f, LiveFlvQuiet) for f in logger.filters):
            logger.addFilter(LiveFlvQuiet())


class _Formatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        held = getattr(record, "sprig_held", 0)
        return f"{text} (and {held} more like it held back)" if held else text


def catch_unhandled() -> None:
    """Errors nobody caught (in a button's code, a background thread, Qt itself) go to the log too.
    Without this a windowed app loses them: there is no console to print them to."""
    import sys
    import threading

    errors = logging.getLogger("sprigcast.errors")
    previous = sys.excepthook

    def on_error(kind, value, tb):
        if issubclass(kind, KeyboardInterrupt):
            return previous(kind, value, tb)
        errors.error("unhandled error", exc_info=(kind, value, tb))
        if sys.stderr is not None and previous is not sys.__excepthook__:
            previous(kind, value, tb)

    def on_thread_error(args):
        if args.exc_type is SystemExit:
            return
        name = args.thread.name if args.thread is not None else "?"
        errors.error("unhandled error in thread %s", name,
                     exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    def on_unraisable(unraisable):
        errors.warning("ignored error %s: %s", unraisable.err_msg or "in", unraisable.exc_value or unraisable.exc_type)

    sys.excepthook = on_error
    threading.excepthook = on_thread_error
    sys.unraisablehook = on_unraisable
    try:
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler
    except Exception:
        return
    qt = logging.getLogger("sprigcast.qt")
    said: set[str] = set()

    def on_qt(kind, _context, message):
        if kind in (QtMsgType.QtDebugMsg, QtMsgType.QtInfoMsg):
            return
        if kind == QtMsgType.QtWarningMsg:  # each Qt warning once a run: some repeat every repaint
            if message in said or len(said) > 300:
                return
            said.add(message)
            qt.warning("%s", message)
        else:
            qt.error("%s", message)

    qInstallMessageHandler(on_qt)


def _version() -> str:
    try:
        from sprigcast import build_label

        return build_label()
    except Exception:
        return "?"


def pc_line() -> str:
    """CPU, threads, memory, OS: one line for the top of a report."""
    import platform

    parts = []
    try:
        from sprigcast.app.optimize import _cpu_name

        name = _cpu_name()
        if name:
            parts.append(name)
    except Exception:
        pass
    parts.append(f"{os.cpu_count() or '?'} threads")
    try:
        import psutil

        parts.append(f"{psutil.virtual_memory().total / 2**30:.0f} GB")
    except Exception:
        pass
    parts.append(f"{platform.system()} {platform.release()}")
    return ", ".join(parts)


def _encoder_path(st) -> str:
    direct = getattr(st, "_direct", None)
    if direct is not None:
        if getattr(direct, "shared", False) is True and not getattr(st, "_gpu_failed", False):
            return "SprigCast's own NVENC (frames handed over on the graphics card)"
        return "SprigCast's own NVENC"
    codec = getattr(st, "codec", "?")
    return f"FFmpeg {codec}"


class StreamReport:
    """Writes one stream's report file: the setup, a line per destination every few seconds, the end."""

    def __init__(self, session, collection=None, folder: Path | None = None, clock=time.monotonic,
                 now: datetime | None = None):
        self.session = session
        self.clock = clock
        self._last: dict[int, tuple[float, int, int]] = {}  # id(streamer) -> (time, bytes, frames)
        self._next = 0.0
        self._started = clock()
        self._file = None
        self._process = None  # kept: cpu_percent() measures since its last call on the same object
        try:
            import psutil

            self._process = psutil.Process()
            self._process.cpu_percent()
            psutil.cpu_percent()
        except Exception:
            pass
        folder = folder or log_dir()
        try:
            folder.mkdir(parents=True, exist_ok=True)
            self.path = folder / f"stream {(now or datetime.now()):%Y-%m-%d %H-%M-%S}.txt"
            self._file = open(self.path, "w", encoding="utf-8", buffering=1)  # noqa: SIM115 (open while live)
            self._start_writer()
        except OSError as exc:
            log.warning("no stream report (%s)", exc)
            self.path = None
            return
        self._header(collection)

    def _start_writer(self) -> None:
        """Lines are checked for private data and written on a thread of their own: on the window's thread, a slow
        disk or a busy PC held the window (PC 2026-11-21, a 1.5 s stall went through these writes)."""
        self._lines: queue.SimpleQueue = queue.SimpleQueue()
        self._writer = threading.Thread(target=self._write_lines, name="sprigcast-stream-report", daemon=True)
        self._writer.start()

    def _write(self, text: str) -> None:
        if self._file is None:
            return
        redactors = [r for _, st in self.session.streamers if (r := getattr(st, "redact", None))]
        self._lines.put((text, redactors))

    def _write_lines(self) -> None:
        file = self._file
        while True:
            got = self._lines.get()
            if got is None:
                break
            text, redactors = got
            try:
                for redact in redactors:
                    text = redact(text)
                text = privacy.scrub(text)
                file.write(text + "\n")
            except Exception:  # noqa: BLE001 (a line lost, never the report)
                pass
        try:
            file.close()
        except OSError:
            pass

    def _header(self, collection) -> None:
        from sprigcast.output.recorder import codec_label

        self._write(f"SprigCast {_version()} stream report, {datetime.now():%Y-%m-%d %H:%M:%S}")
        self._write(f"PC: {pc_line()}")
        shared = getattr(self.session, "shared", [])
        if collection is not None:
            enc = collection.stream_encoder
            self._write(f"Stream encoder settings: {enc.rate_control}, preset {enc.preset}, tuning {enc.tuning}, "
                        f"B-frames {enc.b_frames}, look-ahead {enc.lookahead}, multipass {enc.multipass}, "
                        f"keyframes every {enc.keyint_sec or 2} s, audio {enc.audio_kbps} kbps")
        self._write(f"{len(self.session.streamers)} destination(s), {len(shared)} shared encoder(s)")
        for canvas_id, st in self.session.streamers:
            sharing = " (sharing an encoder)" if hasattr(st, "shared") else ""
            auto = " auto-bitrate" if getattr(st, "autopilot", None) is not None else ""
            codec = getattr(st, "codec", None)
            self._write(f"  {st.name}: canvas {canvas_id}, {getattr(st, 'out_width', '?')}x"
                        f"{getattr(st, 'out_height', '?')} @ {getattr(st, 'fps', '?')} fps, "
                        f"{getattr(st, 'bitrate_kbps', '?')} kbps{auto}, "
                        f"{codec_label(codec) if codec else '?'}{sharing}")
        self._write("")
        self._write("time   destination: status, kbps sent (last 5 s / average), frames, dropped, backlog, "
                    "auto-pilot, encode ms, network wait")

    def tick(self, render_stats=None) -> None:
        """Call about once a second; writes a line per destination every EVERY_S seconds."""
        now = self.clock()
        if self._file is None or now < self._next:
            return
        self._next = now + EVERY_S
        t = now - self._started
        stamp = f"{int(t) // 60:3d}:{int(t) % 60:02d}"
        for _, st in self.session.streamers:
            last_t, last_b, last_f = self._last.get(id(st), (self._started, 0, 0))
            sent, frames = st.bytes_out, st.frames
            span = max(1e-6, now - last_t)
            kbps = (sent - last_b) * 8 / 1000 / span
            average = sent * 8 / 1000 / max(1e-6, now - self._started)
            self._last[id(st)] = (now, sent, frames)
            stats = st.stats() if hasattr(st, "stats") else {}
            pilot = getattr(st, "autopilot", None)
            pilot_text = f"{pilot.current_kbps}" if pilot is not None else "off"
            status = "live" if st.connected else getattr(st, "waiting_text", lambda: st.status)()
            self._write(f"{stamp}  {st.name}: {status}, {kbps:.0f} / {average:.0f} kbps, "
                        f"{frames} frames (+{frames - last_f}), {st.dropped} dropped, "
                        f"backlog {stats.get('backlog_s', 0.0):.2f} s, auto-pilot {pilot_text}, "
                        f"encode {stats.get('encode_ms', 0.0):.1f} ms, network {stats.get('network_pct', 0.0):.0f}%"
                        + (f" (sending {stats['sending_pct']:.0f}%)" if "sending_pct" in stats else ""))
        extra = []
        if render_stats is not None:
            extra.append(f"render {render_stats.fps:.0f} fps, {render_stats.render_ms:.1f} ms, "
                         f"{render_stats.dropped_ticks} late ticks")
        try:
            import psutil

            from sprigcast.app.resources import memory_text

            proc = self._process
            extra.append(f"SprigCast {memory_text(proc)}, "
                         f"CPU {proc.cpu_percent() / (os.cpu_count() or 1):.0f}% of the PC, "
                         f"PC CPU {psutil.cpu_percent():.0f}%")
        except Exception:
            pass
        if extra:
            self._write(f"{stamp}  " + "; ".join(extra))

    def finish(self, summaries: list[str]) -> None:
        self._write("")
        self._write(f"Ended after {self.clock() - self._started:.0f} s.")
        for line in summaries:
            self._write(line)
        if self._file is not None:
            self._lines.put(None)  # the writer closes the file once every line is written
            self._writer.join(5)
            self._file = None
