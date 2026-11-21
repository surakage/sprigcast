"""Web panels: your own web pages (an activity feed, alerts, Streamer.bot) as panels (app/panels.py).

Chat isn't one of these: the built-in Chat panel (app/chat_panel.py) reads your chats natively or
shows a chat link itself, so chat never needs a second panel.

The list lives in QSettings ("docks/custom") as JSON. A dock's browser is only created the first time
the panel is shown, and Qt WebEngine is only loaded when at least one custom dock exists, so having none
costs nothing. Pages share one saved browser profile, so signing in to Twitch once is remembered.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import asdict, dataclass

from PySide6.QtCore import QSettings, QStandardPaths, Qt, QTimer, QUrl
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from sprigcast.app import theme
from sprigcast.app.card import CardDialog

log = logging.getLogger(__name__)
SETTINGS_KEY = "docks/custom"
PREFIX = "custom:"  # panel key prefix, so saved panels remember where each one sits

# Ready-made pages: label -> (dock name, URL with {channel}).
PRESETS = {
    "Twitch activity feed": ("Activity", "https://dashboard.twitch.tv/popout/u/{channel}/stream-manager/activity-feed"),
    "Twitch quick actions": ("Quick actions", "https://dashboard.twitch.tv/popout/u/{channel}/stream-manager/quick-actions"),
    "Custom page": ("", ""),
}


@dataclass
class CustomDock:
    id: str
    name: str
    url: str


def load() -> list[CustomDock]:
    try:
        raw = json.loads(str(QSettings().value(SETTINGS_KEY, "[]") or "[]"))
        return [CustomDock(**d) for d in raw if d.get("name") and d.get("url")]
    except (ValueError, TypeError):
        return []


def save(docks: list[CustomDock]) -> None:
    QSettings().setValue(SETTINGS_KEY, json.dumps([asdict(d) for d in docks]))


def normalise_url(text: str) -> str:
    text = text.strip()
    if text and not re.match(r"^[a-z][a-z0-9+.-]*://", text, re.I):
        text = "https://" + text
    return text


def preset_url(preset: str, channel: str) -> str:
    return PRESETS[preset][1].format(channel=channel.strip().lower()) if channel.strip() else ""


def import_web_engine() -> bool:
    """Qt WebEngine imported now (it wants to be before the QApplication when there are web panels)."""
    try:
        from PySide6 import QtWebEngineWidgets  # noqa: F401
    except ImportError:
        return False
    return True


def web_engine_available() -> bool:
    """Is Qt WebEngine installed? Found, not imported: the first import (Chromium's bindings) was 51 ms of opening
    Panels → Web panels on the PC while live (2026-11-18); it's imported when a web panel is first made."""
    import importlib.util

    try:
        return importlib.util.find_spec("PySide6.QtWebEngineWidgets") is not None
    except (ImportError, ValueError):
        return False


_profile = None


def _shared_profile():
    """One persistent browser profile for every custom dock, so sign-ins and cookies are kept."""
    global _profile
    if _profile is None:
        from PySide6.QtWebEngineCore import QWebEngineProfile

        _profile = QWebEngineProfile("sprigcast-docks")
        base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)
        _profile.setPersistentStoragePath(f"{base}/web-docks")
        _profile.setCachePath(f"{base}/web-docks-cache")
        _profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies)
    return _profile


def own_window() -> bool:
    """Web views get a window of their own inside the panel (below) unless SPRIGCAST_WEB_IN_WINDOW=1."""
    import os

    return os.environ.get("SPRIGCAST_WEB_IN_WINDOW") != "1"


def warm_engine() -> bool:
    """Start the browser engine now (the shared profile starts it): at start-up, before the window is shown, when
    there are web panels, so the first one opened mid-stream doesn't stop the window while it starts."""
    import time

    if _profile is not None or not web_engine_available():
        return False
    began = time.perf_counter()
    try:
        _shared_profile()
    except Exception:  # only a head start
        log.warning("web panels: couldn't start the browser engine ahead", exc_info=True)
        return False
    log.info("web panels: the browser engine was started ahead (%d ms)", (time.perf_counter() - began) * 1000)
    return True


