"""The Stream health pop-up: how a recent stream went, at a glance (app/health.py keeps the numbers)."""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QGridLayout, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from sprigcast.app import theme
from sprigcast.app.health import HealthRecord, duration

LEVEL_COLOURS = {"good": theme.ACCENT, "warning": theme.WARN, "bad": theme.BAD}


def _tile(value: str, label: str, level: str = "") -> QWidget:
    tile = QWidget()
    tile.setObjectName("healthTile")
    tile.setStyleSheet(f"#healthTile {{ background: {theme.RAISED}; border-radius: 16px; }}")
    box = QVBoxLayout(tile)
    box.setContentsMargins(16, 12, 16, 12)
    box.setSpacing(2)
    big = QLabel(value)
    big.setStyleSheet(f"color: {LEVEL_COLOURS.get(level, theme.TEXT_BRIGHT)}; font-size: 20px; font-weight: 700;"
                      " background: transparent;")
    small = QLabel(label)
    small.setStyleSheet(f"color: {theme.MUTED}; background: transparent;")
    box.addWidget(big)
    box.addWidget(small)
    return tile


class HealthPage(QWidget):
    def __init__(self, records: list[HealthRecord], open_logs: Callable[[], None] | None = None,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.records = records
        self.setMinimumWidth(580)
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(14)
        if not records:
            empty = QLabel("No streams yet. Go live and this fills in when you end.")
            empty.setStyleSheet(f"color: {theme.MUTED}; background: transparent;")
            box.addWidget(empty)
            return
        self.pick = QComboBox()
        for r in records:
            self.pick.addItem(f"{r.started}  ·  {duration(r.seconds)}")
        self.pick.setVisible(len(records) > 1)
        from sprigcast.app.starter import combo_style

        self.pick.setStyleSheet(combo_style())
        box.addWidget(self.pick)
        self.verdict = QLabel()
        self.verdict.setStyleSheet("font-size: 15px; font-weight: 600; background: transparent;")
        box.addWidget(self.verdict)
        self.tiles = QGridLayout()
        self.tiles.setSpacing(10)
        box.addLayout(self.tiles)
        self.places = QVBoxLayout()
        self.places.setSpacing(6)
        box.addLayout(self.places)
        if open_logs is not None:
            more = QPushButton("Open the full report")
            more.setStyleSheet(theme.LINK_STYLE)
            more.setCursor(Qt.CursorShape.PointingHandCursor)
            more.setToolTip("Every 5 seconds of the stream, in the Logs folder")
            more.clicked.connect(open_logs)
            box.addWidget(more, 0, Qt.AlignmentFlag.AlignLeft)
        self.pick.currentIndexChanged.connect(self.show_record)
        self.show_record(0)

    def show_record(self, index: int) -> None:
        r = self.records[index]
        level, line = r.verdict()
        self.verdict.setText(f'<span style="color:{LEVEL_COLOURS[level]}">●</span>&nbsp;&nbsp;{line}')
        while self.tiles.count():
            self.tiles.takeAt(0).widget().deleteLater()
        tiles = [
            (duration(r.seconds), "Length", ""),
            (f"{r.dropped}", "Dropped frames", "bad" if r.dropped else "good"),
            (f"{r.reconnects}", "Reconnects", "bad" if r.reconnects else "good"),
            (f"{r.peak_mb} MB" if r.peak_mb else "–", "Peak memory", ""),
            (f"{r.hitches}", "Hitches", "warning" if r.hitches else "good"),
        ]
        for i, (value, label, lv) in enumerate(tiles):
            tile = _tile(value, label, lv)
            tile.setToolTip({"Hitches": "Seconds where SprigCast drew a frame late (a tiny stutter)",
                             "Peak memory": "The most memory SprigCast used during the stream"}.get(label, ""))
            # five tiles: three, then two (the last one wider) so the grid has no hole
            self.tiles.addWidget(tile, i // 3, i % 3, 1, 2 if i == len(tiles) - 1 and i % 3 == 1 else 1)
        while self.places.count():
            item = self.places.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        for p in r.places:
            share = "under 0.1%" if p.dropped_pct < 0.1 else f"{p.dropped_pct:.1f}%"
            bits = [f"{p.dropped} dropped" + (f" ({share})" if p.dropped else "")]
            if p.reconnects:
                bits.append(f"{p.reconnects} reconnects")
            if p.steps_down:
                bits.append(f"bitrate dipped to {p.lowest_kbps} of {p.target_kbps} kbps")
            row = QHBoxLayout()
            name = QLabel(p.name)
            name.setStyleSheet(f"color: {theme.TEXT}; font-weight: 600; background: transparent;")
            note = QLabel("  ·  ".join(bits))
            note.setStyleSheet(f"color: {theme.MUTED}; background: transparent;")
            note.setWordWrap(True)
            row.addWidget(name)
            row.addSpacing(10)
            row.addWidget(note, 1)
            holder = QWidget()
            holder.setLayout(row)
            row.setContentsMargins(4, 0, 4, 0)
            self.places.addWidget(holder)


def health_popup(records: list[HealthRecord], parent: QWidget | None = None,
                 open_logs: Callable[[], None] | None = None):
    from sprigcast.app.transitions_panel import PopUp

    pop = PopUp("Stream health", parent)
    pop.page = HealthPage(records, open_logs)
    pop.add_tab("health", "Health", pop.page)
    pop.show_tab("health")
    return pop


def garden_check_popup(records: list[HealthRecord], refresh: Callable[[QWidget], None], parent: QWidget | None = None,
                       open_logs: Callable[[], None] | None = None, tab: str = "now"):
    """The header's Garden check: "Right now" (every number, refreshed each second) and "Stream health"
    (how the last few streams went). refresh(panel) fills a StatsPanel with the current numbers."""
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QFrame, QScrollArea

    from sprigcast.app.stats_panel import StatsPanel
    from sprigcast.app.transitions_panel import PopUp

    pop = PopUp("Garden check", parent)
    pop.now = StatsPanel()
    pop.now.setMinimumSize(580, 360)
    scroll = pop.now.findChild(QScrollArea)
    if scroll is not None:  # the pop-up card is the background, no well inside it
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }")
        pop.now.label.setStyleSheet("background: transparent;")
    pop.add_tab("now", "Right now", pop.now)
    pop.page = HealthPage(records, open_logs)
    pop.add_tab("health", "Stream health", pop.page)
    pop.show_tab(tab)

    def tick() -> None:
        try:
            refresh(pop.now)
        except Exception:  # a number that can't be read this second shows next second
            pass

    tick()
    pop.timer = QTimer(pop)
    pop.timer.timeout.connect(tick)
    pop.timer.start(1000)
    return pop
