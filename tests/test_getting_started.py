"""Get started: answers -> garden, path, places, canvases and the starter blooms."""

import pytest

from sprigcast.app import getting_started as gs
from sprigcast.core.scene import Canvas, Output, SceneCollection


def _collection():
    return SceneCollection(canvases=[Canvas(id="horizontal", width=1920, height=1080, fps=60),
                                     Canvas(id="vertical", width=1080, height=1920, fps=60, enabled=False)])


def test_path_names_say_where_the_show_goes():
    assert gs.plan_path_name(gs.Answers(live=False, record=True)) == "Recording"
    assert gs.plan_path_name(gs.Answers(live=True)) == "Live"
    assert gs.plan_path_name(gs.Answers(places=["twitch", "tiktok"])) == "Twitch + TikTok"
    assert gs.plan_path_name(gs.Answers(places=["twitch", "youtube", "kick", "x"])) == "Multistream"
    assert gs.free_name("Gaming", ["gaming", "Gaming 2"]) == "Gaming 3"
    assert gs.free_name("Gaming", ["Music"]) == "Gaming"


def test_blooms_fit_the_show():
    blooms, names = gs.plan_blooms(gs.Answers(content="podcasts"))
    assert "Gaming" not in blooms and names == {"Just Chatting": "Podcast"}
    blooms, names = gs.plan_blooms(gs.Answers(content="tutorials"))
    assert "Gaming" in blooms and names == {"Gaming": "Screen"}
    assert gs.plan_blooms(gs.Answers(content="gaming")) == (list(gs.ALL_BLOOMS), {})


def test_places_use_their_service_preset_on_a_canvas_that_is_on():
    c = _collection()
    c.outputs = [Output(id="out-1", type="recording", canvas="horizontal")]
    both = gs.Answers(places=["twitch", "tiktok", "x"], shape="both")
    outs = gs.apply_path(both, c, taken={"out-2"})
    assert [(o.id, o.service, o.canvas) for o in outs] == [
        ("out-3", "twitch", "horizontal"), ("out-4", "tiktok", "vertical"), ("out-5", "x", "horizontal")]
    assert outs[1].size == (720, 1280) and outs[1].fps == 30 and all(o.enabled for o in outs)
    assert outs[0].key_ref == "sprigcast/out-3"
    assert [o.id for o in c.outputs] == ["out-1", "out-3", "out-4", "out-5"]  # the recording output stays
    assert c.recording.canvases == ["horizontal", "vertical"]
    # Landscape only: TikTok goes on the horizontal canvas instead of one that's off
    wide = gs.plan_outputs(gs.Answers(places=["tiktok"], shape="horizontal"), _collection(), set())
    assert wide[0].canvas == "horizontal"
    # Record only: no places
    c2 = _collection()
    assert gs.apply_path(gs.Answers(live=False, record=True, places=["twitch"], shape="vertical"), c2, set()) == []
    assert c2.recording.canvases == ["vertical"]


@pytest.fixture
def window(tmp_path, monkeypatch):
    pytest.importorskip("PySide6.QtWidgets")
    import keyring
    from PySide6.QtCore import QCoreApplication, QSettings
    from PySide6.QtWidgets import QApplication

    from sprigcast.app.main import load_collection
    from sprigcast.app.main_window import MainWindow

    QApplication.instance() or QApplication([])
    monkeypatch.setattr(keyring, "get_password", lambda svc, ref: None)
    QCoreApplication.setOrganizationName("SprigCastTests")
    QCoreApplication.setApplicationName("GetStartedTest")
    QSettings().clear()
    w = MainWindow(load_collection(tmp_path / "scenes.json"), tmp_path / "scenes.json")
    yield w
    w.close()
    QSettings().clear()


def test_dialog_needs_a_show_a_shape_and_live_or_record():
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    d = gs.GettingStartedDialog(twitch_account=lambda: "sloth")
    assert not d.go.isEnabled() and d.places_box.isHidden()
    d.content["music"].click()
    d.shapes["both"].click()
    assert not d.go.isEnabled()
    d.live.click()
    assert d.go.isEnabled() and not d.places_box.isHidden()
    d.places["twitch"].click()
    d.places["kick"].click()
    assert d.places["twitch"].note == "sloth" and d.places["kick"].note == "Key next"
    a = d.answers()
    assert (a.content, a.live, a.record, a.shape, a.places) == ("music", True, False, "both", ["twitch", "kick"])
    d.live.click()  # live off: places don't count
    d.record.click()
    assert d.answers().places == [] and d.go.isEnabled()


