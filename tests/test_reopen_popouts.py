"""Settings → General → Projectors → Reopen them next time."""

import os

import pytest

pytest.importorskip("PySide6.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings  # noqa: E402

from sprigcast.app import projector  # noqa: E402
from test_add_menu import _window  # noqa: E402


@pytest.fixture()
def clean():
    for key in (projector.REOPEN, projector.OPEN_LIST):
        QSettings().remove(key)
    yield
    for key in (projector.REOPEN, projector.OPEN_LIST):
        QSettings().remove(key)


def test_off_keeps_nothing_and_opens_nothing(clean, tmp_path, monkeypatch):
    col, w = _window(tmp_path, monkeypatch)
    QSettings().setValue(projector.OPEN_LIST, "[]")  # left from when it was on
    w.open_projector(col.canvases[0].id, None)
    w.close()
    assert not QSettings().contains(projector.OPEN_LIST) and projector.saved() == []
    col, w = _window(tmp_path, monkeypatch)
    QSettings().setValue(projector.OPEN_LIST, '[{"canvas": "horizontal", "kind": "canvas", "ref": "horizontal"}]')
    try:
        assert w.reopen_projectors() == [] and not w.projectors
    finally:
        w.close()


def test_open_pop_outs_come_back_where_they_were_and_gone_ones_are_left_out(clean, tmp_path, monkeypatch):
    col, w = _window(tmp_path, monkeypatch)
    QSettings().setValue(projector.REOPEN, True)  # (after the window: making it starts from clean settings)
    canvas = col.canvases[0].id
    bloom = col.scenes[0].id
    first = w.open_projector(canvas, None)
    first.setGeometry(40, 50, 640, 360)
    w.open_projector(canvas, None, "bloom", bloom)
    w.close()
    kept = projector.saved()
    assert [(d["kind"], d["ref"], d["screen"]) for d in kept] == [("canvas", canvas, ""), ("bloom", bloom, "")]
    assert kept[0]["window"] == [40, 50, 640, 360]

    # Next start: one more saved for a bloom that's gone, one for a screen that isn't plugged in, one that's junk.
    import json

    kept += [{"canvas": canvas, "kind": "bloom", "ref": "gone", "screen": "", "window": []},
             {"canvas": canvas, "kind": "canvas", "ref": canvas, "screen": "HDMI-9", "window": []},
             {"canvas": "nowhere", "kind": "canvas", "ref": "nowhere", "screen": "", "window": []}, "junk"]
    col, w = _window(tmp_path, monkeypatch)
    QSettings().setValue(projector.REOPEN, True)
    QSettings().setValue(projector.OPEN_LIST, json.dumps(kept))
    try:
        opened = w.reopen_projectors()
        assert [p.target for p in opened] == [("canvas", canvas), ("bloom", bloom)]
        box = opened[0].geometry()
        assert (box.x(), box.y(), box.width(), box.height()) == (40, 50, 640, 360)
        assert w.reopen_projectors() == [] and len(w.projectors) == 2  # asked again: not opened twice
    finally:
        w.close()
    QSettings().setValue(projector.OPEN_LIST, "not json")
    assert projector.saved() == []


def test_a_window_saved_off_every_screen_opens_at_its_usual_place(clean):
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    screens = QGuiApplication.screens()
    assert projector.on_a_screen([40, 50, 640, 360], screens)
    assert not projector.on_a_screen([90000, 90000, 640, 360], screens)  # a monitor that's gone
    assert not projector.on_a_screen([], screens) and not projector.on_a_screen([1, 2, 3], screens)


def test_settings_has_the_tick(clean):
    from test_settings_window import _collection
    from test_settings_window import _window as settings

    w = settings(_collection(), tab="General")
    assert not w.projector_reopen.isChecked()
    w.projector_reopen.setChecked(True)
    assert w.apply() and projector.reopen()
    w.close()
