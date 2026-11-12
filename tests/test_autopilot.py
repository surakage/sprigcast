import time

import av
import numpy as np
import pytest

from sprigcast.core.scene import EncoderSettings
from sprigcast.output.autopilot import Autopilot
from sprigcast.output.recorder import video_options
from sprigcast.output.stream import Streamer
from sprigcast.render.engine import Frame


def test_steps_down_when_behind_and_stops_at_the_floor():
    ap = Autopilot(6000)
    assert ap.update(0, 0.0) is None  # caught up at full bitrate: nothing to do
    assert ap.update(1, 0.8) == 4800  # behind: small step
    assert ap.update(2, 0.9) is None  # too soon after the last step
    assert ap.update(3.5, 2.0) == 2880  # far behind: big step
    assert ap.update(6, 3.0) == 2100  # floor: 35% of 6000
    assert ap.update(9, 3.0) is None
    assert ap.steps_down == 3 and ap.lowest_kbps == 2100 and ap.lowered


def test_climbs_back_slowly_once_caught_up():
    ap = Autopilot(6000)
    ap.update(0, 1.0)  # down to 4800
    t, seen = 1.0, []
    while t < 200:
        kbps = ap.update(t, 0.0)
        if kbps:
            seen.append((t, kbps))
        t += 1
    assert seen[0] == (30, 5400)  # waits 30 s after a drop, then +10% of the target
    assert seen[-1][1] == 6000 and len(seen) == 2 and not ap.lowered


def test_small_targets_keep_their_bitrate_as_the_floor():
    ap = Autopilot(600)
    assert ap.floor_kbps == 600 and ap.update(0, 5.0) is None


def test_strict_cbr_signalling_is_left_out_when_steering():
    enc = EncoderSettings(rate_control="cbr")
    assert video_options("libx264", enc, 6000)["nal-hrd"] == "cbr"
    assert "nal-hrd" not in video_options("libx264", enc, 6000, hrd=False)


def test_steering_needs_a_connection_older_than_the_grace_period():
    st = Streamer("T", "rtmp://x/app", "k", 16, 16, 30, 6000, "libx264", 0.0, auto_bitrate=True)
    st.connected, st._connected_at = True, time.monotonic()
    st._steer(2.0)
    assert st._want_kbps == 6000  # just connected: the handshake backlog doesn't count
    st._connected_at -= 60
    st._steer(2.0)
    assert st._want_kbps == 3600
    off = Streamer("T", "rtmp://x/app", "k", 16, 16, 30, 6000, "libx264", 0.0,
                   encoder=EncoderSettings(rate_control="cqp"), auto_bitrate=True)
    assert off.autopilot is None  # constant quality has no bitrate to steer


@pytest.mark.skipif("libx264" not in av.codecs_available, reason="needs libx264")
def test_the_encoder_follows_the_new_bitrate(tmp_path):
    out = tmp_path / "steer.flv"

    class FileStreamer(Streamer):
        def _open_container(self):
            return av.open(str(out), "w", format="flv")

    w, h, fps = 320, 180, 30
    st = FileStreamer("Local", "rtmp://unused/app", "k", w, h, fps, 3000, "libx264", 0.0, auto_bitrate=True)
    st.start()
    t0 = time.perf_counter() + 0.3  # after the stream thread sets its start time
    rng = np.random.default_rng(1)
    try:
        for i in range(8 * fps):
            if i == 4 * fps:
                st._want_kbps = 600
            data = rng.integers(0, 255, (h, w, 4), dtype=np.uint8).tobytes()  # noise: fills any bitrate
            st.push_frame(Frame("horizontal", w, h, data, t0 + i / fps))
            st.push_audio(np.zeros((1600, 2), dtype=np.float32))
            if i % 10 == 0:
                time.sleep(0.05)  # let the encoder keep up so nothing is skipped
    finally:
        st.stop()
    assert st.error is None
    halves = [0, 0]
    with av.open(str(out)) as f:
        for packet in f.demux(video=0):
            if packet.pts is not None and packet.size:
                t = float(packet.pts * packet.time_base)
                if 1 <= t < 4:
                    halves[0] += packet.size
                elif 5 <= t < 8:
                    halves[1] += packet.size
    assert halves[1] < 0.4 * halves[0]  # 600 vs 3000 kbps
