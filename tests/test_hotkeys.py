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
    assert ids == ["toggle_stream", "toggle_record", "studio_go", "save_replay", "scene:a", "scene:b"]


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
