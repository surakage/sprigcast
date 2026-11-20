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
from PySide6.QtWidgets import QWidget

from sprigcast.app import theme
from sprigcast.sources import browser

log = logging.getLogger(__name__)

# Settings that need the page loaded again when they change. Size, fps and mute apply in place.
HIDDEN_TICK_MS = 1000  # a paused page's frame timer (it grabs nothing while paused anyway)
RELOAD_KEYS = ("url", "file", "css", "audio_to_mixer")

SHIM = """
(() => {
  if (window.obsstudio) return;
  const status = {recording: false, streaming: false, recordingPaused: false, replaybuffer: false, virtualcam: false};
  const page = {app: "SprigCast", version: "__VERSION__", visible: true, bloom: "", blooms: {}, shape: "",
                streaming: false, recording: false};
  const fire = (name, detail) => window.dispatchEvent(new CustomEvent(name, {detail}));
  window.obsstudio = {
    pluginVersion: "2.24.0",
    sprigcast: true,
    getControlLevel: (cb) => cb(1),
    getStatus: (cb) => cb(status),
    getCurrentScene: (cb) => cb({name: page.bloom, width: window.innerWidth, height: window.innerHeight}),
    getScenes: (cb) => cb(page.bloom ? [page.bloom] : []),
    getCurrentTransition: (cb) => cb(""),
    getTransitions: (cb) => cb([]),
    onVisibilityChange: null,
    onActiveChange: null,
  };
  // SprigCast's own names (the same object as window.obsstudio, so OBS overlays and SprigCast-aware ones agree).
  for (const key of Object.keys(page)) {
    Object.defineProperty(window.obsstudio, key, {get: () => page[key], enumerable: true});
  }
  window.sprigcast = window.obsstudio;
  window.__sprigcastVisible = (shown) => {
    const changed = page.visible !== shown;
    page.visible = shown;
    fire("obsSourceVisibleChanged", {visible: shown});
    fire("obsSourceActiveChanged", {active: shown});
    if (changed) fire("sprigcastVisibleChanged", {visible: shown});
    if (typeof window.obsstudio.onVisibilityChange === "function") window.obsstudio.onVisibilityChange(shown);
    if (typeof window.obsstudio.onActiveChange === "function") window.obsstudio.onActiveChange(shown);
  };
  window.__sprigcastSet = (now) => {
    const was = Object.assign({}, page);
    Object.assign(page, now);
    status.streaming = page.streaming;
    status.recording = page.recording;
    if (now.visible !== undefined && now.visible !== was.visible) window.__sprigcastVisible(now.visible);
    if (page.bloom !== was.bloom) fire("obsSceneChanged", {name: page.bloom, width: window.innerWidth,
                                                            height: window.innerHeight});
    if (page.streaming !== was.streaming) fire(page.streaming ? "obsStreamingStarted" : "obsStreamingStopped", {});
    if (page.recording !== was.recording) fire(page.recording ? "obsRecordingStarted" : "obsRecordingStopped", {});
    fire("sprigcastStateChanged", Object.assign({}, page));
  };
})();
"""


# Tells SprigCast (once per page and kind) when a <video>/<audio> can't play its file, so the source's status
# says so instead of the page showing nothing. Runs in every frame (a clip player may sit in an iframe).
# Chromium in the pip PySide6 wheels has no H.264/AAC: an MP4 clip is blamed on that only while this engine
# can't play H.264, so a page engine with the codecs never shows it.
MEDIA_JS = """
(() => {
  if (window.__sprigcastMediaWatch) return;
  window.__sprigcastMediaWatch = true;
  const told = new Set();
  const tell = (kind, src) => {
    if (told.has(kind)) return;
    told.add(kind);
    console.info("sprigcast:media " + kind + " " + String(src || "").slice(0, 300));
  };
  let h264 = null;
  const noH264 = () => {
    if (h264 === null) h264 = document.createElement("video").canPlayType('video/mp4; codecs="avc1.42E01E"') !== "";
    return !h264;
  };
  const looksH264 = (text) => /avc|h264|mp4a|hvc1|hev1|\\.(mp4|m4v|mov)(\\?|#|$)|video\\/mp4|mpegurl|\\.m3u8/i.test(text || "");
  const blame = (src, type) => (noH264() && looksH264(type + " " + src)) ? "h264" : "format";
  window.addEventListener("error", (e) => {
    const el = e.target;
    if (el instanceof HTMLSourceElement) {
      const media = el.parentElement;
      if (!(media instanceof HTMLMediaElement)) return;
      if ((el.type && media.canPlayType(el.type) === "") || blame(el.src, el.type) === "h264") tell(blame(el.src, el.type), el.src);
    } else if (el instanceof HTMLMediaElement && el.error && el.error.code === 4) {
      tell(blame(el.currentSrc || el.src, ""), el.currentSrc || el.src);
    }
  }, true);
  const MS = window.MediaSource;
  if (MS && MS.prototype.addSourceBuffer) {
    const add = MS.prototype.addSourceBuffer;
    MS.prototype.addSourceBuffer = function (type) {
      try { return add.call(this, type); }
      catch (err) { if (err && err.name === "NotSupportedError") tell(blame("", type), type); throw err; }
    };
  }
})();
"""

