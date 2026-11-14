import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtCore import QCoreApplication, QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QMenu, QWidget  # noqa: E402

from sprigcast.app.panels import Panel, Panels  # noqa: E402


@pytest.fixture
def app():
    app = QApplication.instance() or QApplication([])
    QCoreApplication.setOrganizationName("SprigCastTests")
    QCoreApplication.setApplicationName("PanelsTest")
    QSettings().clear()
    yield app
    QSettings().clear()


def _window():
    window = QWidget()
    panels = Panels(window)
    for mode, card in panels.cards.items():
        card.setParent(window)
    window.show()
    return window, panels


def test_each_screen_shows_its_own_panels_with_tabs(app):
    window, panels = _window()
    seen = []
    panels.add(Panel("chat", "Chat", QLabel("chat"), on_show=lambda: seen.append("chat")), shown_in=("live",))
    panels.add(Panel("feed", "Activity", QLabel("feed")))
    panels.set_mode("build")
    assert not panels.cards["build"].isVisible()  # Build stays clear
    panels.set_mode("live")
    card = panels.cards["live"]
    assert card.isVisible() and list(card.buttons) == ["chat"] and seen
    panels.set_shown("feed", True)
    assert list(card.buttons) == ["chat", "feed"] and card.current == "feed"  # a new one comes to the front
    card.buttons["chat"].click()
    assert card.stack.currentWidget().text() == "chat"
    # Remembered for next time.
    again = Panels(QWidget())
    assert again.state.shown["live"] == ["chat", "feed"] and again.state.active["live"] == "chat"
    card.close_button.click()
    assert list(card.buttons) == ["feed"]
    window.close()


def test_pop_out_and_back(app):
    window, panels = _window()
    label = QLabel("chat")
    panels.add(Panel("chat", "Chat", label), shown_in=("live",))
    panels.set_mode("live")
    panels.pop_out("chat")
    popped = panels.windows["chat"]
    assert popped.isVisible() and label.window() is popped
    assert not panels.cards["live"].isVisible()
    panels.set_mode("build")  # a popped panel stays out whichever screen you're on
    assert popped.isVisible()
    popped.close()
    assert "chat" not in panels.windows and not panels.state.popped
    panels.set_mode("live")
    assert panels.cards["live"].isVisible() and label.window() is window
    window.close()


def test_menu_lists_panels_for_this_screen(app):
    window, panels = _window()
    panels.add(Panel("chat", "Chat", QLabel("chat")))
    panels.add(Panel("only_build", "Tools", QLabel("x"), modes=("build",)))
    panels.set_mode("live")
    menu = QMenu()
    panels.fill_menu(menu)
    texts = [a.text() for a in menu.actions()]
    assert "Chat" in texts and "Tools" not in texts and texts[0].startswith("Shown on Live")
    chat = next(a for a in menu.actions() if a.text() == "Chat")
    chat.trigger()
    assert panels.is_shown("chat") and panels.cards["live"].isVisible()
    panels.reset()
    assert not panels.is_shown("chat")
    window.close()
