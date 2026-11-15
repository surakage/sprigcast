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
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QFrame,
    QFileDialog,
    QFormLayout,
    QGraphicsDropShadowEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from sprigcast.app import theme
from sprigcast.core.scene import ANY, SceneCollection, Transition, TransitionRule

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



# While live, the Live card's picture only plays when pointed at: no repainting during a stream for show.
QUIET = [False]


def set_quiet(quiet: bool) -> None:
    QUIET[0] = quiet

class TransitionPicture(QWidget):
    """A small moving picture of the transition: bloom "A" turning into bloom "B", looping.

    It mixes the two pictures the same way the compositor's transition shader does (same edges, same
    softness, same leaf and flower), so what it shows is what goes on air.
    """

    HOLD_A = 450  # ms on A before the change, and on B after it
    HOLD_B = 650

    def __init__(self, transition: Callable[[], Transition], parent: QWidget | None = None, wide: bool = False):
        super().__init__(parent)
        self.transition = transition
        self.wide = wide  # a little stream screen (the pop-up) rather than a thin strip
        self.setFixedHeight(56 if not wide else 180)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._clock = QElapsedTimer()
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._step)
        self._loops_left = 0
        self.rest = 0.0  # where the picture waits between plays (0 = the A card)
        self.progress = 0.0
        self._scenes: tuple | None = None  # (key, a, b, x, y): A and B as float arrays, and pixel grids
        self._wipe: tuple[str, object] | None = None  # (path, its grey picture at this size)

    def fill(self) -> None:
        """Take whatever room is left (Live's card)."""
        self.setMinimumHeight(36)
        self.setMaximumHeight(16777215)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setToolTip("How blooms change: it plays every few seconds, or point at it")
        # Play once every few seconds while it's on screen; it rests (and costs nothing) in between.
        self._every = QTimer(self)
        self._every.setInterval(5000)
        self._every.timeout.connect(lambda: self.play(1) if not self._loops_left and not QUIET[0] else None)

    def play(self, loops: int = 2) -> None:
        """Run the picture a couple of times (it rests on the A card in between, costing nothing)."""
        self._loops_left = loops
        self._clock.start()
        if self.isVisible():
            self._timer.start()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if hasattr(self, "_every"):
            self._every.start()
        if self.wide:
            self.play(1000)  # the pop-up's screen keeps showing it while open (it stops when hidden)
        if self._loops_left:
            self._timer.start()

    def hideEvent(self, event) -> None:  # noqa: N802
        self._timer.stop()
        if hasattr(self, "_every"):
            self._every.stop()
        super().hideEvent(event)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self.wide:
            height = round(self.width() * 9 / 16)
            if self.height() != height:
                self.setFixedHeight(height)

    def enterEvent(self, event) -> None:  # noqa: N802
        self.play(1)
        super().enterEvent(event)

    def run_ms(self) -> int:
        """How long the change itself plays: its own length, kept long enough to see and short enough
        to loop (a stinger shows its swap over a fixed second)."""
        t = self.transition()
        if t.kind == "cut":
            return 0
        if t.kind == "stinger":
            return 1100
        return max(450, min(2500, t.duration_ms))

    def loop_ms(self) -> int:
        return self.HOLD_A + self.run_ms() + self.HOLD_B

    def progress_at(self, at: float) -> float:
        """Where the change is, `at` ms into a loop: on A, then the change, then on B."""
        run = self.run_ms()
        if run <= 0:
            return 1.0 if at >= self.HOLD_A else 0.0
        return min(1.0, max(0.0, (at - self.HOLD_A) / run))

    def _step(self) -> None:
        cycle, at = divmod(self._clock.elapsed(), self.loop_ms())
        if cycle >= self._loops_left:
            self._timer.stop()
            self._loops_left, self.progress = 0, self.rest
        else:
            self.progress = self.progress_at(at)
        self.update()

    # -- drawing ------------------------------------------------------------------------------------

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        box = QRectF(0, 0, self.width(), self.height())
        radius = 18 if self.wide else 14
        frame = QPainterPath()
        frame.addRoundedRect(box, radius, radius)
        p.setClipPath(frame)
        p.drawImage(box, self.frame(self.progress))

    def frame(self, progress: float) -> QImage:
        """The picture `progress` (0 .. 1) of the way from A to B, at the widget's size in device pixels."""
        import numpy as np

        dpr = max(1.0, self.devicePixelRatioF())
        w, h = max(2, round(self.width() * dpr)), max(2, round(self.height() * dpr))
        a, b, x, y = self._pictures(w, h, dpr)
        t = self.transition()
        progress = min(1.0, max(0.0, progress))
        ease = progress * progress * (3 - 2 * progress)
        dx, dy = {"left": (-1, 0), "right": (1, 0), "up": (0, -1), "down": (0, 1)}[t.direction]
        if t.kind in ("cut", "stinger"):
            out = b if progress >= 0.5 else a
        elif t.kind == "fade":
            out = a * (1 - progress) + b * progress
        elif t.kind in ("slide", "swipe"):
            # B comes in from the far side; on slide A leaves ahead of it, on swipe A stays put under it.
            shift_x, shift_y = round(dx * ease * w), round(dy * ease * h)
            out = a.copy() if t.kind == "swipe" else self._shifted(a, shift_x, shift_y)
            if t.kind == "swipe" and 0.0 < ease < 1.0:  # a soft shadow ahead of B's edge, so it reads as on top
                reach = max(4, round(0.04 * max(w, h)))
                pos, size = (x * w, w) if dx else (y * h, h)
                ahead = pos - ease * size if dx + dy > 0 else (1 - ease) * size - pos
                out = out * (1 - 0.35 * np.clip(1 - ahead / reach, 0, 1) * (ahead > 0))[..., None]
            out = self._shifted(b, shift_x - dx * w, shift_y - dy * h, out)
        else:
            if t.kind == "wipe":
                grey = self._wipe_map(t.wipe_image, w, h)
                level = grey if grey is not None else (x * dx + y * dy + max(0, -(dx + dy)))
            else:
                from sprigcast.render.compositor import BLOOM_SPIN, LEAF_SPIN, LEAF_TURN, shape_reach

                qx, qy = (x - 0.5) * (w / h), y - 0.5
                if t.kind == "leaf":
                    angle = LEAF_TURN + LEAF_SPIN * progress
                    c, s = math.cos(angle), math.sin(angle)
                    u, v = c * qx + s * qy, -s * qx + c * qy
                    level = np.abs(v) + np.hypot(u, v)
                else:
                    level = np.hypot(qx, qy) / (0.72 + 0.28 * np.cos(5 * np.arctan2(qy, qx) + BLOOM_SPIN * progress))
                level = level / shape_reach(t.kind, (w, h), progress)
            soft = max(t.softness, 0.001)
            edge = progress * (1 + soft)
            # The shader's smoothstep: 1 where the change has reached, 0 where it hasn't yet.
            k = np.clip((level - (edge - soft)) / soft, 0.0, 1.0)
            amount = (1.0 - k * k * (3 - 2 * k))[..., None]
            out = a * (1 - amount) + b * amount
        image = self._image(out)
        image.setDevicePixelRatio(dpr)
        if t.kind == "stinger" and 0.0 < progress < 1.0:
            painter = QPainter(image)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            self._sweep(painter, QRectF(0, 0, w / dpr, h / dpr), progress)
            painter.end()
        return image

    @staticmethod
    def _shifted(picture, sx: int, sy: int, under=None):
        """`picture` moved by (sx, sy) pixels, over `under` (or over nothing where it has left)."""
        import numpy as np

        h, w = picture.shape[:2]
        out = np.zeros_like(picture) if under is None else under.copy()
        if abs(sx) >= w or abs(sy) >= h:
            return out
        src_x, dst_x = (slice(0, w - sx), slice(sx, w)) if sx >= 0 else (slice(-sx, w), slice(0, w + sx))
        src_y, dst_y = (slice(0, h - sy), slice(sy, h)) if sy >= 0 else (slice(-sy, h), slice(0, h + sy))
        out[dst_y, dst_x] = picture[src_y, src_x]
        return out

    @staticmethod
    def _image(pixels) -> QImage:
        import numpy as np

        data = np.ascontiguousarray(np.clip(pixels, 0, 255).astype(np.uint8))
        h, w = data.shape[:2]
        return QImage(data.data, w, h, w * 4, QImage.Format.Format_RGBA8888).copy()

    def _pictures(self, w: int, h: int, dpr: float):
        """Blooms A and B at this size, as float RGBA arrays, with each pixel's (x, y) from 0 to 1."""
        import numpy as np

        key = (w, h, dpr, self.wide)
        if self._scenes is None or self._scenes[0] != key:
            pictures = []
            for label, fill, ink, flip in (("A", theme.STAGE_EMPTY, theme.SELECT_TEXT, False),
                                           ("B", theme.ACCENT, theme.ACCENT_TEXT, True)):
                image = QImage(w, h, QImage.Format.Format_RGBA8888)
                image.setDevicePixelRatio(dpr)
                image.fill(QColor(fill))
                painter = QPainter(image)
                painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                self._scene(painter, QRectF(0, 0, w / dpr, h / dpr), label, ink, flip)
                painter.end()
                bits = np.frombuffer(image.constBits(), np.uint8, image.sizeInBytes())
                pictures.append(bits.reshape(h, image.bytesPerLine())[:, : w * 4].reshape(h, w, 4).astype(np.float32))
            x = ((np.arange(w, dtype=np.float32) + 0.5) / w)[None, :]
            y = ((np.arange(h, dtype=np.float32) + 0.5) / h)[:, None]
            self._scenes = (key, pictures[0], pictures[1], x, y)
        return self._scenes[1:]

    def _scene(self, p: QPainter, rect: QRectF, label: str, ink: str, flip: bool) -> None:
        """One bloom: its letter, and on the pop-up's screen a camera box and a name bar (on the other
        sides for B, so the change reads even where the colours meet)."""
        font = QFont(self.font())
        font.setBold(True)
        if self.wide:
            unit = rect.height() / 9
            cam = QRectF(0, 0, 4 * unit, 3 * unit)
            bar = QRectF(0, 0, 5 * unit, 0.9 * unit)
            if flip:
                cam.moveTopLeft(QPointF(rect.left() + 0.6 * unit, rect.top() + 0.6 * unit))
                bar.moveBottomRight(QPointF(rect.right() - 0.6 * unit, rect.bottom() - 0.7 * unit))
            else:
                cam.moveBottomRight(QPointF(rect.right() - 0.6 * unit, rect.bottom() - 0.8 * unit))
                bar.moveBottomLeft(QPointF(rect.left() + 0.6 * unit, rect.bottom() - 0.7 * unit))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(ink))
            p.setOpacity(0.18)
            p.drawRoundedRect(cam, unit * 0.5, unit * 0.5)
            p.drawRoundedRect(bar, bar.height() / 2, bar.height() / 2)
            p.setOpacity(1.0)
            font.setPixelSize(max(12, round(rect.height() * 0.24)))
        else:
            font.setPixelSize(max(10, round(min(rect.height() * 0.42, 22))))
        p.setFont(font)
        p.setPen(QColor(ink))
        # A's letter sits left of centre and B's right of it, so a fade never stacks one on the other.
        spot = QRectF(rect)
        spot.setWidth(rect.width() * 0.5)
        spot.moveCenter(QPointF(rect.left() + rect.width() * (0.62 if flip else 0.38), rect.center().y()))
        p.drawText(spot, Qt.AlignmentFlag.AlignCenter, label)

    def _wipe_map(self, path: str, w: int, h: int):
        """The wipe image as grey levels 0 .. 1 at this size (None when there is none or it can't be read)."""
        import numpy as np

        if not path:
            return None
        key = f"{path}|{w}x{h}"
        if self._wipe is None or self._wipe[0] != key:
            image = QImage(path)
            grey = None
            if not image.isNull():
                image = image.scaled(w, h, Qt.AspectRatioMode.IgnoreAspectRatio,
                                     Qt.TransformationMode.SmoothTransformation)
                image = image.convertToFormat(QImage.Format.Format_Grayscale8)
                bits = np.frombuffer(image.constBits(), np.uint8, image.sizeInBytes())
                grey = bits.reshape(h, image.bytesPerLine())[:, :w].astype(np.float32) / 255.0
            self._wipe = (key, grey)
        return self._wipe[1]

    @staticmethod
    def _sweep(p: QPainter, box: QRectF, progress: float) -> None:
        """The stinger stand-in: a band of leaves sweeps across, covering the whole screen at the swap."""
        width = box.width() * 1.8
        left = box.left() - width + (box.width() + width) * progress
        band = QRectF(left, box.top() - 2, width, box.height() + 4)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(theme.ACCENT_DARK))
        p.drawRoundedRect(band, box.height() / 2, box.height() / 2)
        size = box.height() * 0.9
        for i, (u, v, tilt) in enumerate(((0.25, 0.3, -30), (0.5, 0.62, 15), (0.75, 0.32, -10))):
            leaf = QRectF(0, 0, size * (0.8 if i != 1 else 1.0), size * (0.8 if i != 1 else 1.0))
            leaf.moveCenter(QPointF(band.left() + band.width() * u, box.top() + box.height() * v))
            theme.paint_leaf(p, leaf, True, theme.ACCENT, vein=theme.ACCENT_DARK, tilt=tilt,
                             width=max(1.2, size / 40))