def test_window_sets_up_garden_path_and_starter(window, monkeypatch):
    from PySide6.QtWidgets import QDialog

    from sprigcast.app.getting_started import GettingStartedDialog

    w = window
    answers = gs.Answers(content="gaming", live=True, record=True, shape="both", places=["twitch", "tiktok"])
    monkeypatch.setattr(GettingStartedDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    monkeypatch.setattr(GettingStartedDialog, "answers", lambda self: answers)
    settings = []
    monkeypatch.setattr(w, "_open_settings", lambda tab="General", output_id=None: settings.append((tab, output_id)))
    assert w.open_getting_started() is answers
    shelf = w.gardens.shelf
    assert shelf.garden().name == "Gaming" and shelf.path().name == "Twitch + TikTok"
    assert [g.name for g in shelf.gardens()] == ["Gaming"]  # a fresh install's garden is renamed, not doubled
    assert [(o.service, o.canvas) for o in w.collection.outputs if o.type == "stream"] == [
        ("twitch", "horizontal"), ("tiktok", "vertical")]
    assert all(c.enabled for c in w.collection.canvases)
    shows = {s.name for s in w.collection.scenes if s.kind == "show"}
    assert shows == set(gs.ALL_BLOOMS) | {f"{n} (vertical)" for n in gs.ALL_BLOOMS}  # the example ones replaced
    assert {c.name for c in w.collection.sources if c.kind == "app_audio"} >= {"Games", "Music"}
    assert settings and settings[0][0] == "Stream"  # keys still to paste: straight to the first place

    # Again later (not fresh any more, places set): a new garden and a new path, the old ones kept
    from sprigcast.core.scene import Scene

    w.collection.scenes.append(Scene(id="mine", name="Mine", canvas="horizontal"))
    answers = gs.Answers(content="podcasts", live=False, record=True, shape="vertical")
    assert w.open_getting_started() is answers
    assert [g.name for g in shelf.gardens()] == ["Gaming", "Podcasts"] and shelf.garden().name == "Podcasts"
    assert sorted(p.name for p in shelf.paths()) == ["Recording", "Twitch + TikTok"]
    assert not any(o.type == "stream" for o in w.collection.outputs)
    assert [c.id for c in w.collection.canvases if c.enabled] == ["vertical"]
    shows = {s.name for s in w.collection.scenes if s.kind == "show"}
    assert {"Podcast", "Podcast (vertical)", "Starting soon (vertical)"} <= shows and "Gaming" not in shows


def test_gear_step_lists_devices_once_and_finds_the_capture_card():
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtWidgets import QApplication

    from sprigcast.sources.camera import CameraDevice

    QApplication.instance() or QApplication([])
    calls = []
    face, card = CameraDevice(0, "Elgato Facecam MK.2", "p0"), CameraDevice(1, "Game Capture HD60 X", "p1")

    def gear():
        calls.append(1)
        return gs.Gear(cameras=[face, card], mics=["Mic (Wave XLR)", "Game Capture HD60 X"],
                       speakers=["Game (Elgato Virtual Audio)", "Music (Elgato Virtual Audio)",
                                 "Voice Chat (Elgato Virtual Audio)", "Speakers (Realtek)"])

    d = gs.GettingStartedDialog(list_gear=gear)
    d.content["gaming"].click()
    d.shapes["horizontal"].click()
    d.record.click()
    d.go.click()
    assert d.pages.currentIndex() == 1 and d.go.text().startswith("Get started") and d.skip.text() == "Back"
    picks = d.gear()
    assert picks.camera is face and picks.capture is card and picks.mic == "default" and picks.sound == "wavelink"
    assert d.mic_pick.findData("Game Capture HD60 X") == -1  # the card's sound isn't offered as a mic
    d.skip.click()  # back, then forward again: devices aren't listed twice
    d.go.click()
    assert calls == [1]
    assert gs.capture_audio(card.name, ["Mic (Wave XLR)", "Game Capture HD60 X"]) == "Game Capture HD60 X"
    sounds, desktop = gs.plan_sound("wavelink", gear().speakers)
    assert desktop is None and sounds == {"games": {"device": "Game (Elgato Virtual Audio)"},
                                          "music": {"device": "Music (Elgato Virtual Audio)"},
                                          "voice": {"device": "Voice Chat (Elgato Virtual Audio)"}}
    assert gs.plan_sound("all", []) == (None, "default") and gs.plan_sound("none", []) == (None, None)


def test_a_capture_card_fills_the_gaming_bloom():
    from sprigcast.app.starter import StarterChoice, build_starter

    c = _collection()
    build_starter(c, StarterChoice(capture={"name": "HD60 X", "width": 1920, "height": 1080}, monitor=(1, 2560, 1440),
                                   capture_audio="Game Capture HD60 X", blooms=["Gaming"], vertical=False,
                                   names={"Gaming": "Console"}))
    assert [s.name for s in c.scenes if s.kind == "show"] == ["Console"]
    kinds = {x.kind for x in c.sources}
    assert "display_capture" not in kinds  # the card, not the screen
    assert any(x.name == "Capture card" for x in c.sources)
    assert any(x.kind == "audio_input" and x.settings["device"] == "Game Capture HD60 X" for x in c.sources)
