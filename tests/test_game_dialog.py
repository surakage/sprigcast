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


def test_display_capture_dialog(app):
    from sprigcast.app.capture_dialog import DisplayCaptureDialog
    from sprigcast.sources.win32_enum import MonitorInfo

    monitors = [MonitorInfo(1, "a", 1920, 1080, True), MonitorInfo(2, "b", 2560, 1440, False)]
    d = DisplayCaptureDialog({"monitor": 2, "cursor": True}, monitors)
    assert d.values() == {"monitor": 2, "cursor": True, "sprigcast": False} and d.monitor().width == 2560
    d.cursor.setChecked(False)
    d.screen.setCurrentIndex(0)
    assert d.values() == {"monitor": 1, "cursor": False, "sprigcast": False}
    gone = DisplayCaptureDialog({"monitor": 3}, monitors)  # unplugged: kept, pointer on by default
    assert gone.values() == {"monitor": 3, "cursor": True, "sprigcast": False} and gone.monitor() is None


def test_window_capture_dialog(app):
    from sprigcast.app.capture_dialog import WindowCaptureDialog
    from sprigcast.sources.win32_enum import WindowInfo

    windows = [WindowInfo(1, "Zed", "zed.exe"), WindowInfo(2, "New tab - Firefox", "firefox.exe")]
    d = WindowCaptureDialog({"title": "Old tab - Firefox", "process": "firefox.exe", "cursor": True}, windows,
                            False, list_windows=lambda: windows)
    texts = [d.window.itemText(i) for i in range(d.window.count())]
    assert texts == sorted(texts, key=str.lower) and [d.match.itemText(i) for i in range(2)] == \
        ["Exact title", "Same app"]
    changes, sound = d.values()
    assert changes == {"title": "New tab - Firefox", "process": "firefox.exe", "match": "app", "cursor": True,
                       "sprigcast": False}
    assert not sound
    d.match.setCurrentIndex(d.match.findData("title"))
    d.sound.setChecked(True)
    d._reload()
    changes, sound = d.values()
    assert sound and changes["match"] == "title" and changes["process"] == "firefox.exe"

    closed = WindowCaptureDialog({"title": "Hades", "process": "hades.exe", "match": "title"}, windows, True)
    assert closed.values()[0]["title"] == "Hades" and closed.values()[0]["match"] == "title"


def test_capture_dialogs_show_sprigcast(app):
    from sprigcast.app.capture_dialog import DisplayCaptureDialog, WindowCaptureDialog
    from sprigcast.sources.win32_enum import MonitorInfo, WindowInfo

    d = DisplayCaptureDialog({"monitor": 1, "sprigcast": True}, [MonitorInfo(1, "a", 1920, 1080, True)])
    assert d.values()["sprigcast"] and d.show_own.accessibleName() == "Show SprigCast"
    d.show_own.setChecked(False)
    assert d.values()["sprigcast"] is False

    others = [WindowInfo(1, "Zed", "zed.exe")]
    own = others + [WindowInfo(9, "SprigCast", "SprigCast.exe")]
    w = WindowCaptureDialog({"title": "Zed", "process": "zed.exe"}, others, False, list_windows=lambda: others,
                            list_own=lambda: own)
    assert w.window.count() == 1 and not w.values()[0]["sprigcast"]
    w.show_own.setChecked(True)  # SprigCast's own windows join the list
    assert w.window.count() == 2 and w.values()[0]["sprigcast"]
    w.window.setCurrentIndex(w.window.findText("SprigCast", flags=__import__("PySide6.QtCore").QtCore.Qt.MatchFlag.MatchContains))
    assert w.values()[0]["process"] == "SprigCast.exe"
    w.show_own.setChecked(False)
    assert [w.window.itemData(i)[1] for i in range(w.window.count())] == ["zed.exe", "SprigCast.exe"]  # kept

    opened = WindowCaptureDialog({"title": "SprigCast", "process": "SprigCast.exe", "sprigcast": True}, others,
                                 False, list_windows=lambda: others, list_own=lambda: own)
    assert opened.window.count() == 2 and opened.values()[0]["process"] == "SprigCast.exe"


