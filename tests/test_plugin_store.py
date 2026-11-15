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
        first = show.video_frame()
        assert first.shape == (200, 400, 4) and first[100, 200, 0] > 200  # red, fitted
        assert show.video_frame() is None  # nothing new until its time is up
        show._next_at = 0
        import time

        deadline, second = time.monotonic() + 5, None
        while second is None and time.monotonic() < deadline:  # the next one is made in the background
            second = show.video_frame()
            time.sleep(0.01)
        assert second[100, 200, 2] > 200 and second[100, 10, 3] == 0  # blue in the middle, see-through sides
    finally:
        plugin_store.remove(tmp_path / "plugins", "slide_show")


def test_store_page_adds_and_removes(tmp_path, app, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

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
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.No))
    row.button.click()
    assert row.button.text() == "Remove"  # kept: something still uses it
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
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
