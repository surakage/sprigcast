import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtCore import QCoreApplication, QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from sprigcast.chat import ChatMessage  # noqa: E402


@pytest.fixture
def app():
    app = QApplication.instance() or QApplication([])
    QCoreApplication.setOrganizationName("SprigCastTests")
    QCoreApplication.setApplicationName("ChatLooks")
    QSettings().clear()
    yield app
    QSettings().clear()


def test_looks_change_what_a_line_shows():
    from sprigcast.app.chat_panel import Look, clock, message_html

    line = ChatMessage("twitch", "Moss", "hi", color="#f5c518", badges=("moderator", "first"), at=1_700_000_000)
    classic = message_html(line, True)
    assert "platform:twitch" in classic and "First" in classic and "●" in classic and clock(line.at) not in classic
    plain = message_html(line, True, look=Look(logos=False, badges=False, colors=False, times=True))
    assert "platform:" not in plain and "First" not in plain and "●" not in plain
    assert clock(line.at) in plain and "#f5c518" not in plain
    assert "font-size:18px" in message_html(line, look=Look(style="big"))
    assert "font-size:11px" in message_html(line, look=Look(style="compact"))
    assert clock(0) and ":" in clock(1_700_000_000)


def test_look_is_saved_and_redraws_the_view(app):
    from sprigcast.app import chat_panel

    look = chat_panel.Look(style="bubbles", times=True, colors=False)
    look.save()
    assert chat_panel.Look.saved() == look
    view = chat_panel.ChatView()
    assert view.look == look
    view.resize(400, 300)
    view.show()
    for i in range(3):
        view.add(ChatMessage("twitch", f"a{i}", "hi @surakage" if i == 1 else "hello", at=1_700_000_000))
    view.names = frozenset({"surakage"})
    view.flush()
    view.repaint()  # Bubbles paints a card behind each line
    app.processEvents()
    block = view.document().firstBlock()
    from PySide6.QtCore import Qt

    assert block.blockFormat().leftMargin() == 16
    assert block.next().blockFormat().background().style() == Qt.BrushStyle.NoBrush  # the bubble turns green instead
    view.set_look(chat_panel.Look(style="compact"))
    assert view.document().blockCount() == 3 and view.document().firstBlock().blockFormat().leftMargin() == 0


def test_hub_stamps_when_a_line_came_in(app):
    from sprigcast.chat.hub import ChatHub

    hub = ChatHub(lambda: {})
    seen = []
    hub.message.connect(seen.append)
    hub._message(ChatMessage("twitch", "a", "hi"))
    assert seen and seen[0].at > 0


def test_panel_menu_has_the_look_choices(app, monkeypatch):
    from sprigcast.app import chat_panel

    panel = chat_panel.ChatPanel()
    monkeypatch.setattr(panel, "source", lambda: ("chat", {}))
    menu = panel.build_menu()
    look = next(a.menu() for a in menu.actions() if a.text() == "Look")
    labels = [a.text() for a in look.actions() if not a.isSeparator()]
    assert labels == ["Classic", "Bubbles", "Compact", "Big text", "Times", "Platform logos", "Badges", "Name colors"]
    next(a for a in look.actions() if a.text() == "Bubbles").trigger()
    assert panel.view.look.style == "bubbles" and chat_panel.Look.saved().style == "bubbles"
    next(a for a in look.actions() if a.text() == "Times").trigger()
    assert panel.view.look.times and panel.view.look.style == "bubbles"
    panel.sleep()
