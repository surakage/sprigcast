"""Live mode's own screen: the stage, the bloom tiles under it, and a calm column on the right.

    ┌ stage ─────────────────────────────────────┐ ┌ End stream ┐
    │ horizontal program   vertical program      │ │ Record  Clip│
    │ NEXT · Gaming                  [Bloom it]  │ │ Where it goes
    └────────────────────────────────────────────┘ │ Sound       │
    [Just Chatting] [Gaming] [BRB] [Ending] …      │ CPU · RAM   │

No docks: the main window moves its canvas previews and the Live panel (bloom tiles) in here when
Live mode starts, and back when Build mode starts. The docks are still in the Docks menu for anyone
who wants them, but none show by default.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from sprigcast.app import theme

SIDE_WIDTH = 280
GAP = 20


def heading(text: str, size: int = 14) -> QLabel:
    label = QLabel(text)
    font = label.font()
    font.setPixelSize(size + 2)
    font.setWeight(font.Weight.DemiBold)
    label.setFont(font)
    label.setStyleSheet(f"color: {theme.TEXT_BRIGHT}; background: transparent;")
    return label


def pill(text: str, style: str = "") -> QPushButton:
    button = QPushButton(text)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setStyleSheet(style or theme.PILL_STYLE)
    return button


class ThinMeter(QWidget):
    """A 6 px rounded level bar: green, yellow when loud, pink when it's about to clip."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.level = 0.0  # 0..1 of -60..0 dBFS
        self.muted = False
        self.setFixedHeight(6)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_db(self, rms: float) -> None:
        target = (min(0.0, max(-60.0, rms)) + 60.0) / 60.0
        level = target if target > self.level else max(target, self.level - 0.02)  # falls back slowly
        if abs(level - self.level) > 0.002:
            self.level = level
            self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        r = QRectF(self.rect())
        p.setBrush(QColor(theme.TRACK))
        p.drawRoundedRect(r, 3, 3)
        if self.level > 0 and not self.muted:
            colour = theme.ACCENT if self.level < 2 / 3 else theme.WARN if self.level < 0.85 else theme.LIVE
            p.setBrush(QColor(colour))
            p.drawRoundedRect(QRectF(0, 0, max(6.0, r.width() * self.level), r.height()), 3, 3)
        p.end()


@dataclass(frozen=True)
class Destination:
    id: str
    name: str
    on: bool
    note: str  # "Ready", "Needs a stream key", or the live bitrate


