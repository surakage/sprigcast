"""Keep the monitors and the PC awake while SprigCast streams or records.

PC native run 2026-11-17: each time Windows turned the monitors off for idle (and back on), Direct3D, NVENC and the
window all stalled for 0.3-1 s (Kernel-Power 566 lined up with every freeze), and the stream and the file lost
frames. While a stream or recording runs SprigCast asks Windows to keep the display and the system on, as other
streaming apps do; when both stop it lets go. The request belongs to the thread that made it (the window's), and
ends with the app.
"""

from __future__ import annotations

import logging
import sys

log = logging.getLogger(__name__)

ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002
ES_CONTINUOUS = 0x80000000

_held = False


def _set(flags: int) -> bool:
    if sys.platform != "win32":
        return False
    import ctypes

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.SetThreadExecutionState.restype = ctypes.c_uint32
        kernel32.SetThreadExecutionState.argtypes = [ctypes.c_uint32]
        return kernel32.SetThreadExecutionState(flags) != 0
    except (OSError, AttributeError) as exc:
        log.warning("keeping the PC awake isn't possible here (%s)", exc)
        return False


def hold(busy: bool) -> None:
    """Call from the window's thread whenever it looks (cheap: Windows is only told on a change). busy: a stream
    or recording is running."""
    global _held
    if busy == _held:
        return
    _held = busy
    if busy:
        ok = _set(ES_CONTINUOUS | ES_DISPLAY_REQUIRED | ES_SYSTEM_REQUIRED)
        log.info("keeping the monitors and the PC awake while live or recording%s", "" if ok else " (not possible)")
    else:
        _set(ES_CONTINUOUS)
        log.info("the monitors and the PC may sleep again")


def held() -> bool:
    return _held
