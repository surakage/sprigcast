import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtWidgets import QApplication, QPushButton, QWidget  # noqa: E402

from sprigcast.core.scene import SourceConfig  # noqa: E402

NOTES = ["Odd: some_plugin_source has no match in SprigCast yet.", "Mic: muted until you pick its sound device (that "
         "device wasn't found).", "From your OBS profile (Streaming): 60 fps.", "Logo: flipped in OBS; SprigCast "
         "can't flip it yet, so it shows unflipped."]


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def test_report_groups_what_went_where():
    from sprigcast.app.gardens_menu import report_groups

    groups = dict(report_groups(NOTES))
    assert groups["Didn't come over"] == [NOTES[0], NOTES[3]]
    assert groups["Check these"] == [NOTES[1]]
    assert groups["Brought over differently"] == [NOTES[2]]
    assert report_groups([]) == []


def test_report_opens_the_leaf_a_line_is_about(app, monkeypatch):
    from sprigcast.app import gardens_menu

    window = QWidget()
    window.collection = type("C", (), {"sources": [SourceConfig(id="mic-1", kind="audio_input", name="Mic",
                                                                settings={})]})()
    opened = []
    monkeypatch.setattr(gardens_menu, "open_source", lambda w, sid: opened.append(sid) or True)
    report = gardens_menu.ImportNotes(window, "Mine", NOTES)
    buttons = [b for b in report.findChildren(QPushButton) if b.text() == "Open"]
    assert len(buttons) == 1  # only Mic is a leaf or sound in the new garden
    buttons[0].click()
    assert opened == ["mic-1"] and report.opened == ["mic-1"] and buttons[0].text() == "✓ Opened"