class LiveSide(QWidget):
    """The right-hand column: the big stream button, Record and Save clip, where it goes, and sound."""

    def __init__(self, on_live: Callable[[], None], on_record: Callable[[], None], on_clip: Callable[[], None],
                 on_destination: Callable[[str | None], None]):
        super().__init__()
        self.on_destination = on_destination
        self.setFixedWidth(SIDE_WIDTH)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(24)

        buttons = QVBoxLayout()
        buttons.setSpacing(10)
        self.live = pill("Go live", theme.BIG_GO_STYLE)
        self.live.setFixedHeight(52)
        self.live.clicked.connect(on_live)
        buttons.addWidget(self.live)
        row = QHBoxLayout()
        row.setSpacing(10)
        self.record = pill("Record")
        self.record.setToolTip("Record the canvases ticked in Controls, to your recordings folder")
        self.record.clicked.connect(on_record)
        self.clip = pill("Save clip")
        self.clip.clicked.connect(on_clip)
        row.addWidget(self.record)
        row.addWidget(self.clip)
        buttons.addLayout(row)
        self.rec_note = QLabel("")
        self.rec_note.setStyleSheet(f"color: {theme.LIVE_TEXT}; background: transparent;")
        self.rec_note.setVisible(False)
        buttons.addWidget(self.rec_note)
        root.addLayout(buttons)

        where = QVBoxLayout()
        where.setSpacing(10)
        top = QHBoxLayout()
        top.addWidget(heading("Where it goes"), 1)
        manage = QPushButton("Edit")
        manage.setStyleSheet(theme.LINK_STYLE)
        manage.setCursor(Qt.CursorShape.PointingHandCursor)
        manage.setToolTip("Add or change where you stream")
        manage.clicked.connect(lambda: on_destination(None))
        top.addWidget(manage)
        where.addLayout(top)
        self.places = QVBoxLayout()
        self.places.setSpacing(10)
        where.addLayout(self.places)
        root.addLayout(where)
        self.place_rows: dict[str, tuple[QLabel, QLabel, QLabel]] = {}

        sound = QVBoxLayout()
        sound.setSpacing(12)
        sound.addWidget(heading("Sound"))
        self.sounds = QVBoxLayout()
        self.sounds.setSpacing(12)
        sound.addLayout(self.sounds)
        root.addLayout(sound)
        self.meters: dict[str, ThinMeter] = {}
        self._sound_shape: list | None = None

        root.addStretch(1)
        self.stats = QLabel("")
        self.stats.setStyleSheet(f"color: {theme.MUTED}; background: transparent;")
        self.stats.setWordWrap(True)
        root.addWidget(self.stats)

    # The buttons ------------------------------------------------------------------------

    def set_live(self, live: bool) -> None:
        self.live.setText("End stream" if live else "Go live")
        self.live.setStyleSheet(theme.BIG_END_STYLE if live else theme.BIG_GO_STYLE)

    def set_recording(self, text: str) -> None:
        """'' when not recording, else the REC clock line."""
        self.record.setText("Stop recording" if text else "Record")
        self.record.setStyleSheet(theme.PILL_ON_AIR_STYLE if text else theme.PILL_STYLE)
        self.rec_note.setText(text)
        self.rec_note.setVisible(bool(text))

    def set_clip(self, on: bool, seconds: int) -> None:
        self.clip.setToolTip(f"Save the last {seconds} s as a clip" if on else
                             f"Start keeping the last {seconds} s in memory, ready to save as a clip")
        self.clip.setText("Save clip" if on else "Clip buffer")
        self.clip.setStyleSheet(theme.PILL_SELECTED_STYLE if on else theme.PILL_STYLE)

    # Where it goes --------------------------------------------------------------------------

    def show_destinations(self, places: list[Destination], levels: dict[str, str]) -> None:
        """levels: destination id -> health level ("good", "warning", "bad", "idle", "off")."""
        if [p.id for p in places] != list(self.place_rows):
            while self.places.count():
                item = self.places.takeAt(0)
                widget = item.widget()
                if widget is not None:
                    widget.setParent(None)
                    widget.deleteLater()
            self.place_rows = {}
            if not places:
                empty = QPushButton("+ Add a place to stream")
                empty.setStyleSheet(theme.LINK_STYLE)
                empty.clicked.connect(lambda: self.on_destination(None))
                self.places.addWidget(empty)
            for place in places:
                box = QPushButton()
                box.setStyleSheet("QPushButton { background: transparent; border: none; padding: 0; text-align: left; }")
                box.setCursor(Qt.CursorShape.PointingHandCursor)
                box.setToolTip("Edit this destination")
                box.clicked.connect(lambda _=False, pid=place.id: self.on_destination(pid))
                line = QHBoxLayout(box)
                line.setContentsMargins(0, 0, 0, 0)
                line.setSpacing(10)
                dot, name, note = QLabel("●"), QLabel(), QLabel()
                for label in (dot, name, note):
                    label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
                note.setStyleSheet(f"color: {theme.MUTED}; background: transparent;")
                line.addWidget(dot)
                line.addWidget(name, 1)
                line.addWidget(note)
                box.setMinimumHeight(22)
                self.places.addWidget(box)
                self.place_rows[place.id] = (dot, name, note)
        from sprigcast.app.destinations import HEALTH_COLORS

        for place in places:
            dot, name, note = self.place_rows[place.id]
            level = levels.get(place.id, "idle" if place.on else "off")
            dot.setStyleSheet(f"color: {HEALTH_COLORS.get(level, HEALTH_COLORS['idle'])}; background: transparent;")
            name.setText(place.name)
            name.setStyleSheet(f"color: {theme.TEXT if place.on else theme.MUTED}; background: transparent;")
            note.setText(place.note)

    # Sound --------------------------------------------------------------------------------

    def show_sound(self, sources: list[tuple[str, str, bool]]) -> None:
        """(source id, name, muted) for every audio source shown in the mixer."""
        if sources == self._sound_shape:
            return
        self._sound_shape = list(sources)
        while self.sounds.count():
            item = self.sounds.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self.meters = {}
        if not sources:
            empty = QLabel("No mic or desktop audio yet")
            empty.setStyleSheet(f"color: {theme.MUTED}; background: transparent;")
            self.sounds.addWidget(empty)
        for source_id, name, muted in sources:
            box = QWidget()
            col = QVBoxLayout(box)
            col.setContentsMargins(0, 0, 0, 0)
            col.setSpacing(7)
            label = QLabel(name + ("   muted" if muted else ""))
            label.setStyleSheet(f"color: {theme.MUTED if muted else theme.TEXT}; background: transparent;")
            meter = ThinMeter()
            meter.muted = muted
            col.addWidget(label)
            col.addWidget(meter)
            self.sounds.addWidget(box)
            self.meters[source_id] = meter

    def set_levels(self, levels: dict[str, tuple[float, float]]) -> None:
        for source_id, meter in self.meters.items():
            if source_id in levels:
                meter.set_db(levels[source_id][1])


