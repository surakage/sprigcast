"""The Garden check pop-up: how SprigCast and your streams are doing, at a glance.

The header's Garden check pill opens it. The main window hands it a snapshot (a plain dict, see
`GardenSnapshot`) once a second while it's open; "Check again" asks for one at once.

    ┌ (sloth) Garden check                                   × ┐
    │         All good · Light as a leaf                       │
    │ [CPU] [Memory] [GPU] [Disk free]                         │
    │ [Frames dropped] [Render time] [Upload] [Recording]      │
    │ Stream health                                            │
    │ ● Twitch   Horizontal · 4015 kbps · 0 dropped      Good  │
    │ Things we checked                                        │
    │ (✓ Mic heard) (✓ Camera on) (! Disk has room) …          │
    │                          [Last streams] [Check again] [Done] │
    └──────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from sprigcast.app import theme

LEVEL_COLOURS = {"good": theme.ACCENT, "warning": theme.WARN, "bad": theme.BAD, "idle": theme.MUTED}
MOODS = {  # level -> (the line under the title, the sloth picture)
    "good": ("All good · Light as a leaf", "happy"),
    "warning": ("A few bumps · worth a look", "napping"),
    "bad": ("Something needs a look", "trouble"),
}


@dataclass
class GardenSnapshot:
    """Everything the pop-up shows. Numbers are None when they can't be read (they show as "–")."""

    cpu: float | None = None  # % of the whole PC, SprigCast only
    ram_mb: float | None = None
    gpu: float | None = None
    disk_gb: float | None = None  # free space where recordings go
    dropped: int = 0
    render_ms: float | None = None
    upload_kbps: int | None = None  # while live: what every place is sent, added up
    recording: str = ""  # "1:55:04" while recording
    rows: list[tuple[str, str, str, str]] = field(default_factory=list)  # (name, detail, level, word)
    checks: list[tuple[str, bool | None, str]] = field(default_factory=list)  # (label, ok, tooltip); None: just quiet

    def level(self) -> str:
        levels = [r[2] for r in self.rows] + ["warning" for _label, ok, _tip in self.checks if ok is False]
        if "bad" in levels:
            return "bad"
        return "warning" if "warning" in levels or self.dropped else "good"


def _tile(label: str) -> tuple[QFrame, QLabel]:
    tile = QFrame()
    tile.setObjectName("gardenTile")
    tile.setStyleSheet(f"#gardenTile {{ background: {theme.RAISED}; border-radius: 14px; }}")
    box = QVBoxLayout(tile)
    box.setContentsMargins(14, 10, 14, 12)
    box.setSpacing(2)
    small = QLabel(label)
    small.setStyleSheet(f"color: {theme.MUTED}; font-size: 12px; background: transparent;")
    big = QLabel("–")
    big.setStyleSheet(f"color: {theme.TEXT_BRIGHT}; font-size: 18px; font-weight: 700; background: transparent;")
    box.addWidget(small)
    box.addWidget(big)
    return tile, big


def _section(text: str) -> QLabel:
    label = QLabel(text)
    label.setStyleSheet(f"color: {theme.TEXT_BRIGHT}; font-size: 15px; font-weight: 700; background: transparent;")
    return label


CHIP_OK = (f"color: {theme.ACCENT}; background: {theme.SELECT_BG}; border-radius: 13px; padding: 0 12px;"
           " font-weight: 600;")
CHIP_QUIET = (f"color: {theme.MUTED}; background: {theme.RAISED}; border-radius: 13px; padding: 0 12px;"
              " font-weight: 600;")
CHIP_WARN = (f"color: {theme.WARN}; background: {theme.RAISED}; border-radius: 13px; padding: 0 12px;"
             " font-weight: 600;")


