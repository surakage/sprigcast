"""Say so when the PC itself is busy, and name the other apps keeping it busy.

On the user's PC (2026-11-15) the CPU sat at 55-81% with two copies of OBS, Meld Studio, Camera Hub and
Streamfog open, and a recording dropped 3,489 frames in bursts that looked like SprigCast's fault.
When the whole PC's CPU or graphics card stays above BUSY_PCT for BUSY_FOR_S, SprigCast names the
heaviest other apps once (then at most every WARN_EVERY_S).
"""

from __future__ import annotations

import os
import time

BUSY_PCT = 80.0
BUSY_FOR_S = 60.0
WARN_EVERY_S = 600.0

# Process names (lower case) of apps people stream with, by the name they know them as.
KNOWN = {
    "obs64.exe": "OBS", "obs32.exe": "OBS", "obs": "OBS",
    "meld studio.exe": "Meld Studio", "meldstudio.exe": "Meld Studio", "meld.exe": "Meld Studio",
    "streamlabs obs.exe": "Streamlabs", "streamlabs desktop.exe": "Streamlabs",
    "camera hub.exe": "Camera Hub", "elgatocamerahub.exe": "Camera Hub",
    "streamfog.exe": "Streamfog",
    "tiktok live studio.exe": "TikTok LIVE Studio", "tiktoklivestudio.exe": "TikTok LIVE Studio",
    "xsplit.core.exe": "XSplit", "twitch studio.exe": "Twitch Studio",
    "nvidia broadcast.exe": "NVIDIA Broadcast", "wavelink.exe": "Wave Link",
}
IGNORE = {"system idle process", "idle", "system", "sprigcast.exe", "python.exe", "pythonw.exe"}


class BusyWatch:
    """feed() a reading every few seconds; it says when a warning is due."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._since: float | None = None
        self._warned = float("-inf")

    def feed(self, cpu: float | None, gpu: float | None) -> bool:
        now = self._clock()
        if max(cpu or 0.0, gpu or 0.0) < BUSY_PCT:
            self._since = None
            return False
        if self._since is None:
            self._since = now
        if now - self._since < BUSY_FOR_S or now - self._warned < WARN_EVERY_S:
            return False
        self._warned = now
        return True


def friendly(name: str) -> str:
    key = name.lower()
    return KNOWN.get(key) or (name[:-4] if key.endswith(".exe") else name)


def heavy_apps(limit: int = 3, wait: float = 1.0) -> list[str]:
    """The other apps using the most CPU right now, by the names people know (takes `wait` seconds)."""
    try:
        import psutil
    except ImportError:
        return []
    me = os.getpid()
    procs = []
    for proc in psutil.process_iter(["name"]):
        try:
            if proc.pid != me:
                proc.cpu_percent()  # starts the measurement
                procs.append(proc)
        except (psutil.Error, OSError):
            continue
    time.sleep(wait)
    use: dict[str, float] = {}
    for proc in procs:
        try:
            name = proc.info.get("name") or ""
            if name.lower() in IGNORE:
                continue
            label = friendly(name)
            use[label] = use.get(label, 0.0) + proc.cpu_percent()
        except (psutil.Error, OSError):
            continue
    cores = psutil.cpu_count() or 1
    ranked = sorted(use.items(), key=lambda kv: kv[1], reverse=True)
    known = set(KNOWN.values())
    # Streaming apps first when they are busy at all, then whatever else is heaviest.
    heavy = [n for n, pct in ranked if pct / cores >= 2.0]
    heavy.sort(key=lambda n: (n not in known, -use[n]))
    return heavy[:limit]


def busy_message(apps: list[str], cpu: float | None, gpu: float | None) -> str:
    parts = []
    if cpu is not None and cpu >= BUSY_PCT:
        parts.append(f"CPU {cpu:.0f}%")
    if gpu is not None and gpu >= BUSY_PCT:
        parts.append(f"graphics card {gpu:.0f}%")
    load = " and ".join(parts) or "busy"
    if not apps:
        return f"Your PC is busy ({load}). Dropped frames now are from the PC being busy, not from SprigCast."
    names = apps[0] if len(apps) == 1 else ", ".join(apps[:-1]) + " and " + apps[-1]
    verb = "is" if len(apps) == 1 else "are"
    return (f"Your PC is busy ({load}): {names} {verb} using it too. "
            "Closing them helps; dropped frames now come from that, not from SprigCast.")
