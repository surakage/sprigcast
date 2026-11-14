"""Web panels: your own web pages (Twitch chat, an activity feed, alerts) as panels (app/panels.py).

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

from PySide6.QtCore import QSettings, QStandardPaths, Qt, QUrl
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
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

log = logging.getLogger(__name__)
SETTINGS_KEY = "docks/custom"
PREFIX = "custom:"  # panel key prefix, so saved panels remember where each one sits

# Ready-made pages: label -> (dock name, URL with {channel}).
PRESETS = {
    "Twitch chat": ("Chat", "https://www.twitch.tv/popout/{channel}/chat"),
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


def web_engine_available() -> bool:
    try:
        from PySide6 import QtWebEngineWidgets  # noqa: F401
    except ImportError:
        return False
    return True


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


class WebDockBody(QWidget):
    """Holds the page. The browser is made the first time the panel is shown, never before."""

    def __init__(self, url: str):
        super().__init__()
        self.url = url
        self.view = None
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
        self.view = QWebEngineView()
        self.view.setPage(QWebEnginePage(_shared_profile(), self.view))
        self.view.setUrl(QUrl(self.url))
        self._layout.removeWidget(self.placeholder)
        self.placeholder.hide()
        self._layout.addWidget(self.view)

    def set_url(self, url: str) -> None:
        self.url = url
        if self.view is not None:
            self.view.setUrl(QUrl(url))

    def reload(self) -> None:
        if self.view is not None:
            self.view.reload()


class CustomDocksDialog(QDialog):
    """Add, rename, re-point and remove web panels."""

    def __init__(self, docks: list[CustomDock], parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Web panels")
        self.setMinimumWidth(560)
        self.docks = [CustomDock(**asdict(d)) for d in docks]
        root = QVBoxLayout(self)
        intro = QLabel("Web pages as panels: Twitch chat, your activity feed, alerts or any page. "
                       "Each shows up in the Panels menu; a new one appears on Live, and ↗ pops it out.")
        intro.setWordWrap(True)
        root.addWidget(intro)
        body = QHBoxLayout()
        self.list = QListWidget()
        self.list.currentRowChanged.connect(self._show)
        body.addWidget(self.list, 1)
        form_box = QWidget()
        form = QFormLayout(form_box)
        self.preset = QComboBox()
        self.preset.addItems(list(PRESETS))
        self.preset.currentTextChanged.connect(self._preset_changed)
        self.channel = QLineEdit()
        self.channel.setPlaceholderText("your Twitch name")
        self.channel.textEdited.connect(self._channel_edited)
        self.name = QLineEdit()
        self.name.textEdited.connect(self._edited)
        self.url = QLineEdit()
        self.url.setPlaceholderText("https://…")
        self.url.textEdited.connect(self._edited)
        form.addRow("Page", self.preset)
        form.addRow("Channel", self.channel)
        form.addRow("Panel name", self.name)
        form.addRow("URL", self.url)
        body.addWidget(form_box, 2)
        root.addLayout(body)
        row = QHBoxLayout()
        add = QPushButton("+ Add panel")
        add.clicked.connect(self._add)
        self.remove = QPushButton("− Remove")
        self.remove.clicked.connect(self._remove)
        row.addWidget(add)
        row.addWidget(self.remove)
        row.addStretch(1)
        root.addLayout(row)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
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
        self.docks.append(CustomDock(uuid.uuid4().hex[:8], "Chat", ""))
        self._refill(len(self.docks) - 1)
        self.preset.setCurrentText("Twitch chat")
        self._preset_changed("Twitch chat")
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
