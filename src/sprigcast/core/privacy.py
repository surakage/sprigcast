"""Nothing personal in a log: one scrubber for every line SprigCast writes about itself.

Every log handler (the day log, the console, the stderr file of a windowed start), every stream report and
crash.log go through `scrub`, which hides:

* stream keys, sign-in tokens, the API password and paired-device keys: the actual values this run has seen
  (`remember_secret`, called where they're read) and anything shaped like one (Twitch, YouTube, Kick keys,
  ``key=``/``token=`` pairs, ``Bearer``/``oauth:`` tokens);
* everything after the host in a stream, web or socket address (``rtmp://host/[hidden]``), so a key or
  token in a path or query never gets through, whatever the error message looks like;
* the Windows user name in folder paths, the user and PC name anywhere, e-mail addresses, public IP addresses,
  device instance paths (they hold serial numbers);
* channel and account names this run has seen (`remember_name`: chat channels, signed-in accounts).

File → Export report and Help → Report a problem use the same `Scrubber`, with the secrets saved on the PC added.
"""

from __future__ import annotations

import logging
import os
import platform
import re
import threading
import urllib.parse
from pathlib import Path

HIDDEN = "[hidden]"

_URL = re.compile(r"\b(rtmps?|srt|rist|wss?|https?|udp|tcp)://([^/\s\"'<>?#,]+)([^\s\"'<>,)]*)", re.I)
_ASSIGN = re.compile(r"(?i)\b(stream[_ ]?key|key|token|access_token|refresh_token|password|passwd|secret|auth|"
                     r"sig|signature|oauth|streamid)(\s*[=:]\s*)([\"']?)([^\s\"'&,;(\[][^\s\"'&,;]*)")
_BEARER = re.compile(r"(?i)\b(bearer|oauth:)\s*[A-Za-z0-9._~+/=-]{8,}")
_KNOWN_KEYS = [
    re.compile(r"\blive_\d+_[A-Za-z0-9]{10,}"),  # Twitch stream key
    re.compile(r"\bv2_[A-Za-z0-9_.~+/=?*-]{20,}"),  # Twitch Enhanced Broadcasting's sign-in for one stream
    re.compile(r"\b[a-z0-9]{4}(?:-[a-z0-9]{4}){3,4}\b"),  # YouTube stream key
    re.compile(r"\bsk_[a-z]{2}-[A-Za-z0-9]{6,}"),  # Kick stream key
    re.compile(r"\bsc_local_[A-Za-z0-9_-]{8,}"),  # SprigCast's own API key for apps on this PC
]
_USER_FOLDER = re.compile(r"(?i)([A-Z]:[\\/]+(?:Users|Documents and Settings)[\\/]+)([^\\/\s\"'<>|:*?]+)")
_UNIX_HOME = re.compile(r"(/(?:home|Users)/)([^/\s\"']+)")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_IPV4 = re.compile(r"\b(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\b")
# A device's instance path ("@device_pnp_\\?\usb#vid_0fd9&pid_0078&mi_00#7&2c3e9b1&0&0000#{...}\global"): the part
# after the vendor and product ids is the device's serial number or where it's plugged in.
_DEVICE_PATH = re.compile(r"(?i)(\\\\\?\\[a-z]+#(?:vid_[0-9a-f]{4}&pid_[0-9a-f]{4}(?:&mi_[0-9a-f]{2})?)?)[^\s\]\"']*")
_COMMON_NAMES = {"user", "admin", "administrator", "root", "public", "default", "owner", "guest"}


def _private_ip(parts: tuple[int, ...]) -> bool:
    a, b = parts[0], parts[1]
    return (a in (0, 10, 127) or (a == 172 and 16 <= b <= 31) or (a == 192 and b == 168)
            or (a == 169 and b == 254) or a >= 224)


def _word(name: str) -> re.Pattern:
    return re.compile(rf"(?<![\w]){re.escape(name)}(?![\w])", re.I)


