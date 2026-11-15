import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _pick(d, mode, process=""):
    box = d.capture
    i = next(i for i in range(box.count()) if box.itemData(i) and box.itemData(i)[0] == mode
             and (not process or box.itemData(i)[2] == process))
    box.setCurrentIndex(i)


def test_game_dialog_one_dropdown(app):
    from sprigcast.app.game_dialog import AUTO, HOTKEY, GameCaptureDialog
    from sprigcast.sources.win32_enum import WindowInfo

    windows = [WindowInfo(hwnd=1, title="Zed", process="zed.exe"),
               WindowInfo(hwnd=2, title="Elden Ring", process="eldenring.exe")]
    d = GameCaptureDialog({"mode": "any", "cursor": False, "fps_limit": 30}, windows, True)
    names = [d.capture.itemText(i) for i in range(d.capture.count()) if d.capture.itemData(i)]
    assert names[0] == AUTO and names[-1] == HOTKEY and names[1].startswith("Elden")  # A to Z between
    assert not hasattr(d, "more") and d.mode() == "any" and not d.hotkey_note.isVisibleTo(d)
    changes, sound = d.values()
    assert sound and changes == {"mode": "any", "cursor": False, "see_through": False, "fps_limit": 0,
                                 "border": False, "title": "", "process": ""}
    _pick(d, "window", "eldenring.exe")
    d.sound.setChecked(False)
    changes, sound = d.values()
    assert not sound and (changes["mode"], changes["title"], changes["process"]) == \
        ("window", "Elden Ring", "eldenring.exe")
    _pick(d, "hotkey")
    assert d.hotkey_note.isVisibleTo(d) and d.values()[0]["process"] == ""  # empty until its key is pressed


def test_game_dialog_keeps_closed_window_and_relists(app):
    from sprigcast.app.game_dialog import GameCaptureDialog
    from sprigcast.sources.win32_enum import WindowInfo

    later = [WindowInfo(hwnd=3, title="Hades", process="hades.exe")]
    d = GameCaptureDialog({"mode": "window", "title": "Elden Ring", "process": "eldenring.exe"}, [], True,
                          list_windows=lambda: later)
    assert d.capture.currentData() == ("window", "Elden Ring", "eldenring.exe")  # not open now, still kept
    d._reload()  # what opening the drop-down does
    assert d.capture.currentData() == ("window", "Elden Ring", "eldenring.exe")
    assert any(d.capture.itemData(i) == ("window", "Hades", "hades.exe") for i in range(d.capture.count()))


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