MEDIA_PREFIX = "sprigcast:media "
MEDIA_NOTES = {"h264": "Video can't play: page uses H.264 (MP4)", "format": "Video can't play: format not supported"}


def media_note(message: str) -> tuple[str, str] | None:
    """(status, file) for a console line from MEDIA_JS, or None for any other console line."""
    if not message.startswith(MEDIA_PREFIX):
        return None
    kind, _, src = message[len(MEDIA_PREFIX):].partition(" ")
    note = MEDIA_NOTES.get(kind)
    return (note, src) if note else None


RENDERER_LIMIT = 2  # Chromium page processes for every browser source and web dock together


def chromium_flags(current: str = "") -> str:
    """Chromium's start flags with a cap on page processes: each site got its own (60-100 MB each), so five
    overlays from five sites cost five. Two keeps one crashing page from taking all of them down with it.
    Must be set before Qt WebEngine loads (main() does it first thing)."""
    if "--renderer-process-limit" in current:
        return current  # set by hand (to test something): kept
    return f"{current} --renderer-process-limit={RENDERER_LIMIT}".strip()


def shim() -> str:
    from sprigcast import __version__

    return SHIM.replace("__VERSION__", __version__)


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


_ConsolePage = None


def _console_page_class():
    """QWebEnginePage that hands each console line to its `console` callable (MEDIA_JS speaks through it)."""
    global _ConsolePage
    if _ConsolePage is None:
        from PySide6.QtWebEngineCore import QWebEnginePage

        class ConsolePage(QWebEnginePage):
            console = None

            def javaScriptConsoleMessage(self, level, message, line, source):  # Qt's name
                if self.console is not None and message.startswith(MEDIA_PREFIX):
                    self.console(message)

        _ConsolePage = ConsolePage
    return _ConsolePage


def page_noted(page, note: tuple[str, str]) -> None:
    """A page (widget or card) said its video can't play: logged once per page and kind, shown in its status."""
    text, src = note
    if text in page._media_noted:
        return
    page._media_noted.add(text)
    log.warning("browser source %s: %s (%s)", page.id, text.lower(), src or "no file named")
    page.host.set_note(page.id, text)


