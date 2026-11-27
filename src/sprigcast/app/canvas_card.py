"""Right-click a canvas on Live: one small card with everything for that canvas (user's pick, 2026-11-27, "B").

Preview as a switch, Projector screens as buttons (no side menu to slide into), Safe zones on a tall canvas,
Fit to view when zoomed in, and Canvas… for its size and frame rate. Nothing here edits a bloom.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QPoint, QSize, Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from sprigcast.app import icons, theme
from sprigcast.app.card import keep_on_screen
from sprigcast.app.live_panel import FlowLayout
from sprigcast.app.live_stage import Switch

WIDTH = 330


@dataclass
class Screen:
    label: str  # "Screen 1"
    main: bool
    size: str  # "1920×1080", in the tooltip
    open: Callable[[], None]


def _row_style() -> str:
    return (f"QPushButton {{ background: transparent; color: {theme.TEXT}; border: none; border-radius: 10px;"
            f" padding: 10px 12px; text-align: left; }}"
            f"QPushButton:hover {{ background: {theme.SELECT_BG}; color: {theme.SELECT_TEXT}; }}")


def _divider() -> QFrame:
    line = QFrame()
    line.setFixedHeight(1)
    line.setStyleSheet(f"background: {theme.EDGE_STRONG}; border: none;")
    return line


def _inset(widget: QWidget) -> QWidget:
    """Chips lined up with the rows' text, with room under them before the next divider."""
    box = QWidget()
    box.setStyleSheet("background: transparent;")
    line = QVBoxLayout(box)
    line.setContentsMargins(42, 2, 12, 12)
    line.addWidget(widget)
    return box


def _label_row(icon: str, text: str) -> tuple[QWidget, QHBoxLayout]:
    row = QWidget()
    row.setStyleSheet("background: transparent;")
    line = QHBoxLayout(row)
    line.setContentsMargins(12, 8, 12, 8)
    line.setSpacing(12)
    glyph = QLabel()
    glyph.setPixmap(icons.icon(icon, theme.MUTED).pixmap(18, 18))
    line.addWidget(glyph)
    name = QLabel(text)
    name.setStyleSheet(f"color: {theme.TEXT}; background: transparent;")
    line.addWidget(name)
    line.addStretch(1)
    return row, line


