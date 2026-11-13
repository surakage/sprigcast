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