class Scrubber:
    """Turns text into text that is safe to keep or send: see the module docstring for what it hides."""

    def __init__(self, secrets=(), names=(), personal=()):
        # Longest first, so a secret that contains another is hidden whole.
        self.secrets = sorted({s for s in secrets if s and len(s) >= 4}, key=len, reverse=True)
        self.names = [_word(n) for n in names if n and len(n) >= 3]  # the Windows user name, the PC's name
        self.personal = [_word(n) for n in sorted({n for n in personal if n and len(n) >= 3}, key=len, reverse=True)]

    @classmethod
    def for_this_pc(cls, secrets=()) -> Scrubber:
        """With the user and PC name, and the secrets and channel names this run has seen."""
        seen, personal = remembered()
        return cls([*secrets, *seen], pc_names(), personal)

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
            if secret in text:
                text = text.replace(secret, HIDDEN)
        text = _URL.sub(self._url, text)
        text = _BEARER.sub(lambda m: f"{m.group(1)} {HIDDEN}", text)
        text = _ASSIGN.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}{HIDDEN}", text)
        for pattern in _KNOWN_KEYS:
            text = pattern.sub(HIDDEN, text)
        text = _DEVICE_PATH.sub(lambda m: m.group(1) + "#[device]", text)
        text = _USER_FOLDER.sub(r"\1you", text)
        text = _UNIX_HOME.sub(r"\1you", text)
        text = _EMAIL.sub("[email]", text)
        text = _IPV4.sub(self._ip, text)
        for name in self.personal:
            text = name.sub("[name]", text)
        for name in self.names:
            text = name.sub("you", text)
        return text


def pc_names() -> list[str]:
    """The Windows user name and the PC's name (common ones like "User" are left alone: they'd hide words)."""
    found = [os.environ.get("USERNAME", ""), os.environ.get("USER", ""), Path.home().name,
             os.environ.get("COMPUTERNAME", ""), platform.node().split(".")[0]]
    return list(dict.fromkeys(n for n in found if n and n.lower() not in _COMMON_NAMES))


# What this run has seen -----------------------------------------------------------------------------------

_lock = threading.Lock()
_secrets: set[str] = set()
_personal: set[str] = set()
_scrubber: Scrubber | None = None


def _forms(value: str) -> set[str]:
    """A secret as it can turn up in text: as it is, URL-quoted, and each value inside it (around "?", "&", "=")."""
    forms = {value, urllib.parse.quote(value, safe=""), urllib.parse.quote(value)}
    for part in re.split(r"[?&=]", value):
        if len(part) >= 20 or (len(part) >= 12 and any(c.isdigit() for c in part)):
            forms.add(part)
    return {f for f in forms if len(f) >= 4}


_SECRET_FIELD = re.compile(r"(?i)key|token|secret|password|auth|code")


def _secret_fields(value, found: list[str]) -> None:
    """The values in a JSON object whose names say they're secret (a sign-in's tokens, a device's key)."""
    if isinstance(value, dict):
        for name, inner in value.items():
            if isinstance(inner, str) and _SECRET_FIELD.search(str(name)):
                found.append(inner)
            else:
                _secret_fields(inner, found)
    elif isinstance(value, list):
        for inner in value:
            _secret_fields(inner, found)


def remember_secret(*values) -> None:
    """A key, token or password this run has read: hidden from every log line from now on. A JSON value (a
    sign-in, a list of paired devices) hides its secret fields, not the whole text only."""
    global _scrubber
    new = set()
    for value in values:
        if not isinstance(value, str) or len(value) < 4:
            continue
        new |= _forms(value)
        if value.lstrip()[:1] in ("{", "["):
            import json

            inner: list[str] = []
            try:
                _secret_fields(json.loads(value), inner)
            except ValueError:
                pass
            for item in inner:
                if len(item) >= 8:
                    new |= _forms(item)
    with _lock:
        if new - _secrets:
            _secrets.update(new)
            _scrubber = None


def remember_name(*names) -> None:
    """A channel or account name (chat, a sign-in): shown as [name] in logs from now on."""
    global _scrubber
    new = set()
    for name in names:
        if isinstance(name, str):
            name = name.strip()
            for form in (name, name.lstrip("@")):
                if len(form) >= 3 and form.lower() not in _COMMON_NAMES:
                    new.add(form)
    with _lock:
        if new - _personal:
            _personal.update(new)
            _scrubber = None


def mask(text: str, secrets=()) -> str:
    """For text shown on screen too (a stream's status): each of `secrets` in any form, and everything after the
    host of a stream, web or socket address, becomes ****. The log adds `scrub` on top."""
    if not text:
        return text

    def url(match: re.Match) -> str:
        host = match.group(2).rsplit("@", 1)[-1]
        rest = match.group(3)
        return f"{match.group(1)}://{host}" + ("/****" if rest.strip("/").strip("*") else rest)

    text = _URL.sub(url, text)
    for secret in sorted({f for s in secrets if s for f in _forms(s)}, key=len, reverse=True):
        if secret in text:
            text = text.replace(secret, "****")
    return text


def remembered() -> tuple[list[str], list[str]]:
    """The secrets and names this run has seen (for a report's scrubber)."""
    with _lock:
        return list(_secrets), list(_personal)


def forget_all() -> None:
    """Tests: start over."""
    global _scrubber
    with _lock:
        _secrets.clear()
        _personal.clear()
        _scrubber = None


