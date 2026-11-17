import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from sprigcast.app.hotkeys import Hotkeys, actions, to_pynput  # noqa: E402
from sprigcast.core.scene import Canvas, Scene, SceneCollection  # noqa: E402


@pytest.mark.parametrize(
    "qt, expected",
    [
        ("Ctrl+Alt+1", "<ctrl>+<alt>+1"),
        ("Ctrl+Shift+F5", "<ctrl>+<shift>+<f5>"),
        ("Alt+PgUp", "<alt>+<page_up>"),
        ("Ctrl+R", "<ctrl>+r"),
        ("F13", "<f13>"),
        ("Ctrl+Alt", None),  # modifiers alone
        ("", None),
        ("Ctrl+Volume Up", None),
    ],
)
def test_qt_combos_become_pynput_combos(qt, expected):
    assert to_pynput(qt) == expected


def test_actions_list_every_scene_and_the_toggles():
    col = SceneCollection(
        canvases=[Canvas(id="horizontal", width=16, height=9), Canvas(id="vertical", width=9, height=16)],
        scenes=[Scene(id="a", name="Live", canvas="horizontal"), Scene(id="b", name="Live V", canvas="vertical")],
    )
    ids = [a for a, _ in actions(col)]
    assert ids == ["toggle_stream", "toggle_record", "pause_record", "chapter_mark", "save_replay", "toggle_clips",
                   "screenshot", "scene:a", "scene:b", "mute_all", "toggle_mode"]


def test_falls_back_to_window_shortcuts_without_a_global_hook(monkeypatch):
    import sys

    app = QApplication.instance() or QApplication([])  # noqa: F841
    monkeypatch.setitem(sys.modules, "pynput", None)  # import fails, as on a machine without pynput
    window = QWidget()
    hk = Hotkeys(window)
    fired = []
    hk.triggered.connect(fired.append)
    hk.apply({"toggle_record": "Ctrl+Alt+R", "scene:a": ""})
    assert hk.mode == "window" and len(hk._shortcuts) == 1
    hk._shortcuts[0].activated.emit()
    assert fired == ["toggle_record"]
    hk.stop()
    assert hk.mode == "off" and not hk._shortcuts


def test_global_hook_gets_pynput_combos(monkeypatch):
    import sys
    import types

    app = QApplication.instance() or QApplication([])  # noqa: F841
    started = {}

    class FakeGlobalHotKeys:
        def __init__(self, mapping):
            started["mapping"] = mapping

        def start(self):
            started["running"] = True

        def stop(self):
            started["running"] = False

    keyboard = types.SimpleNamespace(GlobalHotKeys=FakeGlobalHotKeys)
    monkeypatch.setitem(sys.modules, "pynput", types.SimpleNamespace(keyboard=keyboard))
    hk = Hotkeys(QWidget())
    fired = []
    hk.triggered.connect(fired.append)
    hk.apply({"toggle_stream": "Ctrl+Alt+L"})
    assert hk.mode == "global" and started["running"]
    started["mapping"]["<ctrl>+<alt>+l"]()
    assert fired == ["toggle_stream"]
    hk.stop()
    assert started["running"] is False


def test_new_hotkeys_mute_and_switch_mode(tmp_path, monkeypatch):
    import keyring
    from PySide6.QtCore import QCoreApplication, QSettings

    from sprigcast.app.hotkeys import MUTE_ALL, SOUND_PREFIX, TOGGLE_MODE
    from sprigcast.app.main_window import MainWindow
    from sprigcast.core.scene import SourceConfig

    QApplication.instance() or QApplication([])
    monkeypatch.setattr(keyring, "get_password", lambda svc, ref: None)
    QCoreApplication.setOrganizationName("SprigCastTests")
    QCoreApplication.setApplicationName("HotkeyActions")
    QSettings().clear()
    col = SceneCollection(
        canvases=[Canvas(id="horizontal", width=16, height=9)],
        scenes=[Scene(id="a", name="Live", canvas="horizontal")],
        sources=[SourceConfig(id="m", kind="audio_input", name="Mic", settings={"device": "default"}),
                 SourceConfig(id="d", kind="audio_output", name="Desk", settings={"device": "default", "muted": True})],
    )
    w = MainWindow(col, tmp_path / "scenes.json")
    try:
        muted = lambda: [bool(c.settings.get("muted")) for c in col.sources]  # noqa: E731
        w._on_hotkey(f"{SOUND_PREFIX}m:toggle")
        assert muted() == [True, True]
        w._on_hotkey(MUTE_ALL)  # everything muted: unmute all
        assert muted() == [False, False]
        w._on_hotkey(MUTE_ALL)
        assert muted() == [True, True]
        w._on_hotkey(f"{SOUND_PREFIX}d:unmute")
        assert muted() == [True, False]
        start = w.mode
        w._on_hotkey(TOGGLE_MODE)
        assert w.mode != start
        w._on_hotkey("toggle_studio")  # a key saved before Studio mode was removed does nothing
        w._on_hotkey("studio_go")
    finally:
        w.close()
        QSettings().clear()
