"""A video or sound file's settings, as one rounded card. Same keys as sources/media.py MediaSource.defaults,
plus the sound keys its strip uses (volume_db, muted, monitor).

    ╭ Game Over ────────────────────────────╮
    │ ╭ File ───────────────────────────╮   │
    │ │ F:/…/2.webm             Choose… │   │
    │ ╰─────────────────────────────────╯   │
    │ ╭ Playback ───────────────────────╮   │
    │ │ Loop                        ( ) │   │
    │ │ When shown   [Restart][Resume]│
    │ │ When it ends [Last picture][Nothing]│
    │ │ Speed   [½][¾][1×][1¼][1½][2×]  │   │
    │ │ Use graphics card           ( ) │   │
    │ ╰─────────────────────────────────╯   │
    │ ╭ Sound ──────────────────────────╮   │
    │ │ Volume ────────●──── 0 dB  Mute │   │
    │ │ Who hears it [Stream][Both][Just me]│
    │ ╰─────────────────────────────────╯   │
    │                     Cancel  [ Save ]  │
    ╰────────────────────────────────────────╯
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from sprigcast.app import theme
from sprigcast.app.card import CardDialog
from sprigcast.app.live_stage import Switch
from sprigcast.sources.media import MediaSource, clamp_speed, file_filter, is_sound_file

CHIP = theme.SMALL_PILL_STYLE + f"QPushButton {{ background: {theme.RAISED}; padding: 7px 14px; border-radius: 15px; }}" \
    f"QPushButton:hover {{ background: {theme.RAISED_HOVER}; }}" \
    f"QPushButton:checked {{ background: {theme.SELECT_BG}; color: {theme.SELECT_TEXT}; }}" \
    f"QPushButton:disabled {{ color: {theme.LEAF_OFF}; }}"
SMALL_CHIP = CHIP + "QPushButton { padding: 7px 6px; }"
ROW_HEIGHT = 34
LABEL_WIDTH = 132
SPEEDS = ((0.5, "0.5×"), (0.75, "0.75×"), (1.0, "1×"), (1.25, "1.25×"), (1.5, "1.5×"), (2.0, "2×"))
HEAR = ((False, "Stream"), (True, "Both"), ("only", "Just me"))
VOLUME_RANGE = (-60, 12)  # dB, the same as the sound strip's slider


def _section(title: str) -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName("mediaSection")
    frame.setStyleSheet(f"#mediaSection {{ background: {theme.CARD}; border-radius: 16px; }}")
    box = QVBoxLayout(frame)
    box.setContentsMargins(18, 14, 18, 16)
    box.setSpacing(12)
    heading = QLabel(title)
    heading.setStyleSheet(f"color: {theme.TEXT_BRIGHT}; font-weight: 600; background: transparent;")
    box.addWidget(heading)
    return frame, box


def _name(text: str) -> QLabel:
    label = QLabel(text)
    label.setStyleSheet(f"color: {theme.MUTED}; background: transparent;")
    label.setMinimumWidth(104)
    return label


class Choice(QWidget):
    """A row of chips; one is picked. value() gives the picked one's value."""

    def __init__(self, options, current, parent: QWidget | None = None, style: str = CHIP):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self.group = QButtonGroup(self)
        self.values: list[Any] = []
        for value, text in options:
            chip = QPushButton(text)
            chip.setCheckable(True)
            chip.setStyleSheet(style)
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)  # share the width evenly
            chip.setMinimumWidth(chip.sizeHint().width())
            self.group.addButton(chip, len(self.values))
            self.values.append(value)
            row.addWidget(chip, 1)
        self.set_value(current)

    def set_value(self, value) -> None:
        index = next((i for i, v in enumerate(self.values) if v == value and type(v) is type(value)), None)
        if index is None:
            index = next((i for i, v in enumerate(self.values) if v == value), 0)
        self.group.button(index).setChecked(True)

    def value(self):
        return self.values[max(0, self.group.checkedId())]

    def chip(self, value) -> QPushButton:
        return self.group.button(self.values.index(value))


