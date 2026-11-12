"""Keep SprigCast smooth next to games and other apps (Streamer.bot, TikTok LIVE Studio, ...).

Two things, both Windows-only and both harmless if they fail:
- CPU priority: "Above normal" by default, so a game pegging the CPU doesn't starve rendering and
  encoding. It is a small nudge, not "High"/"Realtime", so it never makes the game itself stutter.
- GPU priority: ask Windows to schedule SprigCast's GPU work ahead of a game's, the way OBS does.
  Without it a game that maxes the GPU can make the stream skip frames even while the game runs fine.
  Windows only allows the strongest level when SprigCast runs as administrator; otherwise it falls
  back to "high", and if that's refused too, it carries on as normal.
"""

from __future__ import annotations

import logging
import sys

log = logging.getLogger(__name__)

SETTING = "general/priority"
LEVELS = {"normal": "Normal", "above_normal": "Above normal (recommended)", "high": "High"}
DEFAULT = "above_normal"

# D3DKMT_SCHEDULINGPRIORITYCLASS
_GPU_REALTIME, _GPU_HIGH = 5, 4


def chosen() -> str:
    from PySide6.QtCore import QSettings

    level = str(QSettings().value(SETTING, DEFAULT))
    return level if level in LEVELS else DEFAULT


def set_cpu_priority(level: str) -> bool:
    """Set this process's CPU priority class. Returns True if it took effect."""
    if sys.platform != "win32":
        return False
    try:
        import psutil

        classes = {"normal": psutil.NORMAL_PRIORITY_CLASS, "above_normal": psutil.ABOVE_NORMAL_PRIORITY_CLASS,
                   "high": psutil.HIGH_PRIORITY_CLASS}
        psutil.Process().nice(classes.get(level, classes[DEFAULT]))
        return True
    except Exception as exc:
        log.info("could not set CPU priority to %s: %s", level, exc)
        return False


def raise_gpu_priority() -> str:
    """Ask Windows to schedule our GPU work first. Returns "realtime", "high" or "" (not changed)."""
    if sys.platform != "win32":
        return ""
    try:
        import ctypes

        gdi32 = ctypes.WinDLL("gdi32")
        fn = gdi32.D3DKMTSetProcessSchedulingPriorityClass
        fn.argtypes = [ctypes.c_void_p, ctypes.c_int]
        fn.restype = ctypes.c_long  # NTSTATUS: 0 = success
        process = ctypes.windll.kernel32.GetCurrentProcess()
        for name, level in (("realtime", _GPU_REALTIME), ("high", _GPU_HIGH)):
            if fn(process, level) == 0:
                return name
    except Exception as exc:
        log.info("could not raise GPU priority: %s", exc)
    return ""


def apply(level: str | None = None) -> str:
    """Apply the saved (or given) CPU priority and raise GPU priority. Returns a short summary."""
    level = level or chosen()
    cpu = set_cpu_priority(level)
    gpu = raise_gpu_priority()
    summary = f"CPU priority {LEVELS[level].split(' (')[0].lower() if cpu else 'unchanged'}, GPU priority {gpu or 'normal'}"
    log.info(summary)
    return summary
