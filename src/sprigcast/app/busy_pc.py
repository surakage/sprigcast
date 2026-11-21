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
# For the log only: the CPU pinned for a few seconds, and who had it. On the user's PC (live, 2026-11-21) the CPU
# sat at 100% for three minutes with SprigCast at 15%; viewers heard choppy voice and the log couldn't say why.
SPIKE_PCT = 90.0
SPIKE_FOR_S = 5.0
SPIKE_EVERY_S = 60.0

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
# Work Windows does by itself that can wait until after a stream, by the name people know it as, and what to do
# about it. On the user's PC (live, 2026-11-21) a Defender scan held the CPU at 100% and the voice went choppy.
BACKGROUND = {
    "msmpeng.exe": "Windows Defender", "mpcmdrun.exe": "Windows Defender", "mrt.exe": "Windows Defender",
    "tiworker.exe": "Windows Update", "trustedinstaller.exe": "Windows Update", "wuauclt.exe": "Windows Update",
    "musnotification.exe": "Windows Update", "usoclient.exe": "Windows Update",
    "searchindexer.exe": "Windows Search", "searchprotocolhost.exe": "Windows Search",
    "searchfilterhost.exe": "Windows Search",
}
ADVICE = {
    "Windows Defender": "schedule its scans outside stream hours",
    "Windows Update": "let it finish before you go live",
    "Windows Search": "let it finish before you go live",
}
BACKGROUND_PCT = 10.0  # of the whole PC: a background job using this much is worth saying
HEAVY_PCT = 25.0  # any other program using this much is named in the Stream doctor
IGNORE = {"system idle process", "idle", "system", "sprigcast.exe", "python.exe", "pythonw.exe"}


class BusyWatch:
    """feed() a reading every few seconds; it says when a warning is due."""

    def __init__(self, clock=time.monotonic, pct: float = BUSY_PCT, for_s: float = BUSY_FOR_S,
                 every_s: float = WARN_EVERY_S):
        self._clock = clock
        self._since: float | None = None
        self._warned = float("-inf")
        self.pct, self.for_s, self.every_s = pct, for_s, every_s

    def feed(self, cpu: float | None, gpu: float | None) -> bool:
        now = self._clock()
        if max(cpu or 0.0, gpu or 0.0) < self.pct:
            self._since = None
            return False
        if self._since is None:
            self._since = now
        if now - self._since < self.for_s or now - self._warned < self.every_s:
            return False
        self._warned = now
        return True


def friendly(name: str) -> str:
    key = name.lower()
    return KNOWN.get(key) or BACKGROUND.get(key) or (name[:-4] if key.endswith(".exe") else name)


def background_jobs(apps: list[tuple[str, float]]) -> list[tuple[str, float]]:
    """Of (name, % of the whole PC) pairs: Windows' own jobs that can wait (Defender, Update, Search) using
    BACKGROUND_PCT or more, heaviest first."""
    names = set(BACKGROUND.values())
    return sorted(((n, pct) for n, pct in apps if n in names and pct >= BACKGROUND_PCT), key=lambda a: -a[1])


def job_message(name: str, pct: float) -> str:
    """One background job, said plainly: "Windows Defender is using 38% of the CPU: schedule its scans outside
    stream hours." """
    return f"{name} is using {pct:.0f}% of the CPU: {ADVICE.get(name, 'let it finish before you go live')}."


def spike_watch(clock=time.monotonic) -> BusyWatch:
    """The log-only watch: the CPU at SPIKE_PCT or more for SPIKE_FOR_S (feed it the CPU only)."""
    return BusyWatch(clock, SPIKE_PCT, SPIKE_FOR_S, SPIKE_EVERY_S)


def cpu_by_app(wait: float = 1.0) -> dict[str, float] | None:
    """Each other app's CPU over `wait` seconds, in % of one core, by the name people know. Two looks at everything
    running (core/processes.py), one call each; None where Windows can't be asked that way."""
    from sprigcast.core import processes

    try:
        before = {p.pid: p for p in processes.snapshot()}
        began = time.monotonic()
        time.sleep(wait)
        after = processes.snapshot()
        took = max(0.001, time.monotonic() - began)
    except OSError:
        return None
    me = os.getpid()
    use: dict[str, float] = {}
    for proc in after:
        was = before.get(proc.pid)
        if was is None or was.name != proc.name or proc.pid == me or not proc.name or proc.name.lower() in IGNORE:
            continue
        label = friendly(proc.name)
        use[label] = use.get(label, 0.0) + 100.0 * max(0.0, proc.cpu_s - was.cpu_s) / took
    return use


def _cpu_by_app_psutil(wait: float) -> dict[str, float]:
    import psutil

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
    return use


def heavy_apps(limit: int = 3, wait: float = 1.0, with_use: bool = False) -> list:
    """The other apps using the most CPU right now, by the names people know (takes `wait` seconds).
    with_use: (name, % of the whole PC) pairs, heaviest first, whatever they are."""
    use = cpu_by_app(wait)
    if use is None:  # not Windows: one process at a time
        try:
            use = _cpu_by_app_psutil(wait)
        except ImportError:
            return []
    cores = os.cpu_count() or 1
    ranked = sorted(use.items(), key=lambda kv: kv[1], reverse=True)
    if with_use:
        return [(name, pct / cores) for name, pct in ranked[:limit]]
    known = set(KNOWN.values())
    # Streaming apps first when they are busy at all, then whatever else is heaviest.
    heavy = [n for n, pct in ranked if pct / cores >= 2.0]
    waiting = {n for n, _pct in background_jobs([(n, pct / cores) for n, pct in ranked])}
    heavy.sort(key=lambda n: (n not in waiting, n not in known, -use[n]))  # a scan that can wait is said first
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
    if apps[0] in ADVICE:  # Windows' own job (heavy_apps puts it first): what to do about that one
        advice = ADVICE[apps[0]]
        return f"Your PC is busy ({load}): {apps[0]} is using it. {advice[:1].upper()}{advice[1:]}."
    names = apps[0] if len(apps) == 1 else ", ".join(apps[:-1]) + " and " + apps[-1]
    verb = "is" if len(apps) == 1 else "are"
    return (f"Your PC is busy ({load}): {names} {verb} using it too. "
            "Closing them helps; dropped frames now come from that, not from SprigCast.")


def spike_line(apps: list[tuple[str, float]], cpu: float | None) -> str:
    """The log line for a pinned CPU: who had it."""
    who = ", ".join(f"{name} {pct:.0f}%" for name, pct in apps) or "no single app stood out"
    return f"PC CPU {cpu or 0:.0f}% for {SPIKE_FOR_S:.0f} s or more (SprigCast not counted); heaviest: {who}"