class GardenCheckPage(QWidget):
    """The pop-up's page: a "now" view (tiles, stream health, checks) and the last streams' health."""

    TILES = (("cpu", "CPU"), ("ram", "Memory"), ("gpu", "GPU"), ("disk", "Disk free"),
             ("dropped", "Frames dropped"), ("render", "Render time"), ("upload", "Upload"), ("recording", "Recording"))

    def __init__(self, snapshot: Callable[[], GardenSnapshot], records: list, open_logs: Callable[[], None] | None = None,
                 parent: QWidget | None = None):
        super().__init__(parent)
        from sprigcast.app.empty import ArtPicture
        from sprigcast.app.health_page import HealthPage

        self.snapshot = snapshot
        self.setMinimumWidth(620)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(16)
        head = QHBoxLayout()
        head.setSpacing(14)
        self.sloth = ArtPicture("happy", 64, height=64)
        head.addWidget(self.sloth)
        words = QVBoxLayout()
        words.setSpacing(2)
        title = QLabel("Garden check")
        title.setStyleSheet(f"color: {theme.TEXT_BRIGHT}; font-size: 22px; font-weight: 700; background: transparent;")
        self.mood = QLabel("")
        words.addStretch(1)
        words.addWidget(title)
        words.addWidget(self.mood)
        words.addStretch(1)
        head.addLayout(words, 1)
        self.close_button = QPushButton("×")
        self.close_button.setStyleSheet(
            f"QPushButton {{ background: {theme.RAISED}; color: {theme.TEXT}; border: none; border-radius: 14px;"
            f" font-weight: 600; font-size: 15px; padding: 0; min-height: 0; }}"
            f" QPushButton:hover {{ background: {theme.RAISED_HOVER}; }}")
        self.close_button.setFixedSize(28, 28)
        self.close_button.setToolTip("Close")
        self.close_button.setCursor(Qt.CursorShape.PointingHandCursor)
        head.addWidget(self.close_button, 0, Qt.AlignmentFlag.AlignTop)
        root.addLayout(head)

        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)
        now = QWidget()
        body = QVBoxLayout(now)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(12)
        grid = QGridLayout()
        grid.setSpacing(10)
        self.values: dict[str, QLabel] = {}
        for i, (key, label) in enumerate(self.TILES):
            tile, value = _tile(label)
            grid.addWidget(tile, i // 4, i % 4)
            self.values[key] = value
        body.addLayout(grid)
        body.addSpacing(4)
        body.addWidget(_section("Stream health"))
        self.rows = QVBoxLayout()
        self.rows.setSpacing(0)
        body.addLayout(self.rows)
        body.addSpacing(4)
        body.addWidget(_section("Things we checked"))
        self.checks = QGridLayout()
        self.checks.setHorizontalSpacing(8)
        self.checks.setVerticalSpacing(8)
        checks_row = QHBoxLayout()
        checks_row.addLayout(self.checks)
        checks_row.addStretch(1)
        body.addLayout(checks_row)
        body.addStretch(1)
        self.stack.addWidget(now)
        self.page = HealthPage(records, open_logs)  # the last few streams
        self.stack.addWidget(self.page)

        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        self.history = QPushButton("Last streams")
        self.history.setStyleSheet(theme.LINK_STYLE)
        self.history.setCursor(Qt.CursorShape.PointingHandCursor)
        self.history.setToolTip("Dropped frames, reconnects, peak memory and hitches from your last few streams")
        self.history.clicked.connect(lambda: self.show_view("health" if self.stack.currentIndex() == 0 else "now"))
        buttons.addWidget(self.history)
        buttons.addStretch(1)
        self.again = QPushButton("Check again")
        self.again.setStyleSheet(theme.PILL_STYLE.replace(theme.PANEL, theme.RAISED, 1))
        self.again.setCursor(Qt.CursorShape.PointingHandCursor)
        self.again.clicked.connect(self.refresh)
        buttons.addWidget(self.again)
        self.done = QPushButton("Done")
        self.done.setStyleSheet(theme.PRIMARY_STYLE)
        self.done.setCursor(Qt.CursorShape.PointingHandCursor)
        buttons.addWidget(self.done)
        root.addLayout(buttons)
        self.refresh()

    def show_view(self, view: str) -> None:
        """"now" or "health" (the last streams)."""
        self.stack.setCurrentIndex(1 if view == "health" else 0)
        self.history.setText("Back to now" if view == "health" else "Last streams")

    def refresh(self) -> None:
        try:
            snap = self.snapshot()
        except Exception:  # a number that can't be read this second shows next second
            return
        self.show_snapshot(snap)

    def show_snapshot(self, s: GardenSnapshot) -> None:
        level = s.level()
        line, picture = MOODS[level]
        self.mood.setText(line)
        self.mood.setStyleSheet(f"color: {LEVEL_COLOURS[level]}; font-weight: 600; background: transparent;")
        self.sloth.set_art(picture)

        def put(key: str, text: str, colour: str = theme.TEXT_BRIGHT) -> None:
            self.values[key].setText(text)
            self.values[key].setStyleSheet(f"color: {colour}; font-size: 18px; font-weight: 700; background: transparent;")

        put("cpu", f"{s.cpu:.1f}%" if s.cpu is not None else "–")
        put("ram", f"{s.ram_mb:.0f} MB" if s.ram_mb is not None else "–")
        put("gpu", f"{s.gpu:.0f}%" if s.gpu is not None else "–")
        put("disk", f"{s.disk_gb:.0f} GB" if s.disk_gb is not None else "–",
            theme.WARN if s.disk_gb is not None and s.disk_gb < 10 else theme.TEXT_BRIGHT)
        put("dropped", f"{s.dropped}", theme.WARN if s.dropped else theme.TEXT_BRIGHT)
        put("render", f"{s.render_ms:.1f} ms" if s.render_ms is not None else "–")
        put("upload", f"{s.upload_kbps / 1000:.1f} Mbps" if s.upload_kbps else "Not live")
        put("recording", s.recording or "Off", theme.LIVE_TEXT if s.recording else theme.TEXT_BRIGHT)
        if s.upload_kbps is None:
            self.values["upload"].setStyleSheet(f"color: {theme.MUTED}; font-size: 18px; font-weight: 700;"
                                                " background: transparent;")
        if not s.recording:
            self.values["recording"].setStyleSheet(f"color: {theme.MUTED}; font-size: 18px; font-weight: 700;"
                                                   " background: transparent;")

        while self.rows.count():
            widget = self.rows.takeAt(0).widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        for i, (name, detail, row_level, word) in enumerate(s.rows):
            row = self._row(name, detail, row_level, word, first=i == 0)
            self.rows.addWidget(row)
            row.show()
        if not s.rows:
            none = QLabel("No places to stream yet.")
            none.setStyleSheet(f"color: {theme.MUTED}; background: transparent; padding: 6px 0;")
            self.rows.addWidget(none)
            none.show()

        while self.checks.count():
            widget = self.checks.takeAt(0).widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        for i, (label, ok, tip) in enumerate(s.checks):
            chip = QLabel(("✓  " if ok else "!  " if ok is False else "–  ") + label)
            chip.setFixedHeight(26)
            chip.setStyleSheet(CHIP_OK if ok else CHIP_WARN if ok is False else CHIP_QUIET)
            chip.setToolTip(tip)
            self.checks.addWidget(chip, i // 4, i % 4)
            chip.show()

    @staticmethod
    def _row(name: str, detail: str, level: str, word: str, first: bool) -> QWidget:
        row = QFrame()
        row.setObjectName("healthRow")
        row.setStyleSheet("#healthRow { background: transparent; border: none;"
                          + ("" if first else f" border-top: 1px solid {theme.EDGE};") + " }")
        line = QHBoxLayout(row)
        line.setContentsMargins(2, 8, 2, 8)
        line.setSpacing(12)
        colour = LEVEL_COLOURS.get(level, theme.MUTED)
        dot = QLabel("●")
        dot.setStyleSheet(f"color: {colour}; background: transparent; font-size: 10px;")
        from sprigcast.app.live_stage import Elided

        title = Elided(name)
        title.setFixedWidth(110)
        title.setStyleSheet(f"color: {theme.TEXT_BRIGHT}; font-weight: 600; background: transparent;")
        info = QLabel(detail)
        info.setWordWrap(True)  # a long line (reconnects, auto-pilot) wraps instead of pushing the word off
        info.setStyleSheet(f"color: {theme.MUTED}; background: transparent;")
        state = QLabel(word)
        state.setStyleSheet(f"color: {colour}; font-weight: 600; background: transparent;")
        line.addWidget(dot)
        line.addWidget(title)
        line.addWidget(info, 1)
        line.addWidget(state)
        return row


def garden_check_popup(snapshot: Callable[[], GardenSnapshot], records: list, parent: QWidget | None = None,
                       open_logs: Callable[[], None] | None = None, view: str = "now"):
    """The pop-up, refreshed once a second while it's open. `view`: "now" or "health" (the last streams)."""
    from sprigcast.app.transitions_panel import PopUp

    pop = PopUp("Garden check", parent, bare=True)  # the page has its own title (with the sloth) and buttons
    pop.garden = GardenCheckPage(snapshot, records, open_logs)
    pop.page = pop.garden.page  # the last streams' HealthPage
    pop.add_tab("garden", "Garden check", pop.garden)
    pop.show_tab("garden")
    pop.garden.done.clicked.connect(pop.accept)
    pop.garden.close_button.clicked.connect(pop.accept)
    pop.garden.show_view(view)
    pop.timer = QTimer(pop)
    pop.timer.timeout.connect(lambda: pop.garden.refresh() if pop.garden.stack.currentIndex() == 0 else None)
    pop.timer.start(1000)
    return pop
