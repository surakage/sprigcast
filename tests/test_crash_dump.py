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
