"""A native crash leaves a small minidump in the Logs folder (Windows); old ones are cleared, nothing elsewhere."""

import os
import sys
import time

from sprigcast.app import crash_dump


def test_only_the_newest_dumps_are_kept(tmp_path):
    for i in range(5):
        path = tmp_path / f"crash 2026-11-1{i} 10-00-00 pid{i}.dmp"
        path.write_bytes(b"MDMP")
        os.utime(path, (time.time() - 100 + i, time.time() - 100 + i))
    (tmp_path / "sprigcast 2026-11-16.log").write_text("kept")
    assert crash_dump.prune(tmp_path, keep=3) == 2
    left = sorted(p.name for p in tmp_path.glob("*.dmp"))
    assert left == [f"crash 2026-11-1{i} 10-00-00 pid{i}.dmp" for i in (2, 3, 4)]
    assert (tmp_path / "sprigcast 2026-11-16.log").exists()


def test_dump_names_say_when_and_which_run():
    name = crash_dump.dump_name(now=time.mktime((2026, 11, 16, 18, 54, 58, 0, 0, -1)))
    assert name == f"crash 2026-11-16 18-54-58 pid{os.getpid()}.dmp"


def test_set_up_only_on_windows(tmp_path):
    if sys.platform != "win32":
        assert crash_dump.install(tmp_path) is False


def test_a_real_crash_writes_a_dump_on_windows(tmp_path):
    if sys.platform != "win32":
        return
    import subprocess

    # No "stopped working" box from Windows; then a real access violation on a native thread (one started at a bad
    # address), the way a driver or Qt crashes: ctypes' own calls would catch it, faulthandler's raise() wouldn't
    # reach the filter.
    code = ("import ctypes, pathlib, time; k = ctypes.windll.kernel32; k.SetErrorMode(0x8003); "
            "from sprigcast.app import crash_dump; "
            f"crash_dump.install(pathlib.Path({str(tmp_path)!r})); "
            "k.CreateThread.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p, "
            "ctypes.c_uint32, ctypes.c_void_p]; k.CreateThread(None, 0, 0x10, None, 0, None); time.sleep(20)")
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    subprocess.run([sys.executable, "-c", code], env=env, timeout=60, capture_output=True)
    dumps = list(tmp_path.glob("crash *.dmp"))
    assert len(dumps) == 1 and dumps[0].read_bytes()[:4] == b"MDMP"


def test_the_crash_report_names_the_last_real_fault_not_com_noise():
    """PC 2026-11-18: crash.log held COM's own "code 0x8001010d" lines (handled by COM itself) before the access
    violation that ended the run; the report named the first one."""
    from sprigcast.app.crash_report import SECTION, read_crash_log

    log = (f"{SECTION} 0.2.0 pid 77 started 2026-11-18 12:00:00\n"
           "Windows fatal exception: code 0x8001010d\n\n"
           'Current thread 0x0001 (most recent call first):\n  File "C:\\\\a\\\\qt.py", line 5 in exec\n\n'
           "Windows fatal exception: access violation\n\n"
           'Thread 0x2d80 (most recent call first):\n  File "C:\\\\a\\\\slide_show.py", line 88 in wake\n\n'
           "Windows fatal exception: code 0x8001010d\n\n")
    found = read_crash_log(log, 77)
    assert found.cause == "access violation" and found.where == "slide_show.py, line 88 in wake"


def test_outside_sprigcast_exe_the_launcher_guard_isnt_used(tmp_path):
    assert crash_dump._launcher_guard(str(tmp_path / "x.dmp")) is False  # python.exe (tests): the Python way


def test_crash_notes_pause_on_windows_and_come_back(monkeypatch):
    import faulthandler

    from sprigcast.app import logbook

    calls = []
    monkeypatch.setattr(faulthandler, "is_enabled", lambda: True)
    monkeypatch.setattr(faulthandler, "disable", lambda: calls.append("off"))
    monkeypatch.setattr(faulthandler, "enable", lambda **kw: calls.append("on"))
    with logbook.crash_notes_paused(platform="win32") as paused:
        assert paused and calls == ["off"]
    assert calls == ["off", "on"]
    calls.clear()
    with logbook.crash_notes_paused(platform="linux") as paused:
        assert not paused
    assert calls == []
