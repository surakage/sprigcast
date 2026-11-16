"""File → Export report…: one .zip a tester can send back when something goes wrong.

Inside:

* ``about.txt``: SprigCast's version, the PC (Windows build, CPU, GPU and driver, memory, screens) and
  the settings that matter (canvases, destinations, encoder, recording, sound, what the garden holds).
* ``logs/``: the day logs and stream reports of the last few days, and the end of ``crash.log``.

Nothing private leaves the PC: every file goes through `Scrubber`, which hides stream keys, the API
password and device keys, sign-in tokens, the user name in folder paths, the PC's name, e-mail
addresses and public IP addresses. Stream servers keep only their host. Chat messages are never
written to the logs in the first place.
"""

from __future__ import annotations

import io
import json
import logging
import os
import platform
import re
import sys
import zipfile
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

DAYS = 7  # how far back the report reaches
MAX_LOG_BYTES = 24 * 2**20  # newest first; older files are left out past this
CRASH_TAIL = 256 * 1024
HIDDEN = "[hidden]"

# Settings whose names say they hold a secret; their values are hidden and also scrubbed from every file.
SECRET_NAME = re.compile(r"password|token|secret|cookie|oauth|(?:^|[/_])key$|control/devices", re.I)
# Settings that name the person (their channel or account): shown only as set / not set.
PERSONAL_NAME = re.compile(r"channel|handle|username|account|email|login|chat/names|chat/link|docks/custom", re.I)


def default_name(now: datetime | None = None) -> str:
    return f"SprigCast report {(now or datetime.now()):%Y-%m-%d %H-%M}.zip"


def default_folder() -> Path:
    try:
        from PySide6.QtCore import QStandardPaths

        desktop = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DesktopLocation)
        if desktop:
            return Path(desktop)
    except Exception:
        pass
    return Path.home()


# Hiding what's private ---------------------------------------------------------------------------------


_URL = re.compile(r"\b(rtmps?|srt|rist|wss?|https?|udp|tcp)://([^/\s\"'<>?#,]+)([^\s\"'<>,)]*)", re.I)
_ASSIGN = re.compile(r"(?i)\b(stream[_ ]?key|key|token|access_token|refresh_token|password|passwd|secret|auth|"
                     r"sig|signature|oauth|streamid)(\s*[=:]\s*)([\"']?)([^\s\"'&,;(\[][^\s\"'&,;]*)")
_BEARER = re.compile(r"(?i)\b(bearer|oauth:)\s*[A-Za-z0-9._~+/=-]{8,}")
_KNOWN_KEYS = [
    re.compile(r"\blive_\d+_[A-Za-z0-9]{10,}"),  # Twitch stream key
    re.compile(r"\b[a-z0-9]{4}(?:-[a-z0-9]{4}){3,4}\b"),  # YouTube stream key
    re.compile(r"\bsk_[a-z]{2}-[A-Za-z0-9]{6,}"),  # Kick stream key
    re.compile(r"\bsc_local_[A-Za-z0-9_-]{8,}"),  # SprigCast's own API key for apps on this PC
]
_USER_FOLDER = re.compile(r"(?i)([A-Z]:[\\/]+(?:Users|Documents and Settings)[\\/]+)([^\\/\s\"'<>|:*?]+)")
_UNIX_HOME = re.compile(r"(/(?:home|Users)/)([^/\s\"']+)")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_IPV4 = re.compile(r"\b(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\b")


def _private_ip(parts: tuple[int, ...]) -> bool:
    a, b = parts[0], parts[1]
    return (a in (0, 10, 127) or (a == 172 and 16 <= b <= 31) or (a == 192 and b == 168)
            or (a == 169 and b == 254) or a >= 224)


