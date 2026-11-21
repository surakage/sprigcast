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
    assert ap.update(1, 0.8) is None  # one reading alone is a spike
    assert ap.update(2, 0.9) == 4800  # two in a row: small step
    assert ap.update(3, 2.0) is None  # too soon after the last step
    assert ap.update(4, 2.0) == 3840  # far behind, but not three readings in a row yet: small step
    assert ap.update(6, 2.0) == 2304  # three far behind in a row: big step
    assert ap.update(8, 3.0) == 2100  # floor: 35% of 6000
    assert ap.update(11, 3.0) is None
    assert ap.steps_down == 4 and ap.lowest_kbps == 2100 and ap.lowered


def test_one_reading_after_a_switch_never_cuts():
    """PC live 2026-11-21: a 3 s reading right after a bloom switch, empty again within 5 s, cut 5400 -> 2592."""
    ap = Autopilot(5400)
    assert ap.update(0, 3.0) is None and ap.update(1, 0.0) is None and not ap.lowered


def test_a_spike_that_empties_fast_goes_straight_back():
    ap = Autopilot(5400)
    ap.update(0, 0.9)
    assert ap.update(1, 3.0) == 4320
    seen = [(t, k) for t in range(2, 30) if (k := ap.update(float(t), 0.0))]
    assert seen == [(12, 5400)]  # 10 s caught up, then back where it was in one step


def test_climbs_back_in_steps_after_a_real_slowdown_and_waits_longer_when_it_comes_back():
    ap = Autopilot(6000)
    for t in range(0, 12):
        ap.update(float(t), 2.0)  # 12 s far behind: a real slowdown
    assert ap.current_kbps == ap.floor_kbps
    last_down = ap._last_down
    seen = [(t, k) for t in range(12, 200) if (k := ap.update(float(t), 0.0))]
    assert seen[0][0] - last_down >= 20  # 20 s after the last step down
    assert all(b - a >= 5 for (a, _), (b, _) in zip(seen, seen[1:]))
    assert seen[-1][1] == 6000 and not ap.lowered
    up_at = seen[-1][0]
    ap.update(up_at + 1.0, 0.8)
    ap.update(up_at + 2.0, 0.8)  # the climb brought it back
    assert ap.lowered and ap._up_wait == 40.0


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
    st._autopilot_at -= 1.0
    st._steer(2.0)
    assert st._want_kbps == 4800
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


def test_the_end_of_stream_line_names_the_place_and_when():
    from types import SimpleNamespace

    from sprigcast.app.optimize import network_trouble

    ap = Autopilot(5400)
    ap.update(100.0, 0.9)
    ap.update(101.0, 3.0)
    yt = SimpleNamespace(name="YouTube", frames=1000, dropped=5, reconnects=0, autopilot=None,
                         shared=SimpleNamespace(autopilot=ap))
    tw = SimpleNamespace(name="Twitch", frames=1000, dropped=0, reconnects=0, autopilot=None, shared=None)
    assert network_trouble([yt, tw], started=40.0) == [
        "YouTube: 5 frames dropped; auto-pilot lowered (its shared encoder) 1 time, first at 0:01:01 (to 4320 kbps)"]
