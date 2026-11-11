"""List monitors and top-level windows on Windows with ctypes (no extra dependencies).

On other platforms both functions return empty lists, so the rest of the app still runs.
"""

from __future__ import annotations

import ctypes
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class MonitorInfo:
    index: int  # 1-based, the numbering Windows Graphics Capture uses
    name: str  # e.g. \\.\DISPLAY1
    width: int
    height: int
    primary: bool

    @property
    def label(self) -> str:
        return f"Monitor {self.index}: {self.width}x{self.height}" + ("  (primary)" if self.primary else "")


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    title: str
    process: str  # executable name, e.g. "obs64.exe"

    @property
    def label(self) -> str:
        return f"[{self.process}] {self.title}" if self.process else self.title


def list_monitors() -> list[MonitorInfo]:
    if sys.platform != "win32":
        return []
    from ctypes import wintypes

    user32 = ctypes.windll.user32

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("rcMonitor", wintypes.RECT),
            ("rcWork", wintypes.RECT),
            ("dwFlags", wintypes.DWORD),
            ("szDevice", wintypes.WCHAR * 32),
        ]

    monitors: list[MonitorInfo] = []
    callback_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM
    )

    def callback(hmonitor, _hdc, _rect, _data):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if user32.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
            r = info.rcMonitor
            monitors.append(
                MonitorInfo(
                    index=len(monitors) + 1,
                    name=info.szDevice,
                    width=r.right - r.left,
                    height=r.bottom - r.top,
                    primary=bool(info.dwFlags & 1),  # MONITORINFOF_PRIMARY
                )
            )
        return True

    user32.EnumDisplayMonitors(None, None, callback_type(callback), 0)
    return monitors


def list_windows() -> list[WindowInfo]:
    """Visible, titled, top-level windows that are not tool windows or cloaked UWP shells."""
    if sys.platform != "win32":
        return []
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    dwmapi = ctypes.windll.dwmapi
    GWL_EXSTYLE, WS_EX_TOOLWINDOW, DWMWA_CLOAKED = -20, 0x00000080, 14
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    own_pid = kernel32.GetCurrentProcessId()

    def process_name(pid: int) -> str:
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(1024)
            buf = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return buf.value.rsplit("\\", 1)[-1]
            return ""
        finally:
            kernel32.CloseHandle(handle)

    windows: list[WindowInfo] = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd, _data):
        if not user32.IsWindowVisible(hwnd) or user32.GetWindowTextLengthW(hwnd) == 0:
            return True
        if user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
            return True
        cloaked = wintypes.DWORD(0)
        dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
        if cloaked.value:
            return True
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == own_pid:
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        windows.append(WindowInfo(hwnd=int(hwnd), title=buf.value, process=process_name(pid.value)))
        return True

    user32.EnumWindows(callback_type(callback), 0)
    return windows


def find_window(title: str, process: str = "") -> WindowInfo | None:
    """Best match for a saved window: same process and title, then same process, then same title."""
    windows = list_windows()
    for match in (
        lambda w: w.title == title and (not process or w.process.lower() == process.lower()),
        lambda w: bool(process) and w.process.lower() == process.lower(),
        lambda w: w.title == title,
    ):
        found = next((w for w in windows if match(w)), None)
        if found is not None:
            return found
    return None
