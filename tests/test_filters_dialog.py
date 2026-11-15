import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from sprigcast.core.scene import SourceConfig


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def test_add_edit_toggle_reorder_remove(app):
    from sprigcast.app.filters_dialog import FiltersDialog

    source = SourceConfig(id="cam", kind="camera", name="Cam")
    calls = []
    d = FiltersDialog(source, lambda save=True: calls.append(save))
    d._fill_add_menu()
    labels = [a.text() for a in d.add_menu.actions()]
    assert "Chroma Key" in labels and "Noise Gate" not in labels  # video source: video filters only

    d.add("chroma_key")
    d.add("color_correction")
    assert [f.kind for f in source.filters] == ["chroma_key", "color_correction"]
    d._fill_add_menu()
    chroma = next(a for a in d.add_menu.actions() if a.text() == "Chroma Key")
    assert not chroma.isEnabled()  # one of each effect

    d.form.set_values({"opacity": 40})
    assert source.filters[1].settings["opacity"] == 40

    d.list.item(1).setCheckState(Qt.CheckState.Unchecked)
    assert not source.filters[1].enabled

    d._move(-1)
    assert [f.kind for f in source.filters] == ["color_correction", "chroma_key"]
    d._reset()
    assert source.filters[0].settings["opacity"] == 100
    d._remove()
    assert [f.kind for f in source.filters] == ["chroma_key"]
    assert calls and not any(calls)  # live updates don't save
    d.reject()
    assert calls[-1] is True  # closing saves


def test_audio_sources_get_audio_filters(app):
    from sprigcast.app.filters_dialog import FiltersDialog

    source = SourceConfig(id="mic", kind="audio_input", name="Mic")
    d = FiltersDialog(source, lambda save=True: None)
    d._fill_add_menu()
    labels = [a.text() for a in d.add_menu.actions()]
    assert {"Noise Gate", "Compressor", "Gain", "Limiter"} <= set(labels) and "Chroma Key" not in labels
    d.add("noise_gate")
    d.form.set_values({"close_db": -40.0})
    assert source.filters[0].settings["close_db"] == -40.0


def test_look_and_shape_filters(app, tmp_path):
    from sprigcast.app.filters_dialog import FiltersDialog
    from sprigcast.core import looks

    source = SourceConfig(id="cam", kind="camera", name="Cam")
    d = FiltersDialog(source, lambda save=True: None)
    d._fill_add_menu()
    labels = [a.text() for a in d.add_menu.actions()]
    assert "Color look (LUT)" in labels and "Shape" in labels

    d.add("lut")
    form = d.form
    assert not form.form.isRowVisible(form.rows["file"])  # a built-in look needs no file
    form.set_values({"look": "file"})
    assert form.form.isRowVisible(form.rows["file"])
    bad = tmp_path / "bad.cube"
    bad.write_text("LUT_1D_SIZE 4\n")
    form.set_values({"file": str(bad)})
    assert "1D" in form.rows["file"].findChildren(type(d.title))[-1].text()

    d.add("shape")
    assert not d.form.form.isRowVisible(d.form.rows["mask_file"])
    assert d.form.form.isRowVisible(d.form.rows["roundness"])
    d.form.set_values({"shape": "circle"})
    assert d.form.form.isRowVisible(d.form.rows["stretch"]) and not d.form.form.isRowVisible(d.form.rows["roundness"])

    d.add("color_correction")
    assert d.save_look_button.isVisibleTo(d)
    d.form.set_values({"saturation": -1.0})
    saved = d._save_look(str(tmp_path / "grey"))
    table = looks.read_cube(saved)
    r, g, b = looks.apply(table, __import__("numpy").array([0.8, 0.3, 0.1], "f4"))
    assert abs(r - g) < 0.01 and abs(g - b) < 0.01
    d.list.setCurrentRow(0)
    assert not d.save_look_button.isVisibleTo(d)


def test_sound_filters_stay_apart_from_effects_and_are_a_to_z(app):
    from sprigcast.app.filters_dialog import FiltersDialog
    from sprigcast.core.scene import Filter

    page = SourceConfig(id="web", kind="browser", name="Alerts", filters=[
        Filter(id="a", kind="chroma_key", name="Chroma Key"),
        Filter(id="b", kind="gain", name="Gain"),
        Filter(id="c", kind="color_correction", name="Color Correction"),
        Filter(id="d", kind="compressor", name="Compressor")])
    sound = FiltersDialog(page, lambda save=True: None, media="audio",
                          sounds=[("web", "Alerts"), ("mic", "Mic"), ("music", "Music")])
    assert [sound.list.item(i).text() for i in range(sound.list.count())] == ["Gain", "Compressor"]
    sound._fill_add_menu()
    labels = [a.text() for a in sound.add_menu.actions()]
    assert labels == sorted(labels, key=str.lower) and "Chroma Key" not in labels
    assert {"Ducking", "De-esser", "Equalizer", "Low Cut"} <= set(labels)
    sound.list.setCurrentRow(1)
    sound._move(-1)  # swaps with the sound filter above, the effects keep their places
    assert [f.id for f in page.filters] == ["a", "d", "c", "b"]
    sound._remove()
    assert [f.id for f in page.filters] == ["a", "c", "b"]
    effects = FiltersDialog(page, lambda save=True: None)
    assert [effects.list.item(i).text() for i in range(effects.list.count())] == ["Chroma Key", "Color Correction"]
    ducking = sound.add("ducking")
    from PySide6.QtWidgets import QComboBox

    combo = sound.form.rows["sound_id"]
    assert isinstance(combo, QComboBox)
    assert [combo.itemText(i) for i in range(combo.count())] == ["Pick a sound", "Mic", "Music"]  # never itself
    combo.setCurrentIndex(1)
    assert ducking.settings["sound_id"] == "mic"


def test_vst_filter_passes_sound_through_until_a_plug_in_loads(app, tmp_path):
    import numpy as np

    from sprigcast.app.filters_dialog import FiltersDialog
    from sprigcast.core.filters import AudioChain

    source = SourceConfig(id="mic", kind="audio_input", name="Mic")
    d = FiltersDialog(source, lambda save=True: None)
    flt = d.add("vst")
    assert d.vst_button.isVisibleTo(d) and not d.form.rows["state"].isVisibleTo(d.form)
    d._open_vst()  # nothing picked yet: says so, no crash
    assert "choose the plug-in" in d.title.text()
    block = np.full((1024, 2), 0.25, dtype=np.float32)
    chain = AudioChain(source.filters)
    assert np.array_equal(chain.process(block), block)
    flt.settings = {**flt.settings, "plugin_file": str(tmp_path / "Missing.vst3")}
    assert chain.update(source.filters)
    assert np.array_equal(chain.process(block), block)  # won't load: the sound still plays
    assert chain.processors[0].problem