class WebDockBody(QWidget):
    """Holds the page. The browser is made the first time the panel is shown, never before.

    The browser is a window of its own, set inside the panel, and never a widget of SprigCast's window: the first
    web view put into a window as a widget makes Qt destroy that whole window and build it again for the graphics
    card, so SprigCast vanished and came back (and stopped for 1.3 s) the moment a chat link or web panel was
    first shown (PC, live, 2026-11-21). In its own window only that small window is drawn by the graphics card,
    and SprigCast's window is left as it is, so a PC that shows no web page pays nothing.

    How: an empty QWindow sits in the panel through Qt's window container, which keeps it in place, shown and
    hidden with the panel and moved along when the panel is popped out; the web view is a frameless window put
    inside that one and kept its size."""

    def __init__(self, url: str):
        super().__init__()
        self.url = url
        self.view = None
        self._host = None  # the empty window in the panel that the web view's window sits in
        self._holder = None  # the widget that keeps _host in place
        self._attached_to = None  # the top window the view was last set into
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self.placeholder = QLabel("Loading…")
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.placeholder.setStyleSheet(f"color: {theme.MUTED}")
        self._layout.addWidget(self.placeholder)

    def ensure_loaded(self) -> None:
        if self.view is not None:
            return
        try:
            from PySide6.QtWebEngineCore import QWebEnginePage
            from PySide6.QtWebEngineWidgets import QWebEngineView
        except ImportError:
            self.placeholder.setText("Web docks need Qt WebEngine, which isn't installed.")
            return
        if own_window():
            from PySide6.QtGui import QWindow

            # No parent widget: a web view anywhere under SprigCast's window, even as a window of its own, marks
            # that window for rebuilding. It's deleted with this panel body instead.
            self.view = view = QWebEngineView()
            self.view.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
            self.destroyed.connect(view.deleteLater)
            # Its own native window now, still hidden, before it gets a page: a page added to a view that has
            # none yet makes Qt look further up for a window to rebuild, and finds SprigCast's.
            self.view.create()
            self._host = QWindow()
            self._holder = QWidget.createWindowContainer(self._host, self)
            self._host.widthChanged.connect(self._fit)
            self._host.heightChanged.connect(self._fit)
        else:
            self.view = QWebEngineView()
        self.view.setPage(QWebEnginePage(_shared_profile(), self.view))
        if self.url and self.url != "about:blank":  # made ahead (the Chat panel's link): no page yet
            self.view.setUrl(QUrl(self.url))
        self._layout.removeWidget(self.placeholder)
        self.placeholder.hide()
        self._layout.addWidget(self._holder if self._holder is not None else self.view)
        if self._holder is not None:
            self._holder.installEventFilter(self)
            if self._holder.isVisible():
                self._attach()

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 (Qt naming)
        from PySide6.QtCore import QEvent

        if watched is self._holder and event.type() == QEvent.Type.Show:
            self._attach()
        return False

    def _attach(self) -> None:
        """Put the web view's window inside the panel's (the first time it shows, and again after the panel moved
        to another window). It's shown out of sight first: until it is inside, it's a window on the desktop."""
        top = self.window()
        if self.view is None or self._host is None or self._attached_to is top:
            return
        self._attached_to = top
        self._host.create()
        self.view.windowHandle().setParent(self._host)
        self.view.setGeometry(-32000, -32000, max(1, self._host.width()), max(1, self._host.height()))
        self.view.show()
        self._settled = False
        QTimer.singleShot(0, self, self._settle)

    def _settle(self) -> None:
        # Showing a widget's window sets it back on the desktop on some systems: set inside again once shown.
        if self.view is None or self._host is None:
            return
        handle = self.view.windowHandle()
        handle.setParent(None)
        handle.setParent(self._host)
        self._settled = True
        self._fit()

    def _fit(self, *_) -> None:
        if self.view is not None and self._host is not None and getattr(self, "_settled", False):
            self.view.setGeometry(0, 0, max(1, self._host.width()), max(1, self._host.height()))

    def set_url(self, url: str) -> None:
        self.url = url
        if self.view is not None:
            self.view.setUrl(QUrl(url))

    def reload(self) -> None:
        if self.view is not None:
            self.view.reload()


