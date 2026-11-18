"""SprigCast ends even if closing gets stuck (core/pacing.exit_guard).

Native simple run (2026-11-18): SprigCast didn't close 15 s after its window was told to, and had to be
killed. The guard runs on faulthandler's own thread, so a thread holding Python's lock can't stop it."""

import subprocess
import sys
import textwrap
import time


def test_a_stuck_close_ends_the_process_with_the_stacks_in_the_log(tmp_path):
    log_file = tmp_path / "sprigcast.log"
    code = textwrap.dedent(f"""
        import logging, time
        logging.basicConfig(filename={str(log_file)!r}, level=logging.INFO)
        from sprigcast.core import pacing
        pacing.exit_guard(0.5)  # not switched on: nothing happens
        assert pacing._closing["at"] is None
        pacing.use_exit_guard()
        pacing.exit_guard(1.0)
        def stuck_in_close():
            while True:
                sum(range(1000))
        stuck_in_close()
    """)
    began = time.monotonic()
    proc = subprocess.run([sys.executable, "-c", code], env={"PYTHONPATH": "src", "PATH": ""}, timeout=30,
                          check=False, capture_output=True)
    assert time.monotonic() - began < 15
    assert proc.returncode != 0, proc.stderr
    text = log_file.read_text(errors="replace")
    assert "still closing" in text and "stuck_in_close" in text
