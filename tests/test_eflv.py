"""Twitch Enhanced Broadcasting's main quality in Enhanced RTMP's own form (output/eflv.py)."""

from __future__ import annotations

import threading
import time
from fractions import Fraction

import av
import numpy as np
import pytest

from sprigcast.output import eflv
from sprigcast.output import enhanced as e


def _tag(kind: int, body: bytes, ts: int = 0) -> bytes:
    head = bytes([kind]) + len(body).to_bytes(3, "big") + (ts & 0xFFFFFF).to_bytes(3, "big") + bytes([ts >> 24]) + b"\0\0\0"
    return head + body + (11 + len(body)).to_bytes(4, "big")


def _tags(data: bytes):
    at, out = 13, []
    while at + 11 <= len(data):
        size = int.from_bytes(data[at + 1:at + 4], "big")
        out.append((data[at], data[at + 11:at + 11 + size], int.from_bytes(data[at + 11 + size:at + 15 + size], "big")))
        at += 15 + size
    assert at == len(data)
    return out


def test_classic_h264_tags_become_enhanced_single_track_tags():
    header = b"FLV\x01\x05\x00\x00\x00\x09\x00\x00\x00\x00"
    stream = (header + _tag(18, b"\x02\x00\x0aonMetaData")
              + _tag(9, b"\x17\x00\x00\x00\x00" + b"AVCC")  # sequence header
              + _tag(9, b"\x17\x01\x00\x00\x00" + b"KEY")  # keyframe, no composition offset
              + _tag(9, b"\x27\x01\x00\x00\x21" + b"B", 33)  # inter frame, 33 ms offset
              + _tag(9, b"\x96\x03avc1\x01" + b"T1")  # already Enhanced (a multitrack packet): as it was
              + _tag(8, b"\xaf\x01AAC"))
    r = eflv.Rewriter()
    out = b"".join(r.feed(stream[i:i + 7]) for i in range(0, len(stream), 7))  # any pieces
    assert out[:13] == header
    tags = _tags(out)
    assert [t[0] for t in tags] == [18, 9, 9, 9, 9, 8]
    assert tags[1][1] == b"\x90avc1AVCC"  # SequenceStart, keyframe
    assert tags[2][1] == b"\x93avc1KEY"  # CodedFramesX
    assert tags[3][1] == b"\xa1avc1\x00\x00\x21B"  # CodedFrames with its offset, inter frame
    assert tags[4][1] == b"\x96\x03avc1\x01T1" and tags[5][1] == b"\xaf\x01AAC"
    assert all(size == 11 + len(body) for _, body, size in tags)


def _write(out, frames=30):
    vs = []
    for w, h in ((320, 180), (180, 320)):
        s = out.add_stream("libx264", rate=30)
        s.width, s.height, s.pix_fmt = w, h, "yuv420p"
        vs.append(s)
    a = out.add_stream("aac", rate=48000)
    a.layout = "stereo"
    for n in range(frames):
        for s in vs:
            f = av.VideoFrame.from_ndarray(np.zeros((s.height, s.width, 3), np.uint8), format="rgb24")
            f.pts, f.time_base = n, Fraction(1, 30)
            out.mux(s.encode(f))
        af = av.AudioFrame.from_ndarray(np.zeros((2, 1600), np.float32), format="fltp", layout="stereo")
        af.sample_rate, af.pts = 48000, n * 1600
        out.mux(a.encode(af))
    for s in vs:
        out.mux(s.encode())
    out.close()


def test_the_main_quality_goes_out_enhanced_and_reads_back(tmp_path):
    path = tmp_path / "out.flv"
    _write(eflv.EnhancedIngest(str(path)))
    data = path.read_bytes()
    video = [body for kind, body, _ in _tags(data) if kind == 9]
    assert video and all(body[0] & 0x80 for body in video)  # no classic FLV video left: track 0 is avc1 too
    with av.open(str(path)) as c:
        assert [(s.codec_context.name, s.codec_context.width) for s in c.streams.video] == [("h264", 320), ("h264", 180)]
        counts = {}
        for p in c.demux():
            if p.size:
                counts[p.stream.index] = counts.get(p.stream.index, 0) + 1
    assert counts[0] == 30 and counts[1] == 30 and counts[2] > 20


def test_it_goes_through_ffmpegs_rtmp_to_a_server():
    """Through FFmpeg's own RTMP protocol (what Twitch gets) to an FFmpeg RTMP server on this machine."""
    port = 19000 + int(time.time() * 1000) % 900
    got: dict = {}

    def serve():
        try:
            c = av.open(f"rtmp://127.0.0.1:{port}/app/live_key", "r", options={"listen": "1"}, timeout=15)
        except Exception as exc:  # noqa: BLE001
            got["error"] = exc
            return
        got["streams"] = [(s.type, s.codec_context.name) for s in c.streams]
        counts = got["counts"] = {}
        try:
            for p in c.demux():
                if p.size:
                    counts[p.stream.index] = counts.get(p.stream.index, 0) + 1
        except Exception:  # noqa: BLE001, S110 - the sender hung up: the end
            pass

    server = threading.Thread(target=serve, daemon=True)
    server.start()
    time.sleep(0.5)
    try:
        out = eflv.EnhancedIngest(f"rtmp://127.0.0.1:{port}/app/live_key?clientConfigId=cfg", timeout=(10, 20))
    except av.error.FFmpegError as exc:
        pytest.skip(f"no RTMP here: {exc}")
    _write(out)
    server.join(20)
    assert "error" not in got, got
    assert got["streams"] == [("video", "h264"), ("video", "h264"), ("audio", "aac")]
    assert got["counts"][0] == 30 and got["counts"][1] == 30


def test_twitchs_answer_goes_in_the_log_without_the_key():
    from test_enhanced import ANSWER

    text = e.answer_summary(ANSWER)
    assert "live_cfgkey" not in text and "{stream_key}" not in text
    assert "config cfg-123" in text and "track 0: obs_x264 canvas 0 640x360@30" in text and "'bitrate': 1500" in text
    assert "servers RTMP ingest.example, RTMPS ingest.example" in text
