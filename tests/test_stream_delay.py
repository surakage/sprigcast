import threading
import time

import av

from sprigcast.output.delay import DelayLine
from sprigcast.output.shared_stream import SharedStreamEncoder, StreamLink

from test_shared_stream import _Canvas, _feed


def test_delay_line_releases_in_order_after_the_delay():
    got, done = [], threading.Event()

    def release(item):
        got.append((item, time.monotonic()))
        if item == 2:
            done.set()
    line = DelayLine(0.3, release)
    began = time.monotonic()
    for i in range(3):
        line.put(i)
    assert line.waiting() == 3 and got == []
    assert done.wait(2)
    assert [i for i, _ in got] == [0, 1, 2] and got[0][1] - began >= 0.29
    line.close()


def test_closing_drops_what_waits():
    got = []
    line = DelayLine(0.2, got.append)
    line.put("a")
    line.close()
    line.put("b")  # ignored after close
    time.sleep(0.35)
    assert got == [] and line.waiting() == 0


def test_a_delayed_link_sends_late_and_its_wait_is_not_backlog(tmp_path):
    enc = SharedStreamEncoder(_Canvas(), 30, 800, "libx264", time.perf_counter())
    path = tmp_path / "late.flv"
    link = StreamLink("late", "unused", "key", enc, open_container=lambda u: av.open(str(path), "w", format="flv"))
    link.delay_s = 1.0
    enc.links.append(link)
    enc.start()
    link.start()
    try:
        _feed(enc, 0.7)
        assert link.bytes_out == 0 and not link.connected  # everything is still in the delay
        assert link.backlog_seconds() < 0.5 and link._delay_line.waiting() > 5
        _feed(enc, 1.3)
        assert link.connected and link.bytes_out > 0
        assert link.health() == ("good", "live, 1 s delay")
    finally:
        link.stop()
        enc.stop()
        link._thread.join(5)  # with a delay, the link ends in the background once the tail is out
    with av.open(str(path)) as f:
        assert sum(1 for p in f.demux(video=0) if p.size) > 5


def test_waiting_out_the_delay_is_not_connecting():
    import time

    from sprigcast.app.destinations import live_line
    from sprigcast.output.stream import Streamer

    st = Streamer("T", "rtmp://x/app", "k", 16, 16, 30, 1000, "libx264", 0.0)
    st.delay_s = 30.0
    assert st.delay_left() > 25
    assert st.health() == ("good", "going out in 30 s (stream delay)")
    level, text = live_line(st)
    assert level == "good" and text.startswith("Going out in")
    st.started_at = time.monotonic() - 31  # the delay has passed and still nothing went out: connecting
    assert st.delay_left() == 0 and st.health()[0] == "warning"
    st.started_at, st.reconnects = time.monotonic(), 1  # after a drop it's a reconnect, not the delay
    assert st.delay_left() == 0


def test_finishing_sends_what_waits_on_time_then_takes_nothing():
    got = []
    line = DelayLine(0.3, lambda item: got.append((item, time.monotonic())))
    began = time.monotonic()
    line.put("a")
    line.put("b")
    assert line.finish(2) is True
    assert [i for i, _ in got] == ["a", "b"] and got[0][1] - began >= 0.29  # still on time, not rushed
    line.put("c")
    time.sleep(0.35)
    assert [i for i, _ in got] == ["a", "b"]


def test_ending_a_delayed_stream_still_sends_the_delayed_part(tmp_path):
    # OBS keeps sending until the delay has drained; stopping used to cut the last N seconds.
    enc = SharedStreamEncoder(_Canvas(), 30, 800, "libx264", time.perf_counter())
    path = tmp_path / "tail.flv"
    link = StreamLink("tail", "unused", "key", enc, open_container=lambda u: av.open(str(path), "w", format="flv"))
    link.delay_s = 1.0
    enc.links.append(link)
    enc.start()
    link.start()
    _feed(enc, 2.0)
    fed_until = time.monotonic()
    began = time.monotonic()
    link.stop()
    assert time.monotonic() - began < 0.5  # ending doesn't hold the window up for the delay
    enc.stop()
    link._thread.join(5)
    assert not link._thread.is_alive() and time.monotonic() - fed_until >= 0.9
    with av.open(str(path)) as f:
        video = f.streams.video[0]
        last = max(float(p.pts * video.time_base) for p in f.demux(video) if p.pts is not None)
    assert last > 1.5  # the second that was still in the delay when we stopped went out too
