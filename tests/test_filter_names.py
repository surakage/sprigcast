"""One-word filter names (user 2026-11-21): old names still work, the Filters window shows the new ones with
icons, a grouped Add filter picker with search, and the live Before and after split."""

import os

import pytest

from sprigcast.core.filters import OLD_LABELS, filter_kind, kind_for_label, shown_name
from sprigcast.core.scene import Filter

NEW = {"barrel_roll": "Spin", "blackout": "Curtain", "blur": "Mist", "chroma_key": "Greenscreen",
       "color_correction": "Tone", "color_key": "Colorcut", "lut": "Mood", "luma_key": "Lightcut",
       "pixelate": "Mosaic", "scroll": "Drift", "shape": "Frame", "sharpen": "Crisp", "stroke": "Outline",
       "freeze": "Freeze", "glow": "Glow", "shadow": "Shadow"}


def _load_plugin_filters():
    import sprigcast.store.barrel_roll  # noqa: F401
    import sprigcast.store.freeze  # noqa: F401
    import sprigcast.store.hide_it  # noqa: F401


def test_every_video_filter_has_its_one_word_name():
    _load_plugin_filters()
    for kind, label in NEW.items():
        assert filter_kind(kind).label == label
        assert " " not in label


def test_old_and_new_names_find_the_kind():
    _load_plugin_filters()
    assert kind_for_label("Chroma Key") == kind_for_label("greenscreen") == "chroma_key"
    assert kind_for_label("Color look (LUT)") == "lut"
    assert kind_for_label("Barrel roll") == kind_for_label("Spin") == "barrel_roll"
    assert kind_for_label("Nonsense") is None
    assert set(OLD_LABELS) <= set(NEW)


def test_old_default_names_show_as_new_names_but_chosen_names_stay():
    assert shown_name("chroma_key", "Chroma Key") == "Greenscreen"
    assert shown_name("blur", "Blur 2") == "Mist 2"
    assert shown_name("blur", "Face blur") == "Face blur"
    assert shown_name("glow", "Glow") == "Glow"
    assert shown_name("unknown_kind", "Whatever") == "Whatever"


def test_control_api_finds_a_filter_by_old_or_new_name():
    from test_control_native import _ok
    from test_control_streambridge import _native

    api, host = _native()
    cam = host.collection.source("cam")
    cam.filters = [Filter(id="f1", kind="chroma_key", name="Chroma Key", enabled=False)]
    _ok(api, "filter.set", source="cam", filter="Greenscreen", on=True)
    assert cam.filters[0].enabled
    _ok(api, "filter.set", source="cam", filter="Chroma Key", on=False)  # Streamer.bot actions made before
    assert not cam.filters[0].enabled
    _ok(api, "filter.set", source="cam", filter="chroma_key", on=True)
    assert cam.filters[0].enabled
    _ok(api, "filter.add", source="cam", kind="blur")
    assert cam.filters[-1].name == "Mist"


# The window --------------------------------------------------------------------------------------------

def _app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_filter_icons_all_draw():
    _app()
    from PySide6.QtGui import QColor

    from sprigcast.app import icons
    from sprigcast.app.filters_dialog import KIND_ICONS

    for name in {*KIND_ICONS.values(), "close"}:
        assert name in icons.NAMES
        image = icons.pixmap(name, "#ffffff", 18, 2.0).toImage()
        lit = sum(1 for x in range(image.width()) for y in range(image.height())
                  if QColor(image.pixel(x, y)).alpha() > 0)
        assert lit > 40, name  # a real drawing, not the unknown-name dot


def test_add_picker_groups_searches_and_adds():
    _app()
    from sprigcast.app.filters_dialog import FiltersDialog
    from sprigcast.core.scene import SourceConfig

    source = SourceConfig(id="cam", kind="camera", name="Cam")
    d = FiltersDialog(source, lambda save=True: None)
    d._fill_add_menu()
    headings = [h.text() for h, _ in d.picker._groups]
    assert headings[:4] == ["Color", "Cut out", "Look", "Motion"]
    d.picker.search.setText("chroma")  # an old name finds it too
    shown = [label for label, tile in d.picker.tiles.items() if not tile.isHidden()]
    assert shown == ["Greenscreen"]
    d.picker._first()
    assert [f.kind for f in source.filters] == ["chroma_key"]
    assert source.filters[0].name == "Greenscreen"
    d._fill_add_menu()
    assert not d.picker.tiles["Greenscreen"].isEnabled()


def test_list_switch_turns_a_filter_off():
    _app()
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QStyleOptionViewItem

    from sprigcast.app.filters_dialog import FiltersDialog
    from sprigcast.core.scene import SourceConfig

    source = SourceConfig(id="cam", kind="camera", name="Cam")
    d = FiltersDialog(source, lambda save=True: None)
    d.add("glow")
    d.resize(900, 600)
    d.show()
    index = d.list.model().index(0, 0)
    option = QStyleOptionViewItem()
    option.rect = d.list.visualRect(index)
    delegate = d.list.itemDelegate()
    spot = QPointF(delegate._switch_rect(option.rect.adjusted(0, 2, 0, -2)).center())
    click = QMouseEvent(QEvent.Type.MouseButtonRelease, spot, spot, Qt.MouseButton.LeftButton,
                        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
    assert delegate.editorEvent(click, d.list.model(), option, index)
    assert not source.filters[0].enabled
    d.reject()


def test_before_and_after_asks_for_both_pictures():
    app = _app()
    import numpy as np

    from sprigcast.app.filters_dialog import FiltersDialog
    from sprigcast.core.scene import SourceConfig

    asked = []

    def grab(source_id, back, raw, limit):
        asked.append((source_id, raw, limit))
        back(np.full((90, 160, 4), 200 if raw else 50, np.uint8))

    source = SourceConfig(id="cam", kind="camera", name="Cam")
    d = FiltersDialog(source, lambda save=True: None, grab=grab)
    assert d.preview is not None
    d.show()
    app.processEvents()
    assert {(sid, raw) for sid, raw, _ in asked} == {("cam", True), ("cam", False)}
    assert all(limit <= 480 for _, _, limit in asked)
    assert d.preview.before is not None and d.preview.after is not None
    assert not d.preview.grab().isNull()  # paints without trouble
    d.reject()
    assert not d.preview.timer.isActive()  # stops asking once closed
    sound = FiltersDialog(SourceConfig(id="mic", kind="audio_input", name="Mic"), lambda save=True: None, grab=grab)
    assert sound.preview is None  # sound filters have no picture