class Scrubber:
    """Turns text into text that is safe to send: see the module docstring for what it hides."""

    def __init__(self, secrets=(), names=()):
        # Longest first, so a secret that contains another is hidden whole.
        self.secrets = sorted({s for s in secrets if s and len(s) >= 4}, key=len, reverse=True)
        self.names = []
        for name in names:  # the Windows user name, the PC's name
            if name and len(name) >= 3:
                self.names.append(re.compile(rf"(?<![\w]){re.escape(name)}(?![\w])", re.I))

    @classmethod
    def for_this_pc(cls, secrets=()) -> Scrubber:
        names = [os.environ.get("USERNAME", ""), os.environ.get("USER", ""), Path.home().name,
                 os.environ.get("COMPUTERNAME", ""), platform.node().split(".")[0]]
        return cls(secrets, [n for n in names if n.lower() not in {"user", "admin", "root", "public", "default"}])

    @staticmethod
    def _url(match: re.Match) -> str:
        scheme, host, rest = match.group(1), match.group(2), match.group(3)
        if "@" in host:  # user:password@host
            host = host.rsplit("@", 1)[1]
        return f"{scheme}://{host}" + ("/" + HIDDEN if rest.strip("/") else rest)

    @staticmethod
    def _ip(match: re.Match) -> str:
        parts = tuple(int(p) for p in match.groups())
        if any(p > 255 for p in parts) or _private_ip(parts):
            return match.group(0)
        return "[ip]"

    def __call__(self, text: str) -> str:
        for secret in self.secrets:
            text = text.replace(secret, HIDDEN)
        text = _URL.sub(self._url, text)
        text = _BEARER.sub(lambda m: f"{m.group(1)} {HIDDEN}", text)
        text = _ASSIGN.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}{HIDDEN}", text)
        for pattern in _KNOWN_KEYS:
            text = pattern.sub(HIDDEN, text)
        text = _USER_FOLDER.sub(r"\1you", text)
        text = _UNIX_HOME.sub(r"\1you", text)
        text = _EMAIL.sub("[email]", text)
        text = _IPV4.sub(self._ip, text)
        for name in self.names:
            text = name.sub("you", text)
        return text


