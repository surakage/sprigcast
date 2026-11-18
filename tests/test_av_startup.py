"""PyAV is imported once, on the main thread, before the threads that use it start (app/main.py import_av)."""

from __future__ import annotations

import inspect
import subprocess
import sys
import textwrap
from pathlib import Path

from sprigcast.app import main

SRC = str(Path(__file__).resolve().parents[1] / "src")


def test_startup_imports_pyav_before_anything_that_uses_it_starts():
    source = inspect.getsource(main.run)
    assert "import_av()" in source
    assert source.index("import_av()") < source.index("MainWindow(")
    assert source.index("import_av()") < source.index("QApplication(")


def test_after_it_many_threads_can_use_pyav_at_once():
    # A fresh process, as at startup: the threads only find PyAV whole if import_av ran first.
    script = textwrap.dedent("""
        import threading
        from sprigcast.app import main
        assert main.import_av()
        errors = []
        go = threading.Barrier(8)

        def use():
            try:
                go.wait()
                import av
                from av.audio.frame import AudioFrame
                from av.video.frame import VideoFrame
                from av.video.reformatter import VideoReformatter
                assert "matroska" in av.formats_available and av.codecs_available
                VideoReformatter()
                out = av.open("pipe:", "w", format="matroska")
                stream = out.add_stream("pcm_s16le", rate=48000)
                frame = AudioFrame(format="s16", layout="stereo", samples=480)
                frame.sample_rate = 48000
                for p in frame.planes:
                    p.update(bytes(p.buffer_size))
                stream.codec_context.encode(frame)
                VideoFrame(16, 16, "yuv420p").reformat(format="rgb24")
            except Exception as exc:
                errors.append(repr(exc))

        threads = [threading.Thread(target=use) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        print("errors:", errors)
        assert not errors
    """)
    done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120,
                          env={**__import__("os").environ, "PYTHONPATH": SRC, "QT_QPA_PLATFORM": "offscreen"})
    assert done.returncode == 0, done.stdout + done.stderr


def test_a_missing_pyav_is_said_once_and_startup_goes_on(monkeypatch, caplog):
    import importlib

    real = importlib.import_module

    def broken(name, *args):
        if name.startswith("av"):
            raise ImportError("no av here")
        return real(name, *args)

    monkeypatch.setattr(importlib, "import_module", broken)
    assert main.import_av() is False
    assert "PyAV could not be loaded" in caplog.text