def forget_notes(page) -> None:
    """The page loads again: what it said before no longer holds."""
    if page._media_noted:
        page._media_noted.clear()
        page.host.set_note(page.id, "")


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
        from PySide6.QtWebEngineCore import QWebEngineScript
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
        self.page = _console_page_class()(shared_profile(), self.view)
        self.page.console = self._console
        self._media_noted: set[str] = set()
        self.page.setBackgroundColor(Qt.GlobalColor.transparent)
        if hasattr(self.page, "permissionRequested"):  # Qt 6.8+: camera, mic, notifications... all no
            self.page.permissionRequested.connect(lambda permission: permission.deny())
        else:
            self.page.featurePermissionRequested.connect(self._deny)
        self.page.renderProcessTerminated.connect(self._crashed)
        self.page.loadFinished.connect(self._loaded)
        self.view.setPage(self.page)
        self._Script = QWebEngineScript
        self.interacting: InteractWindow | None = None
        self.sound = None  # PageSound while the page's sound goes to the mixer (app/browser_audio.py)
        self._channel = None
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
        fps = browser.page_fps(settings)
        self.timer.start(max(4, round(1000 / fps)))
        if any(old.get(k) != settings.get(k) for k in RELOAD_KEYS):
            self._set_up_sound()
            self._scripts()
            forget_notes(self)
            url = browser.page_url(settings)
            self.page.setUrl(QUrl(url) if url else QUrl("about:blank"))
        self._mute()

    def _mute(self) -> None:
        """Silent on the desktop when muted, or when the mixer plays the page's sound instead."""
        to_mixer = self.sound is not None and not self.sound.on_desktop_reason
        self.page.setAudioMuted(bool(self.settings.get("mute", False)) or to_mixer)

    def _set_up_sound(self) -> None:
        from sprigcast.sources.browser import sound_to_mixer

        if not sound_to_mixer(self.settings):
            self.sound = None
            self.host.set_sound(self.id, None)
            return
        from PySide6.QtWebChannel import QWebChannel

        from sprigcast.app.browser_audio import PageSound

        self.sound = PageSound(on_desktop=lambda _why: self._mute())
        if self._channel is not None:
            self._channel.deleteLater()
        self._channel = QWebChannel(self.page)
        self.page.setWebChannel(self._channel)
        self._channel.registerObject("sprigcastAudio", self.sound)
        self.host.set_sound(self.id, self.sound)

    def _scripts(self) -> None:
        S = self._Script
        scripts = self.page.scripts()
        scripts.clear()
        wanted = [
            ("sprigcast-obs", shim(), S.InjectionPoint.DocumentCreation),
            ("sprigcast-media", MEDIA_JS, S.InjectionPoint.DocumentCreation),
            ("sprigcast-css", css_script(str(self.settings.get("css", "") or "")), S.InjectionPoint.DocumentReady),
        ]
        if self.sound is not None:
            from sprigcast.app.browser_audio import CAPTURE_JS, qwebchannel_js

            wanted[1:1] = [("sprigcast-channel", qwebchannel_js(), S.InjectionPoint.DocumentCreation),
                           ("sprigcast-sound", CAPTURE_JS, S.InjectionPoint.DocumentCreation)]
        for name, source, point in wanted:
            script = S()
            script.setName(name)
            script.setSourceCode(source)
            script.setInjectionPoint(point)
            script.setWorldId(S.ScriptWorldId.MainWorld)
            script.setRunsOnSubFrames(name == "sprigcast-media")
            scripts.insert(script)

    # Events ----------------------------------------------------------------------

    def _console(self, message: str) -> None:
        note = media_note(message)
        if note is not None:
            page_noted(self, note)

    def _deny(self, origin, feature) -> None:
        from PySide6.QtWebEngineCore import QWebEnginePage

        self.page.setFeaturePermission(origin, feature, QWebEnginePage.PermissionPolicy.PermissionDeniedByUser)

    def _loaded(self, ok: bool) -> None:
        self.dirty = True
        self.push_state()
        if not ok:
            log.info("browser source %s: page didn't load", self.id)

    def _crashed(self, status, code) -> None:
        log.warning("browser source %s: page stopped (%s, %s), loading it again", self.id, status, code)
        QTimer.singleShot(2000, self.view.reload)

    def push_state(self) -> None:
        """Tell the page where it is (window.sprigcast: visible, bloom on air, live, recording)."""
        import json

        state = {**self.host.page_state(self.id), "visible": not self.paused}
        self.page.runJavaScript(f"window.__sprigcastSet && window.__sprigcastSet({json.dumps(state)})")

    def set_paused(self, paused: bool) -> None:
        if paused == self.paused:
            return
        self.paused = paused
        self.page.runJavaScript(f"window.__sprigcastVisible && window.__sprigcastVisible({'false' if paused else 'true'})")
        # A hidden page kept running (an alert box that must stay connected) is told it is hidden, as a
        # background browser tab is: Chromium stops painting it and runs its timers about once a second,
        # while its connections stay open. Our own frame timer slows down to match.
        try:
            self.page.setVisible(not paused)
        except AttributeError:  # older Qt
            pass
        self.timer.start(HIDDEN_TICK_MS if paused else max(4, round(1000 / browser.page_fps(self.settings))))
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

    def reload(self) -> None:
        forget_notes(self)
        self.page.triggerAction(self.page.WebAction.ReloadAndBypassCache)

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
        self.host.set_sound(self.id, None)
        if self.interacting is not None:
            self.interacting.scroll.takeWidget()
            self.interacting.close()
        # Deleted now, not later: Chromium complains (and can crash on exit) if the profile goes first.
        shiboken6.delete(self.page)
        shiboken6.delete(self.view)


