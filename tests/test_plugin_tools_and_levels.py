"""Plugin hooks for a Tools menu entry and sound levels (plugin_hooks.register_tool, register_level_watcher)."""

import pytest

from sprigcast import plugin_hooks

pytest.importorskip("PySide6.QtWidgets")


def test_level_watchers_get_levels_and_a_failing_one_is_dropped(monkeypatch):
    monkeypatch.setattr(plugin_hooks, "_LEVEL_WATCHERS", [])
    heard = []

    def watch(api, levels):
        heard.append(levels)

    plugin_hooks.register_level_watcher(watch)
    plugin_hooks.register_level_watcher(watch)  # loaded again: still one
    plugin_hooks.levels_heard(None, {"mic": (-10.0, -12.0)})
    assert heard == [{"mic": (-10.0, -12.0)}]

    def broken(api, levels):
        raise ValueError("oops")

    plugin_hooks.register_level_watcher(broken)
    plugin_hooks.levels_heard(None, {})
    assert broken not in plugin_hooks._LEVEL_WATCHERS and watch in plugin_hooks._LEVEL_WATCHERS


def test_tools_entries_are_listed_a_to_z_once(monkeypatch):
    monkeypatch.setattr(plugin_hooks, "_TOOLS", [])
    plugin_hooks.register_tool("Zebra…", lambda api: None)
    plugin_hooks.register_tool("Apple…", lambda api: None)
    plugin_hooks.register_tool("Zebra…", lambda api: None)
    assert [label for label, _ in plugin_hooks.tools()] == ["Apple…", "Zebra…"]


def test_the_tools_menu_shows_plugin_entries(tmp_path, monkeypatch):
    import keyring
    from PySide6.QtWidgets import QApplication

    from sprigcast.app.main import load_collection
    from sprigcast.app.main_window import MainWindow

    QApplication.instance() or QApplication([])
    monkeypatch.setattr(keyring, "get_password", lambda *a: None)
    monkeypatch.setattr(plugin_hooks, "_TOOLS", [])
    ran = []
    plugin_hooks.register_tool("My window…", lambda api: ran.append(api))
    w = MainWindow(load_collection(tmp_path / "scenes.json"), tmp_path / "scenes.json")
    try:
        w._tools_menu.aboutToShow.emit()
        w._tools_menu.aboutToShow.emit()  # filled again, not twice
        actions = [a for a in w._tools_menu.actions() if a.text() == "My window…"]
        assert len(actions) == 1
        actions[0].trigger()
        assert ran == [w.api]
    finally:
        w.close()