def summary(transition: Transition) -> str:
    """What the Live chip says: the kind, and its length when it has one."""
    label = dict((k, name) for k, name, _tip in KINDS)[transition.kind]
    if transition.kind in TIMED:
        return f"{label} · {transition.duration_ms / 1000:g} s"
    return label


class TransitionsPanel(QWidget):
    """Edits `transition` in place; calls changed() after every edit."""

    def __init__(self, transition: Transition, changed: Callable[[], None],
                 probe: Callable[[str], float | None] | None = None, parent: QWidget | None = None,
                 collection: SceneCollection | None = None, compact: bool = False, wide: bool = False):
        super().__init__(parent)
        self.compact = compact  # Live's short card: the type and length here, the rest in a pop-up
        self.transition = transition
        self.changed = changed
        self.probe = probe
        self.collection = collection
        self._narrow = 200 if compact else NARROW  # Live's card keeps labels beside their settings
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
        choices = QWidget()  # the eight pills; on Live's short card a drop-down takes their place
        choices.setLayout(grid)
        choices.setVisible(not compact)
        root.addWidget(choices)
        self._reflow(4)
        self.kind_pick: QPushButton | None = None
        if compact:
            self.kind_pick = QPushButton()
            self.kind_pick.setStyleSheet(PILL_STYLE)
            self.kind_pick.setCursor(Qt.CursorShape.PointingHandCursor)
            self.kind_pick.setToolTip("How blooms change on air")
            menu = QMenu(self.kind_pick)
            picks = {}
            for kind, label, tip in KINDS:
                action = menu.addAction(label, lambda k=kind: self.set_kind(k))
                action.setToolTip(tip)
                action.setCheckable(True)
                picks[kind] = action
            menu.aboutToShow.connect(lambda: [a.setChecked(k == self.transition.kind) for k, a in picks.items()])
            self.kind_pick.setMenu(menu)

        self.picture = TransitionPicture(lambda: self.transition, wide=wide)
        if not compact:
            root.addWidget(self.picture)  # Live's card puts it last, in whatever room is left

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.form = form
        if self.kind_pick is not None:
            form.addRow("Type", self.kind_pick)

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
        self.more_button: QPushButton | None = None
        self.clip_note: QLabel | None = None
        if compact:
            # Everything deeper (direction, edge, images, stinger clips and sound) lives in a pop-up.
            self.clip_note = QLabel("")
            self.clip_note.setStyleSheet(theme.MUTED_STYLE)
            form.insertRow(2, "Clip", self.clip_note)
            self.more_button = QPushButton("More settings…")
            self.more_button.setStyleSheet(PILL_STYLE)
            self.more_button.setCursor(Qt.CursorShape.PointingHandCursor)
            self.more_button.setToolTip("Direction, edge, wipe image, stinger clips and sound")
            self.more_button.clicked.connect(self.open_settings)
            root.addWidget(self.more_button)
        self.rules_button: QPushButton | None = None
        if collection is not None:
            # The table of bloom-to-bloom rules opens in its own pop-up: there's no room for it here.
            row = QHBoxLayout()
            row.setSpacing(8)
            label = QLabel("Between blooms")
            label.setToolTip("Give particular switches their own transition, like a stinger into BRB")
            row.addWidget(label)
            self.rules_count = QLabel("")
            self.rules_count.setStyleSheet(theme.MUTED_STYLE)
            row.addWidget(self.rules_count, 1)
            self.rules_button = QPushButton("Edit")
            self.rules_button.setStyleSheet(PILL_STYLE)
            self.rules_button.setCursor(Qt.CursorShape.PointingHandCursor)
            self.rules_button.clicked.connect(self.open_rules)
            row.addWidget(self.rules_button)
            root.addSpacing(4)
            root.addLayout(row)
            self.show_rule_count()
        root.addStretch(1)
        self.load(transition)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if not hasattr(self, "form"):
            return
        inner = self.width() - 16
        self._reflow(4 if inner >= 4 * CHOICE_WIDTH else 2)  # 4 or 2 columns: eight choices fill whole rows
        wrap = QFormLayout.RowWrapPolicy.WrapAllRows if self.width() < self._narrow else QFormLayout.RowWrapPolicy.DontWrapRows
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
        self._show_kind()
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
        self.show_rule_count()

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
        if self.compact:
            rows = {widget: False for widget in rows}
            rows[self.length] = kind in TIMED
            rows[self.clip_note] = stinger
            self.clip_note.setText(short_name(self.transition.stinger.path, "None yet"))
        for widget, shown in rows.items():
            self.form.setRowVisible(widget, shown)

    def _show_clip_length(self) -> None:
        path = self.transition.stinger.path
        if getattr(self, "_probed", (None, None))[0] != path:  # opening the file once per clip is enough
            self._probed = (path, self.probe(path) if (self.probe and path) else None)
        seconds = self._probed[1]
        self.clip_length.setText(f"of {seconds:.1f} s" if seconds else "")
        if seconds:
            self.cut_at.setMaximum(max(0, int(seconds * 1000)))

    def show_rule_count(self) -> None:
        if self.collection is None:
            return
        count = len(self.collection.transition_rules)
        self.rules_count.setText(f"{count} rule{'s' if count != 1 else ''}" if count else "None")

    def popup(self, tab: str = "transition") -> "PopUp":
        """The pop-up: a "Transition" tab with every setting and a "Between blooms" tab with the table.

        Live's card opens it on either tab. Build already shows the settings, so its pop-up has only the
        table. Built fresh each time, so bloom names are current.
        """
        dialog = PopUp("Transitions", self)
        if self.compact:
            def edited() -> None:
                self.load(self.transition)
                self.changed()

            panel = TransitionsPanel(self.transition, edited, self.probe, wide=True)
            panel.layout().setContentsMargins(0, 0, 0, 0)
            dialog.add_tab("transition", "Transition", scrolled(panel))
            dialog.panel = panel
        if self.collection is not None:
            def rules_edited() -> None:
                self.show_rule_count()
                self.changed()

            table = RulesTable(self.collection, rules_edited, self.probe)
            dialog.add_tab("rules", "Between blooms", scrolled(table))
            dialog.table = table
        dialog.show_tab(tab)
        dialog.resize(480, 640)
        return dialog

    def rules_dialog(self) -> "PopUp":
        return self.popup("rules")

    def settings_dialog(self) -> "PopUp":
        return self.popup("transition")

    def open_settings(self) -> None:
        self.settings_dialog().exec()
        self.load(self.transition)
        self.show_rule_count()

    def open_rules(self) -> None:
        self.rules_dialog().exec()
        self.show_rule_count()

    def set_kind(self, kind: str) -> None:
        """Pick a kind from elsewhere (the Live screen's transition chip)."""
        self.kind_buttons[kind].setChecked(True)
        self._pick(kind)

    def _show_kind(self) -> None:
        if self.kind_pick is not None:
            label = dict((k, name) for k, name, _tip in KINDS)[self.transition.kind]
            self.kind_pick.setText(f"{label}  ▾")

    def _pick(self, kind: str) -> None:
        self.transition.kind = kind
        self._show_kind()
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


