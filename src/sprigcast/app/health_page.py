"""The Stream health pop-up: how a recent stream went, at a glance (app/health.py keeps the numbers)."""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QGridLayout, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from sprigcast.app import theme
from sprigcast.app.health import HealthRecord, duration

LEVEL_COLOURS = {"good": theme.ACCENT, "warning": theme.WARN, "bad": theme.BAD}


# The page's look in one style sheet, picked by object names and a "level" property: three style sheets per tile
# and two per place were the Last streams page's 253 ms on the PC while live (2026-11-18).
PAGE_STYLE = (
    f"#healthTile {{ background: {theme.RAISED}; border-radius: 16px; }}"
    f"#healthBig {{ color: {theme.TEXT_BRIGHT}; font-size: 20px; font-weight: 700; background: transparent; }}"
    + "".join(f'#healthBig[level="{lv}"] {{ color: {colour}; }}' for lv, colour in LEVEL_COLOURS.items())
    + f"#healthSmall, #healthNote {{ color: {theme.MUTED}; background: transparent; }}"
    "#healthVerdict { font-size: 15px; font-weight: 600; background: transparent; }"
    f"#healthPlace {{ color: {theme.TEXT}; font-weight: 600; background: transparent; }}"
)


_STYLE: str | None = None


def _page_style() -> str:
    """PAGE_STYLE and the rounded drop-down's look, put together once."""
    global _STYLE
    if _STYLE is None:
        from sprigcast.app.starter import combo_style

        _STYLE = PAGE_STYLE + combo_style()
    return _STYLE


def _tile(value: str, label: str, level: str = "") -> QWidget:
    tile = QWidget()
    tile.setObjectName("healthTile")  # styled by the page (PAGE_STYLE)
    box = QVBoxLayout(tile)
    box.setContentsMargins(16, 12, 16, 12)
    box.setSpacing(2)
    big = QLabel(value)
    big.setObjectName("healthBig")
    if level:
        big.setProperty("level", level)
    small = QLabel(label)
    small.setObjectName("healthSmall")
    box.addWidget(big)
    box.addWidget(small)
    return tile


class HealthPage(QWidget):
    def __init__(self, records: list[HealthRecord], open_logs: Callable[[], None] | None = None,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.records = records
        self.setStyleSheet(_page_style())
        self.setMinimumWidth(580)
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(14)
        if not records:
            empty = QLabel("No streams yet. Go live and this fills in when you end.")
            empty.setObjectName("healthNote")
            box.addWidget(empty)
            return
        self.pick = QComboBox()
        self.pick.addItems([f"{r.started}  ·  {duration(r.seconds)}" for r in records])
        box.addWidget(self.pick)
        # Hidden or shown only once it's in the page: shown with no parent yet, it opened as a window of its own
        # (made, styled, then taken in again), 90-133 ms of Last streams on the PC while live (2026-11-18).
        # (Its look is in the page's style too: one of its own was 95 ms.)
        self.pick.setVisible(len(records) > 1)
        self.verdict = QLabel()
        self.verdict.setObjectName("healthVerdict")
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
            name.setObjectName("healthPlace")
            note = QLabel("  ·  ".join(bits))
            note.setObjectName("healthNote")
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

