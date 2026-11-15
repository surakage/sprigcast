import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_game_dialog_modes_and_more(app):
    from sprigcast.app.game_dialog import GameCaptureDialog
    from sprigcast.sources.win32_enum import WindowInfo

    windows = [WindowInfo(hwnd=1, title="Elden Ring", process="eldenring.exe")]
    d = GameCaptureDialog({"mode": "any", "cursor": False}, windows, True)
    assert d.mode() == "any" and not d.window_box.isVisibleTo(d) and not d.more.isVisibleTo(d)
    changes, sound = d.values()
    assert sound and changes == {"mode": "any", "cursor": False, "see_through": False, "fps_limit": 0,
                                 "border": False, "title": "", "process": ""}
    next(b for b in d.mode_group.buttons() if b.property("mode") == "window").click()
    assert d.window_box.isVisibleTo(d)
    d.more_button.click()
    assert d.more.isVisibleTo(d)
    d.fps.setCurrentIndex(2)
    d.sound.setChecked(False)
    changes, sound = d.values()
    assert not sound and changes["fps_limit"] == 60 and (changes["title"], changes["process"]) == \
        ("Elden Ring", "eldenring.exe")
    next(b for b in d.mode_group.buttons() if b.property("mode") == "hotkey").click()
    assert d.hotkey_note.isVisibleTo(d) and d.values()[0]["process"] == ""  # empty until its key is pressed


def test_game_capture_source_options():
    from sprigcast.sources.capture import GameCaptureSource

    src = GameCaptureSource("g", {"mode": "any"})
    assert src.channel_order == "BGRX"  # solid by default
    src.update({"mode": "any", "see_through": True})
    assert src.channel_order == "BGRA"
    src.update({"mode": "hotkey"})
    assert src._target() is None  # nothing until the hotkey picks a window
    src.update({"fps_limit": 30})
    kept = sum(src._due() for _ in range(50))  # 50 frames at once: only the first is due
    assert kept == 1
    src.update({"fps_limit": 0})
    assert all(src._due() for _ in range(5))


def test_hotkey_lists_hotkey_game_captures():
    from sprigcast.app.hotkeys import GAME_PREFIX, actions
    from sprigcast.core.scene import Canvas, SceneCollection, SourceConfig

    col = SceneCollection(canvases=[Canvas(id="h", width=1920, height=1080)], scenes=[], sources=[
        SourceConfig(id="g1", kind="game_capture", name="Game", settings={"mode": "hotkey"}),
        SourceConfig(id="g2", kind="game_capture", name="Other", settings={"mode": "any"})])
    ids = [a for a, _ in actions(col)]
    assert f"{GAME_PREFIX}g1" in ids and f"{GAME_PREFIX}g2" not in ids
