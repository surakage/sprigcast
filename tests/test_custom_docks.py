import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtCore import QCoreApplication, QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication, QMainWindow  # noqa: E402

from sprigcast.app import custom_docks as cd  # noqa: E402


@pytest.fixture
def app(tmp_path):
    app = QApplication.instance() or QApplication([])
    QCoreApplication.setOrganizationName("SprigCastTests")
    QCoreApplication.setApplicationName("CustomDocksTest")
    QSettings().clear()
    yield app
    QSettings().clear()


def test_urls_and_presets():
    assert cd.normalise_url("streamelements.com/overlay") == "https://streamelements.com/overlay"
    assert cd.normalise_url("http://localhost:8080") == "http://localhost:8080"
    assert cd.preset_url("Twitch chat", " Surakage ") == "https://www.twitch.tv/popout/surakage/chat"
    assert cd.preset_url("Twitch chat", "") == ""


def test_saved_list_round_trips(app):
    assert cd.load() == []
    cd.save([cd.CustomDock("a1", "Chat", "https://www.twitch.tv/popout/x/chat")])
    assert cd.load() == [cd.CustomDock("a1", "Chat", "https://www.twitch.tv/popout/x/chat")]


def test_dialog_adds_a_twitch_chat_from_a_channel_name(app):
    dialog = cd.CustomDocksDialog([])
    dialog._add()
    dialog.channel.setText("surakage")
    dialog._channel_edited()
    docks = dialog.result_docks()
    assert [(d.name, d.url) for d in docks] == [("Chat", "https://www.twitch.tv/popout/surakage/chat")]
    again = cd.CustomDocksDialog(docks)  # reopening recognises the preset and the channel
    assert again.preset.currentText() == "Twitch chat" and again.channel.text() == "surakage"
    again._remove()
    assert again.result_docks() == []


def test_apply_adds_updates_and_removes_web_panels_without_loading_pages(app):
    from sprigcast.app.panels import Panels

    window = QMainWindow()
    window.panels = Panels(window)
    spec = cd.CustomDock("a1", "Chat", "https://example.com/a")
    cd.apply(window, [spec])
    panel = window.panels.panels["custom:a1"]
    assert panel.widget.view is None  # not shown yet: no browser
    assert window.panels.is_shown("custom:a1", "live") and not window.panels.is_shown("custom:a1", "build")
    cd.apply(window, [cd.CustomDock("a1", "Alerts", "https://example.com/b")])
    assert panel.title == "Alerts" and panel.widget.url == "https://example.com/b"
    cd.apply(window, [])
    assert window.panels.panels == {} and cd.load() == []
    assert not window.panels.is_shown("custom:a1", "live")
