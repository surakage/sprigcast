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


from test_store_effects import added, app  # noqa: E402,F401


@pytest.fixture
def _load_plugin_filters(added):  # noqa: F811
    """The store's filters, added for one test and removed after it: imported straight in, they stayed for every
    later test, and "without the plugin, Freeze does nothing" then failed in a full run (2026-11-21)."""
    def load():
        for pid in ("barrel_roll", "freeze", "hide_it"):
            added(pid)

    return load


def test_every_video_filter_has_its_one_word_name(_load_plugin_filters):
    _load_plugin_filters()
    for kind, label in NEW.items():
        assert filter_kind(kind).label == label
        assert " " not in label


def test_old_and_new_names_find_the_kind(_load_plugin_filters):
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

    def grab(source_id, back, raw, limit, filters=None):
        asked.append((source_id, raw, limit))
        back(np.full((90, 160, 4), 200 if raw else 50, np.uint8))

    from sprigcast.core.scene import Filter

    source = SourceConfig(id="cam", kind="camera", name="Cam", filters=[Filter(id="f", kind="blur", name="Mist")])
    d = FiltersDialog(source, lambda save=True: None, grab=grab)  # with a filter: the picture shows only then
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


def test_before_and_after_come_from_the_native_engine():
    """PC 2026-11-22: After showed nothing with Backdrop (asking for Before let go of After's view) and the preview
    was choppy. With the native engine both sides are small views it draws every frame, After with the window's
    unapplied filters."""
    _app()
    import numpy as np

    from sprigcast.app.filters_dialog import EngineGrab, nv12_to_rgba
    from sprigcast.core.filters import VideoEffects
    from sprigcast.core.scene import Canvas, SceneCollection, SourceConfig, item_key
    from sprigcast.render.engine import Frame

    grey = np.full(64 * 36 * 3 // 2, 128, np.uint8)
    grey[: 64 * 36] = 126  # limited-range mid grey
    rgba = nv12_to_rgba(grey.tobytes(), 64, 36)
    assert rgba.shape == (36, 64, 4) and abs(int(rgba[0, 0, 0]) - 128) <= 2 and rgba[0, 0, 3] == 255

    class FakeEngine:
        def __init__(self, native=True):
            self._native = object() if native else None
            self.views, self.taps, self.grabs, self.looks = {}, {}, [], {}

        def grab_source(self, sid, back, raw, limit, filters=None):
            self.grabs.append((sid, raw))
            back(None)

        def set_view(self, name, canvas_id=None, kind="bloom", ref=None):
            if canvas_id is None:
                self.views.pop(name, None)
            else:
                self.views[name] = (canvas_id, kind, ref)

        def set_view_effects(self, effects):
            self.looks = dict(effects)

        def add_frame_tap(self, key, cb, nv12_size=None):
            self.taps[key] = (cb, nv12_size)

        def remove_frame_tap(self, key, cb):
            self.taps.pop(key, None)

    col = SceneCollection(canvases=[Canvas(id="horizontal", name="Horizontal", width=1920, height=1080)],
                          sources=[SourceConfig(id="cam", kind="camera", name="Cam",
                                                filters=[Filter(id="b", kind="background_effect", name="Backdrop")])])
    engine = FakeEngine()
    grab = EngineGrab(engine, col)
    assert grab.every_ms < 100  # smooth
    got = {True: [], False: []}
    draft = [Filter(id="b", kind="background_effect", name="Backdrop", settings={"blur": 0.57})]
    for _ in range(3):  # the window asks for both, Before first, again and again: the views stay
        grab("cam", got[True].append, True, 480)
        grab("cam", got[False].append, False, 480, filters=draft)
        for name in ("filters-before", "filters-after"):
            cb, size = engine.taps[f"view:{name}"]
            assert size == (480, 270) and engine.views[name] == ("horizontal", "draft", ("cam", 480))
            cb(Frame(f"view:{name}", 480, 270, bytes(480 * 270 * 3 // 2), fmt="nv12"))
    assert engine.grabs == []  # nothing from Python's compositor
    assert got[True][-1].shape == got[False][-1].shape == (270, 480, 4)
    assert engine.looks[item_key("view:filters-before", "cam")] == VideoEffects()
    assert engine.looks[item_key("view:filters-after", "cam")].bg_mode  # the draft's Backdrop
    grab("cam", got[False].append, False, 480, filters=draft)
    grab("cam", got[False].append, False, 480, filters=draft)
    assert got[False][-1] is got[False][-2]  # no new frame: not converted again
    grab.stop()
    assert not engine.taps and not engine.views and engine.looks == {}
    python = FakeEngine(native=False)
    old = EngineGrab(python, col)
    old("cam", got[True].append, True, 480)
    old("cam", got[False].append, False, 480, filters=draft)
    assert python.grabs == [("cam", True), ("cam", False)] and old.every_ms == 200


def test_preview_first_then_apply_or_cancel():  # noqa: PLR0915
    """User 2026-11-21: changes show only in the mini screen until Apply; Cancel drops them."""
    app = _app()
    from PySide6.QtCore import Qt

    from sprigcast.app.filters_dialog import FiltersDialog
    from sprigcast.core.scene import SourceConfig

    drafts = []

    def grab(source_id, back, raw, limit, filters=None):
        if not raw:
            drafts.append([(f.kind, f.enabled, dict(f.settings)) for f in filters])
        back(None)

    calls = []
    source = SourceConfig(id="cam", kind="camera", name="Cam", filters=[Filter(id="g", kind="glow", name="Glow")])
    d = FiltersDialog(source, lambda save=True: calls.append(save), grab=grab)
    d.show()
    app.processEvents()
    assert not d.apply_button.isEnabled()
    d.add("blur")
    d.form.set_values({"size": 12})
    d.list.item(0).setCheckState(Qt.CheckState.Unchecked)
    assert [f.kind for f in source.filters] == ["glow"] and source.filters[0].enabled  # the stream is untouched
    assert calls == []
    assert drafts[-1][0] == ("glow", False, drafts[-1][0][2]) and drafts[-1][1][0] == "blur"  # the After shows it
    assert d.apply_button.isEnabled()
    d.reject()
    assert [f.kind for f in source.filters] == ["glow"] and source.filters[0].enabled  # Cancel dropped it all
    d = FiltersDialog(source, lambda save=True: calls.append(save), grab=grab)
    d.add("blur")
    d.apply()
    assert [f.kind for f in source.filters] == ["glow", "blur"] and calls[-1] is True
    assert source.filters[1] is not d.source.filters[1]  # the source got its own copies


def test_sound_filters_preview_first_too():
    """User 2026-11-21: sound filters work like picture filters: nothing changes until Apply."""
    _app()
    from sprigcast.app.filters_dialog import FiltersDialog
    from sprigcast.core.scene import SourceConfig

    source = SourceConfig(id="mic", kind="audio_input", name="Mic")
    d = FiltersDialog(source, lambda save=True: None, grab=lambda *a, **k: None)
    assert d.preview_first
    d.add("gain")
    assert source.filters == []
    d.apply()
    assert [f.kind for f in source.filters] == ["gain"]


def test_before_and_after_shows_only_with_a_filter(app):
    """User 2026-11-25: with nothing picked, the before and after picture isn't shown."""
    import numpy as np

    from sprigcast.app.filters_dialog import FiltersDialog
    from sprigcast.core.scene import Filter, SourceConfig

    def grab(source_id, back, raw, limit, filters=None):
        back(np.full((90, 160, 4), 120, np.uint8))

    d = FiltersDialog(SourceConfig(id="cam", kind="camera", name="Cam"), lambda save=True: None, grab=grab)
    d.show()
    app.processEvents()
    assert not d.preview.isVisible() and not d.preview.timer.isActive()
    d.add("blur")
    app.processEvents()
    assert d.preview.isVisible()
    d.reject()
    one = FiltersDialog(SourceConfig(id="cam", kind="camera", name="Cam", filters=[Filter(id="f", kind="blur", name="Mist")]),
                        lambda save=True: None, grab=grab)
    one.show()
    app.processEvents()
    assert one.preview.isVisible()
    one.reject()


def test_f2_renames_the_picked_filter(monkeypatch):
    _app()
    from PySide6.QtGui import QKeySequence, QShortcut

    from sprigcast.app import card
    from sprigcast.app.filters_dialog import FiltersDialog
    from sprigcast.core.scene import SourceConfig

    source = SourceConfig(id="cam", kind="camera", name="Cam")
    d = FiltersDialog(source, lambda save=True: None)
    flt = d.add("chroma_key")
    assert any(s.key() == QKeySequence("F2") for s in d.findChildren(QShortcut))
    monkeypatch.setattr(card, "ask_text", lambda *a, **k: ("Green wall", True))
    assert d.rename_selected()
    assert flt.name == "Green wall" and d.list.item(0).text() == "Green wall"
    monkeypatch.setattr(card, "ask_text", lambda *a, **k: ("", False))
    assert not d.rename_selected() and flt.name == "Green wall"
    d.close()
