"""The window warns when the old engine draws on a PC whose graphics card could run the new one, and says nothing
on a PC without one (the old engine is kept for those)."""

from PySide6.QtWidgets import QApplication, QMainWindow

from sprigcast import sprigcore
from sprigcast.app import engine_warning


def _pc(monkeypatch, software=False, safe=False, asked="", there=True, failed=""):
    monkeypatch.setattr(sprigcore.sys, "platform", "win32")
    monkeypatch.setattr(sprigcore, "software_only", lambda: software)
    monkeypatch.setattr(sprigcore, "_python_only", safe)
    monkeypatch.setattr(sprigcore, "available", lambda: there)
    monkeypatch.setattr(sprigcore, "_engine_failed", failed)
    if asked:
        monkeypatch.setenv("SPRIGCAST_ENGINE", asked)
    else:
        monkeypatch.delenv("SPRIGCAST_ENGINE", raising=False)


def test_nothing_is_said_while_the_new_engine_draws_or_without_a_graphics_card(monkeypatch):
    _pc(monkeypatch)
    assert engine_warning.text() == ""
    _pc(monkeypatch, software=True, safe=True, asked="python", there=False, failed="x")
    assert engine_warning.text() == ""  # an older PC: the old engine is what it should use


def test_each_reason_the_old_engine_draws_on_a_pc_with_a_graphics_card(monkeypatch):
    _pc(monkeypatch, safe=True)
    assert engine_warning.text() == "Old engine in use (safe mode)"
    _pc(monkeypatch, asked="python")
    assert engine_warning.text() == "Old engine in use (turned off)"
    _pc(monkeypatch, there=False)
    assert engine_warning.text() == "Old engine in use (engine file missing)"
    _pc(monkeypatch)
    sprigcore.note_engine_failed("a lost graphics card")
    assert engine_warning.text() == "Old engine in use (engine stopped)"


def test_the_label_shows_only_while_the_warning_holds(monkeypatch):
    QApplication.instance() or QApplication([])
    _pc(monkeypatch)
    window = QMainWindow()
    label = engine_warning.install(window)
    assert label.isHidden() and label.text() == ""
    sprigcore.note_engine_failed("a frame stuck for 3 s")  # the engine stops while SprigCast runs
    label.refresh()
    assert not label.isHidden() and label.text() == "Old engine in use (engine stopped)"
