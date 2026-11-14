"""The Transitions panel: how blooms change on air.

A row of pill choices (Cut, Fade, Slide, Swipe, Wipe, Leaf, Bloom, Stinger), a little moving picture
of the one picked, and only the settings that one uses. It edits the collection's Transition in place
and calls `changed` after each edit.
"""

from __future__ import annotations

import math
import os
from collections.abc import Callable

from PySide6.QtCore import QElapsedTimer, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from sprigcast.app import theme
from sprigcast.core.scene import Transition

KINDS = [  # (kind, label, tooltip)
    ("cut", "Cut", "Swap at once"),
    ("fade", "Fade", "Melt into the next bloom"),
    ("slide", "Slide", "Push the old bloom out"),
    ("swipe", "Swipe", "Slide the new bloom over the old"),
    ("wipe", "Wipe", "Sweep a soft edge across, or follow an image"),
    ("leaf", "Leaf", "Open through a growing leaf"),
    ("bloom", "Bloom", "Open through a flower"),
    ("stinger", "Stinger", "Play a video over the swap"),
]
TIMED = {"fade", "slide", "swipe", "wipe", "leaf", "bloom"}
MOVES = {"slide", "swipe", "wipe"}
SOFT = {"wipe", "leaf", "bloom"}
ARROWS = [("left", "←"), ("right", "→"), ("up", "↑"), ("down", "↓")]
VIDEO_FILTER = "Videos (*.webm *.mov *.mp4 *.mkv *.avi *.gif);;All files (*)"
IMAGE_FILTER = "Images (*.png *.jpg *.jpeg *.webp *.bmp)"
MATTES = [("none", "No matte"), ("right", "Matte on the right"), ("below", "Matte below")]

# The panel sits on a PANEL card (Live's side column, Build's left column, or its own window), so its
# pills and boxes are one step lighter (RAISED) to read as things you can press or type in.
PILL_STYLE = (
    f"QPushButton {{ background: {theme.RAISED}; color: {theme.TEXT}; border: none; border-radius: 13px;"
    f" padding: 6px 8px; font-weight: 600; }}"
    f"QPushButton:hover {{ background: {theme.RAISED_HOVER}; }}"
    f"QPushButton:disabled {{ color: {theme.MUTED}; }}"
    "QPushButton::menu-indicator { width: 0; image: none; }"
)
CHOICE_STYLE = PILL_STYLE + (
    f"QPushButton:checked {{ background: {theme.SELECT_BG}; color: {theme.SELECT_TEXT}; }}"
)

# Number boxes as soft pills too, the number centred with room on both sides. No arrow buttons (type,
# scroll or use the arrow keys); setButtonSymbols(NoButtons) can't be used, the app stylesheet then
# shrinks the text to 1 px.
FIELD_STYLE = (
    f"QSpinBox {{ background: {theme.RAISED}; color: {theme.TEXT}; border: none; border-radius: 13px;"
    f" padding: 6px 14px; min-height: 14px; }}"
    f"QSpinBox:hover, QSpinBox:focus {{ background: {theme.RAISED_HOVER}; }}"
    "QSpinBox::up-button, QSpinBox::down-button { subcontrol-origin: margin; width: 0px; height: 0px; border: none; }"
)
CHOICE_WIDTH = 64  # a choice pill needs about this much room ("Stinger" plus padding)
NARROW = 280  # below this width labels go above their settings


def short_name(path: str, empty: str = "Choose…") -> str:
    if not path:
        return empty
    name = os.path.basename(path)
    return name if len(name) <= 22 else name[:10] + "…" + name[-10:]


