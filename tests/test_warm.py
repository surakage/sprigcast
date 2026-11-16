"""Code that recording and going live need is loaded in the background after startup (the PC, 2026-11-16:
a first load froze the window 2.4 s when recording started), and the window watch names a slow import."""

import sys
import threading

from sprigcast.core import pacing, warm


def test_warm_loads_what_is_missing_and_skips_the_rest(monkeypatch):
    monkeypatch.delitem(sys.modules, "sprigcast.output.naming", raising=False)
    loaded = warm.warm(["sprigcast.output.naming", "sys", "sprigcast.no_such_module"], pause_s=0)
    assert loaded == ["sprigcast.output.naming"] and "sprigcast.output.naming" in sys.modules


def test_every_module_on_the_list_exists():
    import importlib.util

    for name in warm.MODULES + warm.WINDOWS_ONLY:
        assert importlib.util.find_spec(name) is not None, name


def test_the_window_watch_names_a_module_being_loaded(tmp_path, monkeypatch):
    (tmp_path / "slow_thing_for_watch.py").write_text("import time\nSTARTED.set()\nGO.wait(5)\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    started, go = threading.Event(), threading.Event()
    import builtins

    monkeypatch.setattr(builtins, "STARTED", started, raising=False)
    monkeypatch.setattr(builtins, "GO", go, raising=False)
    worker = threading.Thread(target=lambda: __import__("slow_thing_for_watch"))
    worker.start()
    try:
        assert started.wait(5)
        where = pacing._where(sys._current_frames()[worker.ident])
    finally:
        go.set()
        worker.join()
        sys.modules.pop("slow_thing_for_watch", None)
    assert where.startswith("loading slow_thing_for_watch: ") and "slow_thing_for_watch.py:3 <module>" in where
    assert "<frozen" not in where and "test_warm.py" in where
