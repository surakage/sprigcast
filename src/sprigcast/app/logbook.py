"""The logbook: everything worth checking later, in one folder (Documents\\SprigCast\\Logs).

* ``sprigcast YYYY-MM-DD.log``: every message SprigCast logs that day (encoder choices, stream
  summaries, the Optimize wizard's findings, warnings and errors). Kept for 14 days.
* ``stream YYYY-MM-DD HH-MM-SS.txt``: one per stream. What went live and how (destinations, sizes,
  bitrates, encoder path, shared encoders, the PC), then every 5 seconds a line per destination
  (bitrate actually sent, frames, drops, upload backlog, auto-pilot), then the end-of-stream summary.
* ``crash.log``: where a native library crashed, if one ever does.

SPRIGCAST_LOG_DIR puts the folder somewhere else. Stream keys never reach these files.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

KEEP_DAYS = 14
EVERY_S = 5.0
FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_handler: logging.Handler | None = None
_crash_file = None


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


def _prune(folder: Path, now: datetime) -> None:
    cutoff = (now - timedelta(days=KEEP_DAYS)).timestamp()
    for path in folder.glob("*"):
        if path.suffix in (".log", ".txt") and path.name != "crash.log":
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                pass


def setup(now: datetime | None = None) -> Path | None:
    """Send every log message to today's file too (call once at start). Returns the file, or None."""
    global _handler, _crash_file
    now = now or datetime.now()
    folder = log_dir()
    try:
        folder.mkdir(parents=True, exist_ok=True)
        _prune(folder, now)
        path = folder / f"sprigcast {now:%Y-%m-%d}.log"
        handler = logging.FileHandler(path, encoding="utf-8")
    except OSError as exc:
        log.warning("no log file (%s)", exc)
        return None
    handler.setFormatter(logging.Formatter(FORMAT))
    handler.setLevel(logging.INFO)
    root = logging.getLogger()
    if _handler is not None:
        root.removeHandler(_handler)
        _handler.close()
    root.addHandler(handler)
    if root.level > logging.INFO or root.level == logging.NOTSET:
        root.setLevel(logging.INFO)
    _handler = handler
    try:
        import faulthandler

        if _crash_file is None:
            _crash_file = open(folder / "crash.log", "a", encoding="utf-8")  # noqa: SIM115 (open for the run)
            faulthandler.enable(file=_crash_file)
    except (OSError, RuntimeError):
        pass
    logging.getLogger("sprigcast").info("SprigCast %s started; logs in %s", _version(), folder)
    return path


def _version() -> str:
    try:
        from sprigcast import __version__

        return __version__
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
    if getattr(st, "_direct", None) is not None:
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
        folder = folder or log_dir()
        try:
            folder.mkdir(parents=True, exist_ok=True)
            self.path = folder / f"stream {(now or datetime.now()):%Y-%m-%d %H-%M-%S}.txt"
            self._file = open(self.path, "w", encoding="utf-8", buffering=1)  # noqa: SIM115 (open while live)
        except OSError as exc:
            log.warning("no stream report (%s)", exc)
            self.path = None
            return
        self._header(collection)

    def _write(self, text: str) -> None:
        if self._file is None:
            return
        for st in [st for _, st in self.session.streamers]:
            redact = getattr(st, "redact", None)
            if redact:
                text = redact(text)
        try:
            self._file.write(text + "\n")
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
            status = "live" if st.connected else st.status
            self._write(f"{stamp}  {st.name}: {status}, {kbps:.0f} / {average:.0f} kbps, "
                        f"{frames} frames (+{frames - last_f}), {st.dropped} dropped, "
                        f"backlog {stats.get('backlog_s', 0.0):.2f} s, auto-pilot {pilot_text}, "
                        f"encode {stats.get('encode_ms', 0.0):.1f} ms, network {stats.get('network_pct', 0.0):.0f}%")
        extra = []
        if render_stats is not None:
            extra.append(f"render {render_stats.fps:.0f} fps, {render_stats.render_ms:.1f} ms, "
                         f"{render_stats.dropped_ticks} late ticks")
        try:
            import psutil

            proc = psutil.Process()
            extra.append(f"SprigCast {proc.memory_info().rss / 2**20:.0f} MB, "
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
            try:
                self._file.close()
            except OSError:
                pass
            self._file = None
