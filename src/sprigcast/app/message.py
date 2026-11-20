"""Messages and yes/no questions as rounded cards, in place of Windows' square message boxes.

    ╭──────────────────────────────╮        ╭──────────────────────────────╮
    │ Select a scene first.        │        │ ● Couldn't go live           │
    │                       [ OK ] │        │ The key was refused.         │
    ╰──────────────────────────────╯        │                       [ OK ] │
                                            ╰──────────────────────────────╯

information(), warning(), critical() and question() take what QMessageBox's do and answer the same way (question
gives back a QMessageBox.StandardButton), so a caller only swaps the name. MessageCard is for anything with its
own buttons: add_button() returns the button, clicked is the one pressed (None for Esc).
"""

from __future__ import annotations

import logging
import time

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QMessageBox, QPushButton, QWidget

from sprigcast.app import theme
from sprigcast.app.card import PRIMARY_PILL, CardDialog

Button = QMessageBox.StandardButton
NAMES = {Button.Ok: "OK", Button.Yes: "Yes", Button.No: "No", Button.Cancel: "Cancel", Button.Close: "Close"}
ORDER = (Button.Cancel, Button.No, Button.Close, Button.Ok, Button.Yes)  # the way out on the left, the go on the right
GO = (Button.Yes, Button.Ok)
DOT = {"warn": theme.WARN, "bad": theme.BAD}
# A card's buttons take no press for this long after it shows. It can pop up while you're typing or clicking
# elsewhere (Record pressed from an app or UI Automation: PC test 2026-11-20, a recording stopped with no
# "Stop recording?" seen), and a stray Enter, Space or click mustn't answer it unread.
ARM_S = 0.5
log = logging.getLogger(__name__)
PLAIN_PILL = theme.button_style(17, "9px 18px")
DANGER_PILL = PLAIN_PILL + (f"QPushButton {{ color: {theme.BAD}; border-color: {theme.BAD}; font-weight: 600; }}"
                            f"QPushButton:hover {{ background: {theme.BAD}; color: {theme.ACCENT_TEXT}; }}")


class MessageCard(CardDialog):
    """kind: "info", "ask", "warn" or "bad" (the last two get a yellow or red dot beside the title)."""

    def __init__(self, title: str, text: str, parent: QWidget | None = None, kind: str = "info", width: int = 440,
                 rich: bool = False):
        super().__init__(title, parent, width)
        self.clicked: QPushButton | None = None
        self._buttons: list[QPushButton] = []
        self._shown_at: float | None = None
        plain_title = not title or title == "SprigCast"  # the app's name says nothing: the message is the card
        if kind in DOT and not plain_title:
            self.heading.setText(f"<span style='color: {DOT[kind]};'>●</span>&nbsp; {title}")
            self.heading.setTextFormat(Qt.TextFormat.RichText)
        self.heading.setVisible(not plain_title)
        self.label = QLabel(text)
        self.label.setWordWrap(True)
        self.label.setTextFormat(Qt.TextFormat.RichText if rich else Qt.TextFormat.PlainText)
        self.label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.label.setStyleSheet(f"color: {theme.TEXT_BRIGHT if plain_title else theme.TEXT}; background: transparent;"
                                 + (" font-size: 14px;" if plain_title else ""))
        self.body.addWidget(self.label)
        self._row = QHBoxLayout()
        self._row.setSpacing(8)
        self._row.addStretch(1)
        self.body.addSpacing(4)
        self.body.addLayout(self._row)

    def add_widget(self, widget: QWidget) -> None:
        """Something between the message and the buttons, such as a tick box."""
        self.body.insertWidget(self.body.indexOf(self.label) + 1, widget)

    def add_button(self, text: str, primary: bool = False, danger: bool = False, default: bool = False) -> QPushButton:
        """Buttons sit left to right in the order added. primary is the green one (and Enter's, unless another
        is default); danger is outlined red and never Enter's."""
        button = QPushButton(text)
        button.setStyleSheet(PRIMARY_PILL if primary else DANGER_PILL if danger else PLAIN_PILL)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setAutoDefault(False)
        button.clicked.connect(lambda: self._press(button))
        self._row.addWidget(button)
        self._buttons.append(button)
        if default or (primary and not any(b.isDefault() for b in self._buttons)):
            self.set_default(button)
        return button

    def set_default(self, button: QPushButton) -> None:
        for other in self._buttons:
            other.setDefault(other is button)
        button.setFocus()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if self._shown_at is None:
            self._shown_at = time.monotonic()

    def armed(self) -> bool:
        return self._shown_at is None or time.monotonic() - self._shown_at >= ARM_S

    def _press(self, button: QPushButton) -> None:
        if not self.armed():
            log.info("ignored a press on %r: the card had only just shown", button.text())
            return
        self.clicked = button
        self.accept()


def _show(parent: QWidget | None, title: str, text: str, kind: str, buttons, default) -> Button:
    from sprigcast.app.modal import modal

    card = MessageCard(title, text, parent, kind)
    made = {}
    for which in ORDER:
        if buttons & which:
            made[which] = card.add_button(NAMES[which], primary=which in GO)
    if default in made:
        card.set_default(made[default])
    if kind == "ask":
        log.info("asked: %s", text.splitlines()[0][:80] if text else title)
    modal(card)
    picked = next((which for which, button in made.items() if button is card.clicked), None)
    if picked is None:  # Esc: the way out, else the only button
        picked = next((which for which in ORDER if which in made and which not in GO), next(iter(made)))
    if kind == "ask":
        log.info("answered: %s", NAMES.get(picked, "?"))
    return picked


def information(parent: QWidget | None, title: str, text: str, buttons=Button.Ok, default=Button.NoButton) -> Button:
    return _show(parent, title, text, "info", buttons, default)


def warning(parent: QWidget | None, title: str, text: str, buttons=Button.Ok, default=Button.NoButton) -> Button:
    return _show(parent, title, text, "warn", buttons, default)


def critical(parent: QWidget | None, title: str, text: str, buttons=Button.Ok, default=Button.NoButton) -> Button:
    return _show(parent, title, text, "bad", buttons, default)


def question(parent: QWidget | None, title: str, text: str, buttons=Button.Yes | Button.No,
             default=Button.NoButton) -> Button:
    return _show(parent, title, text, "ask", buttons, default)
