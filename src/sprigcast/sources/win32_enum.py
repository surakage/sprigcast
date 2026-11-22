"""List monitors and top-level windows on Windows with ctypes (no extra dependencies).

On other platforms both functions return empty lists, so the rest of the app still runs.
"""

from __future__ import annotations

import ctypes
import functools
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
    path: str = ""  # full path to the executable (for its icon)
    app: str = ""  # the app's own name for itself ("Firefox", "Calculator"), when it has one
    pid: int = 0

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


@functools.lru_cache(maxsize=64)
def app_name(path: str) -> str:
    """The name an app gives itself in its file details ("Firefox", "Google Chrome"); "" when it gives none.
    Read once per program (cached): a few small reads of the file's version block."""
    if sys.platform != "win32" or not path:
        return ""
    from ctypes import wintypes

    version = ctypes.windll.version
    try:
        size = version.GetFileVersionInfoSizeW(path, None)
        if not size:
            return ""
        block = ctypes.create_string_buffer(size)
        if not version.GetFileVersionInfoW(path, 0, size, block):
            return ""
        pointer, length = ctypes.c_void_p(), wintypes.UINT(0)
        pairs = []
        if version.VerQueryValueW(block, "\\VarFileInfo\\Translation", ctypes.byref(pointer), ctypes.byref(length)):
            words = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_ushort * (length.value // 2))).contents
            pairs = [(words[i], words[i + 1]) for i in range(0, len(words) - 1, 2)]
        for lang, page in [*pairs, (0x0409, 0x04B0), (0x0409, 0x04E4)]:
            for key in ("FileDescription", "ProductName"):
                where = f"\\StringFileInfo\\{lang:04x}{page:04x}\\{key}"
                if version.VerQueryValueW(block, where, ctypes.byref(pointer), ctypes.byref(length)) and length.value:
                    text = ctypes.wstring_at(pointer.value, length.value).rstrip("\0").strip()
                    if text:
                        return text[:60]
    except (OSError, AttributeError, ValueError):
        return ""
    return ""


FRAME_HOST = "applicationframehost.exe"  # Windows' frame around Store apps (Settings, Calculator, Photos...)


def list_windows(include_own: bool = False) -> list[WindowInfo]:
    """Visible, titled, top-level windows that are not tool windows or cloaked UWP shells.
    include_own: SprigCast's own windows too (a window capture with Show SprigCast on)."""
    if sys.platform != "win32":
        return []
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    dwmapi = ctypes.windll.dwmapi
    GWL_EXSTYLE, WS_EX_TOOLWINDOW, DWMWA_CLOAKED = -20, 0x00000080, 14
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    own_pid = kernel32.GetCurrentProcessId()

    def process_path(pid: int) -> str:
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(1024)
            buf = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return buf.value
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
        if pid.value == own_pid and not include_own:
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        path = process_path(pid.value)
        process = path.rsplit("\\", 1)[-1]
        app, icon_path = "", path
        if process.lower() == FRAME_HOST:
            # A Store app inside Windows' frame: its name is the window's title, its icon the app's own program
            icon_path = hosted_path(hwnd, pid.value) or path
            app = buf.value
        else:
            app = app_name(path)
        windows.append(WindowInfo(hwnd=int(hwnd), title=buf.value, process=process, path=icon_path, app=app,
                                  pid=int(pid.value)))
        return True

    def hosted_path(frame, frame_pid: int) -> str:
        """The program of the Store app a frame holds (its first child window from another process)."""
        found: list[str] = []

        def child(hwnd, _data):
            other = wintypes.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(other))
            if other.value and other.value != frame_pid:
                found.append(process_path(other.value))
                return False
            return True

        user32.EnumChildWindows(frame, callback_type(child), 0)
        return next((p for p in found if p), "")

    user32.EnumWindows(callback_type(callback), 0)
    return windows


def find_window(title: str, process: str = "", exact: bool = False, include_own: bool = False) -> WindowInfo | None:
    """Best match for a saved window: same process and title, then same process, then same title.
    exact: only the same title (in the same process, when one is saved), never another window of that app."""
    windows = list_windows(include_own=True) if include_own else list_windows()
    same_title = (lambda w: w.title == title and (not process or w.process.lower() == process.lower()))
    rules = (same_title,) if exact else (
        same_title,
        lambda w: bool(process) and w.process.lower() == process.lower(),
        lambda w: w.title == title,
    )
    for match in rules:
        found = next((w for w in windows if match(w)), None)
        if found is not None:
            return found
    return None


def foreground_window() -> WindowInfo | None:
    """The window in front (the one with the keyboard), if it's one list_windows() would show."""
    if sys.platform != "win32":
        return None
    hwnd = int(ctypes.windll.user32.GetForegroundWindow() or 0)
    return next((w for w in list_windows() if w.hwnd == hwnd), None) if hwnd else None
