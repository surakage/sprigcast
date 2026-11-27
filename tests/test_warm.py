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
        where = pacing._where(pacing.thread_stacks()[worker.ident])
    finally:
        go.set()
        worker.join()
        sys.modules.pop("slow_thing_for_watch", None)
    assert where.startswith("loading slow_thing_for_watch: ") and "slow_thing_for_watch.py:3 <module>" in where
    assert "<frozen" not in where and "test_warm.py" in where


def test_encoders_are_checked_in_the_background_before_recording(monkeypatch):
    from sprigcast.core.scene import EncoderSettings
    from sprigcast.output import recorder

    calls = []
    monkeypatch.setattr(recorder, "pick_codec", lambda: calls.append("pick") or "h264_nvenc")
    monkeypatch.setattr(recorder, "resolve_codec",
                        lambda enc, streaming=False: calls.append(("resolve", streaming)) or
                        ("h264_nvenc" if streaming else enc.encoder))
    monkeypatch.setattr(recorder, "direct_hevc_ok", lambda: calls.append("hevc") or True)
    warm.warm_encoders(EncoderSettings(encoder="hevc_nvenc"), EncoderSettings())
    assert calls == ["pick", ("resolve", False), ("resolve", True), "hevc"]
    calls.clear()
    warm.warm_encoders(EncoderSettings(encoder="h264_nvenc"), None)
    assert calls == ["pick", ("resolve", False)]  # no HEVC check when nothing uses it
    monkeypatch.setattr(recorder, "pick_codec", lambda: 1 / 0)
    warm.warm_encoders(None, None)  # never raises


def test_the_password_store_is_loaded_without_reading_anything(monkeypatch):
    import keyring

    asked = []
    monkeypatch.setattr(keyring, "get_password", lambda *a: asked.append(a))
    warm.warm_keyring()
    assert asked == []
    monkeypatch.setattr(keyring, "get_keyring", lambda: 1 / 0)
    warm.warm_keyring()  # never raises


def test_a_slow_warm_up_names_what_took_it():
    """PC 2026-11-21: the first start of a new build warmed up in 3,698 ms against 566 to 890 ms, and the log
    couldn't say which load it was."""
    from sprigcast.core import warm

    took = [(2100.4, "sprigcast.output.recorder"), (12.0, "av.video.frame"), (600.0, "sprigcast.app.doctor"),
            (3.0, "sprigcast.app.about")]
    assert warm.slowest(took, 900.0) == ""
    assert warm.slowest(took, 3698.0) == "; slowest: output.recorder 2100 ms, app.doctor 600 ms, av.video.frame 12 ms"


def test_keyring_goes_straight_to_windows_credential_manager(monkeypatch):
    """keyring's own search imports every backend, and the macOS one leaves sys.modules again part way: that
    raced Qt's signature code and crashed test build 6 at start (PC 2026-11-27). On Windows the store is named."""
    from sprigcast.core import warm

    monkeypatch.delenv("PYTHON_KEYRING_BACKEND", raising=False)
    monkeypatch.setattr(warm.sys, "platform", "linux")
    warm.windows_store_only()
    assert "PYTHON_KEYRING_BACKEND" not in __import__("os").environ  # elsewhere keyring picks as before
    monkeypatch.setattr(warm.sys, "platform", "win32")
    warm.windows_store_only()
    assert __import__("os").environ["PYTHON_KEYRING_BACKEND"] == "keyring.backends.Windows.WinVaultKeyring"
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "mine.Store")
    warm.windows_store_only()
    assert __import__("os").environ["PYTHON_KEYRING_BACKEND"] == "mine.Store"  # someone's own choice stays


def test_the_windows_store_name_loads():
    """The name must be one keyring can load (a typo would leave keys unreadable on Windows)."""
    import keyring.backends.Windows as win

    from sprigcast.core import warm

    module, _, name = warm.WINDOWS_STORE.rpartition(".")
    assert module == win.__name__ and hasattr(win, name)
