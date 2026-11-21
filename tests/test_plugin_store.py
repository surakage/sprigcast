"""The plugin store: optional plugins shipped with SprigCast, added and removed from Settings → Plugins."""

import pytest

from sprigcast import plugin_store
from sprigcast.core.source import create_source, known_kinds, source_class
from sprigcast.plugins import PluginReport, load_plugins

pytest.importorskip("PySide6.QtWidgets")


@pytest.fixture
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _slide_show():
    return next(p for p in plugin_store.catalog() if p.id == "slide_show")


def test_the_store_lists_its_plugins_without_running_them():
    plugin = _slide_show()
    assert plugin.name == "Slide show" and plugin.adds == "Source" and plugin.version >= 1
    assert "slideshow" not in known_kinds()  # shipped, but nothing runs until it's added


def test_add_loads_it_now_and_remove_takes_it_away(tmp_path, app):
    plugins = tmp_path / "plugins"
    report = PluginReport()
    assert plugin_store.installed_version(plugins, "slide_show") is None
    assert plugin_store.install(plugins, _slide_show(), report) is None
    assert plugin_store.installed_version(plugins, "slide_show") == _slide_show().version
    assert report.loaded == ["slide_show.py"] and "slideshow" in known_kinds()
    assert source_class("slideshow").label == "Slide show"
    plugin_store.remove(plugins, "slide_show", report)
    assert not (plugins / "slide_show.py").exists() and report.loaded == []
    assert "slideshow" not in known_kinds()  # gone from + → Plugins at once
    # Added again, it loads at the next start like any plugin in the folder.
    plugin_store.install(plugins, _slide_show())
    assert load_plugins(plugins).loaded == ["slide_show.py"]
    plugin_store.remove(plugins, "slide_show")


def test_an_older_copy_offers_an_update(tmp_path, app):
    import re

    from sprigcast.app.plugin_store_page import PluginStorePage

    plugins = tmp_path / "plugins"
    plugins.mkdir()
    old = re.sub(r'"version": \d+', '"version": 0', _slide_show().file.read_text(encoding="utf-8"), count=1)
    (plugins / "slide_show.py").write_text(old, encoding="utf-8")
    assert plugin_store.installed_version(plugins, "slide_show") == 0
    page = PluginStorePage(plugins, PluginReport())
    row = page.rows["slide_show"]
    assert row.update_button.isVisibleTo(page) and row.button.text() == "Remove"
    row.update_button.click()
    assert plugin_store.installed_version(plugins, "slide_show") == _slide_show().version
    assert not row.update_button.isVisibleTo(page)
    plugin_store.remove(plugins, "slide_show")


