"""Browser pages drawn straight onto the graphics card (native engine, Windows): the page is a Qt Quick
WebEngineView drawn by a QQuickRenderControl on a Direct3D 11 device on the engine's own card, into a texture the
engine opens by name and copies on the card (native/core/src/page_texture.cpp). Chromium's picture never passes
through memory or Python.

Used by the browser helper (app/browser_helper.py) when SprigCast runs the native engine and tells the helper its
card; otherwise pages are app/browser_host.py's widget pages, read back into memory. A page here behaves as one
there: the same profile on disk, scripts, window.sprigcast, sound to the mixer, pausing and Interact. While
SprigCast reads the pictures itself (its own compositor, a projector), they go to memory instead (want_memory). If
the card path fails, the page says so once and goes on in memory.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import numpy as np
from PySide6.QtCore import QEvent, QSize, Qt, QTimer, QUrl
from PySide6.QtWidgets import QWidget

from sprigcast.app import theme
from sprigcast.sources import browser

log = logging.getLogger(__name__)

HIDDEN_TICK_MS = 1000
STUCK_S = 1.0  # the engine took no picture for this long: a new texture
MEMORY_S = 2.0  # how long one ask for pictures in memory lasts (SprigCast asks again while it reads them)
RELOAD_KEYS = ("url", "file", "css", "audio_to_mixer")
INJECT = {"DocumentCreation": 2, "DocumentReady": 1}  # QWebEngineScript.InjectionPoint

QML = b"""
import QtQuick
import QtWebEngine
import QtWebChannel

Item {
    id: root
    signal loaded(bool ok)
    signal crashed(int status, int code)
    property var scripts: []
    property alias view: view

    function run(code) { view.runJavaScript(code) }
    function reloadPage() { view.reloadAndBypassCache() }
    function register(name, object) { channel.registerObject(name, object) }
    function focusPage() { view.forceActiveFocus() }

    WebChannel { id: channel }

    WebEngineView {
        id: view
        anchors.fill: parent
        backgroundColor: "transparent"
        webChannel: channel
        userScripts.collection: root.scripts
        settings.javascriptEnabled: true
        settings.webGLEnabled: true
        settings.accelerated2dCanvasEnabled: true
        settings.playbackRequiresUserGesture: false
        settings.localStorageEnabled: true
        settings.localContentCanAccessRemoteUrls: true
        settings.localContentCanAccessFileUrls: true
        settings.showScrollBars: false
        settings.javascriptCanOpenWindows: false
        settings.errorPageEnabled: true
        settings.focusOnNavigationEnabled: false
        onPermissionRequested: function(permission) { permission.deny() }
        onLoadingChanged: function(info) {
            if (info.status === WebEngineView.LoadSucceededStatus) root.loaded(true)
            else if (info.status === WebEngineView.LoadFailedStatus) root.loaded(false)
        }
        onRenderProcessTerminated: function(status, code) { root.crashed(status, code) }
    }
}
"""

_profile = None
_engine = None
_component = None


def available() -> bool:
    """Qt Quick's WebEngineView can be used here (Windows, the modules installed)."""
    import importlib.util
    import sys

    return sys.platform == "win32" and all(importlib.util.find_spec(m) is not None for m in (
        "PySide6.QtQuick", "PySide6.QtQml", "PySide6.QtWebEngineQuick"))


def prepare() -> None:
    """Before the QApplication: Qt Quick on Direct3D 11, and WebEngine set up for Qt Quick."""
    from PySide6.QtQuick import QQuickWindow, QSGRendererInterface
    from PySide6.QtWebEngineQuick import QtWebEngineQuick

    QQuickWindow.setGraphicsApi(QSGRendererInterface.GraphicsApi.Direct3D11)
    QtWebEngineQuick.initialize()


