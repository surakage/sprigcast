"""Web page settings (its own card), its frame rate, reload when on air, and the SFX sound's name."""

import os

import pytest

from sprigcast.core.scene import Canvas, Scene, SceneCollection, SceneItem, SourceConfig
from sprigcast.sources.browser import STANDARD_FPS, page_fps


def test_frame_rate_is_standard_unless_custom():
    assert page_fps({}) == STANDARD_FPS
    assert page_fps({"custom_fps": False, "fps": 60}) == STANDARD_FPS
    assert page_fps({"custom_fps": True, "fps": 60}) == 60
    assert page_fps({"fps": 60}) == 60  # an older page set to 60 keeps it
    assert page_fps({"custom_fps": True, "fps": 500}) == 120


def _collection():
    return SceneCollection(
        canvases=[Canvas(id="horizontal", width=1920, height=1080)],
        sources=[SourceConfig(id="alerts", kind="browser", name="Alerts", settings={"reload_on_air": True}),
                 SourceConfig(id="chat", kind="browser", name="Chat"),
                 SourceConfig(id="sfx", kind="app_audio", name="SFX & alerts", settings={"preset": "sfx"}),
                 SourceConfig(id="mine", kind="app_audio", name="SFX & alerts", settings={"preset": "custom"})],
        scenes=[Scene(id="game", name="Gaming", canvas="horizontal", items=[SceneItem(source="alerts"),
                                                                            SceneItem(source="chat")]),
                Scene(id="soon", name="Starting soon", canvas="horizontal", items=[SceneItem(source="chat")])],
        active_scenes={"horizontal": "soon"},
    )


def test_sfx_and_alerts_is_just_sfx_now():
    from sprigcast.sources.app_audio import TYPES

    assert TYPES["sfx"].name == "SFX"
    col = SceneCollection.from_json(_collection().model_dump_json())
    assert col.source("sfx").name == "SFX"
    assert col.source("mine").name == "SFX & alerts"  # not the SFX type: a name you chose stays


def test_pages_reload_when_their_bloom_goes_on_air():
    from sprigcast.render.engine import Engine

    class Page:
        def __init__(self, settings):
            self.settings, self.reloads = settings, 0

        def reload(self):
            self.reloads += 1

    old = _collection()
    new = old.model_copy(deep=True)
    new.set_active("game")
    engine = Engine.__new__(Engine)
    engine._sources = {"alerts": Page({"reload_on_air": True}), "chat": Page({"reload_on_air": True})}
    went = engine._went_on_air(old, new)
    assert went == {"alerts"}  # chat was on air already
    engine._reload_on_air(went)
    assert engine._sources["alerts"].reloads == 1 and engine._sources["chat"].reloads == 0
    assert engine._went_on_air(new, new) == set()


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def app():
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_web_page_card_keeps_every_setting(app):
    from sprigcast.app.web_page_dialog import WebPageDialog

    reloads = []
    d = WebPageDialog("Alerts", {"url": "https://example.com/alerts", "width": 1920, "height": 1080, "fps": 60,
                                 "keep_running": True}, on_reload=lambda: reloads.append(1))
    v = d.values()
    assert v["url"] == "https://example.com/alerts" and v["file"] == ""
    assert (v["width"], v["height"]) == (1920, 1080)
    assert v["custom_fps"] and v["fps"] == 60  # an older page at 60 stays custom
    assert v["keep_running"] and v["audio_to_mixer"] and not v["reload_on_air"]
    d.custom_fps.setChecked(False)
    d.switches["sleep"].setChecked(True)
    d.switches["reload_on_air"].setChecked(True)
    d.file_tab.setChecked(True)
    d.file.setText("C:/overlays/alerts.html")
    v = d.values()
    assert not v["custom_fps"] and v["fps"] == STANDARD_FPS
    assert not v["keep_running"] and v["reload_on_air"]
    assert v["file"] == "C:/overlays/alerts.html"
    d.reload_button.click()
    assert reloads == [1]


def test_new_web_page_starts_light(app):
    from sprigcast.app.web_page_dialog import WebPageDialog
    from sprigcast.sources.browser import DEFAULTS

    d = WebPageDialog("New web page", DEFAULTS, primary="Add")
    v = d.values()
    assert v["audio_to_mixer"] and not v["keep_running"] and not v["custom_fps"]  # sound in SprigCast, sleeps
    assert d.url.text() == "" and not d.css.isVisibleTo(d)  # the usual CSS stays folded away
    assert not hasattr(d, "reload_button")
    assert not hasattr(d, "interact_button")  # nothing to click in until it exists


def test_interact_from_the_page_card_closes_with_it(app):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QWidget

    from sprigcast.app.web_page_dialog import WebPageDialog

    opened = []

    def interact(card):
        window = QWidget(card, Qt.WindowType.Window)  # like the interact window
        opened.append((card, window))
        window.show()
        return window

    d = WebPageDialog("Alerts", {"url": "https://example.com/alerts"}, on_interact=interact)
    d.interact_button.click()
    d.interact_button.click()  # the same window again: closed once
    assert len(opened) == 2 and opened[0][0] is d
    window = opened[-1][1]
    assert window.isVisible()
    d.done(0)
    assert not window.isVisible()  # the page goes back off screen with the card