def scrolled(widget: QWidget) -> QScrollArea:
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }")
    scroll.setWidget(widget)
    return scroll


TAB_STYLE = CHOICE_STYLE.replace("padding: 6px 8px", "padding: 7px 16px")


class PopUp(QDialog):
    """A rounded, raised pop-up card with pill tabs along the top. Drag it by any empty spot."""

    def __init__(self, title: str, parent: QWidget | None = None):
        super().__init__(parent, Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setWindowTitle(title)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 14, 20, 26)  # room for the shadow
        card = QFrame()
        card.setObjectName("popCard")
        card.setStyleSheet(f"#popCard {{ background: {theme.PANEL}; border: 1px solid {theme.EDGE_STRONG};"
                           " border-radius: 24px; }")
        shadow = QGraphicsDropShadowEffect(card)
        shadow.setBlurRadius(36)
        shadow.setOffset(0, 10)
        shadow.setColor(QColor(0, 0, 0, 190))
        card.setGraphicsEffect(shadow)
        outer.addWidget(card)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 16, 20, 18)
        layout.setSpacing(14)
        head = QHBoxLayout()
        head.setSpacing(6)
        name = QLabel(title)
        name.setStyleSheet(f"color: {theme.TEXT_BRIGHT}; font-size: 16px; font-weight: 700;")
        head.addWidget(name)
        head.addSpacing(10)
        self.tab_row = QHBoxLayout()
        self.tab_row.setSpacing(6)
        head.addLayout(self.tab_row)
        head.addStretch(1)
        close = QPushButton("×")
        close.setStyleSheet(PILL_STYLE)
        close.setFixedSize(28, 28)
        close.setToolTip("Close")
        close.clicked.connect(self.accept)
        head.addWidget(close)
        layout.addLayout(head)
        self.stack = QStackedWidget()
        layout.addWidget(self.stack, 1)
        done = QPushButton("Done")
        done.setStyleSheet(theme.PRIMARY_STYLE)
        done.clicked.connect(self.accept)
        layout.addWidget(done, 0, Qt.AlignmentFlag.AlignRight)
        self.tabs: dict[str, QPushButton] = {}
        self._group = QButtonGroup(self)
        self._drag = None

    def add_tab(self, key: str, label: str, page: QWidget) -> None:
        button = QPushButton(label)
        button.setCheckable(True)
        button.setStyleSheet(TAB_STYLE)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        index = self.stack.addWidget(page)
        button.clicked.connect(lambda: self.stack.setCurrentIndex(index))
        self._group.addButton(button)
        self.tab_row.addWidget(button)
        self.tabs[key] = button
        for b in self.tabs.values():  # one page needs no tabs
            b.setVisible(len(self.tabs) > 1)

    def show_tab(self, key: str) -> None:
        button = self.tabs.get(key) or next(iter(self.tabs.values()))
        button.setChecked(True)
        button.click()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._drag = None
        super().mouseReleaseEvent(event)


