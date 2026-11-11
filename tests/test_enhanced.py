import time
from types import SimpleNamespace as NS

import numpy as np
import pytest

from sprigcast.core.scene import Canvas
from sprigcast.output import enhanced as e

ANSWER = {
    "meta": {"service": "IVS", "schema_version": "2025-01-25", "config_id": "cfg-123"},
    "status": {"result": "success"},
    "ingest_endpoints": [
        {"protocol": "RTMP", "url_template": "rtmp://ingest.example/app/{stream_key}", "authentication": "live_cfgkey"},
        {"protocol": "RTMPS", "url_template": "rtmps://ingest.example:443/app/{stream_key}", "authentication": "live_cfgkey"},
    ],
    "encoder_configurations": [
        {"type": "obs_x264", "width": 640, "height": 360, "framerate": {"numerator": 30, "denominator": 1},
         "canvas_index": 0, "settings": {"bitrate": 1500, "keyint_sec": 1, "bf": 0}},
        {"type": "obs_x264", "width": 320, "height": 180, "framerate": {"numerator": 15, "denominator": 1},
         "canvas_index": 0, "settings": {"bitrate": 400, "keyint_sec": 1, "bf": 0}},
        {"type": "obs_x264", "width": 360, "height": 640, "framerate": {"numerator": 30, "denominator": 1},
         "canvas_index": 1, "settings": {"bitrate": 1200, "keyint_sec": 1, "bf": 0}},
    ],
    "audio_configurations": {"live": [{"codec": "aac", "track_id": 0, "channels": 2, "settings": {"bitrate": 128}}]},
}


def test_request_describes_canvases_and_pc(monkeypatch):
    monkeypatch.setattr(e, "capabilities", lambda: {"cpu": {"logical_cores": 8}})
    body = e.build_request("live_key", [e.CanvasInfo(1920, 1080, 60), e.CanvasInfo(1080, 1920, 60)], "0.1.0", max_tracks=3)
    assert body["service"] == "IVS" and body["authentication"] == "live_key"
    assert body["client"] == {"name": "sprigcast", "version": "0.1.0", "supported_codecs": ["h264"]}
    assert [c["height"] for c in body["preferences"]["canvases"]] == [1080, 1920]
    assert body["preferences"]["maximum_video_tracks"] == 3
    assert body["preferences"]["canvases"][0]["framerate"] == {"numerator": 60, "denominator": 1}


def test_capabilities_runs_here():
    caps = e.capabilities()
    assert caps["cpu"]["logical_cores"] >= 1 and caps["memory"]["total"] > 0 and caps["system"]["bits"] in (32, 64)


def test_parse_answer_into_tracks_and_url():
    cfg = e.parse_config(ANSWER, "live_user", "h264_nvenc", bandwidth_test=True)
    assert cfg.url == "rtmp://ingest.example/app/live_cfgkey?bandwidthtest=true&clientConfigId=cfg-123"
    assert [(t.width, t.height, t.fps, t.bitrate_kbps, t.canvas_index) for t in cfg.tracks] == [
        (640, 360, 30, 1500, 0), (320, 180, 15, 400, 0), (360, 640, 30, 1200, 1)]
    assert cfg.tracks[0].encoder == "libx264" and cfg.audio_kbps == 128
    assert e.parse_config(ANSWER, "k", "h264_nvenc", use_rtmps=True).server == "rtmps://ingest.example:443/app"


def test_nvenc_settings_translate():
    opts = e.encoder_options("h264_nvenc", {"preset2": "p6", "rate_control": "CBR", "multipass": "qres",
                                            "lookahead": False, "psycho_aq": True, "profile": "high"}, 6000)
    assert opts["preset"] == "p6" and opts["rc"] == "cbr" and opts["multipass"] == "qres"
    assert opts["no-scenecut"] == "1" and opts["forced-idr"] == "1" and opts["maxrate"] == "6000k"
    assert "rc-lookahead" not in opts and opts["spatial-aq"] == "1"


def test_errors_and_warnings_from_twitch():
    with pytest.raises(e.EnhancedError, match="GPU is not supported"):
        e.parse_config({"status": {"result": "error", "html_en_us": "<p>Your GPU is not supported</p>"}}, "k", "x")
    warned = e.parse_config({**ANSWER, "status": {"result": "warning", "html_en_us": "<b>Old</b> driver"}}, "k", "x")
    assert warned.warning == "Old driver"


