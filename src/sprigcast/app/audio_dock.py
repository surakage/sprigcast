"""The Audio Mixer dock: a meter, volume, mute and monitor control per audio source.

Shown as rows (horizontal) or side-by-side channel strips (vertical). Sources can be hidden from
the mixer (they still play), and a "Ducking" note on the master line shows when your voice is
lowering everything else.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QBoxLayout,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

FLOOR_DB = -60.0
GREEN, YELLOW, RED = QColor("#3ddc84"), QColor("#ffd23f"), QColor("#ff6b6b")
HORIZONTAL, VERTICAL = "horizontal", "vertical"


@dataclass(frozen=True)
class AudioInfo:
    id: str
    name: str
    volume_db: float = 0.0
    muted: bool = False
    monitor: bool = False
    hidden: bool = False
    voice: bool = False  # leads ducking
    kind: str = "audio_input"


class LevelMeter(QWidget):
    """RMS as a solid bar, peak as a thin marker, -60 to 0 dBFS. Horizontal or vertical (fills upward)."""

    def __init__(self, orientation: str = HORIZONTAL, parent: QWidget | None = None):
        super().__init__(parent)
        self.orientation = orientation
        self.peak, self.rms = FLOOR_DB, FLOOR_DB
        if orientation == HORIZONTAL:
            self.setMinimumHeight(10)
            self.setMaximumHeight(14)
            self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        else:
            self.setMinimumSize(12, 120)
            self.setMaximumWidth(16)
            self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)

    def set_levels(self, peak: float, rms: float) -> None:
        # Fall back slowly so the meter is readable, rise instantly so peaks are never missed.
        self.peak = peak if peak > self.peak else max(peak, self.peak - 1.5)
        self.rms = rms if rms > self.rms else max(rms, self.rms - 1.0)
        self.update()

    def _frac(self, db: float) -> float:
        return (min(0.0, max(FLOOR_DB, db)) - FLOOR_DB) / -FLOOR_DB

    def _span(self, a: float, b: float) -> QRectF:
        """The meter area between two fractions of the scale."""
        w, h = float(self.width()), float(self.height())
        if self.orientation == HORIZONTAL:
            return QRectF(a * w, 0, (b - a) * w, h)
        return QRectF(0, h - b * h, w, (b - a) * h)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#08170f"))
        # Zones: green to -20 dB, yellow to -9 dB, red above.
        for start, end, colour in ((FLOOR_DB, -20.0, GREEN), (-20.0, -9.0, YELLOW), (-9.0, 0.0, RED)):
            if self.rms > start:
                p.fillRect(self._span(self._frac(start), self._frac(min(self.rms, end))), colour)
        if self.peak > FLOOR_DB:
            f = self._frac(self.peak)
            thin = 2 / max(1, self.width() if self.orientation == HORIZONTAL else self.height())
            p.fillRect(self._span(f - thin, f), RED if self.peak > -9 else QColor("#f0f0f0"))
        p.end()


class AudioRow(QWidget):
    """One source: a row in the horizontal mixer or a channel strip in the vertical one."""

    def __init__(self, info: AudioInfo, orientation: str, on_change: Callable, on_menu: Callable):
        super().__init__()
        self.info = info
        self.source_id = info.id
        self._on_change = on_change
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(lambda pos: on_menu(self.source_id, self.mapToGlobal(pos)))
        self.select = QRadioButton(info.name)
        self.select.setToolTip(info.name)
        self.db_label = QLabel()
        self.db_label.setMinimumWidth(56)
        self.mute = QToolButton()
        self.mute.setCheckable(True)
        self.mute.setChecked(info.muted)
        self.mute.setToolTip("Mute: silent everywhere, on stream too")
        self.mute.toggled.connect(lambda on: self._changed(muted=on))
        self.monitor = QToolButton()
        self.monitor.setCheckable(True)
        self.monitor.setChecked(info.monitor)
        if info.kind == "audio_output":
            self.monitor.setEnabled(False)
            self.monitor.setToolTip("Desktop audio already plays on your speakers")
        else:
            self.monitor.setToolTip("Monitor: hear it on your speakers. It goes to the stream either way.")
        self.monitor.toggled.connect(lambda on: self._changed(monitor=on))
        self.meter = LevelMeter(orientation)
        self.slider = QSlider(Qt.Orientation.Horizontal if orientation == HORIZONTAL else Qt.Orientation.Vertical)
        self.slider.setRange(-600, 120)  # tenths of a dB, -60 dB to +12 dB
        self.slider.setValue(int(round(info.volume_db * 10)))
        self.slider.valueChanged.connect(lambda v: self._changed(volume_db=v / 10))
        if orientation == HORIZONTAL:
            self._lay_out_row()
        else:
            self._lay_out_strip()
        if info.hidden:
            self.setStyleSheet("color: #8cc4a3")
            self.select.setText(f"{info.name} (hidden)")
        self._update_labels()

    def _lay_out_row(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(2)
        top = QHBoxLayout()
        top.addWidget(self.select, 1)
        self.db_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        top.addWidget(self.db_label)
        top.addWidget(self.monitor)
        top.addWidget(self.mute)
        layout.addLayout(top)
        layout.addWidget(self.meter)
        layout.addWidget(self.slider)

    def _lay_out_strip(self) -> None:
        self.setMinimumWidth(92)
        self.setMaximumWidth(120)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(4)
        self.db_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.select)
        layout.addWidget(self.db_label)
        middle = QHBoxLayout()
        middle.addStretch(1)
        middle.addWidget(self.meter)
        middle.addWidget(self.slider)
        middle.addStretch(1)
        layout.addLayout(middle, 1)
        for button in (self.mute, self.monitor):
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            layout.addWidget(button)

    def _update_labels(self) -> None:
        self.db_label.setText(f"{self.slider.value() / 10:+.1f} dB")
        self.mute.setText("Muted" if self.mute.isChecked() else "Mute")
        self.monitor.setText("🎧 On" if self.monitor.isChecked() else "🎧 Off")

    def _changed(self, **changes) -> None:
        self._update_labels()
        self._on_change(self.source_id, changes)


class AudioPanel(QWidget):
    """Master meter on top, then one row (or strip) per audio source that isn't hidden.

    on_change(source_id, {"volume_db" | "muted" | "monitor" | "hidden" | "duck_voice": value}).
    """

    def __init__(self, on_change: Callable[[str, dict], None], orientation: str = HORIZONTAL):
        super().__init__()
        self._on_change = on_change
        self.orientation = orientation
        self.show_hidden = False
        self.rows: dict[str, AudioRow] = {}
        self._infos: list[AudioInfo] = []
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        master = QHBoxLayout()
        master.addWidget(QLabel("Master"))
        self.master = LevelMeter()
        master.addWidget(self.master, 1)
        self.ducking = QLabel()
        self.ducking.setToolTip("Your voice is lowering the other sounds")
        self.ducking.setStyleSheet("color: #ffd23f")
        master.addWidget(self.ducking)
        self._layout.addLayout(master)
        self._rows_box = QBoxLayout(QBoxLayout.Direction.TopToBottom)
        self._layout.addLayout(self._rows_box, 1)
        self.empty = QLabel("No audio yet. Add a mic or desktop audio with +.")
        self.empty.setWordWrap(True)
        self._layout.addWidget(self.empty)
        self.hidden_button = QPushButton()
        self.hidden_button.setFlat(True)
        self.hidden_button.setStyleSheet(
            "QPushButton { border: none; background: transparent; color: #8cc4a3; padding: 2px; }"
            "QPushButton:hover { color: #f0fff5; }")
        self.hidden_button.clicked.connect(lambda: self.set_show_hidden(not self.show_hidden))
        self._layout.addWidget(self.hidden_button)
        self._stretch = self._layout.addStretch(0)

    # Layout ---------------------------------------------------------------------------

    def set_orientation(self, orientation: str) -> None:
        if orientation != self.orientation:
            self.orientation = orientation
            self._rebuild()

    def set_show_hidden(self, show: bool) -> None:
        self.show_hidden = show
        self._rebuild()

    def set_sources(self, infos: list[AudioInfo]) -> None:
        """Rebuilds rows only when what they show changed (not on every volume tweak)."""
        def shape(items):
            return [(i.id, i.name, i.hidden, i.kind) for i in items]

        if shape(infos) == shape(self._infos) and self.rows:
            self._infos = list(infos)
            return
        self._infos = list(infos)
        self._rebuild()

    def _rebuild(self) -> None:
        selected = self.selected_id()
        for row in self.rows.values():
            self._rows_box.removeWidget(row)
            row.setParent(None)
            row.deleteLater()
        while self._rows_box.count():
            self._rows_box.takeAt(0)
        self.rows = {}
        vertical = self.orientation == VERTICAL
        self._rows_box.setDirection(QBoxLayout.Direction.LeftToRight if vertical else QBoxLayout.Direction.TopToBottom)
        for info in self._infos:
            if info.hidden and not self.show_hidden:
                continue
            row = AudioRow(info, self.orientation, self._on_change, self._row_menu)
            self.rows[info.id] = row
            self._rows_box.addWidget(row)
            if info.id == selected:
                row.select.setChecked(True)
        if vertical:
            self._rows_box.addStretch(1)
        self._layout.setStretch(self._layout.indexOf(self._rows_box), 1 if vertical else 0)
        self.empty.setVisible(not self._infos)
        hidden = sum(1 for i in self._infos if i.hidden)
        self.hidden_button.setVisible(bool(hidden))
        self.hidden_button.setText(("Hide hidden" if self.show_hidden else f"{hidden} hidden · show") if hidden else "")

    def _row_menu(self, source_id: str, pos) -> None:
        info = next((i for i in self._infos if i.id == source_id), None)
        if info is None:
            return
        menu = QMenu(self)
        menu.addAction("Show in mixer" if info.hidden else "Hide from mixer").triggered.connect(
            lambda: self._on_change(source_id, {"hidden": not info.hidden}))
        voice = menu.addAction("Lowers other sounds when it's loud (voice)")
        voice.setCheckable(True)
        voice.setChecked(info.voice)
        voice.toggled.connect(lambda on: self._on_change(source_id, {"duck_voice": on}))
        menu.exec(pos)

    # State --------------------------------------------------------------------------

    def selected_id(self) -> str | None:
        return next((sid for sid, row in self.rows.items() if row.select.isChecked()), None)

    def set_levels(self, levels: dict[str, tuple[float, float]]) -> None:
        if "master" in levels:
            self.master.set_levels(*levels["master"])
        duck = levels.get("duck", (0.0, 0.0))[0]
        self.ducking.setText(f"Ducking {duck:.0f} dB" if duck < -0.5 else "")
        for sid, row in self.rows.items():
            if sid in levels:
                row.meter.set_levels(*levels[sid])