def _leaves(value) -> list[str]:
    """Every string inside a JSON value (a saved device list holds its keys somewhere inside)."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _leaves(v)] + [k for k in value if len(k) >= 16]
    if isinstance(value, list):
        return [s for v in value for s in _leaves(v)]
    return []


def known_secrets(settings=None, collection=None) -> list[str]:
    """The actual secret values on this PC, so they can be hidden wherever they turn up: stream keys
    (from the Windows credential store), the API password and keys, paired devices, sign-ins."""
    found: list[str] = []
    if settings is not None:
        from sprigcast.control import secret_store

        for key in settings.allKeys():
            if not SECRET_NAME.search(key):
                continue
            value = settings.value(key)
            if not isinstance(value, str) or not value:
                continue
            try:
                value = secret_store.unprotect(value)
            except Exception:
                pass
            found.append(value)
            try:
                found += _leaves(json.loads(value))
            except (ValueError, TypeError):
                pass
    if collection is not None:
        from sprigcast.output.keys import get_key
        from sprigcast.output.twitch_auth import TOKEN_REF

        for ref in [o.key_ref for o in collection.outputs if o.key_ref] + [TOKEN_REF]:
            try:
                key = get_key(ref)
            except Exception:
                key = None
            if not key:
                continue
            found.append(key)
            try:
                found += _leaves(json.loads(key))  # the Twitch sign-in: access and refresh tokens
            except (ValueError, TypeError):
                pass
    return found


# About this PC -----------------------------------------------------------------------------------------


def _windows() -> str:
    if sys.platform != "win32":
        return f"{platform.system()} {platform.release()} ({platform.version()})"
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion") as k:
            def get(name, default=""):
                try:
                    return winreg.QueryValueEx(k, name)[0]
                except OSError:
                    return default

            build = int(get("CurrentBuildNumber", "0") or 0)
            product = str(get("ProductName", "Windows"))
            if build >= 22000:
                product = product.replace("Windows 10", "Windows 11")  # the registry still says 10
            return f"{product} {get('DisplayVersion', '')}, build {build}.{get('UBR', 0)}".replace("  ", " ")
    except OSError:
        return f"Windows {platform.version()}"


def _gpus() -> list[str]:
    if sys.platform != "win32":
        return []
    try:
        from sprigcast.output.enhanced import _gpus_windows

        return [f"{g['model']}, driver {g['driver_version']}, {g['dedicated_video_memory'] / 2**30:.0f} GB"
                for g in _gpus_windows()]
    except Exception as exc:
        return [f"? ({exc})"]


def _screens() -> list[str]:
    try:
        from PySide6.QtGui import QGuiApplication

        return [f"{s.size().width()}x{s.size().height()} @ {s.refreshRate():.0f} Hz, scale "
                f"{s.devicePixelRatio():g}" for s in QGuiApplication.screens()]
    except Exception:
        return []


def _libraries() -> str:
    parts = [f"Python {platform.python_version()}"]
    try:
        import PySide6

        parts.append(f"PySide6 {PySide6.__version__}")
    except Exception:
        pass
    try:
        import av

        parts.append(f"PyAV {av.__version__}, FFmpeg {av.library_versions.get('libavcodec', ('?',))}")
    except Exception:
        pass
    return ", ".join(parts)


def pc_lines() -> list[str]:
    from sprigcast.app.logbook import _version

    lines = [f"SprigCast {_version()}{'' if getattr(sys, 'frozen', False) else ' (run from source)'}",
             f"Windows: {_windows()}"]
    try:
        from sprigcast.app.optimize import _cpu_name

        lines.append(f"CPU: {_cpu_name() or '?'}, {os.cpu_count() or '?'} threads")
    except Exception:
        lines.append(f"CPU: {os.cpu_count() or '?'} threads")
    try:
        import psutil

        memory = psutil.virtual_memory()
        lines.append(f"Memory: {memory.total / 2**30:.0f} GB, {memory.available / 2**30:.1f} GB free now")
    except Exception:
        pass
    lines += [f"GPU: {g}" for g in _gpus()]
    lines += [f"Screen: {s}" for s in _screens()]
    lines.append(f"Libraries: {_libraries()}")
    try:
        from sprigcast.core.pacing import memory_line

        lines.append(f"SprigCast now: {memory_line()}")
    except Exception:
        pass
    return lines


# Settings that matter ----------------------------------------------------------------------------------


def _plain(value):
    return value if isinstance(value, (str, int, float, bool)) or value is None else str(value)


def collection_lines(collection) -> list[str]:
    """Canvases, destinations, encoder, recording, sound and what the garden holds (counts, not names)."""
    lines = ["Canvases:"]
    for c in collection.canvases:
        out = "x".join(map(str, c.output_size())) if hasattr(c, "output_size") else "?"
        lines.append(f"  {c.id}: {c.width}x{c.height} @ {c.fps} fps, sent at {out} ({c.downscale}),"
                     f" {'on' if c.enabled else 'off'}")
    lines.append("Destinations:")
    for o in collection.outputs:
        if o.type != "stream":
            continue
        size = "x".join(map(str, o.size)) if o.size else "canvas size"
        flags = [name for name in ("enabled", "auto_bitrate", "enhanced", "enhanced_vertical", "bandwidth_test",
                                   "twitch_account", "ignore_recommendations") if getattr(o, name, False)]
        server = f", server {Scrubber()(o.server)}" if o.server else ""
        lines.append(f"  {o.service or 'custom'} on {o.canvas}: {o.bitrate_kbps} kbps, {size}, "
                     f"{o.fps or 'canvas'} fps, key {'saved' if o.key_ref else 'none'}{server}"
                     f"{', ' + ', '.join(flags) if flags else ''}")
    if not any(o.type == "stream" for o in collection.outputs):
        lines.append("  none")
    for title, model in (("Stream encoder", collection.stream_encoder), ("Recording", collection.recording),
                         ("Sound", collection.audio)):
        data = model.model_dump()
        if title == "Recording":
            data["folder"] = "custom" if data.get("folder") else "default"
        lines.append(f"{title}: " + ", ".join(f"{k} {_plain(v)}" for k, v in data.items()))
    transition = getattr(collection, "transition", None)
    if transition is not None:
        lines.append(f"Transition: {getattr(transition, 'kind', None) or getattr(transition, 'type', '?')}, "
                     f"{len(getattr(collection, 'transition_rules', []))} rule(s)")
    blooms = [s for s in collection.scenes if getattr(s, "kind", "scene") != "part"]
    sprigs = len(collection.scenes) - len(blooms)
    lines.append(f"Garden: {len(blooms)} blooms, {sprigs} sprigs, {len(collection.sources)} sources")
    kinds = Counter(s.kind for s in collection.sources)
    lines.append("  sources: " + ", ".join(f"{k} {n}" for k, n in sorted(kinds.items())))
    filters = Counter(f.kind if hasattr(f, "kind") else getattr(f, "type", "?")
                      for s in collection.sources for f in s.filters)
    if filters:
        lines.append("  filters: " + ", ".join(f"{k} {n}" for k, n in sorted(filters.items())))
    return lines


_SKIP_SETTING = re.compile(r"geometry|window_state|/state$|splitter|recent|last_dir|folder$", re.I)


def settings_lines(settings) -> list[str]:
    """Everything SprigCast keeps in its settings that is a plain value: secrets hidden, the person's
    channels and accounts shown only as set or not, window sizes and folders left out."""
    lines = []
    for key in sorted(settings.allKeys()):
        if _SKIP_SETTING.search(key):
            continue
        value = settings.value(key)
        if isinstance(value, (bytes, bytearray)) or type(value).__name__ in ("QByteArray", "QPoint", "QSize",
                                                                            "QRect", "QColor"):
            continue
        if SECRET_NAME.search(key) or PERSONAL_NAME.search(key):
            shown = "(set)" if value not in (None, "", []) else "(not set)"
        else:
            shown = str(value)
            if len(shown) > 160:
                shown = shown[:157] + "…"
        lines.append(f"  {key} = {shown}")
    return lines


def plugin_lines(plugins_dir: Path | None) -> list[str]:
    if plugins_dir is None or not plugins_dir.is_dir():
        return []
    names = sorted(p.stem for p in plugins_dir.glob("*.py") if not p.name.startswith("_"))
    return [f"Plugins: {', '.join(names) if names else 'none'}"]


# The report --------------------------------------------------------------------------------------------


def _log_files(folder: Path, now: datetime, days: int) -> list[Path]:
    """Day logs and stream reports from the last `days` days, newest first, within MAX_LOG_BYTES."""
    cutoff = (now - timedelta(days=days)).timestamp()
    files = []
    for path in [*folder.glob("sprigcast *.log"), *folder.glob("stream *.txt")]:
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_mtime >= cutoff:
            files.append((stat.st_mtime, stat.st_size, path))
    files.sort(reverse=True)
    picked, total = [], 0
    for _, size, path in files:
        if total + size > MAX_LOG_BYTES and picked:
            continue
        picked.append(path)
        total += size
    return picked


def _read(path: Path, tail: int | None = None) -> str:
    with path.open("rb") as f:
        if tail is not None:
            f.seek(0, io.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - tail))
        data = f.read()
    return data.decode("utf-8", errors="replace")


def build(target: Path, *, log_folder: Path | None = None, collection=None, settings=None,
          plugins_dir: Path | None = None, scrub: Scrubber | None = None, now: datetime | None = None,
          days: int = DAYS, about: list[str] | None = None) -> Path:
    """Write the report .zip to `target` and return it. Raises OSError if it can't be written."""
    from sprigcast.app.logbook import log_dir

    now = now or datetime.now()
    folder = log_folder or log_dir()
    if scrub is None:
        scrub = Scrubber.for_this_pc(known_secrets(settings, collection))
    sections = [f"SprigCast report, {now:%Y-%m-%d %H:%M}", ""]
    sections += about if about is not None else pc_lines()
    sections += plugin_lines(plugins_dir)
    if collection is not None:
        sections += ["", *collection_lines(collection)]
    if settings is not None:
        sections += ["", "Settings:", *settings_lines(settings)]
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".part")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        z.writestr("about.txt", scrub("\n".join(sections) + "\n"))
        if folder.is_dir():
            for path in _log_files(folder, now, days):
                try:
                    z.writestr(f"logs/{path.name}", scrub(_read(path)))
                except OSError as exc:
                    log.info("report: skipped %s (%s)", path.name, exc)
            crash = folder / "crash.log"
            if crash.is_file():
                try:
                    z.writestr("logs/crash.log", scrub(_read(crash, CRASH_TAIL)))
                except OSError:
                    pass
    os.replace(tmp, target)
    return target
