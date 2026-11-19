"""A section card with a small badge at the right of its title: "Expert" on Video's Sent out at, the server's
state on Connections' Remote control."""

from __future__ import annotations

from PySide6.QtWidgets import QGroupBox, QLabel, QWidget

from sprigcast.app import theme


class BadgedGroup(QGroupBox):
    def __init__(self, title: str, badge: str, colour: str | None = None, parent: QWidget | None = None):
        super().__init__(title, parent)
        self.badge = QLabel(self)
        self.badge.setStyleSheet(f"background: {theme.SELECT_BG}; color: {colour or theme.INFO}; border-radius: 8px;"
                                 f" padding: 2px 9px; font-size: 11px; font-weight: 600;")
        self.set_badge(badge)

    def set_badge(self, text: str) -> None:
        self.badge.setText(text)
        self.badge.setVisible(bool(text))
        self.badge.adjustSize()
        self._place()

    def _place(self) -> None:
        self.badge.move(self.width() - self.badge.width() - 16, 12)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._place()
