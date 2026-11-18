import threading

from sprigcast.core import crash_test


def test_parse():
    assert crash_test.parse(None) is None
    assert crash_test.parse("") is None
    assert crash_test.parse("soon") is None
    assert crash_test.parse("-1") is None
    assert crash_test.parse("30") == (30.0, False)
    assert crash_test.parse("5,main") == (5.0, True)
    assert crash_test.parse("5, Main") == (5.0, True)


def test_unset_does_nothing(monkeypatch):
    monkeypatch.delenv(crash_test.ENV, raising=False)
    assert crash_test.schedule(lambda ms, fn: None, crash_now=lambda: None) is None


def test_main_thread_goes_to_the_window():
    asked = []
    assert crash_test.schedule(lambda ms, fn: asked.append((ms, fn)), "2,main", crash_now=print) == "main"
    assert asked == [(2000, print)]


def test_thread_crash_runs_on_its_own_thread():
    ran = threading.Event()
    where = []

    def fake():
        where.append(threading.current_thread().name)
        ran.set()

    assert crash_test.schedule(lambda ms, fn: None, "0", crash_now=fake) == "thread"
    assert ran.wait(5)
    assert where == [crash_test.THREAD_NAME]


def test_the_real_crash_is_unguarded():
    assert callable(getattr(crash_test.faulthandler, "_read_null"))
