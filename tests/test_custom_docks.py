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
    assert cd.preset_url("Twitch activity feed", " Surakage ") == \
        "https://dashboard.twitch.tv/popout/u/surakage/stream-manager/activity-feed"
    assert cd.preset_url("Twitch activity feed", "") == ""
    assert "Twitch chat" not in cd.PRESETS  # chat lives in the Chat panel, never a second panel


def test_saved_list_round_trips(app):
    assert cd.load() == []
    cd.save([cd.CustomDock("a1", "Chat", "https://www.twitch.tv/popout/x/chat")])
    assert cd.load() == [cd.CustomDock("a1", "Chat", "https://www.twitch.tv/popout/x/chat")]


def test_dialog_adds_an_activity_feed_from_a_channel_name(app):
    dialog = cd.CustomDocksDialog([])
    dialog._add()
    dialog.channel.setText("surakage")
    dialog._channel_edited()
    docks = dialog.result_docks()
    assert [(d.name, d.url) for d in docks] == [
        ("Activity", "https://dashboard.twitch.tv/popout/u/surakage/stream-manager/activity-feed")]
    again = cd.CustomDocksDialog(docks)  # reopening recognises the preset and the channel
    assert again.preset.currentText() == "Twitch activity feed" and again.channel.text() == "surakage"
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


def test_a_web_view_is_a_window_of_its_own_and_never_a_widget_of_the_main_window(app, monkeypatch):
    """The first web view put into the shown window as a widget made Qt destroy and rebuild that whole window:
    SprigCast vanished and came back, and stopped 1.3 s, when a chat link was picked live (PC, 2026-11-21)."""
    pytest.importorskip("PySide6.QtWebEngineWidgets")
    from PySide6.QtWebEngineCore import QWebEngineProfile

    monkeypatch.setattr(cd, "_shared_profile", QWebEngineProfile.defaultProfile)
    window = QMainWindow()
    body = cd.WebDockBody("about:blank")
    window.setCentralWidget(body)
    window.show()
    app.processEvents()
    handle, surface = window.windowHandle(), window.windowHandle().surfaceType()
    body.ensure_loaded()
    for _ in range(5):
        app.processEvents()
    view = body.view
    assert view.isWindow() and view.parent() is None  # under no widget of the main window
    assert view.windowHandle().parent() is body._host and body._holder.parent() is body  # set inside the panel
    assert window.windowHandle() is handle and window.windowHandle().surfaceType() == surface  # left as it was
    window.resize(640, 480)
    for _ in range(5):
        app.processEvents()
    assert view.size() == body._holder.size()  # kept the panel's size

    monkeypatch.setenv("SPRIGCAST_WEB_IN_WINDOW", "1")  # the old way stays one switch away
    old = cd.WebDockBody("about:blank")
    old.ensure_loaded()
    assert old.view.parent() is old and old._host is None
    for widget in (old, window):
        widget.deleteLater()
    app.processEvents()


def test_web_panels_at_start_up_start_the_browser_engine_before_the_window_shows(app, monkeypatch):
    started = []
    monkeypatch.setattr(cd, "_profile", None)
    monkeypatch.setattr(cd, "web_engine_available", lambda: True)
    monkeypatch.setattr(cd, "_shared_profile", lambda: started.append(1))
    assert cd.warm_engine() and started == [1]
