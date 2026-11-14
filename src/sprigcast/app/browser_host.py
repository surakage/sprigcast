"""Draws browser sources (sources/browser.py) with Qt WebEngine, Chromium, on the UI thread.

Each page lives in an off-screen QWebEngineView the size of the source with a see-through background.
It's copied to a frame only after Chromium has painted something new, and at most `fps` times a second,
so a still alert box costs nothing between alerts. The render thread picks frames up with take().

Pages share one saved profile (local storage, cookies) so widgets that remember things keep working.
Pages get the newest Chromium features Qt ships: WebGL, GPU canvas, autoplay with sound, and a
`window.obsstudio` stand-in so overlays written for OBS (StreamElements, Streamlabs, TikFinity) see the
visibility events they expect.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

import numpy as np
from PySide6.QtCore import QEvent, QObject, QStandardPaths, Qt, QTimer, QUrl, Signal, Slot

from sprigcast.sources import browser

log = logging.getLogger(__name__)

# Settings that need the page loaded again when they change. Size, fps and mute apply in place.
RELOAD_KEYS = ("url", "file", "css")

SHIM = """
(() => {
  if (window.obsstudio) return;
  const status = {recording: false, streaming: false, recordingPaused: false, replaybuffer: false, virtualcam: false};
  window.obsstudio = {
    pluginVersion: "2.24.0",
    sprigcast: true,
    getControlLevel: (cb) => cb(1),
    getStatus: (cb) => cb(status),
    getCurrentScene: (cb) => cb({name: "", width: window.innerWidth, height: window.innerHeight}),
    getScenes: (cb) => cb([]),
    getCurrentTransition: (cb) => cb(""),
    getTransitions: (cb) => cb([]),
    onVisibilityChange: null,
    onActiveChange: null,
  };
  window.sprigcast = window.obsstudio;
  window.__sprigcastVisible = (shown) => {
    window.dispatchEvent(new CustomEvent("obsSourceVisibleChanged", {detail: {visible: shown}}));
    window.dispatchEvent(new CustomEvent("obsSourceActiveChanged", {detail: {active: shown}}));
    if (typeof window.obsstudio.onVisibilityChange === "function") window.obsstudio.onVisibilityChange(shown);
    if (typeof window.obsstudio.onActiveChange === "function") window.obsstudio.onActiveChange(shown);
  };
})();
"""


def css_script(css: str) -> str:
    """JavaScript that adds `css` to the page in a <style>, as early as the page allows."""
    import json

    return ("(() => { const add = () => { const s = document.createElement('style'); s.textContent = %s;"
            " (document.head || document.documentElement).appendChild(s); };"
            " if (document.head) add(); else document.addEventListener('DOMContentLoaded', add); })();"
            % json.dumps(css))


_profile = None


def shared_profile():
    global _profile
    if _profile is None:
        from PySide6.QtWebEngineCore import QWebEngineProfile

        _profile = QWebEngineProfile("sprigcast-browser")
        base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)
        if base:
            _profile.setPersistentStoragePath(f"{base}/web-sources")
            _profile.setCachePath(f"{base}/web-sources-cache")
        _profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies)
        _profile.setHttpCacheMaximumSize(64 * 1024 * 1024)
        _turn_on_features(_profile.settings())
    return _profile


def _turn_on_features(settings) -> None:
    from PySide6.QtWebEngineCore import QWebEngineSettings

    A = QWebEngineSettings.WebAttribute
    for attr, on in (
        (A.JavascriptEnabled, True),
        (A.WebGLEnabled, True),
        (A.Accelerated2dCanvasEnabled, True),
        (A.PlaybackRequiresUserGesture, False),  # alerts play their sound without a click
        (A.LocalStorageEnabled, True),
        (A.LocalContentCanAccessRemoteUrls, True),  # a local overlay file may load web fonts and APIs
        (A.LocalContentCanAccessFileUrls, True),
        (A.ShowScrollBars, False),
        (A.ScrollAnimatorEnabled, False),
        (A.JavascriptCanOpenWindows, False),
        (A.ErrorPageEnabled, True),
        (A.FocusOnNavigationEnabled, False),
    ):
        settings.setAttribute(attr, on)


class _Painted(QObject):
    """Notices when Chromium has drawn a new picture into the view."""

    def __init__(self, page: "_Page"):
        super().__init__()
        self.page = page

    def eventFilter(self, obj, event) -> bool:
        if event.type() in (QEvent.Type.Paint, QEvent.Type.UpdateRequest):
            self.page.dirty = True
        return False


class _Page:
    def __init__(self, host: "BrowserHost", source_id: str, settings: dict[str, Any]):
        from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineScript
        from PySide6.QtWebEngineWidgets import QWebEngineView

        self.host, self.id = host, source_id
        self.settings: dict[str, Any] = {}
        self.dirty = True
        self.paused = False
        self._watched = None
        self._painted = _Painted(self)
        self.view = QWebEngineView()
        self.view.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        self.view.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.view.setStyleSheet("background: transparent")
        self.page = QWebEnginePage(shared_profile(), self.view)
        self.page.setBackgroundColor(Qt.GlobalColor.transparent)
        if hasattr(self.page, "permissionRequested"):  # Qt 6.8+: camera, mic, notifications... all no
            self.page.permissionRequested.connect(lambda permission: permission.deny())
        else:
            self.page.featurePermissionRequested.connect(self._deny)
        self.page.renderProcessTerminated.connect(self._crashed)
        self.page.loadFinished.connect(self._loaded)
        self.view.setPage(self.page)
        self._Script = QWebEngineScript
        self.timer = QTimer()
        self.timer.timeout.connect(self._tick)
        self.apply(settings)
        self.view.show()

    # Settings ---------------------------------------------------------------------

    def apply(self, settings: dict[str, Any]) -> None:
        old, self.settings = self.settings, dict(settings)
        w, h = max(1, int(settings.get("width", 800))), max(1, int(settings.get("height", 600)))
        if self.view.size().width() != w or self.view.size().height() != h:
            self.view.resize(w, h)
            self.dirty = True
        fps = max(1, min(120, int(settings.get("fps", 30))))
        self.timer.start(max(4, round(1000 / fps)))
        self.page.setAudioMuted(bool(settings.get("mute", False)))
        if any(old.get(k) != settings.get(k) for k in RELOAD_KEYS):
            self._scripts()
            url = browser.page_url(settings)
            self.page.setUrl(QUrl(url) if url else QUrl("about:blank"))

    def _scripts(self) -> None:
        S = self._Script
        scripts = self.page.scripts()
        scripts.clear()
        for name, source, point in (
            ("sprigcast-obs", SHIM, S.InjectionPoint.DocumentCreation),
            ("sprigcast-css", css_script(str(self.settings.get("css", "") or "")), S.InjectionPoint.DocumentReady),
        ):
            script = S()
            script.setName(name)
            script.setSourceCode(source)
            script.setInjectionPoint(point)
            script.setWorldId(S.ScriptWorldId.MainWorld)
            script.setRunsOnSubFrames(False)
            scripts.insert(script)

    # Events ----------------------------------------------------------------------

    def _deny(self, origin, feature) -> None:
        from PySide6.QtWebEngineCore import QWebEnginePage

        self.page.setFeaturePermission(origin, feature, QWebEnginePage.PermissionPolicy.PermissionDeniedByUser)

    def _loaded(self, ok: bool) -> None:
        self.dirty = True
        if not ok:
            log.info("browser source %s: page didn't load", self.id)

    def _crashed(self, status, code) -> None:
        log.warning("browser source %s: page stopped (%s, %s), loading it again", self.id, status, code)
        QTimer.singleShot(2000, self.view.reload)

    def set_paused(self, paused: bool) -> None:
        if paused == self.paused:
            return
        self.paused = paused
        self.page.runJavaScript(f"window.__sprigcastVisible && window.__sprigcastVisible({'false' if paused else 'true'})")
        if not paused:
            self.dirty = True

    # Frames ----------------------------------------------------------------------

    def _watch(self) -> None:
        proxy = self.view.focusProxy()  # the widget Chromium paints into, made once the page starts
        if proxy is not None and proxy is not self._watched:
            proxy.installEventFilter(self._painted)
            self._watched = proxy

    def _tick(self) -> None:
        self._watch()
        if self.paused or not self.dirty:
            return
        self.dirty = False
        self.host.store(self.id, self.grab())

    def grab(self) -> np.ndarray:
        from PySide6.QtGui import QImage

        w, h = self.view.width(), self.view.height()
        image = QImage(w, h, QImage.Format.Format_RGBA8888)
        image.fill(0)
        self.view.render(image)
        rows = np.frombuffer(image.constBits(), dtype=np.uint8).reshape(h, image.bytesPerLine())
        return rows[:, : w * 4].reshape(h, w, 4).copy()

    def close(self) -> None:
        import shiboken6

        self.timer.stop()
        # Deleted now, not later: Chromium complains (and can crash on exit) if the profile goes first.
        shiboken6.delete(self.page)
        shiboken6.delete(self.view)


class BrowserHost(QObject):
    """Owns the pages. open/close/pause/take may be called from the render thread."""

    _request = Signal(str, str, object)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.pages: dict[str, _Page] = {}
        self._frames: dict[str, np.ndarray] = {}
        self._lock = threading.Lock()
        self._request.connect(self._handle, Qt.ConnectionType.QueuedConnection)

    # Any thread ---------------------------------------------------------------------

    def open(self, source_id: str, settings: dict[str, Any]) -> None:
        self._request.emit("open", source_id, dict(settings))

    def close(self, source_id: str) -> None:
        self._request.emit("close", source_id, None)
        with self._lock:
            self._frames.pop(source_id, None)

    def pause(self, source_id: str, paused: bool) -> None:
        self._request.emit("pause", source_id, paused)

    def take(self, source_id: str) -> np.ndarray | None:
        with self._lock:
            return self._frames.pop(source_id, None)

    def reload(self, source_id: str) -> None:
        self._request.emit("reload", source_id, None)

    # UI thread ----------------------------------------------------------------------

    def store(self, source_id: str, frame: np.ndarray) -> None:
        with self._lock:
            self._frames[source_id] = frame

    @Slot(str, str, object)
    def _handle(self, action: str, source_id: str, value) -> None:
        page = self.pages.get(source_id)
        try:
            if action == "open":
                if page is None:
                    self.pages[source_id] = _Page(self, source_id, value)
                else:
                    page.apply(value)
            elif action == "close" and page is not None:
                page.close()
                del self.pages[source_id]
            elif action == "pause" and page is not None:
                page.set_paused(bool(value))
            elif action == "reload" and page is not None:
                page.page.triggerAction(page.page.WebAction.ReloadAndBypassCache)
        except Exception:  # a broken page must never take the app down
            log.exception("browser source %s: %s failed", source_id, action)

    def shutdown(self) -> None:
        for page in self.pages.values():
            page.close()
        self.pages.clear()
        browser.set_host(None)


def start_host(parent: QObject | None = None) -> BrowserHost | None:
    """Make the host and hand it to browser sources, or None when Qt WebEngine isn't installed."""
    import importlib.util

    # Only checks it's there: Qt WebEngine is loaded when the first page opens, so no browser sources
    # means no Chromium in memory.
    if importlib.util.find_spec("PySide6.QtWebEngineWidgets") is None:
        return None
    host = BrowserHost(parent)
    browser.set_host(host)
    return host
