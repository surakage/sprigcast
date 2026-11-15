"""The camera picker: lean choices, what the camera really sends, and a lighter camera when there is one."""

import pytest

from sprigcast.sources.camera import CameraDevice, mode_cost, plain_mode

ELGATO_VIRTUAL = [("pixel_format", "yuyv422", 1920, 1080, 59.9999, 59.9999),
                  ("pixel_format", "nv12", 1920, 1080, 59.9999, 59.9999)]
FACECAM = [("vcodec", "mjpeg", 1280, 720, 30, 60), ("pixel_format", "nv12", 1280, 720, 30, 60),
           ("pixel_format", "nv12", 1920, 1080, 30, 60)]


def test_plain_mode_and_cost():
    nv12 = {"video_size": "1920x1080", "framerate": "10000000/166667", "pixel_format": "nv12"}
    assert plain_mode(nv12) == "1920 x 1080 at 60 fps (NV12)"
    yuyv = {**{k: v for k, v in nv12.items() if k != "pixel_format"}, "pixel_format": "yuyv422"}
    assert mode_cost(yuyv, 30) > mode_cost(nv12, 30)
    small = {"video_size": "1280x720", "framerate": "30", "vcodec": "mjpeg"}
    assert mode_cost(small, 30) < mode_cost(nv12, 30) / 10


def test_describe_mode_says_what_sprigcast_does_with_a_bigger_faster_camera():
    from sprigcast.app.camera_dialog import describe_mode

    text = describe_mode(ELGATO_VIRTUAL, 1280, 720, 30)
    assert "1920 x 1080 at 60 fps (NV12)" in text
    assert "shrinks it to 1280 x 720" in text and "skips frames down to 30 fps" in text
    assert "exactly" in describe_mode(FACECAM, 1280, 720, 30)
    assert describe_mode([], 1280, 720, 30) == ""


def test_lighter_camera_points_at_the_facecam():
    from sprigcast.app.camera_dialog import lighter_camera

    elgato, facecam, other = CameraDevice(0, "Elgato Virtual Camera", "a"), CameraDevice(1, "Facecam MK.2", "b"), \
        CameraDevice(2, "Nothing", "c")
    modes = {"a": ELGATO_VIRTUAL, "b": FACECAM, "c": []}
    cam, mode = lighter_camera(elgato, [facecam, other], lambda c: modes[c.path], 1280, 720, 30)
    assert cam == facecam and mode["video_size"] == "1280x720"
    assert lighter_camera(facecam, [elgato], lambda c: modes[c.path], 1280, 720, 30) is None


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_dialog_caps_rate_to_the_canvas_and_pauses_by_default(qapp):
    from sprigcast.app.camera_dialog import CameraDialog

    cams = [CameraDevice(0, "Elgato Virtual Camera", "a")]
    dialog = CameraDialog(cams, {"name": "Elgato Virtual Camera", "path": "a", "fps": 60}, max_fps=30,
                          modes=ELGATO_VIRTUAL)
    settings = dialog.settings()
    assert settings["fps"] == 30 and settings["pause_off_screen"] is True
    assert (settings["width"], settings["height"]) == (1280, 720)
    assert [dialog.rate.itemData(i) for i in range(dialog.rate.count())] == [30, 15]
    assert "skips frames" in dialog.note.text()
    assert not dialog.lighter.isVisibleTo(dialog)  # only one camera
    dialog.deleteLater()


def test_dialog_keeps_a_cameras_own_choices(qapp):
    from sprigcast.app.camera_dialog import CameraDialog

    cams = [CameraDevice(0, "A", "a"), CameraDevice(1, "B", "b")]
    dialog = CameraDialog(cams, {"name": "B", "path": "b", "width": 1920, "height": 1080, "fps": 60,
                                 "mirror": True, "pause_off_screen": False})
    s = dialog.settings()
    assert (s["name"], s["width"], s["fps"], s["mirror"], s["pause_off_screen"]) == ("B", 1920, 60, True, False)
    assert dialog.lighter.isVisibleTo(dialog)
    dialog.deleteLater()


def test_pausing_doesnt_split_a_shared_feed():
    from sprigcast.sources.camera import _feed_key

    base = {"name": "A", "path": "a", "width": 1280, "height": 720, "fps": 30}
    assert _feed_key({**base, "pause_off_screen": True}) == _feed_key({**base, "pause_off_screen": False})