def test_headers_are_read_safely(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text("PLUGIN = {'id': __import__('os').getcwd()}\n", encoding="utf-8")
    assert plugin_store.read_header(bad) is None  # never runs anything to read it
    broken = tmp_path / "broken.py"
    broken.write_text("PLUGIN = {", encoding="utf-8")
    assert plugin_store.read_header(broken) is None
    assert plugin_store.catalog(tmp_path) == []


def test_slide_show_fits_each_picture_and_moves_on(tmp_path, app):
    from PySide6.QtGui import QColor, QImage

    plugin_store.install(tmp_path / "plugins", _slide_show())
    try:
        pictures = tmp_path / "pictures"
        pictures.mkdir()
        for name, (w, h), colour in (("a.png", (200, 100), "red"), ("b.png", (50, 100), "blue")):
            img = QImage(w, h, QImage.Format.Format_RGBA8888)
            img.fill(QColor(colour))
            img.save(str(pictures / name))
        show = create_source("slideshow", "s", {"folder": str(pictures), "seconds": 0.5, "width": 400, "height": 200})
        assert show.size == (400, 200) and show.status == "ok"
        import time

        deadline, first = time.monotonic() + 5, None
        while first is None and time.monotonic() < deadline:  # even the first is made in the background
            first = show.video_frame()
            time.sleep(0.01)
        assert first.shape == (200, 400, 4) and first[100, 200, 0] > 200  # red, fitted
        assert show.video_frame() is None  # nothing new until its time is up
        show._next_at = 0

        deadline, second = time.monotonic() + 5, None
        while second is None and time.monotonic() < deadline:  # the next one is made in the background
            second = show.video_frame()
            time.sleep(0.01)
        assert second[100, 200, 2] > 200 and second[100, 10, 3] == 0  # blue in the middle, see-through sides
    finally:
        plugin_store.remove(tmp_path / "plugins", "slide_show")


def test_store_page_adds_and_removes(tmp_path, app, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from sprigcast.app import message as msgbox

    from sprigcast.app.plugin_store_page import PluginStorePage
    from sprigcast.core.scene import SceneCollection, SourceConfig

    col = SceneCollection(canvases=[])
    page = PluginStorePage(tmp_path / "plugins", PluginReport(loaded=["mine.py"]), collection=col)
    changed = []
    page.changed.connect(lambda: changed.append(1))
    row = page.rows["slide_show"]
    assert row.button.text() == "Add" and not row.update_button.isVisibleTo(page)
    assert "mine.py" in page.mine.text()  # your own plugins are listed apart from the store
    row.button.click()
    assert row.button.text() == "Remove" and "+ → Plugins → Slide show" in row.note.text() and changed
    col.sources.append(SourceConfig(id="s", kind="slideshow", name="Holiday pics"))
    assert page.used_by("slide_show") == ["Holiday pics"]
    monkeypatch.setattr(msgbox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.No))
    row.button.click()
    assert row.button.text() == "Remove"  # kept: something still uses it
    monkeypatch.setattr(msgbox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
    row.button.click()
    assert row.button.text() == "Add" and "slideshow" not in known_kinds()


def test_added_store_plugins_get_the_new_version_at_start(tmp_path):
    folder = tmp_path / "plugins"
    folder.mkdir()
    snap = next(p for p in plugin_store.catalog() if p.id == "snap")
    old = plugin_store.installed_path(folder, "snap")
    old.write_text(snap.file.read_text(encoding="utf-8").replace(f'"version": {snap.version}', '"version": 1'),
                   encoding="utf-8")
    mine = folder / "my_own.py"
    mine.write_text('PLUGIN = {"id": "my_own", "version": 1}', encoding="utf-8")
    assert plugin_store.refresh_installed(folder) == ["snap"]
    assert plugin_store.installed_version(folder, "snap") == snap.version
    assert not plugin_store.installed_path(folder, "death_count").exists()  # never added: left alone
    assert plugin_store.refresh_installed(folder) == []


def test_retired_store_plugins_are_deleted_at_start(tmp_path):
    """Background remover and Live captions were taken out (2026-11-17, docs/REVISIT.md): added copies never load again."""
    folder = tmp_path / "plugins"
    folder.mkdir()
    for plugin_id in ("background_remover", "live_captions"):
        plugin_store.installed_path(folder, plugin_id).write_text(f'PLUGIN = {{"id": "{plugin_id}", "version": 2}}',
                                                                  encoding="utf-8")
    plugin_store.refresh_installed(folder)
    for plugin_id in ("background_remover", "live_captions"):
        assert not plugin_store.installed_path(folder, plugin_id).exists()
        assert plugin_id not in {p.id for p in plugin_store.catalog()}


def test_slide_show_leaves_out_sprigcasts_own_screenshots(tmp_path):
    """Soak 2026-11-15: playing the Screenshots folder put pictures of the canvas on the canvas: stacked copies."""
    from sprigcast.store.slide_show import pictures_in

    for name in ("screenshot horizontal 2026-11-15 09-17-02.png", "screenshot vertical 2026-11-15 09-17-02 (2).png",
                 "snap 2026-11-15 09-18-00.png", "holiday.png", "screenshot of my game.png"):
        (tmp_path / name).write_bytes(b"x")
    assert [p.name for p in pictures_in(str(tmp_path))] == ["holiday.png", "screenshot of my game.png"]


def test_an_update_says_whats_new():
    """PC test 2026-11-15: Death count went from v4 to v5 at start without a word."""
    for plugin in plugin_store.catalog():
        assert plugin.version == 1 or plugin.new, f"{plugin.id} v{plugin.version} needs a 'new' line"
    title, new = plugin_store.update_note(["death_count"])
    assert title == "Death count updated" and new
    assert plugin_store.update_note(["snap", "death_count", "hide_it"]) == ("3 plugins updated",
                                                                          "Death count, Hide it and Snap")
    assert plugin_store.update_note([]) is None and plugin_store.update_note(["not_a_plugin"]) is None


def test_start_up_updates_are_kept_for_the_note(tmp_path):
    folder = tmp_path / "plugins"
    folder.mkdir()
    dc = next(p for p in plugin_store.catalog() if p.id == "death_count")
    plugin_store.installed_path(folder, "death_count").write_text(
        dc.file.read_text(encoding="utf-8").replace(f'"version": {dc.version}', '"version": 4'), encoding="utf-8")
    plugin_store.refresh_installed(folder)
    assert plugin_store.updated_at_start == ["death_count"]
    plugin_store.updated_at_start.clear()


def test_an_update_pressed_on_air_waits_until_you_stop(tmp_path, app):
    from PySide6.QtCore import QSettings

    from sprigcast.app.plugin_store_page import HOLD_ON_AIR, PluginStorePage

    plugins = tmp_path / "plugins"
    plugins.mkdir()
    snap = next(p for p in plugin_store.catalog() if p.id == "barrel_roll")
    old = snap.file.read_text(encoding="utf-8").replace(f'"version": {snap.version}', '"version": 1')
    plugin_store.installed_path(plugins, "barrel_roll").write_text(old, encoding="utf-8")
    QSettings().setValue(HOLD_ON_AIR, True)
    try:
        page = PluginStorePage(plugins, PluginReport(), on_air=True)
        assert page.hold.isChecked()
        row = page.rows["barrel_roll"]
        assert not row.update_button.isHidden()
        row.update_button.click()
        assert plugin_store.waiting == {"barrel_roll": snap} and plugin_store.installed_version(plugins, "barrel_roll") == 1
        assert row.update_button.isHidden() and "once you stop" in row.note.text()
        plugin_store.waiting.clear()

        page.hold.setChecked(False)  # switched off: it updates at once, even on air
        page = PluginStorePage(plugins, PluginReport(), on_air=True)
        page.rows["barrel_roll"].update_button.click()
        assert plugin_store.installed_version(plugins, "barrel_roll") == snap.version and not plugin_store.waiting
        assert snap.new in page.rows["barrel_roll"].note.text()
    finally:
        plugin_store.waiting.clear()
        QSettings().remove(HOLD_ON_AIR)
        plugin_store.remove(plugins, "barrel_roll")


def test_the_window_says_so_and_puts_held_updates_in_once_off_air(tmp_path, app, monkeypatch):
    from types import SimpleNamespace

    from sprigcast.app.main_window import MainWindow

    plugins = tmp_path / "plugins"
    plugins.mkdir()
    snap = next(p for p in plugin_store.catalog() if p.id == "barrel_roll")
    plugin_store.installed_path(plugins, "barrel_roll").write_text(
        snap.file.read_text(encoding="utf-8").replace(f'"version": {snap.version}', '"version": 1'), encoding="utf-8")
    said, later = [], []
    monkeypatch.setattr("sprigcast.app.main_window.QTimer.singleShot", lambda ms, fn: later.append(ms))
    win = SimpleNamespace(streaming=object(), recording=None, plugins_dir=plugins, plugin_report=PluginReport(),
                          _fill_more_menu=lambda: None, engine=SimpleNamespace(send=lambda _m: None),
                          collection=SimpleNamespace(snapshot=lambda: None),
                          _saved_note=lambda title, **k: said.append((title, k["detail"])), _open_settings=None)
    win._plugin_updates = lambda: MainWindow._plugin_updates(win)
    win._send_whole = lambda: MainWindow._send_whole(win)
    plugin_store.updated_at_start[:] = ["death_count"]
    plugin_store.waiting["barrel_roll"] = snap
    try:
        MainWindow._plugin_updates(win)
        assert said == [] and later == [5000]  # live: waits
        win.streaming = None
        MainWindow._plugin_updates(win)
        assert said == [("2 plugins updated", "Barrel Roll and Death count")]
        assert plugin_store.installed_version(plugins, "barrel_roll") == snap.version and not plugin_store.waiting
    finally:
        plugin_store.waiting.clear()
        plugin_store.updated_at_start.clear()
        plugin_store.remove(plugins, "barrel_roll")


def test_headers_are_read_from_the_plugin_lines_and_kept_until_the_file_changes(tmp_path, monkeypatch):
    import ast
    import os

    path = tmp_path / "big.py"
    body = "\n".join(f"def f{i}(x):\n    return x + {i}\n" for i in range(3000))
    path.write_text('"""A big plugin."""\nPLUGIN = {"id": "big", "name": "Big",\n          "version": 2}\n' + body)
    parsed = []
    real = ast.parse
    monkeypatch.setattr(ast, "parse", lambda text, *a, **k: parsed.append(len(text)) or real(text, *a, **k))
    assert plugin_store.read_header(path) == {"id": "big", "name": "Big", "version": 2}
    assert parsed and max(parsed) < 200  # only the PLUGIN lines, not the 3000 functions
    parsed.clear()
    assert plugin_store.read_header(path)["version"] == 2 and parsed == []  # kept
    path.write_text('PLUGIN = {"id": "big", "version": 3}\n' + body)
    os.utime(path, ns=(1, 10**18))  # a new modified time, whatever the clock's resolution
    assert plugin_store.read_header(path)["version"] == 3
    for plugin in plugin_store.catalog():  # every store plugin still reads
        assert plugin.id and plugin.version >= 1


def test_a_retired_plugin_file_left_by_an_older_install_isnt_listed(tmp_path):
    """The PC test after #454: an update left store/auto_clip.py in the install, and the store showed it."""
    (tmp_path / "auto_clip.py").write_text('PLUGIN = {"id": "auto_clip", "name": "Auto clip", "version": 1}\n',
                                          encoding="utf-8")
    (tmp_path / "kept.py").write_text('PLUGIN = {"id": "kept", "name": "Kept", "version": 1}\n', encoding="utf-8")
    assert [p.id for p in plugin_store.catalog(tmp_path)] == ["kept"]
    assert {"auto_clip", "background_remover", "live_captions"} <= set(plugin_store.RETIRED)
