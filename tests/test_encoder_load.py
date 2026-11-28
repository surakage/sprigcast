"""Encoder overload: told apart from a slow upload, and Garden check's one tap to make encoding lighter."""

import os
import queue

import pytest

from sprigcast.core.scene import Canvas, EncoderSettings, SceneCollection
from sprigcast.output.encoder_load import EncoderWatch, lighter, make_lighter


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class _Enc:
    """Looks like a running encoder: frames, time spent encoding, frames eased off, its frame queue."""

    def __init__(self, fps=60, codec="h264_nvenc"):
        self.fps, self.codec = fps, codec
        self.frames, self.encode_seconds, self.eased = 0, 0.0, 0
        self._video = queue.Queue(maxsize=10)

    def run(self, seconds, ms_per_frame):
        frames = int(seconds * self.fps)
        self.frames += frames
        self.encode_seconds += frames * ms_per_frame / 1000


class _Link:  # a StreamLink: its encoder is .shared
    def __init__(self, shared):
        self.shared = shared


class _Recording:
    def __init__(self, *encs):
        self.recorders = [(f"c{i}", e) for i, e in enumerate(encs)]


def _watch(clock, seconds, step=1.0, each=None, **outputs):
    w = EncoderWatch(clock=clock)
    for _ in range(int(seconds / step)):
        if each:
            each(step)
        clock.now += step
        w.tick(**outputs)
    return w


def test_a_fast_encoder_is_fine_and_a_slow_one_is_told_apart():
    clock = _Clock()
    fast, slow = _Enc(), _Enc()
    w = _watch(clock, 12, each=lambda s: (fast.run(s, 4.0), slow.run(s, 15.5)),
               streamers=[("horizontal", fast)], recording=_Recording(slow))
    assert w.overloaded() == ["recording"]  # 15.5 ms of a 16.7 ms frame: can't keep up
    assert w.codecs == {"stream": "h264_nvenc", "recording": "h264_nvenc"}


def test_too_soon_to_say_and_easing_or_a_full_queue_count():
    clock = _Clock()
    enc = _Enc()
    w = _watch(clock, 3, each=lambda s: enc.run(s, 16.0), streamers=[("h", enc)])
    assert w.overloaded() == []  # only three seconds: not judged yet

    clock, shared = _Clock(), _Enc()
    for _ in range(5):
        shared._video.put_nowait(object())  # half full and more
    shared._video.put_nowait(object())
    w = _watch(clock, 8, each=lambda s: shared.run(s, 11.0), streamers=[("a", _Link(shared)), ("b", _Link(shared))])
    assert w.overloaded() == ["stream"]  # one shared encoder, judged once
    # The same full queue with frames encoded in 2-12 ms of 16.7: the pictures came late (a busy graphics card),
    # not a slow encoder (CRGPlays, 2026-11-27)
    for ms in (2.0, 9.0):
        clock = _Clock()
        w = _watch(clock, 8, each=lambda s, ms=ms: shared.run(s, ms), streamers=[("a", _Link(shared))])
        assert w.overloaded() == []

    clock, rec = _Clock(), _Enc()
    w = _watch(clock, 8, each=lambda s: (rec.run(s, 2.0), setattr(rec, "eased", rec.eased + 5)),
               recording=_Recording(rec))
    assert w.overloaded() == ["recording"]


def test_an_encoder_that_stops_is_forgotten():
    clock, enc = _Clock(), _Enc()
    w = _watch(clock, 10, each=lambda s: enc.run(s, 16.0), streamers=[("h", enc)])
    assert w.overloaded() == ["stream"]
    clock.now += 1
    w.tick()
    assert w.overloaded() == [] and w.codecs == {}


