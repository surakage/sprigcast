"""A web page as a source: alerts, chat overlays, goal bars, TikFinity widgets, any URL or local HTML file.

Pages are drawn by Qt WebEngine (Chromium) on the UI thread by app/browser_host.py; this class only
asks for a page and picks up its newest frame. A page that nothing on screen shows is closed (its
Chromium process ends) unless "Keep running when hidden" is on, which keeps an alert box connected.

Settings: url, file (a local .html instead of a URL), width, height, fps, css, mute, keep_running.
"""

from __future__ import annotations

from typing import Any, Protocol

import numpy as np

from sprigcast.core.source import Source, register

# Same idea as OBS's default, so overlays made for OBS look the same here.
DEFAULT_CSS = "body { background-color: rgba(0, 0, 0, 0); margin: 0px auto; overflow: hidden; }"

DEFAULTS: dict[str, Any] = {
    "url": "https://",
    "file": "",
    "width": 800,
    "height": 600,
    "fps": 30,
    "css": DEFAULT_CSS,
    "mute": False,
    "keep_running": False,
}


class Host(Protocol):
    """What the UI provides (app/browser_host.BrowserHost). All methods may be called from any thread."""

    def open(self, source_id: str, settings: dict[str, Any]) -> None: ...

    def close(self, source_id: str) -> None: ...

    def pause(self, source_id: str, paused: bool) -> None: ...

    def take(self, source_id: str) -> np.ndarray | None: ...


_host: Host | None = None


def set_host(host: Host | None) -> None:
    global _host
    _host = host


def page_url(settings: dict[str, Any]) -> str:
    """The address to load: the local file when one is set, else the URL ("" when neither is usable)."""
    path = str(settings.get("file", "") or "").strip()
    if path:
        from pathlib import Path

        return Path(path).resolve().as_uri()
    url = str(settings.get("url", "") or "").strip()
    if url in ("", "https://", "http://"):
        return ""
    if "://" not in url and not url.startswith(("about:", "data:")):
        url = "https://" + url
    return url


def page_name(settings: dict[str, Any]) -> str:
    """A short name for the list: 'streamelements.com/overlay/…' -> 'Streamelements', 'alerts.html' -> 'Alerts'."""
    from pathlib import Path
    from urllib.parse import urlparse

    url = page_url(settings)
    if url.startswith("file:"):
        word = Path(str(settings.get("file", ""))).stem
    else:
        host = urlparse(url).hostname or ""
        parts = [p for p in host.split(".") if p not in ("www", "dashboard", "widgets", "app")]
        word = parts[0] if parts else ""
    word = word.replace("_", " ").replace("-", " ").strip()
    return (word[:1].upper() + word[1:])[:24] if word else "Browser"


@register("browser")
class BrowserSource(Source):
    label = "Browser"
    defaults = DEFAULTS
    sleeps = True

    def __init__(self, source_id: str, settings: dict[str, Any]):
        super().__init__(source_id, settings)
        self._open = False
        self._blank = True  # send one clear frame so a new source has a size before the page draws

    @property
    def size(self) -> tuple[int, int]:
        return max(1, int(self.settings.get("width", 800))), max(1, int(self.settings.get("height", 600)))

    def _keep_running(self) -> bool:
        return bool(self.settings.get("keep_running", False))

    def start(self) -> None:
        self._start_page()

    def _start_page(self) -> None:
        if _host is None:
            self.status = "Browser sources need Qt WebEngine"
            return
        if not page_url(self.settings):
            self.status = "No page set"
        else:
            self.status = "ok"
        _host.open(self.id, dict(self.settings))
        self._open = True

    def stop(self) -> None:
        if _host is not None and self._open:
            _host.close(self.id)
        self._open = False

    def update(self, settings: dict[str, Any]) -> None:
        super().update(settings)
        if self._open:
            self._start_page()  # the host reloads only when something that matters changed

    def sleep(self) -> None:
        if not self._open or _host is None:
            return
        if self._keep_running():
            _host.pause(self.id, True)
        else:
            self.stop()

    def wake(self) -> None:
        self._blank = True
        if self._open and _host is not None:
            _host.pause(self.id, False)
        else:
            self._start_page()

    def video_frame(self) -> np.ndarray | None:
        frame = _host.take(self.id) if _host is not None else None
        if frame is not None:
            self._blank = False
            return frame
        if self._blank:
            self._blank = False
            return np.zeros((2, 2, 4), dtype=np.uint8)  # see-through until the page has drawn
        return None
