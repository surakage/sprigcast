"""memcheck --soak's readouts: which encoder path each encoder took, sources awake, replay bytes in RAM."""

from types import SimpleNamespace

from sprigcast.core.scene import EncoderSettings
from sprigcast.tools.memcheck import awake_sources, encoder_lines, replay_megabytes


def _enc(**kw):
    base = dict(codec="h264_nvenc", out_width=1920, out_height=1080, fps=60, bitrate_kbps=6000,
                encoder=EncoderSettings(), _direct=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_encoder_lines_say_which_path():
    rec = _enc(canvas_id="horizontal", _direct=object(), bitrate_kbps=12000)
    stream = _enc(name="twitch", current_kbps=6000, encoder=EncoderSettings(b_frames=2))
    lines = encoder_lines([rec, stream])
    assert lines[0] == ("  recording/replay horizontal: SprigCast's own NVENC, 1920x1080 @ 60, 12000 kbps, "
                        "B-frames 0, lookahead False")
    assert lines[1].startswith("  stream twitch: FFmpeg h264_nvenc,") and "B-frames 2" in lines[1]


def test_awake_and_replay_counts():
    engine = SimpleNamespace(_sources={"a": SimpleNamespace(asleep=False), "b": SimpleNamespace(asleep=True),
                                       "c": SimpleNamespace()})
    assert awake_sources(engine) == 2
    packets = [SimpleNamespace(data=b"x" * 500_000), SimpleNamespace(data=None), SimpleNamespace(data=b"y" * 500_000)]
    window = SimpleNamespace(encoders=SimpleNamespace(encoders={"h": SimpleNamespace(_packets=packets)}))
    assert replay_megabytes(window) == 1.0
