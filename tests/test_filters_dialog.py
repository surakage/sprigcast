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
