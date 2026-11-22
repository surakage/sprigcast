"""Settings → General → Privacy: keep SprigCast's own windows out of screen captures.

With "Hide SprigCast from screen capture" on, Windows leaves the main window and SprigCast's pop-ups out of every
capture (Display capture in SprigCast or any other app, Snipping Tool, screen sharing), so a full-screen capture
never shows your settings, keys or chat. Projectors are never hidden: they're meant to be captured.

Windows 10 2004 or newer (SetWindowDisplayAffinity with WDA_EXCLUDEFROMCAPTURE). Elsewhere this does nothing.
"""

from __future__ import annotations

import logging
import sys

from PySide6.QtCore import QSettings

log = logging.getLogger(__name__)

HIDE_FROM_CAPTURE = "general/hide_from_capture"
WDA_NONE = 0x0
WDA_EXCLUDEFROMCAPTURE = 0x11


_shown_by_capture = False  # a screen or window capture has "Show SprigCast" on: never hidden while it does


def hidden() -> bool:
    """The Privacy setting itself."""
    return QSettings().value(HIDE_FROM_CAPTURE, False, type=bool)


def hidden_now() -> bool:
    """The setting, unless a screen or window capture asks to show SprigCast (to record the program itself)."""
    return hidden() and not _shown_by_capture


def shows_sprigcast(collection) -> bool:
    """A screen or window capture in this collection has Show SprigCast on."""
    return any(s.kind in ("display_capture", "window_capture") and s.settings.get("sprigcast", False)
               for s in collection.sources)


def set_shown_by_capture(on: bool) -> bool:
    """Record whether a capture shows SprigCast. True when that changed (the open windows need it applied)."""
    global _shown_by_capture
    changed = bool(on) != _shown_by_capture
    _shown_by_capture = bool(on)
    return changed


def supported() -> bool:
    if sys.platform != "win32":
        return False
    build = getattr(sys.getwindowsversion(), "build", 0)
    return build >= 19041  # Windows 10 2004: WDA_EXCLUDEFROMCAPTURE


def apply_to(widget, on: bool | None = None) -> bool:
    """Hide (or show) this window in captures, following the setting unless `on` is given. True when Windows took it."""
    if not supported() or widget is None:
        return False
    on = hidden_now() if on is None else on
    try:
        import ctypes

        hwnd = int(widget.winId())
        ok = bool(ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE if on else WDA_NONE))
        if not ok:
            log.info("privacy: Windows refused to change capture for a window (%s)", ctypes.GetLastError())
        return ok
    except Exception:  # noqa: BLE001  never in the way of a window opening
        log.warning("privacy: couldn't change capture for a window", exc_info=True)
        return False
