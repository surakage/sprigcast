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

import shiboken6
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
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
    overload: str = ""  # "The encoder can't keep up with the stream." when it can't (output/encoder_load.py)

    def level(self) -> str:
        levels = [r[2] for r in self.rows] + ["warning" for _label, ok, _tip in self.checks if ok is False]
        if self.overload:
            levels.append("warning")
        if "bad" in levels:
            return "bad"
        return "warning" if "warning" in levels or self.dropped else "good"


def _tile(label: str) -> tuple[QFrame, QLabel]:
    """Styled by the page (ROW_STYLE): three style sheets of its own per tile were 24 for the eight."""
    tile = QFrame()
    tile.setObjectName("gardenTile")
    box = QVBoxLayout(tile)
    box.setContentsMargins(14, 10, 14, 12)
    box.setSpacing(2)
    small = QLabel(label)
    small.setObjectName("tileName")
    big = QLabel("–")
    big.setObjectName("tileValue")
    big.setProperty("tone", "bright")
    box.addWidget(small)
    box.addWidget(big)
    return tile, big


def _section(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("gardenSection")  # styled by the page (ROW_STYLE)
    return label


def _restyle(widget: QWidget, name: str, value: str) -> None:
    """A new value for a property the page's style picks colours by, shown straight away."""
    widget.setProperty(name, value)
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)


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
                 parent: QWidget | None = None, make_lighter: Callable[[], str] | None = None, fill: bool = True):
        """fill False: the numbers, rows and checks are filled when "now" is first shown (opened on the last
        streams, filling them as well was 150 ms of the open on the PC while live, 2026-11-18)."""
        super().__init__(parent)
        from sprigcast.app.empty import ArtPicture

        self.snapshot = snapshot
        self.make_lighter = make_lighter  # the one tap: returns what it did, in words
        self.setMinimumWidth(620)
        self.setStyleSheet(ROW_STYLE)  # the stream health rows' look, once for all of them
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
        self.mood.setObjectName("gardenMood")
        words.addStretch(1)
        words.addWidget(title)
        words.addWidget(self.mood)
        words.addStretch(1)
        head.addLayout(words, 1)
        self.close_button = QPushButton("×")
        self.close_button.setStyleSheet(
            theme.button_style(13, "0", extra="font-size: 15px; min-height: 0;"))
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
        self._shown: dict[str, tuple[str, str]] = {}
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
        body.addWidget(self._overload_bar())
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
        # The last few streams: made when first shown (made with the rest, it was 148 ms of the PC's 207 ms open
        # of the Garden check while live, 2026-11-18, for a view most opens never show).
        self._records, self._open_logs, self._health = records, open_logs, None

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
        self.again.setStyleSheet(theme.PILL_STYLE)
        self.again.setCursor(Qt.CursorShape.PointingHandCursor)
        self.again.clicked.connect(self.refresh)
        buttons.addWidget(self.again)
        self.done = QPushButton("Done")
        self.done.setStyleSheet(theme.PRIMARY_STYLE)
        self.done.setCursor(Qt.CursorShape.PointingHandCursor)
        buttons.addWidget(self.done)
        root.addLayout(buttons)
        self._filled = False
        if fill:
            self.refresh()

    @property
    def page(self):
        """The last streams' HealthPage, made the first time it's wanted."""
        if self._health is None:
            from sprigcast.app.health_page import HealthPage

            self._health = HealthPage(self._records, self._open_logs, fill_later=True)
            self.stack.addWidget(self._health)
        return self._health

    def show_view(self, view: str, fill: bool = True) -> None:
        """"now" or "health" (the last streams). `fill`: False leaves an unfilled "now" for the caller to fill."""
        self.stack.setCurrentWidget(self.page if view == "health" else self.stack.widget(0))
        if view != "health" and not self._filled and fill:
            self.refresh()
        self.history.setText("Back to now" if view == "health" else "Last streams")

    def refresh(self) -> None:
        try:
            snap = self.snapshot()
        except Exception:  # a number that can't be read this second shows next second
            return
        self.show_snapshot(snap)
        self._filled = True

    def show_snapshot(self, s: GardenSnapshot) -> None:
        level = s.level()
        line, picture = MOODS[level]
        if getattr(self, "_level", None) != level:  # once a second: restyle only when the mood changes
            self._level = level
            self.mood.setText(line)
            _restyle(self.mood, "level", level)  # coloured by the page's style (ROW_STYLE)
            self.sloth.set_art(picture)

        shown = self._shown  # what each tile says and its tone, kept here: no asking Qt each second

        def put(key: str, text: str, tone: str = "bright") -> None:
            if shown.get(key) == (text, tone):
                return
            was = shown.get(key, ("–", "bright"))
            shown[key] = (text, tone)
            label = self.values[key]
            if was[0] != text:
                label.setText(text)
            if was[1] != tone:  # re-polishes: only when the colour changes
                _restyle(label, "tone", tone)

        put("cpu", f"{s.cpu:.1f}%" if s.cpu is not None else "–")
        put("ram", f"{s.ram_mb:.0f} MB" if s.ram_mb is not None else "–")
        put("gpu", f"{s.gpu:.0f}%" if s.gpu is not None else "–")
        put("disk", f"{s.disk_gb:.0f} GB" if s.disk_gb is not None else "–",
            "warn" if s.disk_gb is not None and s.disk_gb < 10 else "bright")
        put("dropped", f"{s.dropped}", "warn" if s.dropped else "bright")
        put("render", f"{s.render_ms:.1f} ms" if s.render_ms is not None else "–")
        put("upload", f"{s.upload_kbps / 1000:.1f} Mbps" if s.upload_kbps else "Not live",
            "muted" if s.upload_kbps is None else "bright")
        put("recording", s.recording or "Off", "live" if s.recording else "muted")
        self._show_overload(s.overload)

        # Rows and chips are made again only when what they say changes, not every second.
        lists = (tuple(s.rows), tuple(s.checks))
        before = getattr(self, "_lists", None)
        if before == lists:
            return
        self._lists = lists
        if before is not None and before[1] == lists[1] and len(before[0]) == len(lists[0]) and lists[0]:
            # Live, each place's line says its kbps: the same rows with new words, changed in place. Made again
            # each second they were 110-160 ms a second on the PC while live (2026-11-18).
            for parts, old_row, new_row in zip(self._row_parts, before[0], lists[0]):
                self._update_row(parts, old_row, new_row)
            return
        self._row_parts: list[tuple[QLabel, QLabel, QLabel, QLabel]] = []
        while self.rows.count():
            widget = self.rows.takeAt(0).widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        for i, (name, detail, row_level, word) in enumerate(s.rows):
            row = self._row(name, detail, row_level, word, first=i == 0)
            self._row_parts.append(row.parts)
            self.rows.addWidget(row)  # shown with the rest when the layout next runs: once, not once per row
        if not s.rows:
            none = QLabel("No places to stream yet.")
            none.setStyleSheet(f"color: {theme.MUTED}; background: transparent; padding: 6px 0;")
            self.rows.addWidget(none)

        while self.checks.count():
            widget = self.checks.takeAt(0).widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        for i, (label, ok, tip) in enumerate(s.checks):
            chip = QLabel(("✓  " if ok else "!  " if ok is False else "–  ") + label)
            chip.setObjectName("checkChip")  # styled by the page (ROW_STYLE), not a style sheet of its own
            chip.setProperty("state", "ok" if ok else "warn" if ok is False else "quiet")
            chip.setToolTip(tip)
            self.checks.addWidget(chip, i // 4, i % 4)

    def _overload_bar(self) -> QFrame:
        """Shown while the encoder can't keep up: says so, with one tap to make encoding lighter."""
        bar = self.overload_bar = QFrame()
        bar.setObjectName("overloadBar")
        bar.setStyleSheet(f"#overloadBar {{ background: {theme.RAISED}; border-radius: 14px; }}")
        line = QHBoxLayout(bar)
        line.setContentsMargins(14, 10, 10, 10)
        line.setSpacing(12)
        self.overload_text = QLabel("")
        self.overload_text.setWordWrap(True)
        self.overload_text.setStyleSheet(f"color: {theme.WARN}; font-weight: 600; background: transparent;")
        line.addWidget(self.overload_text, 1)
        self.lighter = QPushButton("Make it lighter")
        self.lighter.setStyleSheet(theme.PILL_STYLE)
        self.lighter.setCursor(Qt.CursorShape.PointingHandCursor)
        self.lighter.setToolTip("A faster encoder preset, with no look-ahead or B-frames")
        self.lighter.clicked.connect(self._lighter_clicked)
        line.addWidget(self.lighter)
        self._lightened = ""  # what the tap did: shown instead of the warning from then on
        bar.hide()
        return bar

    def _show_overload(self, text: str) -> None:
        if self._lightened:
            return
        if self.overload_text.text() != text:
            self.overload_text.setText(text)
        self.lighter.setVisible(self.make_lighter is not None)
        self.overload_bar.setVisible(bool(text))

    def _lighter_clicked(self) -> None:
        if self.make_lighter is None:
            return
        self._lightened = self.make_lighter() or "Already as light as it goes."
        self.overload_text.setText(self._lightened)
        self.overload_text.setStyleSheet(f"color: {theme.ACCENT}; font-weight: 600; background: transparent;")
        self.lighter.hide()
        self.overload_bar.show()

    @staticmethod
    def _row(name: str, detail: str, level: str, word: str, first: bool) -> QWidget:
        """One place's line. Styled by the page's ROW_STYLE (object names and a "level" property): five style
        sheets of its own per row made the first open 140 ms on the PC while live (2026-11-18)."""
        from sprigcast.app.live_stage import Elided

        level = level if level in LEVEL_COLOURS else "idle"
        row = QFrame()
        row.setObjectName("healthRow")
        row.setProperty("ruled", not first)  # a line above every row but the first
        line = QHBoxLayout(row)
        line.setContentsMargins(2, 8, 2, 8)
        line.setSpacing(12)
        dot = QLabel("●")
        dot.setObjectName("healthDot")
        dot.setProperty("level", level)
        title = Elided(name)
        title.setObjectName("healthTitle")
        title.setFixedWidth(110)
        # Fixed, not Elided's usual Ignored: a layout gives an Ignored label no room at all, which drew the
        # name over its detail.
        title.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        info = Elided(detail)  # one line: a long one (reconnects, auto-pilot) ends in "…", the whole of it on hover
        info.setObjectName("healthInfo")
        info.setMinimumWidth(0)
        state = QLabel(word)
        state.setObjectName("healthState")
        state.setProperty("level", level)
        line.addWidget(dot)
        line.addWidget(title)
        line.addWidget(info, 1)
        line.addWidget(state)
        row.parts = (dot, title, info, state)
        return row

    @staticmethod
    def _update_row(parts: tuple[QLabel, QLabel, QLabel, QLabel], old: tuple, new: tuple) -> None:
        """A row's words and colour from `old` to `new` (name, detail, level, word), changing only what differs."""
        dot, title, info, state = parts
        if not all(shiboken6.isValid(w) for w in parts):
            return
        name, detail, level, word = new
        if name != old[0]:
            title.setText(name)
        if detail != old[1]:
            info.setText(detail)
        if word != old[3]:
            state.setText(word)
        if level != old[2]:
            level = level if level in LEVEL_COLOURS else "idle"
            _restyle(dot, "level", level)
            _restyle(state, "level", level)


TONES = {"bright": theme.TEXT_BRIGHT, "warn": theme.WARN, "muted": theme.MUTED, "live": theme.LIVE_TEXT}
ROW_STYLE = (
    f"#gardenTile {{ background: {theme.RAISED}; border-radius: 14px; }}"
    f"#tileName {{ color: {theme.MUTED}; font-size: 12px; background: transparent; }}"
    "#tileValue { font-size: 18px; font-weight: 700; background: transparent; }"
    + "".join(f'#tileValue[tone="{tone}"] {{ color: {colour}; }}' for tone, colour in TONES.items())
    + f"#gardenSection {{ color: {theme.TEXT_BRIGHT}; font-size: 15px; font-weight: 700; background: transparent; }}"
    "#gardenMood { font-weight: 600; background: transparent; }"
    + "".join(f'#gardenMood[level="{lv}"] {{ color: {colour}; }}' for lv, colour in LEVEL_COLOURS.items())
    +
    "#healthRow { background: transparent; border: none; }"
    f'#healthRow[ruled="true"] {{ border-top: 1px solid {theme.EDGE}; }}'
    "#healthDot { background: transparent; font-size: 10px; }"
    f"#healthTitle {{ color: {theme.TEXT_BRIGHT}; font-weight: 600; background: transparent; }}"
    f"#healthInfo {{ color: {theme.MUTED}; background: transparent; }}"
    "#healthState { font-weight: 600; background: transparent; }"
    + "".join(f'#healthDot[level="{lv}"], #healthState[level="{lv}"] {{ color: {colour}; }}'
              for lv, colour in LEVEL_COLOURS.items())
    + "#checkChip { min-height: 26px; max-height: 26px; }"
    + "".join(f'#checkChip[state="{state}"] {{ {style} }}'
              for state, style in (("ok", CHIP_OK), ("warn", CHIP_WARN), ("quiet", CHIP_QUIET)))
)


def garden_check_popup(snapshot: Callable[[], GardenSnapshot], records: list, parent: QWidget | None = None,
                       open_logs: Callable[[], None] | None = None, view: str = "now",
                       make_lighter: Callable[[], str] | None = None):
    """The pop-up, refreshed once a second while it's open. `view`: "now" or "health" (the last streams)."""
    from sprigcast.app.transitions_panel import PopUp

    pop = PopUp("Garden check", parent, bare=True)  # the page has its own title (with the sloth) and buttons
    # Filled a tick after it's put in the pop-up: its rows made in the same go as the pop-up were 75-101 ms of the
    # open on the PC while live (2026-11-18), and the pop-up's style reached every row as it was put in.
    # Made as the stack's child from the start: made on its own and put in after, Qt went through every tile and
    # label again to hand them the pop-up's look (62 ms in add_tab on the PC while live, 2026-11-19).
    pop.garden = GardenCheckPage(snapshot, records, open_logs, make_lighter=make_lighter, fill=False,
                                 parent=pop.stack)
    pop.add_tab("garden", "Garden check", pop.garden)
    pop.show_tab("garden")
    pop.garden.done.clicked.connect(pop.accept)
    pop.garden.close_button.clicked.connect(pop.accept)
    pop.garden.show_view(view, fill=False)
    if view != "health":
        QTimer.singleShot(0, pop.garden, lambda: None if pop.garden._filled else pop.garden.refresh())
    pop.timer = QTimer(pop)
    pop.timer.timeout.connect(lambda: pop.garden.refresh() if pop.garden.stack.currentIndex() == 0 else None)
    pop.timer.start(1000)
    # Gone when closed: a closed one used to stay, its timer rebuilding hidden rows every second for good.
    pop.finished.connect(lambda _result: (pop.timer.stop(), pop.deleteLater()))
    return pop
