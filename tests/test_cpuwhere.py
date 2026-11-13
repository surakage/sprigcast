import os
import threading
import time

from sprigcast.tools.cpuwhere import CpuWatch
from sprigcast.tools.memcheck import _trace_lines


def test_the_busy_thread_is_named():
    stop = threading.Event()

    def spin():
        while not stop.is_set():
            sum(range(2000))

    worker = threading.Thread(target=spin, name="busy-bee", daemon=True)
    watch = CpuWatch()
    worker.start()
    time.sleep(0.6)
    whole, threads = watch.busiest()
    stop.set()
    worker.join()
    assert whole > 0 and threads[0][0] == "busy-bee" and threads[0][1] > 20
    watch.report("in a test")  # prints, never raises


def test_memory_growth_is_pinned_on_the_line():
    class Meter:  # pretends the second statement of grow() takes 5 MB
        value = 0.0

        def private(self):
            return self.value

    meter = Meter()

    def grow():
        small = 1
        meter.value += 5e6
        return small

    stop, lines = _trace_lines(meter, grow)
    try:
        grow()
    finally:
        stop()
    assert len(lines) == 1 and lines[0][1] == "meter.value += 5e6" and lines[0][2] == 5e6


def test_text_probe_reports_each_step(capsys):
    import json
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from sprigcast.tools.memcheck import TEXT_STEPS, _text_probe

    _text_probe()
    result = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert [step for step, _ in result["steps"]] == [*(s[0] for s in TEXT_STEPS), "styled tab bar"]


def test_light_font_engine_only_on_windows_and_never_over_a_hand_set_platform(monkeypatch):
    from sprigcast.app import theme

    monkeypatch.setattr(theme.sys, "platform", "win32")
    monkeypatch.delenv("QT_QPA_PLATFORM", raising=False)
    monkeypatch.delenv(theme.FONT_ENGINE, raising=False)
    theme.use_light_font_engine()
    assert os.environ["QT_QPA_PLATFORM"] == "windows:fontengine=gdi"
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    theme.use_light_font_engine()
    assert os.environ["QT_QPA_PLATFORM"] == "offscreen"
    monkeypatch.delenv("QT_QPA_PLATFORM")
    monkeypatch.setenv(theme.FONT_ENGINE, "directwrite")
    theme.use_light_font_engine()
    assert "QT_QPA_PLATFORM" not in os.environ
    monkeypatch.setattr(theme.sys, "platform", "linux")
    monkeypatch.delenv(theme.FONT_ENGINE)
    theme.use_light_font_engine()
    assert "QT_QPA_PLATFORM" not in os.environ


def test_weak_pc_flag_and_light_copy(monkeypatch, capsys):
    import sys

    from sprigcast.core.scene import Canvas, Output, SceneCollection
    from sprigcast.tools import memcheck

    monkeypatch.setattr(sys, "argv", ["memcheck"])
    assert memcheck.weak_pc() is None
    monkeypatch.setattr(sys, "argv", ["memcheck", "--weak"])
    assert memcheck.weak_pc() == "libx264"
    monkeypatch.setattr(sys, "argv", ["memcheck", "--weak=h264_mf"])
    assert memcheck.weak_pc() == "h264_mf"
    col = SceneCollection(canvases=[Canvas(id="horizontal", width=1920, height=1080, fps=60)],
                          outputs=[Output(id="tw", type="stream", service="twitch", canvas="horizontal")])
    memcheck._make_light(col)
    assert col.canvases[0].fps == 30 and col.outputs[0].size == (1280, 720)
    assert "light settings" in capsys.readouterr().out