class StageCard(QFrame):
    """The rounded card that holds the program previews, with a NEXT bar under them in Studio mode."""

    def __init__(self):
        super().__init__()
        self.setObjectName("stageCard")
        self.setStyleSheet(f"#stageCard {{ background: {theme.PANEL}; border: none; border-radius: 24px; }}"
                           "#stageCard QSplitter::handle { background: transparent; }")
        self.root = QVBoxLayout(self)
        self.root.setContentsMargins(20, 16, 20, 20)
        self.root.setSpacing(12)
        self.holder = QVBoxLayout()  # the main window puts its canvas splitter in here
        self.holder.setContentsMargins(0, 0, 0, 0)
        self.root.addLayout(self.holder, 1)
        self.next_bar = QWidget()
        bar = QHBoxLayout(self.next_bar)
        bar.setContentsMargins(4, 0, 0, 0)
        bar.setSpacing(12)
        self.next_label = QLabel("")
        self.next_label.setStyleSheet(f"color: {theme.SELECT_TEXT}; font-weight: 700; background: transparent;")
        bar.addWidget(self.next_label, 1)
        self.go_slot = QHBoxLayout()  # the Live panel's Go button moves here
        bar.addLayout(self.go_slot)
        self.next_bar.setVisible(False)
        self.root.addWidget(self.next_bar)

    def set_next(self, names: list[str], studio: bool) -> None:
        self.next_bar.setVisible(studio)
        self.next_label.setText(("NEXT  ·  " + "  +  ".join(names)).upper() if names else
                                "Click a bloom to line it up here")
        self.next_label.setStyleSheet(
            f"color: {theme.SELECT_TEXT if names else theme.MUTED}; font-weight: {700 if names else 400};"
            " background: transparent;")


class LiveStage(QWidget):
    """The whole Live screen. The main window fills `card.holder` and `tiles_slot` when Live starts."""

    def __init__(self, side: LiveSide):
        super().__init__()
        self.setObjectName("liveStage")
        self.setStyleSheet(f"#liveStage {{ background: {theme.CHROME}; }}")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        root = QHBoxLayout(self)
        root.setContentsMargins(24, 12, 24, 20)
        root.setSpacing(24)
        left = QVBoxLayout()
        left.setSpacing(GAP)
        self.card = StageCard()
        left.addWidget(self.card, 1)
        self.tiles_slot = QVBoxLayout()
        self.tiles_slot.setContentsMargins(0, 0, 0, 0)
        left.addLayout(self.tiles_slot)
        root.addLayout(left, 1)
        self.side = side
        root.addWidget(side)
