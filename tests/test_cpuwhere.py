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
