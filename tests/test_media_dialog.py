"""The media settings card: real settings, no helper text, and sound files show their own rows."""

import pytest

pytest.importorskip("PySide6")


@pytest.fixture
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_a_video_keeps_and_returns_its_settings(app):
    from sprigcast.app.media_dialog import MediaDialog

    d = MediaDialog("Clip", {"path": "C:/v/clip.webm", "restart_on_show": False, "clear_on_end": True,
                             "speed": 1.5, "volume_db": -9.0, "monitor": "only", "hw_decode": True, "group": "x"})
    d.show()
    values = d.values()
    assert values == {"path": "C:/v/clip.webm", "loop": False, "restart_on_show": False, "clear_on_end": True,
                      "play_on_start": False, "speed": 1.5, "hw_decode": True, "volume_db": -9.0, "muted": False,
                      "monitor": "only"}
    assert d.rows["shown"][1].isVisible() and not d.rows["play_on_start"][1].isVisible()
    d.switches["loop"].setChecked(True)
    assert not d.ends.isEnabled()  # a loop never ends
    d.speed.chip(2.0).click()
    d.hear.chip(True).click()
    assert d.values()["speed"] == 2.0 and d.values()["monitor"] is True


def test_a_sound_file_shows_play_at_launch_not_picture_rows(app):
    from sprigcast.app.media_dialog import MediaDialog

    d = MediaDialog("Ding", {"path": "C:/s/ding.mp3", "play_on_start": True})
    d.show()
    assert d.rows["play_on_start"][1].isVisible() and d.values()["play_on_start"]
    assert not d.rows["ends"][1].isVisible() and not d.rows["hw_decode"][1].isVisible()


def test_an_odd_speed_from_the_api_is_kept(app):
    from sprigcast.app.media_dialog import MediaDialog

    assert MediaDialog("Clip", {"path": "a.mp4", "speed": 3.0}).values()["speed"] == 3.0