class TransitionPicture(QWidget):
    """A small moving picture of the transition: an "A" card turning into a "B" card, looping."""

    LOOP_MS = 1800

    def __init__(self, transition: Callable[[], Transition], parent: QWidget | None = None):
        super().__init__(parent)
        self.transition = transition
        self.setFixedHeight(56)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._clock = QElapsedTimer()
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._step)
        self._loops_left = 0
        self.progress = 0.0

    def play(self, loops: int = 2) -> None:
        """Run the picture a couple of times (it rests on the A card in between, costing nothing)."""
        self._loops_left = loops
        self._clock.start()
        if self.isVisible():
            self._timer.start()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if self._loops_left:
            self._timer.start()

    def hideEvent(self, event) -> None:  # noqa: N802
        self._timer.stop()
        super().hideEvent(event)

    def enterEvent(self, event) -> None:  # noqa: N802
        self.play(1)
        super().enterEvent(event)

    def _step(self) -> None:
        elapsed = self._clock.elapsed()
        cycle, at = divmod(elapsed, self.LOOP_MS)
        if cycle >= self._loops_left:
            self._timer.stop()
            self._loops_left, self.progress = 0, 0.0
        else:
            # Hold on A a moment, run the change, then hold on B.
            self.progress = min(1.0, max(0.0, (at - 300) / (self.LOOP_MS - 700)))
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        box = QRectF(0, 0, self.width(), self.height())
        frame = QPainterPath()
        frame.addRoundedRect(box, 13, 13)
        p.setClipPath(frame)
        t = self.transition()
        progress = self.progress
        self._card(p, box, "A", theme.STAGE_EMPTY, theme.SELECT_TEXT)
        if t.kind == "cut":
            if progress >= 0.5:
                self._card(p, box, "B", theme.ACCENT, theme.ACCENT_TEXT)
            return
        eased = progress * progress * (3 - 2 * progress)
        dx, dy = {"left": (-1, 0), "right": (1, 0), "up": (0, -1), "down": (0, 1)}[t.direction]
        if t.kind == "fade":
            p.setOpacity(progress)
            self._card(p, box, "B", theme.ACCENT, theme.ACCENT_TEXT)
        elif t.kind in ("slide", "swipe"):
            if t.kind == "slide":
                self._card(p, box.translated(dx * eased * box.width(), dy * eased * box.height()), "A",
                           theme.STAGE_EMPTY, theme.SELECT_TEXT, clear=True)
            new = box.translated(-dx * (1 - eased) * box.width(), -dy * (1 - eased) * box.height())
            self._card(p, new, "B", theme.ACCENT, theme.ACCENT_TEXT)
        elif t.kind == "wipe":
            reach = QRectF(box)
            if dx:
                reach.setWidth(box.width() * progress)
                if dx < 0:
                    reach.moveRight(box.right())
            else:
                reach.setHeight(box.height() * progress)
                if dy < 0:
                    reach.moveBottom(box.bottom())
            p.setClipRect(reach, Qt.ClipOperation.IntersectClip)
            self._card(p, box, "B", theme.ACCENT, theme.ACCENT_TEXT)
        elif t.kind == "leaf":
            size = 2.4 * max(box.width(), box.height()) * progress
            leaf = QRectF(0, 0, size, size)
            leaf.moveCenter(box.center())
            p.setClipPath(theme.leaf_path(leaf, tilt=30 * progress), Qt.ClipOperation.IntersectClip)
            self._card(p, box, "B", theme.ACCENT, theme.ACCENT_TEXT)
        elif t.kind == "bloom":
            p.setClipPath(self._flower(box.center(), 1.6 * max(box.width(), box.height()) * progress, progress),
                          Qt.ClipOperation.IntersectClip)
            self._card(p, box, "B", theme.ACCENT, theme.ACCENT_TEXT)
        elif t.kind == "stinger":
            if progress >= 0.5:
                self._card(p, box, "B", theme.ACCENT, theme.ACCENT_TEXT)
            if 0.0 < progress < 1.0:  # a big leaf sweeps across, hiding the swap
                size = box.height() * 2.2
                x = box.left() - size + (box.width() + size) * progress
                theme.paint_leaf(p, QRectF(x, box.center().y() - size / 2, size, size), True,
                                 theme.ACCENT_DARK, vein=theme.ACCENT, tilt=-20)

    def _card(self, p: QPainter, rect: QRectF, label: str, fill: str, ink: str, clear: bool = False) -> None:
        p.save()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(fill))
        p.drawRect(rect)
        font = QFont(self.font())
        font.setPointSizeF(font.pointSizeF() * 1.4)
        font.setBold(True)
        p.setFont(font)
        p.setPen(QColor(ink))
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, label)
        p.restore()

    @staticmethod
    def _flower(centre: QPointF, size: float, spin: float) -> QPainterPath:
        path = QPainterPath()
        for i in range(73):
            a = i / 72 * 2 * math.pi
            r = size / 2 * (0.72 + 0.28 * math.cos(5 * a + spin * 1.2))
            point = QPointF(centre.x() + r * math.cos(a), centre.y() + r * math.sin(a))
            path.moveTo(point) if i == 0 else path.lineTo(point)
        path.closeSubpath()
        return path