class CanvasCard(QFrame):
    """The card. Picking a screen, a safe zone, Fit or Canvas… closes it; the Preview switch doesn't."""

    def __init__(self, title: str, preview_on: bool, on_preview: Callable[[bool], None], screens: list[Screen],
                 open_window: Callable[[], None], open_canvas: Callable[[], None],
                 zones: tuple[list[str], str, Callable[[str], None]] | None = None,
                 fit: Callable[[], None] | None = None, parent: QWidget | None = None):
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 14, 20, 26)  # room for the shadow
        card = self._card = QFrame()
        card.setObjectName("canvasCard")
        card.setFixedWidth(WIDTH)
        card.setStyleSheet(f"#canvasCard {{ background: {theme.PANEL}; border: 1px solid {theme.EDGE_STRONG};"
                           " border-radius: 18px; }")
        outer.addWidget(card)
        body = QVBoxLayout(card)
        body.setContentsMargins(12, 14, 12, 12)
        body.setSpacing(4)

        heading = QLabel(title.upper())
        heading.setStyleSheet(f"color: {theme.MUTED}; background: transparent; font-size: 12px;"
                              " letter-spacing: 1px; padding: 2px 12px 6px 12px;")
        body.addWidget(heading)

        row, line = _label_row("eye", "Preview")
        self.preview = Switch()
        self.preview.setChecked(preview_on)
        self.preview.setCursor(Qt.CursorShape.PointingHandCursor)
        self.preview.setToolTip("Off leaves more of the graphics card for the stream. Build and projectors keep "
                                "drawing.")
        self.preview.toggled.connect(on_preview)
        line.addWidget(self.preview)
        body.addWidget(row)
        body.addSpacing(4)
        body.addWidget(_divider())
        body.addSpacing(4)

        row, _line = _label_row("monitor", "Projector")
        body.addWidget(row)
        self.screen_buttons: list[QPushButton] = []
        chips = QWidget()
        chips.setStyleSheet("background: transparent;")
        flow = FlowLayout(chips, spacing=8)
        for screen in screens:
            button = self._chip(screen.label, "Main" if screen.main else "")
            button.setToolTip(f"Full screen, {screen.size}. Press Esc to close it.")
            button.clicked.connect(self._then_close(screen.open))
            self.screen_buttons.append(button)
            flow.addWidget(button)
        self.window_button = self._chip("Window")
        self.window_button.setToolTip("In its own window")
        self.window_button.clicked.connect(self._then_close(open_window))
        flow.addWidget(self.window_button)
        body.addWidget(_inset(chips))

        self.zone_buttons: dict[str, QPushButton] = {}
        if zones is not None:
            choices, current, pick = zones
            body.addWidget(_divider())
            body.addSpacing(4)
            row, _line = _label_row("panel", "Safe zones")
            row.setToolTip("Where TikTok, Shorts and Reels draw over your stream (only you see these)")
            body.addWidget(row)
            holder = QWidget()
            holder.setStyleSheet("background: transparent;")
            zone_flow = FlowLayout(holder, spacing=8)
            for choice in choices:
                button = self._chip(choice)
                button.setCheckable(True)
                button.setChecked(choice == current)
                button.clicked.connect(self._then_close(lambda c=choice: pick(c)))
                self.zone_buttons[choice] = button
                zone_flow.addWidget(button)
            body.addWidget(_inset(holder))

        body.addWidget(_divider())
        body.addSpacing(4)
        self.fit_button = None
        if fit is not None:
            self.fit_button = self._row("search", "Fit to view", "Ctrl+0")
            self.fit_button.clicked.connect(self._then_close(fit))
            body.addWidget(self.fit_button)
        self.canvas_button = self._row("gear", "Canvas…")
        self.canvas_button.setToolTip("Size and frame rate (Settings → Video)")
        self.canvas_button.clicked.connect(self._then_close(open_canvas))
        body.addWidget(self.canvas_button)

    def _chip(self, text: str, tag: str = "") -> QPushButton:
        button = QPushButton()
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setStyleSheet(theme.button_style(12, "8px 14px"))
        inside = QHBoxLayout(button)
        inside.setContentsMargins(14, 8, 14, 8)
        inside.setSpacing(8)
        name = QLabel(text)
        name.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        name.setStyleSheet(f"background: transparent; color: {theme.TEXT}; font-weight: 600; border: none;")
        inside.addWidget(name)
        if tag:
            small = QLabel(tag)
            small.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            small.setStyleSheet(f"background: transparent; color: {theme.LABEL}; font-size: 11px;"
                                f" border: 1px solid {theme.SECTION_EDGE}; border-radius: 8px; padding: 1px 7px;")
            inside.addWidget(small)
        button.setText("")
        button.setAccessibleName(f"{text} {tag}".strip())
        button.setMinimumSize(inside.sizeHint())
        return button

    def _row(self, icon: str, text: str, hint: str = "") -> QPushButton:
        button = QPushButton(f"  {text}")
        button.setIcon(icons.icon(icon, theme.MUTED))
        button.setIconSize(QSize(18, 18))
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setStyleSheet(_row_style())
        if hint:
            button.setToolTip(hint)
        return button

    def _then_close(self, action: Callable[[], None]) -> Callable[..., None]:
        def run(*_args) -> None:
            self.close()
            action()
        return run

    def texts(self) -> list[str]:
        """What it offers, in order (for tests and the tour)."""
        out = ["Preview", "Projector", *[b.accessibleName() for b in self.screen_buttons], "Window"]
        if self.zone_buttons:
            out += ["Safe zones", *self.zone_buttons]
        if self.fit_button is not None:
            out.append("Fit to view")
        return [*out, "Canvas…"]

    def paintEvent(self, event) -> None:  # noqa: N802
        theme.paint_card_shadow(self, self._card)

    def show_at(self, where: QPoint) -> None:
        self.adjustSize()
        self.move(where - QPoint(20, 14))  # the card's corner, not the shadow's, at the pointer
        self.show()
        keep_on_screen(self)
