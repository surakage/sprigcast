"""Copy one filter or all of a leaf's filters and paste them onto another leaf (user 2026-11-26)."""

import os

import pytest

pytest.importorskip("PySide6.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from sprigcast.core.scene import Filter, SourceConfig  # noqa: E402


@pytest.fixture
def app():
    from PySide6.QtWidgets import QApplication

    from sprigcast.app import filters_dialog

    filters_dialog.COPIED_FILTERS.clear()
    yield QApplication.instance() or QApplication([])
    filters_dialog.COPIED_FILTERS.clear()


def _key(window, keys):
    from PySide6.QtGui import QKeySequence, QShortcut

    shortcut = next(s for s in window.findChildren(QShortcut) if s.key() == QKeySequence(keys))
    shortcut.activated.emit()


def test_one_filter_and_all_filters_paste_onto_another_leaf(app):
    from sprigcast.app.filters_dialog import FiltersDialog

    cam = SourceConfig(id="cam", kind="camera", name="Cam", filters=[
        Filter(id="a", kind="chroma_key", name="Greenscreen", settings={"similarity": 0.3}),
        Filter(id="b", kind="color_correction", name="Tone", settings={"gamma": 0.2})])
    text = SourceConfig(id="t", kind="text", name="Title", filters=[
        Filter(id="c", kind="color_correction", name="Tone", settings={"gamma": -0.5})])
    first = FiltersDialog(cam, lambda save=True: None)
    first.list.setCurrentRow(0)
    _key(first, "Ctrl+C")  # the picked one
    second = FiltersDialog(text, lambda save=True: None)
    assert second.paste() == 1
    assert [f.kind for f in text.filters] == ["color_correction", "chroma_key"]
    chroma = text.filters[1]
    assert chroma.settings == {"similarity": 0.3} and chroma.id != "a"
    chroma.settings["similarity"] = 0.9
    assert cam.filters[0].settings == {"similarity": 0.3}  # a copy, not the same filter

    _key(first, "Ctrl+Shift+C")  # all of them
    third = SourceConfig(id="t2", kind="text", name="Credits", filters=[
        Filter(id="d", kind="color_correction", name="Tone", settings={"gamma": -0.5})])
    window = FiltersDialog(third, lambda save=True: None)
    _key(window, "Ctrl+V")
    # An effect it already had takes the copied settings in its place (one of each effect per leaf)
    assert [f.kind for f in third.filters] == ["color_correction", "chroma_key"]
    assert third.filters[0].id == "d" and third.filters[0].settings == {"gamma": 0.2}


def test_sound_filters_only_paste_onto_sound_and_add_on_with_their_own_names(app):
    from sprigcast.app.filters_dialog import FiltersDialog, copy_filters, paste_filters

    copy_filters([Filter(id="g", kind="gain", name="Gain", settings={"db": 3.0}),
                  Filter(id="k", kind="chroma_key", name="Greenscreen")])
    mic = SourceConfig(id="mic", kind="audio_input", name="Mic", filters=[Filter(id="h", kind="gain", name="Gain")])
    window = FiltersDialog(mic, lambda save=True: None, media="audio")
    assert window.paste() == 1  # the picture effect doesn't go on a mic
    assert [(f.kind, f.name) for f in window.source.filters] == [("gain", "Gain"), ("gain", "Gain 2")]
    assert len(mic.filters) == 1  # sound filters preview first: on the mic once applied
    window.apply()
    assert [f.name for f in mic.filters] == ["Gain", "Gain 2"]
    assert paste_filters([], {"video"}) == 1
    copy_filters([Filter(id="p", kind="some_plugin_not_loaded", name="Odd")])
    assert paste_filters([], {"video", "audio"}) == 0


def test_the_filter_list_menu_offers_copy_copy_all_and_paste(app, monkeypatch):
    from sprigcast.app.filters_dialog import FiltersDialog, copy_filters

    cam = SourceConfig(id="cam", kind="camera", name="Cam", filters=[Filter(id="a", kind="chroma_key", name="Greenscreen")])
    window = FiltersDialog(cam, lambda save=True: None)
    spot = window.list.visualItemRect(window.list.item(0)).center()
    assert [a.text() for a in window.list_menu(spot).actions()] == ["Copy", "Copy all"]
    copy_filters(cam.filters * 2)
    assert [a.text() for a in window.list_menu(spot).actions()] == ["Copy", "Copy all", "Paste 2 filters"]


def test_the_leaves_menu_copies_a_leafs_filters_onto_another(app, tmp_path, monkeypatch):
    import sys

    sys.path.insert(0, os.path.dirname(__file__))
    import test_copy_paste_leaves as leaves

    col, w = leaves._window(tmp_path, monkeypatch)
    try:
        leaves._select(w, "p1", "text-1")
        texts = [a.text() for a in w._source_menu(True).actions()]
        assert "Copy filters" in texts and "Paste filters" not in texts
        w.item_actions["copy_filters"].trigger()
        leaves._select(w, "p1", "cam")
        texts = [a.text() for a in w._source_menu(True).actions()]
        assert "Copy filters" in texts and "Paste filters" in texts
        w.item_actions["paste_filters"].trigger()
        assert [f.kind for f in w.collection.source("cam").filters] == ["gain", "color_correction"]
        w.undo()
        assert [f.kind for f in w.collection.source("cam").filters] == ["gain"]
    finally:
        w.close()


def test_an_empty_filter_list_still_offers_paste(app):
    from PySide6.QtCore import QPoint, Qt

    from sprigcast.app.filters_dialog import FiltersDialog, copy_filters

    copy_filters([Filter(id="a", kind="chroma_key", name="Greenscreen")])
    window = FiltersDialog(SourceConfig(id="t", kind="text", name="Title"), lambda save=True: None)
    assert window.empty.contextMenuPolicy() == Qt.ContextMenuPolicy.CustomContextMenu  # "No filters yet"
    assert [a.text() for a in window.list_menu(QPoint(5, 5)).actions()] == ["Paste"]