class CustomDocksDialog(CardDialog):
    """Add, rename, re-point and remove web panels."""

    def __init__(self, docks: list[CustomDock], parent: QWidget | None = None):
        super().__init__("Web panels", parent, width=700)
        from sprigcast.app.card import combo
        from sprigcast.app.settings_dialog import FORM_STYLE, _form, _name
        from sprigcast.app.states_dialog import LIST_STYLE

        self.setStyleSheet(FORM_STYLE)
        self.docks = [CustomDock(**asdict(d)) for d in docks]
        root = self.body
        body = QHBoxLayout()
        body.setSpacing(16)
        self.list = QListWidget()
        self.list.setStyleSheet(LIST_STYLE)
        self.list.setFixedHeight(190)
        self.list.currentRowChanged.connect(self._show)
        body.addWidget(self.list, 1)
        form_box = QWidget()
        form = _form(form_box)
        self.preset = combo()
        self.preset.addItems(list(PRESETS))
        self.preset.currentTextChanged.connect(self._preset_changed)
        self.channel = QLineEdit()
        self.channel.setPlaceholderText("Your Twitch name")
        self.channel.textEdited.connect(self._channel_edited)
        self.name = QLineEdit()
        self.name.textEdited.connect(self._edited)
        self.url = QLineEdit()
        self.url.setPlaceholderText("https://…")
        self.url.textEdited.connect(self._edited)
        form.addRow(_name("Page"), self.preset)
        form.addRow(_name("Channel"), self.channel)
        form.addRow(_name("Panel name"), self.name)
        form.addRow(_name("URL"), self.url)
        body.addWidget(form_box, 2)
        root.addLayout(body)
        row = QHBoxLayout()
        row.setSpacing(8)
        add = QPushButton("+ Add panel")
        add.clicked.connect(self._add)
        self.remove = QPushButton("Remove")
        self.remove.clicked.connect(self._remove)
        for button in (add, self.remove):
            button.setStyleSheet(theme.button_style(15, "7px 14px"))
            button.setAutoDefault(False)
            row.addWidget(button)
        row.addStretch(1)
        root.addLayout(row)
        self.ok = self.add_buttons("Save")
        self._loading = False
        self._refill()

    def _refill(self, select: int = 0) -> None:
        self._loading = True
        self.list.clear()
        for d in self.docks:
            self.list.addItem(QListWidgetItem(d.name))
        self._loading = False
        if self.docks:
            self.list.setCurrentRow(min(select, len(self.docks) - 1))
        self._show(self.list.currentRow())

    def _current(self) -> CustomDock | None:
        row = self.list.currentRow()
        return self.docks[row] if 0 <= row < len(self.docks) else None

    def _show(self, row: int) -> None:
        dock = self._current()
        for widget in (self.preset, self.channel, self.name, self.url, self.remove):
            widget.setEnabled(dock is not None)
        if dock is None:
            return
        self._loading = True
        self.name.setText(dock.name)
        self.url.setText(dock.url)
        self.preset.setCurrentText("Custom page")
        for label, (_, pattern) in PRESETS.items():
            m = re.fullmatch(re.escape(pattern).replace(re.escape("{channel}"), "([^/]+)"), dock.url) if pattern else None
            if m:
                self.preset.setCurrentText(label)
                self.channel.setText(m.group(1))
                break
        self.channel.setEnabled(self.preset.currentText() != "Custom page")
        self._loading = False

    def _add(self) -> None:
        self.docks.append(CustomDock(uuid.uuid4().hex[:8], "Activity", ""))
        self._refill(len(self.docks) - 1)
        self.preset.setCurrentText("Twitch activity feed")
        self._preset_changed("Twitch activity feed")
        self.channel.setFocus()

    def _remove(self) -> None:
        row = self.list.currentRow()
        if 0 <= row < len(self.docks):
            del self.docks[row]
            self._refill(row)

    def _preset_changed(self, label: str) -> None:
        if self._loading or self._current() is None:
            return
        self.channel.setEnabled(label != "Custom page")
        name, _ = PRESETS[label]
        if name:
            self.name.setText(name)
            self.url.setText(preset_url(label, self.channel.text()))
        self._edited()

    def _channel_edited(self) -> None:
        if self.preset.currentText() != "Custom page":
            self.url.setText(preset_url(self.preset.currentText(), self.channel.text()))
        self._edited()

    def _edited(self) -> None:
        dock = self._current()
        if dock is None or self._loading:
            return
        dock.name = self.name.text().strip() or "Dock"
        dock.url = normalise_url(self.url.text())
        self.list.currentItem().setText(dock.name)

    def result_docks(self) -> list[CustomDock]:
        return [d for d in self.docks if d.url]


def apply(window, docks: list[CustomDock]) -> None:
    """Make window's web panels match `docks`: add new ones (shown on Live), update changed ones,
    remove deleted ones."""
    from sprigcast.app.panels import Panel

    panels = window.panels
    if docks and not window.isVisible():  # start-up: the engine starts now, not when a panel is first opened live
        warm_engine()
    current = {key[len(PREFIX):]: panel for key, panel in panels.panels.items() if key.startswith(PREFIX)}
    wanted = {d.id: d for d in docks}
    for dock_id in current:
        if dock_id not in wanted:
            panels.remove(PREFIX + dock_id)
    for spec in docks:
        panel = current.get(spec.id)
        if panel is None:
            body = WebDockBody(spec.url)
            panels.add(Panel(PREFIX + spec.id, spec.name, body, on_show=body.ensure_loaded), shown_in=("live",))
        else:
            if panel.title != spec.name:
                panel.title = spec.name
                panels.refresh()
            if panel.widget.url != spec.url:
                panel.widget.set_url(spec.url)
    save(docks)