class MediaDialog(CardDialog):
    """New or change a video or sound file. values() gives the settings to save."""

    def __init__(self, title: str, settings: dict[str, Any], parent: QWidget | None = None, primary: str = "Save"):
        super().__init__(title, parent, width=540)
        s = {**MediaSource.defaults, **settings}
        self.switches: dict[str, Switch] = {}

        # The file.
        where, box = _section("File")
        row = QHBoxLayout()
        row.setSpacing(8)
        self.path = self.line_edit("Pick a video or sound file")
        self.path.setText(str(s.get("path", "") or ""))
        self.path.setCursorPosition(0)
        browse = QPushButton("Choose…")
        browse.setStyleSheet(CHIP)
        browse.setCursor(Qt.CursorShape.PointingHandCursor)
        browse.clicked.connect(self._pick_file)
        row.addWidget(self.path, 1)
        row.addWidget(browse)
        box.addLayout(row)
        self.body.addWidget(where)

        # How it plays.
        play, box = _section("Playback")
        self.loop = self._switch("loop", bool(s["loop"]))
        self.on_start = self._switch("play_on_start", bool(s["play_on_start"]))
        self.shown = Choice(((True, "Restart"), (False, "Resume")), bool(s["restart_on_show"]))
        self.ends = Choice(((False, "Last picture"), (True, "Nothing")), bool(s["clear_on_end"]))
        speed = clamp_speed(s.get("speed", 1.0))
        speeds = SPEEDS if any(abs(v - speed) < 1e-6 for v, _ in SPEEDS) else (*SPEEDS, (speed, f"{speed:g}×"))
        self.speed = Choice(speeds, next((v for v, _ in speeds if abs(v - speed) < 1e-6), 1.0), style=SMALL_CHIP)
        self.hw = self._switch("hw_decode", bool(s.get("hw_decode", False)))
        self.hw.setToolTip("Decode on the graphics card: lighter on the CPU for big videos")
        self.rows: dict[str, tuple[QLabel, QWidget]] = {}
        self.row_boxes: dict[str, QWidget] = {}
        for key, text, widget in (("loop", "Loop", self.loop), ("play_on_start", "Play at launch", self.on_start),
                                  ("shown", "When shown", self.shown), ("ends", "When it ends", self.ends),
                                  ("speed", "Speed", self.speed), ("hw_decode", "Use graphics card", self.hw)):
            # One row widget each, so hiding a row leaves no gap.
            holder = QWidget()
            holder.setStyleSheet("background: transparent;")
            line = QHBoxLayout(holder)
            line.setContentsMargins(0, 0, 0, 0)
            line.setSpacing(14)
            label = _name(text)
            label.setFixedWidth(LABEL_WIDTH)
            label.setMinimumHeight(ROW_HEIGHT)
            line.addWidget(label)
            line.addWidget(widget, 0 if isinstance(widget, Switch) else 1)
            if isinstance(widget, Switch):
                line.addStretch(1)
            box.addWidget(holder)
            self.rows[key] = (label, widget)
            self.row_boxes[key] = holder
        self.body.addWidget(play)

        # Its sound: the same volume, mute and who-hears-it as its strip under the canvas.
        sound, box = _section("Sound")
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(12)
        volume_row = QHBoxLayout()
        volume_row.setSpacing(10)
        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(*VOLUME_RANGE)
        self.volume.setPageStep(3)
        self.volume.setValue(round(min(max(float(s.get("volume_db", 0.0) or 0.0), VOLUME_RANGE[0]), VOLUME_RANGE[1])))
        self.volume.setCursor(Qt.CursorShape.PointingHandCursor)
        self.volume_note = QLabel()
        self.volume_note.setMinimumWidth(48)
        self.volume_note.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.volume_note.setStyleSheet(f"color: {theme.TEXT}; background: transparent;")
        self.volume.valueChanged.connect(self._show_volume)
        self.mute = QPushButton("Mute")
        self.mute.setCheckable(True)
        self.mute.setChecked(bool(s.get("muted", False)))
        self.mute.setStyleSheet(CHIP)
        self.mute.setCursor(Qt.CursorShape.PointingHandCursor)
        self.mute.toggled.connect(self._show_volume)
        volume_row.addWidget(self.volume, 1)
        volume_row.addWidget(self.volume_note)
        volume_row.addWidget(self.mute)
        volume_label = _name("Volume")
        volume_label.setMinimumHeight(ROW_HEIGHT)
        volume_label.setFixedWidth(LABEL_WIDTH)
        grid.addWidget(volume_label, 0, 0)
        grid.addLayout(volume_row, 0, 1)
        monitor = s.get("monitor", False)
        self.hear = Choice(HEAR, "only" if monitor == "only" else bool(monitor))
        hear_label = _name("Who hears it")
        hear_label.setFixedWidth(LABEL_WIDTH)
        grid.addWidget(hear_label, 1, 0)
        grid.addWidget(self.hear, 1, 1)
        grid.setColumnStretch(1, 1)
        box.addLayout(grid)
        self.body.addWidget(sound)
        self._show_volume()

        self.yes = self.add_buttons(primary)
        self.loop.toggled.connect(self._fit_to_file)
        self.path.textChanged.connect(self._fit_to_file)
        self._fit_to_file()

    # Pieces ---------------------------------------------------------------------------

    def _switch(self, key: str, on: bool) -> Switch:
        switch = Switch()
        switch.setChecked(on)
        switch.setCursor(Qt.CursorShape.PointingHandCursor)
        self.switches[key] = switch
        return switch

    def _show_volume(self) -> None:
        value = self.volume.value()
        self.volume.setEnabled(not self.mute.isChecked())
        self.volume_note.setText("Muted" if self.mute.isChecked() else f"{value:+d} dB" if value else "0 dB")

    def _fit_to_file(self) -> None:
        """A sound file has no picture: no When it ends, but Play at launch. A loop never ends."""
        sound = is_sound_file(self.path.text().strip())
        for key, shown in (("play_on_start", sound), ("ends", not sound), ("shown", not sound),
                           ("hw_decode", not sound)):
            self.row_boxes[key].setVisible(shown)
        self.ends.setEnabled(not self.loop.isChecked())
        self.rows["ends"][0].setEnabled(not self.loop.isChecked())

    def _pick_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose a video or sound file", self.path.text(), file_filter())
        if path:
            self.path.setText(path)
            self.path.setCursorPosition(0)

    def values(self) -> dict[str, Any]:
        return {
            "path": self.path.text().strip(),
            "loop": self.loop.isChecked(),
            "restart_on_show": bool(self.shown.value()),
            "clear_on_end": bool(self.ends.value()),
            "play_on_start": self.on_start.isChecked(),
            "speed": float(self.speed.value()),
            "hw_decode": self.hw.isChecked(),
            "volume_db": float(self.volume.value()),
            "muted": self.mute.isChecked(),
            "monitor": self.hear.value(),
        }