def section_title(text: str, tip: str = "") -> QLabel:
    label = QLabel(text)
    font = label.font()
    font.setPixelSize(14)
    font.setWeight(QFont.Weight.DemiBold)
    label.setFont(font)
    label.setToolTip(tip)
    return label


def bloom_choices(collection: SceneCollection) -> list[tuple[str, str]]:
    """(scene id, name) of every main bloom: shows that are not just another canvas's twin."""
    return [(s.id, s.name) for s in collection.scenes if s.kind == "show" and s.linked_to is None]


ROW_STYLE = f"#ruleRow {{ background: {theme.CARD}; border-radius: 14px; }}"


class RuleRow(QFrame):
    """One line of the table: from bloom → to bloom, and the transition between them."""

    def __init__(self, table: "RulesTable", rule: TransitionRule):
        super().__init__()
        self.table, self.rule = table, rule
        self.setObjectName("ruleRow")
        self.setStyleSheet(ROW_STYLE)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        ends = QHBoxLayout()
        ends.setSpacing(6)
        self.from_pick = self._end_button("from_scene")
        self.to_pick = self._end_button("to_scene")
        arrow = QLabel("→")
        arrow.setStyleSheet(theme.MUTED_STYLE)
        ends.addWidget(self.from_pick, 1)
        ends.addWidget(arrow)
        ends.addWidget(self.to_pick, 1)
        layout.addLayout(ends)
        how = QHBoxLayout()
        how.setSpacing(6)
        self.how = QPushButton()
        self.how.setStyleSheet(CHOICE_STYLE)
        self.how.setToolTip("Change how this switch looks")
        self.how.clicked.connect(self._edit)
        remove = QPushButton("×")
        remove.setStyleSheet(PILL_STYLE)
        remove.setToolTip("Remove this rule")
        remove.clicked.connect(lambda: table.remove(rule))
        how.addWidget(self.how, 1)
        how.addWidget(remove)
        layout.addLayout(how)
        self.show_values()

    def _end_button(self, end: str) -> QPushButton:
        button = QPushButton()
        button.setStyleSheet(PILL_STYLE)
        button.setProperty("end", end)
        menu = QMenu(button)
        menu.aboutToShow.connect(lambda: self._fill_menu(menu, end))
        button.setMenu(menu)
        return button

    def _fill_menu(self, menu: QMenu, end: str) -> None:
        menu.clear()
        current = getattr(self.rule, end)
        for scene_id, name in [(ANY, "Any bloom"), *bloom_choices(self.table.collection)]:
            action = menu.addAction(name, lambda sid=scene_id: self._set_end(end, sid))
            action.setCheckable(True)
            action.setChecked(scene_id == current)

    def _set_end(self, end: str, scene_id: str) -> None:
        setattr(self.rule, end, scene_id)
        self.show_values()
        self.table.changed()

    def _name(self, scene_id: str) -> str:
        if scene_id == ANY:
            return "Any bloom"
        scene = next((s for s in self.table.collection.scenes if s.id == scene_id), None)
        return short_name(scene.name, "?") if scene is not None else "Removed"

    def show_values(self) -> None:
        self.from_pick.setText(self._name(self.rule.from_scene) + "  ▾")
        self.to_pick.setText(self._name(self.rule.to_scene) + "  ▾")
        text = summary(self.rule.transition)
        if self.rule.transition.kind == "stinger" and self.rule.transition.stinger.path:
            text += " · " + short_name(self.rule.transition.stinger.path)
        self.how.setText(text)

    def _edit(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Transition")
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        layout.addWidget(section_title(f"{self._name(self.rule.from_scene)} → {self._name(self.rule.to_scene)}"))

        def edited() -> None:
            self.show_values()
            self.table.changed()

        layout.addWidget(TransitionsPanel(self.rule.transition, edited, self.table.probe))
        done = QPushButton("Done")
        done.setStyleSheet(theme.PRIMARY_STYLE)
        done.clicked.connect(dialog.accept)
        layout.addWidget(done, 0, Qt.AlignmentFlag.AlignRight)
        dialog.resize(380, 560)
        dialog.exec()


class RulesTable(QWidget):
    """Between blooms: transitions for particular switches, like a stinger only into BRB."""

    def __init__(self, collection: SceneCollection, changed: Callable[[], None],
                 probe: Callable[[str], float | None] | None = None):
        super().__init__()
        self.collection, self.changed, self.probe = collection, changed, probe
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        top = QHBoxLayout()
        top.addWidget(section_title("Between blooms", "Use another transition for particular switches"))
        top.addStretch(1)
        add = QPushButton("+ Add")
        add.setStyleSheet(PILL_STYLE)
        add.setToolTip("Add a rule: from one bloom to another, its own transition")
        add.clicked.connect(self.add)
        top.addWidget(add)
        layout.addLayout(top)
        self.hint = QLabel("Every switch uses the usual transition. Add a rule to give some switches "
                           "their own, like a stinger into BRB.")
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet(theme.MUTED_STYLE)
        layout.addWidget(self.hint)
        self.rows = QVBoxLayout()
        self.rows.setSpacing(8)
        layout.addLayout(self.rows)
        layout.addStretch(1)  # rows keep their size; the space stays below them
        self.refresh()

    def refresh(self) -> None:
        """Rebuild the rows (after rules were added or removed, or blooms renamed)."""
        while self.rows.count():
            widget = self.rows.takeAt(0).widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        for rule in self.collection.transition_rules:
            self.rows.addWidget(RuleRow(self, rule))
        self.hint.setVisible(not self.collection.transition_rules)

    def add(self) -> None:
        usual = self.collection.transition
        blooms = bloom_choices(self.collection)
        transition = Transition(kind="stinger", stinger=usual.stinger.model_copy())
        rule = TransitionRule(from_scene=ANY, to_scene=blooms[0][0] if blooms else ANY, transition=transition)
        self.collection.transition_rules.append(rule)
        self.refresh()
        self.changed()

    def remove(self, rule: TransitionRule) -> None:
        self.collection.transition_rules = [r for r in self.collection.transition_rules if r is not rule]
        self.refresh()
        self.changed()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        for i in range(self.rows.count()):
            row = self.rows.itemAt(i).widget()
            if isinstance(row, RuleRow):
                row.show_values()  # bloom names may have changed
