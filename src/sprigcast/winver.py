"""The real Windows build number, whatever the running exe's manifest says.

``sys.getwindowsversion().build`` comes from GetVersionEx, which reports Windows 8 (build 9200) to any exe
without a Windows 10 entry in its manifest. ``platform_version`` is read from kernel32.dll itself, so it's
the true version. SprigCast checks features like per-app sound (Windows 10 2004, build 19041) with this.
"""

from __future__ import annotations

import sys


def windows_build() -> int:
    """The Windows build number (19041 = Windows 10 2004, 22000 = Windows 11), or 0 off Windows."""
    if sys.platform != "win32":
        return 0
    try:
        version = sys.getwindowsversion()
        return max(int(version.platform_version[2]), int(version.build))
    except Exception:  # noqa: BLE001
        return 0
