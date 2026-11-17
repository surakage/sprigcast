"""A web page's settings, as one rounded card: where the page comes from, its size, its sound, how it
rests when it's not on screen, and its look. Same keys as sources/browser.py DEFAULTS.

    ╭ Web page ─────────────────────────────╮
    │ [ Link | File on this PC ]            │
    │ https://…                             │
    │ Size  1920 × 1080      Frame rate …   │
    │ Sound      ○ Its own strip in SprigCast│
    │ Resting    ○ Sleep while not on screen │
    │            ○ Reload when on air        │
    │ ▸ Custom CSS                           │
    │ Reload page             Cancel  [Save] │
    ╰────────────────────────────────────────╯
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from sprigcast.app import theme
from sprigcast.app.card import FIELD_STYLE, CardDialog
from sprigcast.app.live_stage import Switch
from sprigcast.sources.browser import DEFAULT_CSS, DEFAULTS, STANDARD_FPS, page_fps

SPIN_STYLE = (
    f"QSpinBox {{ background: {theme.CARD}; border: 1px solid {theme.EDGE_STRONG}; border-radius: 15px;"
    f" padding: 5px 10px; color: {theme.TEXT}; }} QSpinBox:focus {{ border-color: {theme.ACCENT}; }}"
    "QSpinBox::up-button, QSpinBox::down-button { width: 0px; border: none; }"
)
CHIP = theme.button_style(15, "7px 14px")


def _muted(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet(f"color: {theme.MUTED}; background: transparent;")
    return label


def _section() -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName("pageSection")
    frame.setStyleSheet(f"#pageSection {{ background: {theme.CARD}; border-radius: 16px; }}")
    box = QVBoxLayout(frame)
    box.setContentsMargins(16, 14, 16, 14)
    box.setSpacing(10)
    return frame, box


def _title(text: str) -> QLabel:
    label = QLabel(text)
    label.setStyleSheet(f"color: {theme.TEXT_BRIGHT}; font-weight: 600; background: transparent;")
    return label


class WebPageDialog(CardDialog):
    """New or change a web page. values() gives the settings; on_reload (when set) reloads it now."""

    def __init__(self, title: str, settings: dict[str, Any], parent: QWidget | None = None,
                 on_reload: Callable[[], None] | None = None, primary: str = "Save"):
        super().__init__(title, parent, width=560)
        s = {**DEFAULTS, **settings}
        self.switches: dict[str, Switch] = {}

        # Where the page comes from: a link, or an .html file on this PC.
        where, box = _section()
        tabs = QHBoxLayout()
        tabs.setSpacing(6)
        self.link_tab, self.file_tab = QPushButton("Link"), QPushButton("File on this PC")
        for tab in (self.link_tab, self.file_tab):
            tab.setCheckable(True)
            tab.setAutoExclusive(True)
            tab.setStyleSheet(CHIP)
            tab.setCursor(Qt.CursorShape.PointingHandCursor)
            tabs.addWidget(tab)
        tabs.addStretch(1)
        box.addLayout(tabs)
        self.url = self.line_edit("Paste the page's link, like an alert box or chat widget")
        url = str(s.get("url", "") or "")
        self.url.setText("" if url in ("https://", "http://") else url)
        box.addWidget(self.url)
        file_row = QHBoxLayout()
        self.file = self.line_edit("Pick an .html file")
        self.file.setText(str(s.get("file", "") or ""))
        browse = QPushButton("Choose…")
        browse.setStyleSheet(CHIP)
        browse.setCursor(Qt.CursorShape.PointingHandCursor)
        browse.clicked.connect(self._pick_file)
        file_row.addWidget(self.file, 1)
        file_row.addWidget(browse)
        self.file_box = QWidget()
        self.file_box.setLayout(file_row)
        file_row.setContentsMargins(0, 0, 0, 0)
        box.addWidget(self.file_box)
        self.link_tab.toggled.connect(self._show_where)
        (self.file_tab if self.file.text() else self.link_tab).setChecked(True)
        self._show_where()
        self.body.addWidget(where)

        # Size and frame rate, side by side.
        look, box = _section()
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        grid.addWidget(_title("Size"), 0, 0)
        self.width_box, self.height_box = self._spin(int(s["width"]), 1, 7680), self._spin(int(s["height"]), 1, 7680)
        size = QHBoxLayout()
        size.setSpacing(6)
        size.addWidget(self.width_box)
        size.addWidget(_muted("×"))
        size.addWidget(self.height_box)
        size.addStretch(1)
        grid.addLayout(size, 1, 0)
        grid.addWidget(_title("Frame rate"), 0, 1)
        rate = QHBoxLayout()
        rate.setSpacing(8)
        fps = page_fps(settings)  # the page's own settings: an older page at 60 counts as custom
        self.custom_fps = self._switch("custom_fps", fps != STANDARD_FPS or bool(settings.get("custom_fps")))
        rate.addWidget(self.custom_fps)
        self.fps_box = self._spin(fps if fps != STANDARD_FPS else int(s["fps"] or STANDARD_FPS), 1, 120)
        self.fps_note = _muted(f"Standard ({STANDARD_FPS})")
        rate.addWidget(self.fps_note)
        rate.addWidget(self.fps_box)
        rate.addStretch(1)
        grid.addLayout(rate, 1, 1)
        self.custom_fps.toggled.connect(self._show_fps)
        self._show_fps()
        box.addLayout(grid)
        box.addWidget(_muted("Most alerts and widgets look right at the standard rate, and it's lighter. "
                             "Turn on your own rate for smooth animations."))
        self.body.addWidget(look)

        # Its sound, and how it rests.
        how, box = _section()
        self._switch_row(box, "audio_to_mixer", bool(s["audio_to_mixer"]), "Sound in SprigCast",
                         "Its own strip under the canvas. Off: your speakers only.")
        self._switch_row(box, "mute", bool(s["mute"]), "Silent", "No sound from this page at all.")
        self._switch_row(box, "sleep", not bool(s["keep_running"]), "Sleep while not on screen",
                         "Closes the page while it's off screen, so it uses nothing.")
        self._switch_row(box, "reload_on_air", bool(s.get("reload_on_air", False)), "Reload when on air",
                         "Starts the page fresh each time a bloom with it goes on air.")
        self.body.addWidget(how)

        # Custom CSS, folded away until you want it.
        self.css_toggle = QPushButton()
        self.css_toggle.setStyleSheet(theme.LINK_STYLE + "QPushButton { text-align: left; }")
        self.css_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.css_toggle.clicked.connect(lambda: self._show_css(not self.css.isVisible()))
        self.css = QPlainTextEdit(str(s.get("css", DEFAULT_CSS)))
        self.css.setFixedHeight(96)
        self.css.setStyleSheet(f"QPlainTextEdit {{ background: {theme.CARD}; border: 1px solid {theme.EDGE_STRONG};"
                               f" border-radius: 14px; padding: 8px; color: {theme.TEXT};"
                               " font-family: Consolas, 'Cascadia Mono', monospace; }")
        self.body.addWidget(self.css_toggle)
        self.body.addWidget(self.css)
        self._show_css(self.css.toPlainText().strip() not in ("", DEFAULT_CSS))

        yes = self.add_buttons(primary)
        self.yes = yes
        if on_reload is not None:  # a Reload button, bottom left beside Cancel
            reload = QPushButton("Reload page")
            reload.setStyleSheet(CHIP)
            reload.setCursor(Qt.CursorShape.PointingHandCursor)
            reload.setToolTip("Load the page again now, like pressing F5")
            reload.clicked.connect(on_reload)
            self.reload_button = reload
            self.body.itemAt(self.body.count() - 1).layout().insertWidget(0, reload)

    # Pieces -----------------------------------------------------------------------

    def _spin(self, value: int, low: int, high: int) -> QSpinBox:
        box = QSpinBox()
        box.setRange(low, high)
        box.setValue(value)
        box.setStyleSheet(SPIN_STYLE)
        box.setMinimumWidth(78)
        box.setAlignment(Qt.AlignmentFlag.AlignCenter)
        return box

    def _switch(self, key: str, on: bool) -> Switch:
        switch = Switch()
        switch.setChecked(on)
        switch.setCursor(Qt.CursorShape.PointingHandCursor)
        self.switches[key] = switch
        return switch

    def _switch_row(self, box: QVBoxLayout, key: str, on: bool, title: str, note: str) -> None:
        row = QHBoxLayout()
        row.setSpacing(12)
        switch = self._switch(key, on)
        row.addWidget(switch, 0, Qt.AlignmentFlag.AlignTop)
        words = QVBoxLayout()
        words.setSpacing(2)
        words.addWidget(_title(title))
        words.addWidget(_muted(note))
        row.addLayout(words, 1)
        box.addLayout(row)

    def _show_where(self) -> None:
        link = self.link_tab.isChecked()
        self.url.setVisible(link)
        self.file_box.setVisible(not link)

    def _show_fps(self) -> None:
        custom = self.custom_fps.isChecked()
        self.fps_box.setVisible(custom)
        self.fps_note.setVisible(not custom)

    def _show_css(self, shown: bool) -> None:
        self.css.setVisible(shown)
        self.css_toggle.setText(("▾  " if shown else "▸  ") + "Custom CSS")

    def _pick_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose a page", self.file.text(), "Web page (*.html *.htm)")
        if path:
            self.file.setText(path)

    def values(self) -> dict[str, Any]:
        link = self.link_tab.isChecked()
        url = self.url.text().strip()
        return {
            "url": url or "https://",
            "file": "" if link else self.file.text().strip(),
            "width": self.width_box.value(),
            "height": self.height_box.value(),
            "custom_fps": self.custom_fps.isChecked(),
            "fps": self.fps_box.value() if self.custom_fps.isChecked() else STANDARD_FPS,
            "css": self.css.toPlainText(),
            "mute": self.switches["mute"].isChecked(),
            "audio_to_mixer": self.switches["audio_to_mixer"].isChecked(),
            "keep_running": not self.switches["sleep"].isChecked(),
            "reload_on_air": self.switches["reload_on_air"].isChecked(),
        }