def test_lighter_steps_the_graphics_card_and_cpu_presets():
    gpu = EncoderSettings(preset="p5", lookahead=True, b_frames=2, multipass="qres")
    assert lighter(gpu, "h264_nvenc") == {"lookahead": False, "b_frames": 0, "multipass": "disabled", "preset": "p3"}
    assert lighter(EncoderSettings(preset="p1"), "h264_nvenc") == {}  # already lightest
    assert lighter(EncoderSettings(x264_preset="veryfast"), "libx264") == {"x264_preset": "superfast"}
    assert lighter(EncoderSettings(x264_preset="ultrafast"), "libx264") == {}


def test_make_lighter_changes_only_what_was_overloaded():
    c = SceneCollection(canvases=[Canvas(id="horizontal", width=1920, height=1080)], sources=[], scenes=[])
    c.stream_encoder.preset = c.recording.encoder.preset = "p6"
    assert make_lighter(c, ["stream"], {"stream": "h264_nvenc"}) == ["stream"]
    assert c.stream_encoder.preset == "p4" and c.recording.encoder.preset == "p6"
    c.recording.encoder.x264_preset = "veryfast"
    assert make_lighter(c, ["recording"], {"recording": "libx264"}) == ["recording"]
    assert c.recording.encoder.x264_preset == "superfast" and c.recording.encoder.preset == "p6"


@pytest.fixture
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_garden_check_says_so_and_offers_one_tap(app):
    from sprigcast.app.garden_check import GardenSnapshot, garden_check_popup

    snap = GardenSnapshot()
    taps = []

    def tap():
        taps.append(1)
        return "Lighter settings saved. They start with your next stream."

    pop = garden_check_popup(lambda: snap, [], None, make_lighter=tap)
    pop.show()
    app.processEvents()
    page = pop.garden
    assert not page.overload_bar.isVisible() and snap.level() == "good"
    snap.overload = "The encoder can't keep up with the stream."
    page.refresh()
    app.processEvents()
    assert page.overload_bar.isVisible() and page.lighter.isVisible() and snap.level() == "warning"
    assert page.overload_text.text() == snap.overload
    page.lighter.click()
    page.refresh()
    app.processEvents()
    assert taps == [1] and not page.lighter.isVisible()
    assert page.overload_text.text().startswith("Lighter settings saved")
    pop.accept()


def test_a_slow_replay_buffer_is_called_that_not_a_recording():
    """PC stream 2026-11-17: "The encoder can't keep up with the recording" with nothing recording."""
    from types import SimpleNamespace

    clock = _Clock()
    replay = _Enc()
    w = _watch(clock, 12, each=lambda s: replay.run(s, 15.5), replay=SimpleNamespace(buffers=[replay]))
    assert w.overloaded() == ["replay buffer"]


def test_the_replay_buffer_and_recording_get_one_step_lighter_together():
    c = SceneCollection(canvases=[Canvas(id="horizontal", width=1920, height=1080)], sources=[], scenes=[])
    c.recording.encoder.preset = "p6"
    assert make_lighter(c, ["recording", "replay buffer"]) == ["recording"]
    assert c.recording.encoder.preset == "p4"  # one step, not two


def test_reading_while_a_worker_adds_samples_never_fails():
    """tick() on a worker, overloaded() on the window's thread: "deque mutated during iteration" on the PC."""
    import threading

    clock = _Clock()
    slow = _Enc()
    w = _watch(clock, 12, each=lambda s: slow.run(s, 15.5), recording=_Recording(slow))
    stop, errors = threading.Event(), []

    def worker():
        while not stop.is_set():  # one frame a tick, 15.5 ms each: always overloaded, whatever the window holds
            slow.frames += 1
            slow.encode_seconds += 0.0155
            clock.now += 1 / slow.fps
            w.tick(recording=_Recording(slow))

    t = threading.Thread(target=worker)
    t.start()
    try:
        for _ in range(3000):
            try:
                assert w.overloaded() == ["recording"]
            except RuntimeError as exc:  # pragma: no cover (what the lock prevents)
                errors.append(exc)
                break
    finally:
        stop.set()
        t.join()
    assert errors == []