def _shared():
    """The QML engine, the page component and the profile on disk (the same one widget pages use)."""
    global _profile, _engine, _component
    if _engine is None:
        from PySide6.QtCore import QStandardPaths
        from PySide6.QtQml import QQmlComponent, QQmlEngine
        from PySide6.QtWebEngineQuick import QQuickWebEngineProfile

        _engine = QQmlEngine()
        _component = QQmlComponent(_engine)
        _component.setData(QML, QUrl("sprigcast-page.qml"))
        if _component.isError():
            raise RuntimeError("; ".join(e.toString() for e in _component.errors()))
        _profile = QQuickWebEngineProfile("sprigcast-browser")
        base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)
        if base:
            _profile.setPersistentStoragePath(f"{base}/web-sources")
            _profile.setCachePath(f"{base}/web-sources-cache")
        _profile.setPersistentCookiesPolicy(QQuickWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies)
        _profile.setHttpCacheMaximumSize(64 * 1024 * 1024)
    return _engine, _component, _profile


class QuickPage:
    """One browser source's page, drawn on the card. Same calls as browser_host._Page."""

    def __init__(self, host, source_id: str, settings: dict[str, Any]):
        from PySide6.QtQuick import QQuickRenderControl, QQuickWindow

        self.host, self.id = host, source_id
        self.settings: dict[str, Any] = {}
        self.dirty = True
        self.paused = False
        self.on_card = True  # False after the card path failed: read back only, as widget pages are
        self.interacting = None
        self.sound = None
        self.target = None  # sprigcore.PageTarget, made at the page's size
        self.memory_until = 0.0  # time.monotonic() until which SprigCast wants pictures in memory too
        self._waiting_since: float | None = None  # the engine hasn't taken the last picture since then
        self._initialized = False
        _, component, profile = _shared()
        self.control = QQuickRenderControl()
        self.window = QQuickWindow(self.control)
        self.window.setColor(Qt.GlobalColor.transparent)
        self.root = component.create()
        if self.root is None:
            raise RuntimeError("the page's QML didn't load")
        self.root.setParentItem(self.window.contentItem())
        self.view = self.root.property("view")
        self.view.setProperty("profile", profile)
        self.root.loaded.connect(self._loaded)
        self.root.crashed.connect(self._crashed)
        self.control.sceneChanged.connect(self._changed)
        self.control.renderRequested.connect(self._changed)
        self.timer = QTimer()
        self.timer.timeout.connect(self._tick)
        try:
            self.apply(settings)
        except Exception:
            self.close()
            raise

    # Settings ---------------------------------------------------------------------

    def apply(self, settings: dict[str, Any]) -> None:
        old, self.settings = self.settings, dict(settings)
        w, h = max(1, int(settings.get("width", 800))), max(1, int(settings.get("height", 600)))
        if (w, h) != (self.window.width(), self.window.height()) or self.target is None:
            self._resize(w, h)
        fps = browser.page_fps(settings)
        self.timer.start(max(4, round(1000 / fps)) if not self.paused else HIDDEN_TICK_MS)
        if any(old.get(k) != settings.get(k) for k in RELOAD_KEYS):
            self._set_up_sound()
            self._scripts()
            url = browser.page_url(settings)
            self.view.setProperty("url", QUrl(url) if url else QUrl("about:blank"))
        self._mute()

    def _resize(self, w: int, h: int) -> None:
        """A texture of the page's size on the card (the first one also starts Qt Quick on its device). Raises
        when the first can't be made: the host then makes a widget page instead."""
        from PySide6.QtQuick import QQuickGraphicsDevice, QQuickRenderTarget

        self.window.setGeometry(0, 0, w, h)
        self.root.setWidth(w)
        self.root.setHeight(h)
        old = self.target
        try:
            target = self.host.target_for(self.id, w, h, old.device if old is not None else None)
        except Exception as exc:
            if old is None:
                raise
            log.warning("browser source %s: a new texture couldn't be made (%s), the old one stays", self.id, exc)
            return
        self.target = target
        if not self._initialized:
            self.window.setGraphicsDevice(QQuickGraphicsDevice.fromDeviceAndContext(target.device, target.context))
            if not self.control.initialize():
                self.target = None
                target.close()
                raise RuntimeError("Qt Quick couldn't start on the engine's card")
            self._initialized = True
        self.window.setRenderTarget(QQuickRenderTarget.fromD3D11Texture(target.texture, QSize(w, h)))
        if old is not None:
            old.close()
        self._waiting_since = None
        self.dirty = True

    def _card_failed(self, why: str) -> None:
        if self.on_card:
            log.warning("browser source %s: drawn in memory instead of on the card: %s", self.id, why)
        self.on_card = False

    def _mute(self) -> None:
        to_mixer = self.sound is not None and not self.sound.on_desktop_reason
        self.view.setProperty("audioMuted", bool(self.settings.get("mute", False)) or to_mixer)

    def _set_up_sound(self) -> None:
        from sprigcast.sources.browser import sound_to_mixer

        if not sound_to_mixer(self.settings):
            self.sound = None
            self.host.set_sound(self.id, None)
            return
        from sprigcast.app.browser_audio import PageSound

        self.sound = PageSound(on_desktop=lambda _why: self._mute())
        self._call("register", "sprigcastAudio", self.sound)
        self.host.set_sound(self.id, self.sound)

    def _scripts(self) -> None:
        from sprigcast.app import browser_host

        wanted = [("sprigcast-obs", browser_host.shim(), "DocumentCreation"),
                  ("sprigcast-css", browser_host.css_script(str(self.settings.get("css", "") or "")), "DocumentReady")]
        if self.sound is not None:
            from sprigcast.app.browser_audio import CAPTURE_JS, qwebchannel_js

            wanted[1:1] = [("sprigcast-channel", qwebchannel_js(), "DocumentCreation"),
                           ("sprigcast-sound", CAPTURE_JS, "DocumentCreation")]
        self.root.setProperty("scripts", [
            {"name": name, "sourceCode": code, "injectionPoint": INJECT[point], "worldId": 0, "runsOnSubFrames": False}
            for name, code, point in wanted])

    def _call(self, name: str, *args) -> None:
        from PySide6.QtCore import Q_ARG, QMetaObject

        QMetaObject.invokeMethod(self.root, name, Qt.ConnectionType.DirectConnection,
                                 *(Q_ARG("QVariant", a) for a in args))

    # Events ----------------------------------------------------------------------

    def _changed(self) -> None:
        self.dirty = True

    def _loaded(self, ok: bool) -> None:
        self.dirty = True
        self.push_state()
        if not ok:
            log.info("browser source %s: page didn't load", self.id)

    def _crashed(self, status, code) -> None:
        log.warning("browser source %s: page stopped (%s, %s), loading it again", self.id, status, code)
        QTimer.singleShot(2000, lambda: self._call("reloadPage"))

    def push_state(self) -> None:
        import json

        state = {**self.host.page_state(self.id), "visible": not self.paused}
        self._call("run", f"window.__sprigcastSet && window.__sprigcastSet({json.dumps(state)})")

    def reload(self) -> None:
        self._call("reloadPage")

    def want_memory(self) -> None:
        """SprigCast reads this page's pictures itself (its own compositor, a projector): put them in memory for
        a while too."""
        self.memory_until = time.monotonic() + MEMORY_S
        self.dirty = True

    def set_paused(self, paused: bool) -> None:
        if paused == self.paused:
            return
        self.paused = paused
        self._call("run", f"window.__sprigcastVisible && window.__sprigcastVisible({'false' if paused else 'true'})")
        self.view.setProperty("visible", not paused)  # hidden like a background tab: Chromium stops painting
        self.timer.start(HIDDEN_TICK_MS if paused else max(4, round(1000 / browser.page_fps(self.settings))))
        if not paused:
            self.dirty = True

    # Frames ----------------------------------------------------------------------

    def _tick(self) -> None:
        if self.paused or not self.dirty or self.target is None or not self._initialized:
            return
        now = time.monotonic()
        memory = now < self.memory_until or not self.on_card  # SprigCast draws this page itself too
        if not self.target.begin(0):
            # The engine hasn't taken the last picture. If it stopped taking them (the source left the engine),
            # a new texture lets the page go on (in memory, or for the engine when it's back).
            if self._waiting_since is None:
                self._waiting_since = now
            elif now - self._waiting_since > STUCK_S:
                log.info("browser source %s: the engine stopped taking its pictures, a new texture", self.id)
                w, h = self.target.size
                self._resize(w, h)
            return
        self._waiting_since = None
        publish = self.on_card and not memory  # in memory, the block holds that picture: the engine takes it there
        frame = None
        try:
            self.dirty = False
            self.control.polishItems()
            self.control.beginFrame()
            self.control.sync()
            self.control.render()
            self.control.endFrame()
            if memory or self.interacting is not None:
                frame = self.target.read()
        except Exception as exc:  # noqa: BLE001
            self._card_failed(str(exc))
            publish = False
        finally:
            self.target.end(publish=publish)
        if publish:
            self.host.card_drawn(self.id, self.target)
        if frame is not None:
            if self.interacting is not None:
                self.interacting.show_frame(frame)
            if memory:
                self.host.store(self.id, frame)

    def close(self) -> None:
        import shiboken6

        self.timer.stop()
        self.host.set_sound(self.id, None)
        if self.interacting is not None:
            self.interacting.close()
        target, self.target = self.target, None
        try:
            if self.root is not None:
                shiboken6.delete(self.root)  # the page first: Chromium complains if the profile goes before it
            self.control.invalidate()
            shiboken6.delete(self.window)
            shiboken6.delete(self.control)
        finally:
            if target is not None:
                target.close()


