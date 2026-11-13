import threading

import pytest

from sprigcast.core.mailbox import Mailbox
from sprigcast.core.source import create_source, known_kinds
from sprigcast.sources.color import parse_color


def test_mailbox_keeps_only_newest():
    box = Mailbox()
    box.put(1)
    box.put(2)
    assert box.take() == 2
    assert box.take() is None


def test_mailbox_is_safe_across_threads():
    box = Mailbox()
    threads = [threading.Thread(target=lambda i=i: [box.put(i) for _ in range(1000)]) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert box.take() in range(4)


def test_builtin_kinds_registered():
    assert {"color", "image"} <= set(known_kinds())


def test_parse_color():
    assert parse_color("#ff8000") == (255, 128, 0, 255)
    assert parse_color("#11223344") == (0x11, 0x22, 0x33, 0x44)
    with pytest.raises(ValueError):
        parse_color("#123")


def test_color_source_emits_one_frame_until_changed():
    src = create_source("color", "c", {"color": "#00ff00", "width": 8, "height": 4})
    frame = src.video_frame()
    assert frame.shape == (4, 8, 4)
    assert tuple(frame[0, 0]) == (0, 255, 0, 255)
    assert src.video_frame() is None
    src.update({"color": "#0000ff", "width": 8, "height": 4})
    assert tuple(src.video_frame()[0, 0]) == (0, 0, 255, 255)


def test_missing_image_shows_placeholder(tmp_path):
    src = create_source("image", "i", {"path": str(tmp_path / "nope.png")})
    frame = src.video_frame()
    assert frame.shape == (64, 64, 4)


def test_image_source_reads_file(tmp_path):
    from PIL import Image

    path = tmp_path / "red.png"
    Image.new("RGBA", (5, 3), (255, 0, 0, 255)).save(path)
    src = create_source("image", "i", {"path": str(path)})
    assert src.size == (5, 3)
    assert tuple(src.video_frame()[2, 4]) == (255, 0, 0, 255)


def test_unknown_kind():
    with pytest.raises(ValueError):
        create_source("hologram", "h", {})


def test_teststream_command_never_logs_key():
    from sprigcast.tools.teststream import build_command

    cmd = build_command("rtmp://example/app/KEY", "h264_nvenc", "1280x720", 30, 3000)
    assert cmd[-1] == "rtmp://example/app/KEY"
    assert "-loglevel" in cmd and "h264_nvenc" in cmd


def test_image_source_lets_go_of_pixels_after_upload(tmp_path):
    from PIL import Image

    path = tmp_path / "big.png"
    Image.new("RGBA", (40, 20), (0, 0, 255, 255)).save(path)
    src = create_source("image", "i", {"path": str(path)})
    assert src.video_frame().shape == (20, 40, 4)
    assert src._frame is None and src.size == (40, 20)  # the GPU texture holds it now
    assert src.video_frame() is None
    src.update({"path": str(tmp_path / "gone.png")})
    assert src.video_frame().shape == (64, 64, 4) and src.size == (64, 64)
