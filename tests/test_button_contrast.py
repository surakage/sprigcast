"""Every button reads as a button: its edge stands out from the card, pop-up or panel behind it."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtCore import QRect  # noqa: E402
from PySide6.QtWidgets import QApplication, QPushButton  # noqa: E402

from sprigcast.app import theme  # noqa: E402

MIN_EDGE = 1.8  # the button's edge against what's behind it (the old flat pills were 1.0-1.1)


def _lum(hex_or_colour) -> float:
    from PySide6.QtGui import QColor

    c = QColor(hex_or_colour)

    def ch(x: int) -> float:
        x /= 255
        return x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4

    return 0.2126 * ch(c.red()) + 0.7152 * ch(c.green()) + 0.0722 * ch(c.blue())


def _ratio(a, b) -> float:
    la, lb = _lum(a), _lum(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


@pytest.fixture
def app():
    app = QApplication.instance() or QApplication([])
    theme.apply_theme(app, "slothbloom")
    return app


@pytest.mark.parametrize("surface", ["BG", "CHROME", "PANEL", "CARD", "RAISED"])
def test_button_edge_and_fill_read_on_every_surface(surface):
    back = getattr(theme, surface)
    assert _ratio(theme.BUTTON_EDGE, back) >= MIN_EDGE
    assert _ratio(theme.BUTTON, back) >= 1.15  # the fill itself is a step lighter too


def test_shared_styles_all_draw_an_edge():
    for style in (theme.PILL_STYLE, theme.SMALL_PILL_STYLE, theme.LINK_STYLE, theme.button_style()):
        assert f"border: 1px solid {theme.BUTTON_EDGE}" in style
        assert "border: none" not in style


def _edge_contrast(top, button: QPushButton) -> float:
    img = top.grab().toImage()
    r = QRect(button.mapTo(top, button.rect().topLeft()), button.size())
    y = r.center().y()
    outside = img.pixelColor(r.left() - 3, y)
    return max(_ratio(img.pixelColor(x, y), outside) for x in range(r.left(), r.left() + 3))


def test_sign_in_error_cancel_stands_out(app):
    from sprigcast.app.signin_dialog import SignInDialog
    from sprigcast.output.oauth_loopback import SignInError

    def refused(_service):
        raise SignInError("Sign-in was refused.")

    dialog = SignInDialog("youtube", begin=refused, open_url=lambda u: True)
    dialog.show()
    app.processEvents()
    assert dialog.message.text() == "Sign-in was refused."
    assert _edge_contrast(dialog, dialog.cancel_button) >= MIN_EDGE
    dialog.reject()


def test_card_pop_up_cancel_stands_out(app):
    from sprigcast.app.card import CardDialog

    dialog = CardDialog("Rename bloom")
    dialog.add_buttons("Rename")
    dialog.show()
    app.processEvents()
    cancel = next(b for b in dialog.findChildren(QPushButton) if b.text() == "Cancel")
    assert _edge_contrast(dialog, cancel) >= MIN_EDGE
    dialog.reject()
