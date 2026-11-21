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
    labels = list(d.picker.tiles)
    assert "Greenscreen" in labels and "Noise Gate" not in labels  # video source: video filters only

    d.add("chroma_key")
    d.add("color_correction")
    assert [f.kind for f in source.filters] == ["chroma_key", "color_correction"]
    d._fill_add_menu()
    assert not d.picker.tiles["Greenscreen"].isEnabled()  # one of each effect

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
    labels = list(d.picker.tiles)
    assert {"Noise Gate", "Compressor", "Gain", "Limiter"} <= set(labels) and "Greenscreen" not in labels
    d.add("noise_gate")
    d.form.set_values({"close_db": -40.0})
    assert d.source.filters[0].settings["close_db"] == -40.0 and source.filters == []  # until Apply
    d.apply()
    assert source.filters[0].settings["close_db"] == -40.0


def test_look_and_shape_filters(app, tmp_path):
    from sprigcast.app.filters_dialog import FiltersDialog
    from sprigcast.core import looks

    source = SourceConfig(id="cam", kind="camera", name="Cam")
    d = FiltersDialog(source, lambda save=True: None)
    d._fill_add_menu()
    labels = list(d.picker.tiles)
    assert "Mood" in labels and "Frame" in labels

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
    labels = list(sound.picker.tiles)
    assert labels == sorted(labels, key=str.lower) and "Greenscreen" not in labels
    assert {"Ducking", "De-esser", "Equalizer", "Low Cut"} <= set(labels)
    sound.list.setCurrentRow(1)
    sound._move(-1)  # swaps with the sound filter above, the effects keep their places
    assert [f.id for f in sound.source.filters] == ["a", "d", "c", "b"]
    sound._remove()
    assert [f.id for f in sound.source.filters] == ["a", "c", "b"]
    assert [f.id for f in page.filters] == ["a", "b", "c", "d"]  # previewed: the page keeps its own until Apply
    effects = FiltersDialog(page, lambda save=True: None)
    # filters added before the one-word names keep their saved names, shown by today's
    assert [effects.list.item(i).text() for i in range(effects.list.count())] == ["Greenscreen", "Tone"]
    assert [f.name for f in page.filters if f.kind in ("chroma_key", "color_correction")] == ["Chroma Key", "Color Correction"]
    ducking = sound.add("ducking")
    from PySide6.QtWidgets import QComboBox

    combo = sound.form.rows["sound_id"]
    assert isinstance(combo, QComboBox)
    assert [combo.itemText(i) for i in range(combo.count())] == ["Pick a sound", "Mic", "Music"]  # never itself
    combo.setCurrentIndex(1)
    assert ducking.settings["sound_id"] == "mic"


def test_vst_filters_are_gone_and_old_ones_are_skipped(app):
    """VST3 plug-ins ran someone else's native code inside SprigCast: removed (2026-11-17), see docs/REVISIT.md."""
    import numpy as np

    from sprigcast.core.filters import AudioChain, filter_kind
    from sprigcast.core.scene import Filter

    assert filter_kind("vst") is None
    block = np.full((1024, 2), 0.25, dtype=np.float32)
    chain = AudioChain([Filter(id="f1", kind="vst", name="VST", settings={"plugin_file": "Old.vst3"})])
    assert not chain.processors and np.array_equal(chain.process(block), block)  # a saved one just passes sound


def test_sound_filters_drag_to_reorder(app):
    """User 2026-11-21: "allow drag and drop for the filters"."""
    from PySide6.QtCore import QModelIndex

    from sprigcast.app.filters_dialog import FiltersDialog

    source = SourceConfig(id="mic", kind="audio_input", name="Mic")
    calls = []
    d = FiltersDialog(source, lambda save=True: calls.append(save))
    for kind in ("compressor", "limiter", "noise_suppression"):
        d.add(kind)
    assert d.list.dragDropMode() == d.list.DragDropMode.InternalMove
    d.apply_button.setEnabled(False)
    d.list.model().moveRow(QModelIndex(), 2, QModelIndex(), 0)  # what a drop does: Noise Suppression to the top
    assert [f.kind for f in d.source.filters] == ["noise_suppression", "compressor", "limiter"]
    assert d.apply_button.isEnabled() and calls == []  # previewed until Apply
    assert d.title.text() == "Noise Suppression"  # the picked filter's name over its settings


def test_noise_suppression_methods(monkeypatch):
    import numpy as np

    from sprigcast.core import filters
    from sprigcast.core.filters import NoiseSuppression, filter_kind, settings_for

    fields = {f.key: f for f in filter_kind("noise_suppression").fields}
    assert [v for v, _ in fields["method"].choices] == ["standard", "rnnoise"]
    assert fields["amount_db"].show_when == ("method", ("standard",))
    assert settings_for("noise_suppression", {"amount_db": 20})["method"] == "standard"  # older filters: as before
    rnn = NoiseSuppression(settings_for("noise_suppression", {"method": "rnnoise", "mix": 50}))
    if filters.rnnoise_model():
        noise = (0.05 * np.random.default_rng(1).standard_normal((48000, 2))).astype(np.float32)
        out = np.concatenate([rnn.process(noise[i:i + 960]) for i in range(0, len(noise), 960)])
        assert rnn._built_for[0][0] == "arnndn" and len(out) == len(noise)
    monkeypatch.setattr(filters, "rnnoise_model", lambda: "")  # an FFmpeg without RNNoise: Standard runs instead
    assert rnn.graph_spec()[0][0] == "afftdn"
