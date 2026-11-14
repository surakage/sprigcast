from datetime import datetime

import av
import numpy as np

from sprigcast.output import naming
from sprigcast.output.recorder import recording_path
from sprigcast.output.replay import replay_path

WHEN = datetime(2026, 11, 14, 19, 30, 5)


def teardown_function():
    naming.set_patterns()


def test_default_names_match_the_old_ones(tmp_path):
    assert recording_path(tmp_path, "horizontal", WHEN, "mkv").name == "SprigCast 2026-11-14 19-30-05 horizontal.mkv"
    assert replay_path(tmp_path, "vertical", WHEN, "mp4").name == "SprigCast Replay 2026-11-14 19-30-05 vertical.mp4"


def test_patterns_fill_tokens_and_keep_names_apart(tmp_path):
    naming.set_patterns("{year}/{month} stream", "Clip")
    # the slash Windows refuses becomes '-', and the canvas and time are added so two files never clash
    assert recording_path(tmp_path, "vertical", WHEN, "mkv").name == "2026-11 stream vertical 19-30-05.mkv"
    assert replay_path(tmp_path, "horizontal", WHEN, "mkv").name == "Clip 2026-11-14 19-30-05 horizontal.mkv"
    (tmp_path / "2026-11 stream vertical 19-30-05.mkv").touch()
    assert recording_path(tmp_path, "vertical", WHEN, "mkv").name == "2026-11 stream vertical 19-30-05 (2).mkv"
    naming.set_patterns("  ", "")
    assert naming.recording_name("horizontal", WHEN) == "SprigCast 2026-11-14 19-30-05 horizontal"


def test_remux_copies_an_mkv_into_an_mp4(tmp_path):
    src = tmp_path / "rec.mkv"
    with av.open(str(src), "w") as out:
        stream = out.add_stream("libx264", rate=30)
        stream.width, stream.height, stream.pix_fmt = 64, 36, "yuv420p"
        for i in range(15):
            frame = av.VideoFrame.from_ndarray(np.full((36, 64, 3), i * 10, np.uint8), format="rgb24")
            for packet in stream.encode(frame):
                out.mux(packet)
        for packet in stream.encode():
            out.mux(packet)
    mp4 = naming.remux_to_mp4(src, delete_original=True)
    assert mp4 == tmp_path / "rec.mp4" and not src.exists()
    with av.open(str(mp4)) as f:
        assert sum(1 for _ in f.decode(video=0)) == 15


def test_remux_failure_keeps_the_mkv(tmp_path):
    bad = tmp_path / "broken.mkv"
    bad.write_bytes(b"not a video")
    assert naming.remux_to_mp4(bad, delete_original=True) is None
    assert bad.exists() and not (tmp_path / "broken.mp4").exists()