def _unescape(nal: bytes) -> bytes:
    out, zeros = bytearray(), 0
    for byte in nal:
        if zeros >= 2 and byte == 3:
            zeros = 0
            continue
        out.append(byte)
        zeros = zeros + 1 if byte == 0 else 0
    return bytes(out)


def test_sei_escapes_start_code_lookalikes():
    payload = bytes(16) + b"\x00\x00\x01\x00\x00\x00\x02"
    nal = e.sei_nal(payload)
    assert nal.startswith(b"\x00\x00\x00\x01\x06")
    body = nal[5:]
    assert b"\x00\x00\x01" not in body and b"\x00\x00\x00" not in body
    raw = _unescape(body)
    assert raw[0] == 5 and raw[1] == len(payload) and raw[2:2 + len(payload)] == payload and raw[-1] == 0x80


def test_bpm_reports_changes_since_last_keyframe():
    m = e.TrackMetrics()
    now = time.time()
    first = e.bpm_sei(m, cts=now, fer=now + 0.001, ferc=now + 0.004, pir=now + 0.005,
                      session=(60, 0, 1, 120), rendition=(60, 1, 59))
    nals = first.split(b"\x00\x00\x00\x01")[1:]
    assert len(nals) == 3
    assert e.BPM_TS_UUID in first and e.BPM_SM_UUID in first and e.BPM_ERM_UUID in first
    sm = _unescape(nals[1])
    assert sm.endswith(bytes([1, 0, 0, 0, 0, 2, 0, 0, 0, 0, 3, 0, 0, 0, 0, 4, 0, 0, 0, 0, 0x80]))  # first: all zero
    second = e.bpm_sei(m, cts=now + 2, fer=now + 2.001, ferc=now + 2.003, pir=now + 2.004,
                       session=(180, 0, 3, 360), rendition=(180, 1, 179))
    sm2 = _unescape(second.split(b"\x00\x00\x00\x01")[2])
    assert sm2.endswith(bytes([1, 0, 0, 0, 120, 2, 0, 0, 0, 0, 3, 0, 0, 0, 2, 4, 0, 0, 0, 240, 0x80]))
    ts = _unescape(second.split(b"\x00\x00\x00\x01")[1])
    assert ts[3 + 16] == 3  # NAL header, type, size, uuid, then 3 = four timestamps: CTS, FER, FERC, PIR
    assert e.rfc3339(0.5) == b"1970-01-01T00:00:00.500Z\x00"


def test_multitrack_stream_aligns_keyframes_and_tags_them(tmp_path):
    import av

    from sprigcast.output.enhanced_stream import EnhancedStreamer

    cfg = e.parse_config(ANSWER, "live_user", "libx264")
    h = Canvas(id="horizontal", width=640, height=360, fps=30)
    v = Canvas(id="vertical", width=360, height=640, fps=30)
    start = time.perf_counter()
    st = EnhancedStreamer("Twitch", cfg, [h, v], start, user_key="live_user")
    st._url = str(tmp_path / "out.flv")
    assert st.canvas_ids() == ["horizontal", "vertical"]
    assert st.redact("rtmp://x/app/live_cfgkey?a and live_user") == "rtmp://x/app/****?a and ****"
    st.start()
    for i in range(45):
        t = start + i / 30
        while time.perf_counter() < t:
            time.sleep(0.002)
        for c in (h, v):
            st.push_for(c.id)(NS(data=np.full((c.height, c.width, 4), i * 5 % 255, np.uint8).tobytes(), time=t))
        st.push_audio(np.zeros((1600, 2), np.float32))
    st.stop()
    assert st.error is None, st.error
    assert [t.frames for t in st.tracks] == [45, 23, 45] and st.frames == 45

    with av.open(str(tmp_path / "out.flv")) as c:
        sizes = [(s.codec_context.width, s.codec_context.height) for s in c.streams.video]
        assert sizes == [(640, 360), (320, 180), (360, 640)]
        keys, tagged = {}, []
        for p in c.demux(video=list(range(3))):
            if p.is_keyframe:
                keys.setdefault(p.stream.index, []).append(round(float(p.pts * p.time_base), 2))
                tagged.append(e.BPM_ERM_UUID in bytes(p))
    assert keys[0] == keys[1] == keys[2] and len(keys[0]) == 2
    assert all(tagged)