def summary(transition: Transition) -> str:
    """What the Live chip says: the kind, and its length when it has one."""
    label = dict((k, name) for k, name, _tip in KINDS)[transition.kind]
    if transition.kind in TIMED:
        return f"{label} · {transition.duration_ms / 1000:g} s"
    return label


class TransitionsPanel(QWidget):
    """Edits `transition` in place; calls changed() after every edit."""

    def __init__(self, transition: Transition, changed: Callable[[], None],
                 probe: Callable[[str], float | None] | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.transition = transition
        self.changed = changed
        self.probe = probe
        self._updating = False
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(12)
        self.setStyleSheet(FIELD_STYLE)

        grid = QGridLayout()
        grid.setSpacing(6)
        self.grid = grid
        self._columns = 0
        self.kinds = QButtonGroup(self)
        self.kinds.setExclusive(True)
        self.kind_buttons: dict[str, QPushButton] = {}
        for i, (kind, label, tip) in enumerate(KINDS):
            button = QPushButton(label)
            button.setCheckable(True)
            button.setToolTip(tip)
            button.setStyleSheet(CHOICE_STYLE)
            button.setMinimumHeight(28)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            self.kinds.addButton(button)
            self.kind_buttons[kind] = button
            button.clicked.connect(lambda _=False, k=kind: self._pick(k))
        root.addLayout(grid)
        self._reflow(4)

        self.picture = TransitionPicture(lambda: self.transition)
        root.addWidget(self.picture)

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.form = form

        self.length = QSpinBox()
        self.length.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.length.setRange(50, 5000)
        self.length.setSingleStep(50)
        self.length.setSuffix(" ms")
        self.length.valueChanged.connect(self._edit)
        form.addRow("Length", self.length)

        arrows = QWidget()
        row = QHBoxLayout(arrows)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        self.directions = QButtonGroup(self)
        self.direction_buttons: dict[str, QPushButton] = {}
        for name, arrow in ARROWS:
            button = QPushButton(arrow)
            button.setCheckable(True)
            button.setToolTip(f"Move {name}")
            button.setStyleSheet(CHOICE_STYLE)
            self.directions.addButton(button)
            self.direction_buttons[name] = button
            button.clicked.connect(lambda _=False, d=name: self._direction(d))
            row.addWidget(button)
        row.addStretch(1)
        form.addRow("Way", arrows)

        self.softness = QSlider(Qt.Orientation.Horizontal)
        self.softness.setRange(0, 100)
        self.softness.valueChanged.connect(self._edit)
        form.addRow("Edge", self.softness)

        self.wipe_image = self._file_row(IMAGE_FILTER, "Line", self._set_wipe_image)
        form.addRow("Image", self.wipe_image)

        self.clip = self._file_row(VIDEO_FILTER, "Choose…", self._set_clip)
        form.addRow("Clip", self.clip)
        self.tall_clip = self._file_row(VIDEO_FILTER, "Same clip", self._set_tall_clip)
        self.tall_clip.setToolTip("Its own clip for vertical canvases")
        form.addRow("Vertical", self.tall_clip)
        cut_row = QWidget()
        cut_layout = QHBoxLayout(cut_row)
        cut_layout.setContentsMargins(0, 0, 0, 0)
        cut_layout.setSpacing(10)
        self.cut_at = QSpinBox()
        self.cut_at.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cut_at.setMinimumWidth(96)
        self.cut_at.setRange(0, 20000)
        self.cut_at.setSingleStep(50)
        self.cut_at.setSuffix(" ms")
        self.cut_at.setToolTip("When the blooms swap, while the clip covers the screen")
        self.cut_at.valueChanged.connect(self._edit)
        self.clip_length = QLabel("")
        self.clip_length.setStyleSheet(theme.MUTED_STYLE)
        cut_layout.addWidget(self.cut_at)
        cut_layout.addWidget(self.clip_length, 1)
        form.addRow("Swap at", cut_row)
        self.matte = QPushButton()  # a pill with a little menu, like the other choices
        self.matte.setStyleSheet(PILL_STYLE)
        self.matte.setToolTip("A track matte clip: picture in one half, black and white matte in the other")
        self._build_matte_menu()
        form.addRow("Matte", self.matte)
        self.matte_only = QCheckBox("Hide picture")
        self.matte_only.setToolTip("Swap through the matte without showing the picture half")
        self.matte_only.toggled.connect(self._edit)
        form.addRow("", self.matte_only)
        sound = QWidget()
        sound_layout = QHBoxLayout(sound)
        sound_layout.setContentsMargins(0, 0, 0, 0)
        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(-40, 6)
        self.volume.setToolTip("Clip volume (dB)")
        self.volume.valueChanged.connect(self._edit)
        self.mute = QCheckBox("Mute")
        self.mute.toggled.connect(self._edit)
        sound_layout.addWidget(self.volume, 1)
        sound_layout.addWidget(self.mute)
        form.addRow("Sound", sound)
        root.addLayout(form)
        root.addStretch(1)
        self.load(transition)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if not hasattr(self, "form"):
            return
        inner = self.width() - 16
        self._reflow(4 if inner >= 4 * CHOICE_WIDTH else 2)  # 4 or 2 columns: eight choices fill whole rows
        wrap = QFormLayout.RowWrapPolicy.WrapAllRows if self.width() < NARROW else QFormLayout.RowWrapPolicy.DontWrapRows
        if self.form.rowWrapPolicy() != wrap:
            self.form.setRowWrapPolicy(wrap)

    def _reflow(self, columns: int) -> None:
        """Lay the choices out in `columns` equal columns: four on a wide card, fewer on a narrow one."""
        if columns == self._columns:
            return
        self._columns = columns
        for button in self.kind_buttons.values():
            self.grid.removeWidget(button)
        for i, button in enumerate(self.kind_buttons.values()):
            self.grid.addWidget(button, i // columns, i % columns)
        for c in range(4):
            self.grid.setColumnStretch(c, 1 if c < columns else 0)

    def _build_matte_menu(self) -> None:
        menu = QMenu(self.matte)
        for value, label in MATTES:
            menu.addAction(label, lambda v=value: self._set_matte(v))
        self.matte.setMenu(menu)

    def _show_matte(self, value: str) -> None:
        self.matte.setText(dict(MATTES).get(value, MATTES[0][1]) + "  ▾")

    def _set_matte(self, value: str) -> None:
        self.transition.stinger.matte = value
        self._show_matte(value)
        self._edit()

    def _file_row(self, file_filter: str, empty: str, chosen: Callable[[str], None]) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        pick = QPushButton(empty)
        pick.setStyleSheet(PILL_STYLE)
        pick.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        clear = QPushButton("×")
        clear.setStyleSheet(PILL_STYLE)
        clear.setToolTip("Clear")
        layout.addWidget(pick, 1)
        layout.addWidget(clear)
        row.pick, row.clear, row.empty = pick, clear, empty

        def browse() -> None:
            path, _ = QFileDialog.getOpenFileName(self, "Choose file", "", file_filter)
            if path:
                chosen(path)

        pick.clicked.connect(browse)
        clear.clicked.connect(lambda: chosen(""))
        return row

    @staticmethod
    def _show_file(row: QWidget, path: str) -> None:
        row.pick.setText(short_name(path, row.empty))
        row.pick.setToolTip(path)
        row.clear.setVisible(bool(path))

    # Loading and saving ----------------------------------------------------------------

    def load(self, transition: Transition) -> None:
        """Show another collection's transition (after opening a file)."""
        self.transition = transition
        self._updating = True
        t, s = transition, transition.stinger
        self.kind_buttons[t.kind].setChecked(True)
        self.direction_buttons[t.direction].setChecked(True)
        self.length.setValue(t.duration_ms)
        self.softness.setValue(round(t.softness * 100))
        self.cut_at.setValue(s.cut_ms)
        self._show_matte(s.matte)
        self.matte_only.setChecked(s.matte_only)
        self.volume.setValue(round(s.volume_db))
        self.mute.setChecked(s.muted)
        self._show_file(self.wipe_image, t.wipe_image)
        self._show_file(self.clip, s.path)
        self._show_file(self.tall_clip, s.vertical_path)
        self._updating = False
        self._show_clip_length()
        self._show_rows()

    def _show_rows(self) -> None:
        kind = self.transition.kind
        stinger = kind == "stinger"
        rows = {
            self.length: kind in TIMED,
            self.direction_buttons["left"].parentWidget(): kind in MOVES and not (kind == "wipe" and self.transition.wipe_image),
            self.softness: kind in SOFT,
            self.wipe_image: kind == "wipe",
            self.clip: stinger, self.tall_clip: stinger, self.cut_at.parentWidget(): stinger,
            self.matte: stinger, self.volume.parentWidget(): stinger,
            self.matte_only: stinger and self.transition.stinger.matte != "none",
        }
        for widget, shown in rows.items():
            self.form.setRowVisible(widget, shown)

    def _show_clip_length(self) -> None:
        path = self.transition.stinger.path
        seconds = self.probe(path) if (self.probe and path) else None
        self.clip_length.setText(f"of {seconds:.1f} s" if seconds else "")
        if seconds:
            self.cut_at.setMaximum(max(0, int(seconds * 1000)))

    def set_kind(self, kind: str) -> None:
        """Pick a kind from elsewhere (the Live screen's transition chip)."""
        self.kind_buttons[kind].setChecked(True)
        self._pick(kind)

    def _pick(self, kind: str) -> None:
        self.transition.kind = kind
        self._show_rows()
        self.picture.play()
        self.changed()

    def _direction(self, direction: str) -> None:
        self.transition.direction = direction
        self.picture.play(1)
        self.changed()

    def _edit(self) -> None:
        if self._updating:
            return
        t, s = self.transition, self.transition.stinger
        t.duration_ms = self.length.value()
        t.softness = self.softness.value() / 100
        s.cut_ms = self.cut_at.value()
        s.matte_only = self.matte_only.isChecked()
        s.volume_db = float(self.volume.value())
        s.muted = self.mute.isChecked()
        self._show_rows()
        self.changed()

    def _set_wipe_image(self, path: str) -> None:
        self.transition.wipe_image = path
        self._show_file(self.wipe_image, path)
        self._show_rows()
        self.changed()

    def _set_clip(self, path: str) -> None:
        s = self.transition.stinger
        s.path = path
        self._show_file(self.clip, path)
        seconds = self.probe(path) if (self.probe and path) else None
        if seconds and s.cut_ms > seconds * 1000:
            s.cut_ms = int(seconds * 500)  # a cut point past the end: start from the middle
        self._updating = True
        self.cut_at.setMaximum(20000)
        self.cut_at.setValue(s.cut_ms)
        self._updating = False
        self._show_clip_length()
        self.changed()

    def _set_tall_clip(self, path: str) -> None:
        self.transition.stinger.vertical_path = path
        self._show_file(self.tall_clip, path)
        self.changed()
