"""Live mode's own screen: the stage, the bloom tiles under it, and a calm column on the right.

    ┌ stage ─────────────────────────────────────┐ ┌ End stream ┐
    │ horizontal program   vertical program      │ │ Record  Clip│
    │ NEXT · Gaming                  [Bloom it]  │ │ Where it goes
    └────────────────────────────────────────────┘ │ Sound       │
    [Just Chatting] [Gaming] [BRB] [Ending] …      │ CPU · RAM   │

No docks: the main window moves its canvas previews and the Live panel (bloom tiles) in here when
Live mode starts, and back when Build mode starts. Panels you pick in the Panels menu (chat, activity)
fill the bottom of the right column (app/panels.py); CPU and memory live in the strip along the bottom.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from sprigcast.app import theme

SIDE_WIDTH = 300
GAP = 20


class Switch(QCheckBox):
    """A small on/off switch: a pill with a leaf for a knob, green when on."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedSize(30, 16)

    def hitButton(self, pos) -> bool:  # noqa: N802 (Qt naming): the whole pill is clickable
        return self.rect().contains(pos)

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        on = self.isChecked()
        track = QColor(theme.ACCENT if on else theme.EDGE_STRONG)
        if not self.isEnabled():
            track.setAlpha(110)
        p.setBrush(track)
        p.drawRoundedRect(QRectF(0, 0, 30, 16), 8, 8)
        # The knob is a little leaf: dark on the green track when on, a soft grey one when off.
        knob = QRectF(17 if on else 3, 2.5, 11, 11)
        theme.paint_leaf(p, knob, True, "#0d1210" if on else theme.MUTED,
                         vein=theme.ACCENT if on else theme.EDGE_STRONG, width=1.0)
        p.end()


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
        self.colour: str | None = None  # one calm colour (a sound group's) instead of green/yellow
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
            if self.colour:
                colour = self.colour if self.level < 0.85 else theme.LIVE  # pink only near clipping
            else:
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
    canvas: str = "horizontal"
    test: bool | None = None  # Twitch bandwidth test on/off; None where the service has none


SERVICE_CHOICES = (("twitch", "Twitch"), ("youtube", "YouTube"), ("kick", "Kick"), ("tiktok", "TikTok"),
                   ("custom", "Custom RTMP"))
CHIP_STYLE = (
    f"QPushButton {{ background: {theme.RAISED}; color: {theme.TEXT}; border: none; border-radius: 10px;"
    f" padding: 2px 10px; min-height: 16px; font-size: 11px; }}"
    f"QPushButton:hover {{ background: {theme.RAISED_HOVER}; }}"
    f"QPushButton:checked {{ background: {theme.SELECT_BG}; color: {theme.SELECT_TEXT}; }}"
)
REMOVE_CHIP_STYLE = CHIP_STYLE + f"QPushButton {{ color: {theme.BAD}; }}"


def side_card() -> QFrame:
    """A soft rounded card for a side-column section, padded so nothing touches its edges."""
    card = QFrame()
    card.setObjectName("sideCard")
    card.setStyleSheet(f"#sideCard {{ background: {theme.PANEL}; border: none; border-radius: 18px; }}")
    layout = QVBoxLayout(card)
    layout.setContentsMargins(18, 16, 18, 18)
    layout.setSpacing(14)
    return card


