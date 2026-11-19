"""Light-PC test on 2026-11-19: one 720p30 canvas drew at 60 fps, and Twitch came back slowly after the PC's
internet dropped."""

from types import SimpleNamespace

from sprigcast.core.scene import Canvas, drawing_fps
from sprigcast.output.stream import OFFLINE_RETRY, RETRY_MAX, offline, retry_plan
from sprigcast.render.engine import Engine


def test_a_canvas_that_is_off_doesnt_set_the_drawing_rate():
    h = Canvas(id="horizontal", width=1280, height=720, fps=30)
    v = Canvas(id="vertical", width=1080, height=1920, fps=60, enabled=False)
    assert drawing_fps([h, v]) == 30
    v.enabled = True
    assert drawing_fps([h, v]) == 60
    h.enabled = v.enabled = False
    assert drawing_fps([h, v]) == 60  # nothing on: the fastest, as before
    assert drawing_fps([]) == 60


def test_the_native_engine_and_captures_follow_a_new_rate():
    rates = []
    native = SimpleNamespace(failed=False, fps=60, engine=SimpleNamespace(set_rate=lambda n, d: rates.append((n, d))))
    capture, other = SimpleNamespace(canvas_fps=60), SimpleNamespace()
    fake = SimpleNamespace(_native=native, _sources={"cap": capture, "img": other})
    Engine._follow_rate(fake, 30)
    assert rates == [(30, 1)] and native.fps == 30 and capture.canvas_fps == 30
    Engine._follow_rate(fake, 30)
    assert rates == [(30, 1)]  # already there


def test_with_the_internet_down_reconnects_keep_trying_every_few_seconds():
    dns = ("couldn't connect to rtmp://live.twitch.tv/app: [Errno 5] I/O error; last error log: [rtmp] Cannot open "
           "connection tcp://live.twitch.tv:1935 | FFmpeg: Failed to resolve hostname live.twitch.tv: The name does "
           "not resolve for the supplied parameters")
    assert offline(dns) and not offline("[Errno 10054] Error number -10054 occurred")
    delay, waits = 2.0, []
    for _ in range(6):
        hint, wait, delay = retry_plan(dns, 1000, delay)
        waits.append(wait)
    assert "internet" in hint and max(waits) <= OFFLINE_RETRY
    hint, wait, after = retry_plan("[Errno 104] Connection reset by peer", 0, 16.0)
    assert "stream key" in hint and wait == 16.0 and after == RETRY_MAX  # other failures back off as before
