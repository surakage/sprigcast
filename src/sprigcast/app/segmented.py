"""A row of quick picks in one rounded box (Faster | Balanced | Sharper): one is lit, a click picks another.

Used where a short list of choices reads better laid out than hidden in a drop-down (Settings → Stream → Quality).
A value that none of the buttons stands for (set by hand under Fine tuning, say) lights none of them.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QButtonGroup, QFrame, QHBoxLayout, QPushButton, QWidget

from sprigcast.app import theme


def segmented_style() -> str:
    t = theme
    return (
        f"QFrame#segmented {{ background: {t.FIELD}; border: 1px solid {t.FIELD_EDGE}; border-radius: 12px; }}"
        f"QFrame#segmented QPushButton {{ background: transparent; color: {t.LABEL}; border: none;"
        f" border-radius: 9px; padding: 5px 14px; min-height: 18px; font-weight: 500; }}"
        f"QFrame#segmented QPushButton:hover {{ color: {t.TEXT_BRIGHT}; background: {t.RAISED}; }}"
        f"QFrame#segmented QPushButton:checked {{ background: {t.SELECT_BG}; color: {t.SELECT_TEXT};"
        f" font-weight: 600; }}"
        f"QFrame#segmented QPushButton:disabled {{ color: {t.MUTED}; }}"
    )


class Segmented(QFrame):
    """Quick picks: choices is [(value, label), ...]. picked(value) when the person clicks one."""

    picked = Signal(object)

    def __init__(self, choices: list[tuple[Any, str]], value: Any = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("segmented")
        self.setStyleSheet(segmented_style())
        row = QHBoxLayout(self)
        row.setContentsMargins(3, 3, 3, 3)
        row.setSpacing(2)
        self.group = QButtonGroup(self)
        self.buttons: dict[Any, QPushButton] = {}
        for data, label in choices:
            button = QPushButton(label)
            button.setCheckable(True)
            button.setAutoDefault(False)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _=False, d=data: self._clicked(d))
            self.group.addButton(button)
            row.addWidget(button)
            self.buttons[data] = button
        self.setSizePolicy(self.sizePolicy().Policy.Fixed, self.sizePolicy().Policy.Fixed)
        self.set_value(value)

    def value(self) -> Any:
        return next((d for d, b in self.buttons.items() if b.isChecked()), None)

    def set_value(self, value: Any) -> None:
        """Light the button for `value`; none for a value no button stands for. Never emits picked."""
        self.group.setExclusive(False)  # (an exclusive group can't have none lit)
        for data, button in self.buttons.items():
            button.setChecked(data == value)
        self.group.setExclusive(True)

    def _clicked(self, data: Any) -> None:
        self.set_value(data)
        self.picked.emit(data)