class LiveSide(QWidget):
    """The right-hand column: the big stream button, Record and Save clip, where it goes, and sound."""

    def __init__(self, on_live: Callable[[], None], on_record: Callable[[], None], on_clip: Callable[[], None],
                 on_destination: Callable[[str | None], None],
                 on_toggle: Callable[[str, bool], None] | None = None,
                 on_place: Callable[[str, str], None] | None = None,
                 on_settings: Callable[[], None] | None = None,
                 on_sound: Callable[[str, dict], None] | None = None,
                 on_sound_filters: Callable[[str], None] | None = None,
                 on_mic_first: Callable[[bool], None] | None = None):
        """on_destination(id): open Settings on a place (None: the first, "new:<service>": add one).
        on_place(id, action): Edit mode's quick changes: "remove", "shape", "test"."""
        super().__init__()
        self.on_destination = on_destination
        self.on_toggle = on_toggle or (lambda _id, _on: None)
        self.on_place = on_place or (lambda _id, _action: None)
        self.is_live = False
        self.editing = False
        self._places: list[Destination] = []
        self._levels: dict[str, str] = {}
        self.setFixedWidth(SIDE_WIDTH)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(16)

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
        self.settings = pill("⚙")
        self.settings.setFixedWidth(44)
        self.settings.setToolTip("Settings (Ctrl+,)")
        self.settings.setVisible(on_settings is not None)
        if on_settings is not None:
            self.settings.clicked.connect(on_settings)
        row.addWidget(self.settings)
        buttons.addLayout(row)
        self.rec_note = QLabel("")
        self.rec_note.setStyleSheet(f"color: {theme.LIVE_TEXT}; background: transparent;")
        self.rec_note.setVisible(False)
        buttons.addWidget(self.rec_note)
        root.addLayout(buttons)

        where_card = side_card()
        where = where_card.layout()
        top = QHBoxLayout()
        top.addWidget(heading("Where it goes"), 1)
        self.add_place = QPushButton("+ Add")
        self.add_place.setStyleSheet(theme.LINK_STYLE)
        self.add_place.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_place.setToolTip("Add another place to stream")
        self.add_menu = QMenu(self)
        for key, label in SERVICE_CHOICES:
            self.add_menu.addAction(label).triggered.connect(lambda _=False, k=key: on_destination(f"new:{k}"))
        self.add_place.clicked.connect(self._show_add_menu)
        top.addWidget(self.add_place)
        self.manage = QPushButton("Edit")
        self.manage.setStyleSheet(theme.LINK_STYLE)
        self.manage.setCursor(Qt.CursorShape.PointingHandCursor)
        self.manage.setToolTip("Switch places on or off, remove them, or change their shape, right here")
        self.manage.clicked.connect(lambda: self.set_editing(not self.editing))
        top.addWidget(self.manage)
        where.addLayout(top)
        self.places = QVBoxLayout()
        self.places.setSpacing(12)
        where.addLayout(self.places)
        self.all_settings = QPushButton("All stream settings…")
        self.all_settings.setStyleSheet(theme.LINK_STYLE)
        self.all_settings.setCursor(Qt.CursorShape.PointingHandCursor)
        self.all_settings.clicked.connect(lambda: on_destination(None))
        self.all_settings.setVisible(False)
        where.addWidget(self.all_settings, 0, Qt.AlignmentFlag.AlignLeft)
        root.addWidget(where_card)
        self.place_rows: dict[str, tuple[QLabel, QLabel, QLabel, QCheckBox]] = {}
        self.place_tools: dict[str, dict[str, QPushButton]] = {}
        self._shape_key: tuple | None = None

        from sprigcast.app.sound_card import SoundCard

        sound_card = side_card()
        self.sound = SoundCard(on_sound or (lambda _id, _changes: None), on_sound_filters or (lambda _id: None),
                               on_mic_first)
        sound_card.layout().addWidget(self.sound)
        sound_card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)  # panels get the rest
        root.addWidget(sound_card)

        # Panels you choose for Live (chat, activity feed...) fill the rest of the column (app/panels.py).
        self.panel_slot = QVBoxLayout()
        self.panel_slot.setContentsMargins(0, 0, 0, 0)
        root.addLayout(self.panel_slot, 1)
        self.stats = QLabel("")
        self.stats.setStyleSheet(f"color: {theme.MUTED}; background: transparent;")
        self.stats.setWordWrap(True)
        self.stats.hide()  # the stats strip along the bottom of the window shows these now
        root.addWidget(self.stats)

    # The buttons ------------------------------------------------------------------------

    def set_live(self, live: bool) -> None:
        self.is_live = live
        self.add_place.setEnabled(not live)
        if live and self.editing:
            self.set_editing(False)
        self.manage.setEnabled(not live)
        for *_, switch in self.place_rows.values():
            switch.setEnabled(not live)  # a place joins or leaves only between streams
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

    def _show_add_menu(self) -> None:
        self.add_menu.exec(self.add_place.mapToGlobal(self.add_place.rect().bottomLeft()))

    def set_editing(self, on: bool) -> None:
        """Edit mode: each place gets Remove, its shape and (Twitch) test mode, without leaving Live."""
        self.editing = on and not self.is_live
        self.manage.setText("Done" if self.editing else "Edit")
        self.all_settings.setVisible(self.editing)
        self.show_destinations(self._places, self._levels)

    def show_destinations(self, places: list[Destination], levels: dict[str, str]) -> None:
        """levels: destination id -> health level ("good", "warning", "bad", "idle", "off")."""
        self._places, self._levels = list(places), dict(levels)
        shape = ([p.id for p in places], self.editing)
        if shape != self._shape_key:
            self._shape_key = shape
            while self.places.count():
                item = self.places.takeAt(0)
                widget = item.widget()
                if widget is not None:
                    widget.setParent(None)
                    widget.deleteLater()
            self.place_rows, self.place_tools = {}, {}
            if not places:
                empty = QPushButton("+ Add a place to stream")
                empty.setStyleSheet(theme.LINK_STYLE)
                empty.clicked.connect(self._show_add_menu)
                self.places.addWidget(empty)
            for place in places:
                self.places.addWidget(self._place_row(place))
        from sprigcast.app.destinations import HEALTH_COLORS

        for place in places:
            dot, name, note, switch = self.place_rows[place.id]
            switch.blockSignals(True)
            switch.setChecked(place.on)
            switch.blockSignals(False)
            switch.setEnabled(not self.is_live)
            level = levels.get(place.id, "idle" if place.on else "off")
            dot.setStyleSheet(f"color: {HEALTH_COLORS.get(level, HEALTH_COLORS['idle'])}; background: transparent;")
            name.setText(place.name)
            name.setStyleSheet(f"color: {theme.TEXT if place.on else theme.MUTED}; background: transparent;")
            note.setText(place.note)
            tools = self.place_tools.get(place.id)
            if tools:
                tall = place.canvas == "vertical"
                tools["shape"].setText("Vertical" if tall else "Horizontal")
                if "test" in tools:
                    tools["test"].blockSignals(True)
                    tools["test"].setChecked(bool(place.test))
                    tools["test"].blockSignals(False)

    def _place_row(self, place: Destination) -> QWidget:
        if not self.editing:
            box = QPushButton()
            box.setStyleSheet("QPushButton { background: transparent; border: none; padding: 0; text-align: left; }")
            box.setCursor(Qt.CursorShape.PointingHandCursor)
            box.setToolTip("Open this place's settings")
            box.clicked.connect(lambda _=False, pid=place.id: self.on_destination(pid))
        else:
            box = QWidget()
        outer = QVBoxLayout(box)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)
        line = QHBoxLayout()
        line.setSpacing(10)
        dot, name, note = QLabel("●"), QLabel(), QLabel()
        for label in (dot, name, note):
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        note.setStyleSheet(f"color: {theme.MUTED}; background: transparent;")
        switch = Switch()
        switch.setCursor(Qt.CursorShape.PointingHandCursor)
        switch.setToolTip("On: Go live sends here. Off: skipped.")
        switch.toggled.connect(lambda on, pid=place.id: self.on_toggle(pid, on))
        line.addWidget(switch)
        line.addWidget(dot)
        line.addWidget(name, 1)
        line.addWidget(note)
        outer.addLayout(line)
        box.setMinimumHeight(22)
        self.place_rows[place.id] = (dot, name, note, switch)
        if self.editing:
            tools_row = QHBoxLayout()
            tools_row.setContentsMargins(0, 2, 0, 4)
            tools_row.setSpacing(6)
            tools: dict[str, QPushButton] = {}

            def chip(key: str, text: str, tip: str, style: str = CHIP_STYLE) -> QPushButton:
                b = QPushButton(text)
                b.setStyleSheet(style)
                b.setCursor(Qt.CursorShape.PointingHandCursor)
                b.setToolTip(tip)
                tools[key] = b
                tools_row.addWidget(b)
                return b

            chip("shape", "", "Which canvas this place gets: click to swap").clicked.connect(
                lambda _=False, pid=place.id: self.on_place(pid, "shape"))
            if place.test is not None:
                test = chip("test", "Test", "Bandwidth test: send to Twitch without going live")
                test.setCheckable(True)
                test.toggled.connect(lambda _on, pid=place.id: self.on_place(pid, "test"))
            chip("settings", "⚙", "Key, bitrate and everything else").clicked.connect(
                lambda _=False, pid=place.id: self.on_destination(pid))
            tools_row.addStretch(1)
            remove = chip("remove", "Remove", "Remove this place (click twice)", REMOVE_CHIP_STYLE)
            remove.clicked.connect(lambda _=False, pid=place.id, b=remove: self._remove_clicked(pid, b))
            outer.addLayout(tools_row)
            self.place_tools[place.id] = tools
        return box

    def _remove_clicked(self, place_id: str, button: QPushButton) -> None:
        """First click asks ("Sure?"), the second removes."""
        if button.text() != "Sure?":
            button.setText("Sure?")
            return
        self.on_place(place_id, "remove")

    # Sound --------------------------------------------------------------------------------

    def show_sound(self, sources: list) -> None:
        """Sound items for every audio source shown in Live (see app/sound_card.py)."""
        self.sound.show_sound(sources)

    def set_levels(self, levels: dict[str, tuple[float, float]]) -> None:
        self.sound.set_levels(levels)


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