class InteractWindow(QWidget):
    """The page itself, on screen, so you can click and type in it: sign in to a widget, close a
    cookie banner, press a button. What you do shows on stream too. Closing puts it back off screen."""

    def __init__(self, page: "_Page", title: str, parent: QWidget | None = None):
        from PySide6.QtWidgets import QLabel, QScrollArea, QVBoxLayout

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
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(False)
        self.scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        layout.addWidget(self.scroll, 1)
        view = page.view
        size = view.size()
        view.hide()
        view.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, False)
        self.scroll.setWidget(view)
        view.resize(size)
        view.show()
        view.setFocus()
        self.resize(min(size.width() + 40, 1400), min(size.height() + 80, 900))

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        view = self.scroll.takeWidget()
        if view is not None:
            size = view.size()
            view.hide()
            view.setParent(None)
            view.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
            view.resize(size)
            view.show()
        self.page.interacting = None
        super().closeEvent(event)


class BrowserHost(QObject):
    """Owns the pages. open/close/pause/take may be called from the render thread."""

    _request = Signal(str, str, object)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.pages: dict[str, _Page] = {}
        self._frames: dict[str, np.ndarray] = {}
        self._sounds: dict[str, Any] = {}  # source id -> PageSound, read by the audio thread
        self._notes: dict[str, str] = {}  # source id -> what its page said is wrong (a video that can't play)
        self._lock = threading.Lock()
        self._request.connect(self._handle, Qt.ConnectionType.QueuedConnection)
        self._shut = False
        # What pages see in window.sprigcast: set by the window (state_of), looked at once a second while pages exist.
        self.state_of = None  # callable: () -> {"blooms": {shape: name}, "streaming", "recording", "shapes": {id: shape}}
        self._state: dict[str, Any] = {}
        self._state_ticking = False  # a one-shot check is queued (no timer object: nothing ties the host's life to it)
        # Pages drawn on the native engine's card (app/browser_quick.py), set by the browser helper: card is the
        # engine's LUID; target_for(id, w, h, device) makes a sprigcore.PageTarget; card_drawn(id, target) tells
        # the engine a new picture is there.
        self.card: tuple[int, int] | None = None
        self.target_for = None
        self.card_drawn = None
        self.page_closed = None  # callable(source id): its page is gone now (the helper lets go of its block)

    # Any thread ---------------------------------------------------------------------

    def open(self, source_id: str, settings: dict[str, Any]) -> None:
        self._request.emit("open", source_id, dict(settings))

    def close(self, source_id: str) -> None:
        self._request.emit("close", source_id, None)
        with self._lock:
            self._frames.pop(source_id, None)
            self._notes.pop(source_id, None)

    def pause(self, source_id: str, paused: bool) -> None:
        self._request.emit("pause", source_id, paused)

    def take(self, source_id: str) -> np.ndarray | None:
        with self._lock:
            return self._frames.pop(source_id, None)

    def read_audio(self, source_id: str, n: int) -> np.ndarray | None:
        """The page's next n frames of sound for the mixer, or None when it plays on the desktop."""
        sound = self._sounds.get(source_id)
        if sound is None or sound.on_desktop_reason:
            return None
        return sound.read(n)

    def sound_note(self, source_id: str) -> str:
        sound = self._sounds.get(source_id)
        return "plays on desktop audio" if sound is not None and sound.on_desktop_reason else ""

    def page_note(self, source_id: str) -> str:
        """What the page said is wrong, for the source's status ("" when nothing)."""
        return self._notes.get(source_id, "")

    def set_note(self, source_id: str, text: str) -> None:
        with self._lock:
            if text:
                self._notes[source_id] = text
            else:
                self._notes.pop(source_id, None)

    def set_sound(self, source_id: str, sound) -> None:
        with self._lock:
            if sound is None:
                self._sounds.pop(source_id, None)
            else:
                self._sounds[source_id] = sound

    def interact(self, source_id: str, settings: dict[str, Any], title: str, parent: QWidget | None = None):
        """UI thread: open the page in a window you can click in (opening it first if it's asleep)."""
        page = self.pages.get(source_id)
        if page is None:
            self._handle("open", source_id, dict(settings))
            page = self.pages.get(source_id)
        if page is None:
            return None
        if page.interacting is not None:
            page.interacting.raise_()
            page.interacting.activateWindow()
            return page.interacting
        if hasattr(page, "want_memory"):
            from sprigcast.app.browser_quick import InteractQuick

            page.interacting = InteractQuick(page, title, parent)
        else:
            page.interacting = InteractWindow(page, title, parent)
        page.interacting.show()
        return page.interacting

    def reload(self, source_id: str) -> None:
        self._request.emit("reload", source_id, None)

    def want_memory(self, source_id: str) -> None:
        """SprigCast reads this page's pictures itself for now (only pages drawn on the card care)."""
        self._request.emit("memory", source_id, None)

    # UI thread ----------------------------------------------------------------------

    def page_state(self, source_id: str) -> dict[str, Any]:
        """window.sprigcast for one page: the bloom on air in the shape the page is shown in (else the first)."""
        state = self._state or {}
        blooms = dict(state.get("blooms", {}))
        shape = (state.get("shapes", {}) or {}).get(source_id) or next(iter(blooms), "")
        return {"bloom": blooms.get(shape, ""), "blooms": blooms, "shape": shape,
                "streaming": bool(state.get("streaming")), "recording": bool(state.get("recording"))}

    def _tick_state(self) -> None:
        """Look at the state again in a second, while pages exist. Holds the host only weakly."""
        import weakref

        if self._state_ticking:
            return
        self._state_ticking = True
        ref = weakref.ref(self)

        def later():
            host = ref()
            if host is None or host._shut:
                return
            host._state_ticking = False
            host._check_state()
            if host.pages:
                host._tick_state()

        QTimer.singleShot(1000, later)

    def _check_state(self) -> None:
        if not self.pages:
            return
        try:
            now = self.state_of() if self.state_of is not None else {}
        except Exception:
            log.debug("page state", exc_info=True)
            return
        if now != self._state:
            self._state = now
            for page in self.pages.values():
                page.push_state()

    def store(self, source_id: str, frame: np.ndarray) -> None:
        with self._lock:
            self._frames[source_id] = frame

    @Slot(str, str, object)
    def _handle(self, action: str, source_id: str, value) -> None:
        if self._shut:
            return  # a request still queued when SprigCast closed: no new pages after shutdown
        page = self.pages.get(source_id)
        try:
            if action == "open":
                if page is None:
                    self.pages[source_id] = self._new_page(source_id, value)
                    self._check_state()
                    self._tick_state()
                else:
                    page.apply(value)
            elif action == "close":
                try:
                    if page is not None:
                        del self.pages[source_id]
                        page.close()
                finally:
                    if self.page_closed is not None:
                        self.page_closed(source_id)
            elif action == "pause" and page is not None:
                page.set_paused(bool(value))
            elif action == "reload" and page is not None:
                page.reload()
            elif action == "memory" and hasattr(page, "want_memory"):
                page.want_memory()
        except Exception:  # a broken page must never take the app down
            log.exception("browser source %s: %s failed", source_id, action)

    def _new_page(self, source_id: str, settings: dict[str, Any]):
        """On the engine's card when the helper was given one, else (or if that fails) a widget page."""
        if self.card is not None and self.target_for is not None:
            from sprigcast.app import browser_quick

            try:
                return browser_quick.QuickPage(self, source_id, settings)
            except Exception as exc:  # noqa: BLE001 - the widget page still works
                log.warning("browser source %s: can't be drawn on the card (%s), drawn in memory", source_id, exc)
        return _Page(self, source_id, settings)

    def shutdown(self) -> None:
        self._shut = True
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
    if in_helper():
        from sprigcast.app.browser_helper import RemoteHost

        host = RemoteHost()  # pages in their own process: they can never freeze the window or the renderer
    else:
        host = BrowserHost(parent)
    browser.set_host(host)
    return host


def in_helper() -> bool:
    """Browser sources run in a helper process (app/browser_helper.py) unless SPRIGCAST_BROWSER_IN_PROCESS is set."""
    import os

    return not os.environ.get("SPRIGCAST_BROWSER_IN_PROCESS")