def scrubber() -> Scrubber:
    global _scrubber
    found = _scrubber
    if found is None:
        with _lock:
            found = _scrubber = Scrubber(_secrets, pc_names(), _personal)
    return found


def scrub(text: str) -> str:
    """`text` without anything personal (see the module docstring)."""
    if not text:
        return text
    try:
        return scrubber()(text)
    except Exception:  # never lose a line to the scrubber, and never write it unscrubbed either
        return "[a line that couldn't be checked for private data was left out]"


# Where it's applied -----------------------------------------------------------------------------------------


class ScrubbingFormatter(logging.Formatter):
    """A formatter whose whole output (message, arguments, traceback) goes through `scrub`."""

    def __init__(self, inner: logging.Formatter | None = None):
        super().__init__()
        self.inner = inner or logging.Formatter()

    def format(self, record: logging.LogRecord) -> str:
        return scrub(self.inner.format(record))

    def formatTime(self, record, datefmt=None):  # noqa: N802 (logging's name)
        return self.inner.formatTime(record, datefmt)


def guard_handler(handler: logging.Handler) -> None:
    """Scrub everything `handler` writes (once; its own formatter still decides the layout)."""
    current = handler.formatter
    if isinstance(current, ScrubbingFormatter):
        return
    handler.setFormatter(ScrubbingFormatter(current))


def guard_root() -> None:
    """Every handler on the root logger, and the console handler logging falls back on."""
    for handler in logging.getLogger().handlers:
        guard_handler(handler)
    if logging.lastResort is not None:
        guard_handler(logging.lastResort)


class ScrubbingStream:
    """A text stream (sys.stderr for a windowed start) that scrubs each line before it reaches the file."""

    def __init__(self, stream):
        self._stream = stream
        self._pending = ""

    def write(self, text: str) -> int:
        self._pending += text
        if "\n" in self._pending:
            done, _, self._pending = self._pending.rpartition("\n")
            self._stream.write(scrub(done) + "\n")
        return len(text)

    def flush(self) -> None:
        if self._pending:
            self._stream.write(scrub(self._pending))
            self._pending = ""
        self._stream.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


SWEEP_NOTE = ".privacy-sweep"  # name -> bytes already checked, so each start only reads what's new
_sweep_lock = threading.Lock()


def log_files(folder: Path) -> list[Path]:
    """The files SprigCast writes in the Logs folder."""
    return [*folder.glob("crash.log"), *folder.glob("sprigcast *.log"), *folder.glob("stream *.txt")]


def _note(folder: Path) -> dict:
    import json

    try:
        done = json.loads((folder / SWEEP_NOTE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return done if isinstance(done, dict) else {}


def _scrub_slowly(text: str, pause: float) -> str:
    """`scrub` a piece at a time (whole lines), resting between pieces so a big file doesn't hold up the window."""
    if not pause or len(text) <= 65536:
        return scrub(text)
    import time

    out, at = [], 0
    while at < len(text):
        end = text.find("\n", at + 65536)
        end = len(text) if end < 0 else end + 1
        out.append(scrub(text[at:end]))
        at = end
        time.sleep(pause)
    return "".join(out)


def sweep(folder: Path, paths=None, pause: float = 0.0) -> int:
    """Scrub what reached the logs in `folder` without going through Python's logging: the stacks faulthandler
    writes on a crash or a slow exit (they hold the user name in folder paths), and anything an older SprigCast
    wrote. Only bytes added since the last sweep are read. `paths` (default: all of SprigCast's log files) must
    not be written to meanwhile. Returns how many files changed."""
    import json

    paths = log_files(folder) if paths is None else list(paths)
    with _sweep_lock:
        done = _note(folder)
    changed = 0
    seen: dict[str, int] = {}
    for path in paths:
        try:
            size = path.stat().st_size
            start = int(done.get(path.name, 0) or 0)
            if start > size:
                start = 0  # the file started over
            if start < size:
                with open(path, "rb") as f:
                    head = f.read(start)
                    tail = f.read()
                text = tail.decode("utf-8", "replace")
                clean = _scrub_slowly(text, pause)
                if clean != text:
                    data = head + clean.encode("utf-8")
                    with open(path, "wb") as f:
                        f.write(data)
                    size = len(data)
                    changed += 1
            seen[path.name] = size
        except (OSError, ValueError):
            continue
    with _sweep_lock:
        done = _note(folder)
        done.update(seen)
        done = {name: n for name, n in done.items() if (folder / name).exists()}
        try:
            (folder / SWEEP_NOTE).write_text(json.dumps(done), encoding="utf-8")
        except OSError:
            pass
    return changed