class InteractQuick(QWidget):
    """Interact with a page drawn on the card: its pictures shown here, your clicks, scrolling and typing sent to
    it. What you do shows on stream too."""

    def __init__(self, page: QuickPage, title: str, parent: QWidget | None = None):
        from PySide6.QtWidgets import QLabel, QVBoxLayout

        super().__init__(parent, Qt.WindowType.Window)
        self.page = page
        self.setWindowTitle(f"Interact: {title} · SprigCast")
        self.setObjectName("interactWindow")
        self.setStyleSheet(f"#interactWindow {{ background: {theme.BG}; }}")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 16)
        layout.setSpacing(10)
        note = QLabel("Click and type in the page here. Everything you do shows on stream too.")
        note.setStyleSheet(f"color: {theme.MUTED};")
        layout.addWidget(note)
        self.canvas = _Canvas(self)
        layout.addWidget(self.canvas, 1)
        w, h = page.window.width(), page.window.height()
        self.canvas.setFixedSize(w, h)
        self.resize(min(w + 40, 1400), min(h + 80, 900))
        page.dirty = True
        page._call("focusPage")

    def show_frame(self, frame: np.ndarray) -> None:
        self.canvas.picture = frame
        self.canvas.update()

    def closeEvent(self, event) -> None:
        self.page.interacting = None
        super().closeEvent(event)


class _Canvas(QWidget):
    """Shows the page's newest picture and passes input on to its window."""

    def __init__(self, owner: InteractQuick):
        super().__init__(owner)
        self.owner = owner
        self.picture: np.ndarray | None = None
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def paintEvent(self, event) -> None:
        from PySide6.QtGui import QImage, QPainter

        if self.picture is None:
            return
        h, w = self.picture.shape[:2]
        image = QImage(self.picture.data, w, h, w * 4, QImage.Format.Format_RGBA8888)
        QPainter(self).drawImage(0, 0, image)

    def event(self, event) -> bool:
        from PySide6.QtCore import QCoreApplication

        kinds = (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease, QEvent.Type.MouseButtonDblClick,
                 QEvent.Type.MouseMove, QEvent.Type.Wheel, QEvent.Type.KeyPress, QEvent.Type.KeyRelease)
        if event.type() in kinds:
            QCoreApplication.sendEvent(self.owner.page.window, event.clone())
            self.owner.page.dirty = True
            return True
        return super().event(event)
