"""The Audio mixer dock: a level meter, volume slider and mute button per audio source."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QRadioButton,
    QSizePolicy,
    QSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

FLOOR_DB = -60.0
GREEN, YELLOW, RED = QColor("#9cc89a"), QColor("#e8d27a"), QColor("#e07a7a")


class LevelMeter(QWidget):
    """Horizontal meter: RMS as a solid bar, peak as a thin marker. -60 dBFS to 0 dBFS."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.peak, self.rms = FLOOR_DB, FLOOR_DB
        self.setMinimumHeight(10)
        self.setMaximumHeight(14)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_levels(self, peak: float, rms: float) -> None:
        # Fall back slowly so the meter is readable, rise instantly so peaks are never missed.
        self.peak = peak if peak > self.peak else max(peak, self.peak - 1.5)
        self.rms = rms if rms > self.rms else max(rms, self.rms - 1.0)
        self.update()

    def _x(self, db: float) -> float:
        return (min(0.0, max(FLOOR_DB, db)) - FLOOR_DB) / -FLOOR_DB * self.width()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        h = float(self.height())
        p.fillRect(self.rect(), QColor("#202020"))
        # Zones: green to -20 dB, yellow to -9 dB, red above.
        for start, end, colour in ((FLOOR_DB, -20.0, GREEN), (-20.0, -9.0, YELLOW), (-9.0, 0.0, RED)):
            if self.rms > start:
                x0, x1 = self._x(start), self._x(min(self.rms, end))
                p.fillRect(QRectF(x0, 0, x1 - x0, h), colour)
        if self.peak > FLOOR_DB:
            x = self._x(self.peak)
            p.fillRect(QRectF(x - 1, 0, 2, h), RED if self.peak > -9 else QColor("#f0f0f0"))
        p.end()


class AudioRow(QWidget):
    def __init__(self, source_id: str, name: str, volume_db: float, muted: bool, on_change: Callable):
        super().__init__()
        self.source_id = source_id
        self._on_change = on_change
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(2)
        top = QHBoxLayout()
        self.select = QRadioButton(name)
        top.addWidget(self.select, 1)
        self.db_label = QLabel()
        self.db_label.setMinimumWidth(56)
        self.db_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        top.addWidget(self.db_label)
        self.mute = QToolButton()
        self.mute.setCheckable(True)
        self.mute.setChecked(muted)
        self.mute.toggled.connect(self._changed)
        top.addWidget(self.mute)
        layout.addLayout(top)
        self.meter = LevelMeter()
        layout.addWidget(self.meter)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(-600, 120)  # tenths of a dB, -60 dB to +12 dB
        self.slider.setValue(int(round(volume_db * 10)))
        self.slider.valueChanged.connect(self._changed)
        layout.addWidget(self.slider)
        self._update_labels()

    def _update_labels(self) -> None:
        self.db_label.setText(f"{self.slider.value() / 10:+.1f} dB")
        self.mute.setText("Muted" if self.mute.isChecked() else "Mute")

    def _changed(self, *_):
        self._update_labels()
        self._on_change(self.source_id, self.slider.value() / 10, self.mute.isChecked())


class AudioPanel(QWidget):
    """Master meter on top, then one row per audio source."""

    def __init__(self, on_change: Callable[[str, float, bool], None]):
        super().__init__()
        self._on_change = on_change
        self.rows: dict[str, AudioRow] = {}
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        master = QHBoxLayout()
        master.addWidget(QLabel("Master"))
        self.master = LevelMeter()
        master.addWidget(self.master, 1)
        self._layout.addLayout(master)
        self._rows_box = QVBoxLayout()
        self._layout.addLayout(self._rows_box)
        self.empty = QLabel("No audio sources. Add your mic or desktop audio below.")
        self.empty.setWordWrap(True)
        self._rows_box.addWidget(self.empty)
        self._layout.addStretch(1)

    def set_sources(self, sources: list[tuple[str, str, float, bool]]) -> None:
        """(id, name, volume_db, muted) per audio source. Rebuilds rows only when the list changes."""
        selected = self.selected_id()
        wanted = [(sid, name) for sid, name, _db, _m in sources]
        if wanted == [(sid, row.select.text()) for sid, row in self.rows.items()]:
            return
        for row in self.rows.values():
            self._rows_box.removeWidget(row)
            row.deleteLater()
        self.rows = {}
        for sid, name, volume_db, muted in sources:
            row = AudioRow(sid, name, volume_db, muted, self._on_change)
            self.rows[sid] = row
            self._rows_box.addWidget(row)
            if sid == selected:
                row.select.setChecked(True)
        self.empty.setVisible(not sources)

    def selected_id(self) -> str | None:
        return next((sid for sid, row in self.rows.items() if row.select.isChecked()), None)

    def set_levels(self, levels: dict[str, tuple[float, float]]) -> None:
        if "master" in levels:
            self.master.set_levels(*levels["master"])
        for sid, row in self.rows.items():
            if sid in levels:
                row.meter.set_levels(*levels[sid])