def test_window_list_names_titles_and_searches(app):
    from sprigcast.app.capture_dialog import WindowCaptureDialog
    from sprigcast.app.window_list import APP_ROLE, DETAIL_ROLE, ROW_ROLE, WindowListPopup
    from sprigcast.sources.win32_enum import WindowInfo

    windows = [WindowInfo(i, f"Tab {i} — Mozilla Firefox", "firefox.exe", app="Firefox") for i in range(1, 6)]
    windows += [WindowInfo(8, "Calculator", "ApplicationFrameHost.exe", app="Calculator"),
                WindowInfo(9, "#general - Discord", "Discord.exe", app="Discord")]
    d = WindowCaptureDialog({"title": "Tab 3 — Mozilla Firefox", "process": "firefox.exe"}, windows, False,
                            list_windows=lambda: windows)
    box = d.window
    assert box.itemText(box.currentIndex()) == "Firefox  ·  Tab 3"
    assert box.itemData(0, APP_ROLE) == "Calculator" and box.itemData(0, DETAIL_ROLE) == ""
    popup = WindowListPopup(box)
    assert popup.search is not None and popup.list.count() == 7
    popup.search.setText("tab 5")
    shown = [popup.list.item(r) for r in range(popup.list.count()) if not popup.list.item(r).isHidden()]
    assert [i.data(DETAIL_ROLE) for i in shown] == ["Tab 5"]
    popup._pick_current()
    assert d.values()[0]["title"] == "Tab 5 — Mozilla Firefox"
    assert shown[0].data(ROW_ROLE) == box.currentIndex()
    few = WindowListPopup(WindowCaptureDialog({}, windows[:2], False).window)
    assert few.search is None  # a short list needs no search
    assert not d.popups_row.isVisibleTo(d)  # not SprigCast's own window: no pop-ups switch


def test_own_window_offers_menus_and_popups(app):
    import os

    from sprigcast.app.capture_dialog import WindowCaptureDialog
    from sprigcast.sources.win32_enum import WindowInfo

    own = [WindowInfo(9, "SprigCast", "python.exe", app="SprigCast", pid=os.getpid())]
    d = WindowCaptureDialog({"title": "SprigCast", "process": "python.exe", "sprigcast": True}, own, False,
                            list_own=lambda: own)
    assert d.popups_row.isVisibleTo(d) and d.values()[0]["popups"] is True
    d.popups.setChecked(False)
    assert d.values()[0]["popups"] is False
    d.show_own.setChecked(False)
    assert not d.popups_row.isVisibleTo(d) and "popups" not in d.values()[0]


@pytest.mark.parametrize("which", ["window", "game"])
def test_a_long_window_title_does_not_cut_off_the_popup(app, which):
    """PC 2026-11-25: a long window title made Window capture's body 715 px in a 434 px card; its fields, switches
    and Save were cut off at the right. Game capture had the same."""
    from PySide6.QtWidgets import QApplication

    from sprigcast.app import theme
    from sprigcast.app.capture_dialog import WindowCaptureDialog
    from sprigcast.app.game_dialog import GameCaptureDialog
    from sprigcast.sources.win32_enum import WindowInfo

    QApplication.instance().setStyleSheet(theme.STYLESHEET)
    long = "sprigcast - capture_dialog.py - Visual Studio Code - a very long window title that goes on and on"
    windows = [WindowInfo(hwnd=1, title="Mozilla Firefox", process="firefox.exe"),
               WindowInfo(hwnd=2, title=long, process="Code.exe")]
    make = WindowCaptureDialog if which == "window" else GameCaptureDialog
    d = make({"title": long, "process": "Code.exe"}, windows, False)
    d.show()
    QApplication.processEvents()
    try:
        scroll = d._scroll
        assert scroll.widget().width() <= scroll.viewport().width()
        assert d.ok.geometry().right() < scroll.widget().width()
    finally:
        d.close()
