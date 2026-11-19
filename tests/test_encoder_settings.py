from sprigcast.core.scene import EncoderSettings
from sprigcast.output import recorder


def test_video_options_for_every_family():
    enc = EncoderSettings(rate_control="cbr", profile="high")
    assert recorder.video_options("h264_nvenc", enc, 6000)["profile"] == "high"
    assert recorder.video_options("hevc_nvenc", enc, 6000)["profile"] == "main"
    assert "profile" not in recorder.video_options("av1_nvenc", enc, 6000)
    assert recorder.video_options("av1_nvenc", enc, 6000)["rc"] == "cbr"
    assert "profile" not in recorder.video_options("hevc_amf", enc, 6000)
    assert recorder.video_options("hevc_qsv", enc, 6000)["maxrate"] == "6000k"
    assert recorder.rate_limits("hevc_nvenc", EncoderSettings(rate_control="vbr"), 6000) == (9000, 12000)


def test_x264_preset_and_tune():
    opts = recorder.video_options("libx264", EncoderSettings(x264_preset="faster", x264_tune="film"), 6000)
    assert opts["preset"] == "faster" and opts["tune"] == "film"
    opts = recorder.video_options("libx264", EncoderSettings(x264_preset="bogus"), 6000)
    assert opts["preset"] == "veryfast" and "tune" not in opts


def test_x264_is_kept_small_on_a_small_pc_and_capped_on_a_big_one(monkeypatch):
    """PC 2026-11-19: x264's own choice (1.5 threads per CPU thread) started 28 threads and took 351 MB for one
    stream on a 16-thread PC. Each thread and each frame looked ahead keeps pictures in memory."""
    assert [recorder.x264_threads(n) for n in (1, 2, 4, 6, 8, 16, 32)] == [1, 2, 4, 9, 12, 12, 12]
    monkeypatch.setattr(recorder.os, "cpu_count", lambda: 4)  # the 4 GB all-in-one
    opts = recorder.video_options("libx264", EncoderSettings(x264_preset="medium"), 3000)
    assert opts["threads"] == "4" and opts["rc-lookahead"] == "10"  # medium's own is 40 frames
    for quick in ({"x264_preset": "superfast"}, {"x264_preset": "faster", "x264_tune": "zerolatency"},
                  {"x264_preset": "faster", "tuning": "ull"}):
        opts = recorder.video_options("libx264", EncoderSettings(**quick), 3000)
        assert opts["threads"] == "4" and "rc-lookahead" not in opts  # they look at no frames ahead: left so
    monkeypatch.setattr(recorder.os, "cpu_count", lambda: 16)
    opts = recorder.video_options("libx264", EncoderSettings(x264_preset="medium"), 6000)
    assert opts["threads"] == "12" and "rc-lookahead" not in opts
    assert "threads" not in recorder.video_options("h264_nvenc", EncoderSettings(), 6000)


def test_resolve_codec_falls_back(monkeypatch):
    monkeypatch.setattr(recorder, "pick_codec", lambda: "h264_nvenc")
    monkeypatch.setattr(recorder, "encoder_works", lambda c: c in ("h264_nvenc", "hevc_nvenc", "libx264"))
    assert recorder.resolve_codec(EncoderSettings()) == "h264_nvenc"
    assert recorder.resolve_codec(EncoderSettings(encoder="hevc_nvenc")) == "hevc_nvenc"
    assert recorder.resolve_codec(EncoderSettings(encoder="hevc_nvenc"), streaming=True) == "h264_nvenc"
    assert recorder.resolve_codec(EncoderSettings(encoder="av1_nvenc")) == "h264_nvenc"  # not on this card
    assert recorder.resolve_codec(EncoderSettings(encoder="libx264"), streaming=True) == "libx264"
    assert recorder.encoder_choices(streaming=True) == ["h264_nvenc", "libx264"]
    assert recorder.encoder_choices(streaming=False) == ["h264_nvenc", "hevc_nvenc", "av1_nvenc", "libx264"]
    assert recorder.codec_short("hevc_nvenc") == "NVENC HEVC" and recorder.codec_label("av1_amf") == "AMD AMF AV1"
