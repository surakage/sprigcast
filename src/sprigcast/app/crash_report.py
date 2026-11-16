"""Finding out why SprigCast closed unexpectedly, and saying so on the next launch.

While SprigCast runs, ``running.json`` sits in its settings folder; a clean exit removes it. If it's still
there at start, the last run ended unexpectedly. What it left behind is gathered:

* ``crash.log`` (faulthandler): each run starts a section headed with its process id; a native crash
  adds "Windows fatal exception: access violation" and the Python stack at that moment.
* Windows' own record (Event Viewer → Application, "Application Error" 1000): the faulting module
  (``nvcuda64.dll``, ``avcodec-61.dll`` ...) and exception code, matched by process id.

The finding is written to crash.log and today's log, and the window asks what happened and offers to send
a report (app/problem_report.py).
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

MARK = "running.json"
SECTION = "=== SprigCast run"
CODES = {
    "0xc0000005": "access violation",
    "0xc0000409": "stack buffer overrun",
    "0xc0000374": "heap corruption",
    "0xc00000fd": "stack overflow",
    "0x80000003": "breakpoint",
    "0xc000001d": "illegal instruction",
    "0xe06d7363": "C++ exception",
}


@dataclass
class Crash:
    started: str = ""  # when that run started (ISO)
    cause: str = ""  # "access violation"
    module: str = ""  # "nvcuda64.dll"
    where: str = ""  # the Python line it was on: 'File "...\\render.py", line 120 in draw'

    def summary(self) -> str:
        """One plain line for people: "access violation in nvcuda64.dll", or what's known."""
        if self.cause and self.module:
            return f"{self.cause.capitalize()} in {self.module}"
        if self.module:
            return f"A crash in {self.module}"
        if self.cause:
            return self.cause[0].upper() + self.cause[1:]
        return "No error was recorded. It may have been ended from Task Manager, or the PC turned off"


def section_header(pid: int | None = None, now: datetime | None = None, version: str = "") -> str:
    return f"\n{SECTION} {version} pid {pid or os.getpid()} started {(now or datetime.now()):%Y-%m-%d %H:%M:%S}\n"


def read_crash_log(text: str, pid: int) -> Crash:
    """What crash.log says about the run with this process id."""
    found = Crash()
    head = re.compile(rf"^{re.escape(SECTION)}.* pid {pid} started", re.M)
    match = None
    for match in head.finditer(text):
        pass
    if match is None:
        return found
    rest = text[match.end():]
    nxt = re.search(rf"^{re.escape(SECTION)}", rest, re.M)
    body = rest[: nxt.start()] if nxt else rest
    fatal = re.search(r"^(?:Windows fatal exception|Fatal Python error): (.+)$", body, re.M)
    if fatal:
        found.cause = fatal.group(1).strip()
        frames = re.findall(r'^\s+File "([^"]+)", line (\d+) in (\S+)', body[fatal.end():], re.M)
        if frames:  # faulthandler lists the most recent call first
            path, line, func = frames[0]
            name = re.split(r"[\\/]", path)[-1]  # a Windows path, read anywhere
            found.where = f"{name}, line {line} in {func}"
    return found


def read_windows_events(text: str, pid: int) -> Crash:
    """Windows' "Application Error" records (wevtutil text): the faulting module for this process id."""
    found = Crash()
    for block in re.split(r"^Event\[\d+\]", text, flags=re.M):
        pid_match = re.search(r"Faulting process id:\s*(0x[0-9a-fA-F]+|\d+)", block)
        if not pid_match or int(pid_match.group(1), 0) != pid:
            continue
        module = re.search(r"Faulting module name:\s*([^,\r\n]+)", block)
        code = re.search(r"Exception code:\s*(0x[0-9a-fA-F]+)", block)
        if module:
            found.module = module.group(1).strip()
        if code:
            found.cause = CODES.get(code.group(1).lower(), f"error {code.group(1).lower()}")
        break
    return found


def _windows_events() -> str:
    if sys.platform != "win32":
        return ""
    query = "*[System[Provider[@Name='Application Error'] and (EventID=1000)]]"
    try:
        return subprocess.run(
            ["wevtutil", "qe", "Application", f"/q:{query}", "/c:10", "/rd:true", "/f:text"],
            capture_output=True, text=True, timeout=8, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def check(folder: Path, crash_log: Path | None, events=_windows_events) -> Crash | None:
    """At start: if the last run didn't close cleanly, what's known about why (and note it in crash.log).
    Then mark this run as running. Returns None after a clean exit."""
    mark = folder / MARK
    found = None
    try:
        last = json.loads(mark.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        last = None
    if isinstance(last, dict) and int(last.get("pid", 0)) and int(last["pid"]) != os.getpid():
        pid = int(last["pid"])
        found = Crash(started=str(last.get("started", "")))
        try:
            text = crash_log.read_text(encoding="utf-8", errors="replace") if crash_log else ""
        except OSError:
            text = ""
        from_log = read_crash_log(text, pid)
        found.cause, found.where = from_log.cause, from_log.where
        from_windows = read_windows_events(events(), pid)
        found.module = from_windows.module
        found.cause = found.cause or from_windows.cause
        line = f"Last run (pid {pid}, started {found.started}) closed unexpectedly: {found.summary()}"
        if found.where:
            line += f" (at {found.where})"
        log.warning(line)
        if crash_log is not None:
            try:
                with crash_log.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass
    try:
        folder.mkdir(parents=True, exist_ok=True)
        mark.write_text(json.dumps({"pid": os.getpid(), "started": time.strftime("%Y-%m-%d %H:%M:%S")}),
                        encoding="utf-8")
    except OSError as exc:
        log.warning("could not mark the run: %s", exc)
    return found


def closed_cleanly(folder: Path) -> None:
    try:
        (folder / MARK).unlink()
    except OSError:
        pass
