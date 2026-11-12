"""Memory check: `uv run sprigcast-memcheck`. Shows what each part of SprigCast adds to its memory.

It opens SprigCast with a copy of your scenes, then records and runs the replay buffer for a few
seconds each (into a temporary folder that is deleted afterwards), and prints how much memory each
step added. Nothing in your scenes, settings or recordings folder is changed. Paste the output back
so we can see where the memory goes on your PC.

`uv run sprigcast-memcheck --encoders` instead compares the ways NVENC can reach the GPU.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

WAIT = {"window": 6.0, "recording": 8.0, "replay": 10.0, "stopped": 4.0}


def _mb(n: float) -> str:
    return f"{n / 1e6:7.0f} MB"


class _Meter:
    def __init__(self) -> None:
        import psutil

        self.process = psutil.Process(os.getpid())
        self.rows: list[tuple[str, float, float | None]] = []

    def take(self, label: str) -> None:
        info = self.process.memory_info()
        private = getattr(info, "private", None)  # Windows: memory only SprigCast uses (no shared DLLs)
        self.rows.append((label, info.rss, private))
        last = self.rows[-2][1] if len(self.rows) > 1 else 0
        extra = f"  private {_mb(private)}" if private is not None else ""
        print(f"{label:<34}{_mb(info.rss)}  ({'+' if info.rss >= last else '-'}{_mb(abs(info.rss - last)).strip()}){extra}",
              flush=True)

    def biggest_files(self, count: int = 12) -> None:
        """The DLLs / libraries taking the most memory: GPU drivers, Qt, FFmpeg."""
        try:
            maps = self.process.memory_maps(grouped=True)
        except Exception as exc:  # not allowed or not supported here
            print(f"(couldn't list loaded libraries: {exc})")
            return
        files = sorted((m for m in maps if getattr(m, "path", "") and os.path.isfile(m.path)),
                       key=lambda m: m.rss, reverse=True)
        print("\nBiggest loaded libraries:")
        for m in files[:count]:
            print(f"  {_mb(m.rss)}  {Path(m.path).name}")


ENCODER_VARIANTS = {
    "own": "FFmpeg default (a CUDA context per encoder)",
    "cuda": "one shared CUDA context (current default)",
    "d3d11": "Direct3D 11, no CUDA (like OBS)",
}


def _encoder_probe(variant: str) -> None:
    """Child process: run two NVENC encoders (both canvases) for a few seconds; print the memory they add."""
    import json

    os.environ["SPRIGCAST_NVENC_DEVICE"] = variant
    import psutil

    from sprigcast.output.recorder import Recorder, nvenc_hwaccel, nvenc_works
    from sprigcast.render.engine import Frame

    process = psutil.Process()
    if not nvenc_works():
        print(json.dumps({"error": "NVENC not available"}))
        return
    if variant != "own" and nvenc_hwaccel() is None:
        print(json.dumps({"error": "this setup doesn't work on this PC"}))
        return
    before = process.memory_info()
    work = Path(tempfile.mkdtemp(prefix="sprigcast-enc-"))
    try:
        start = time.perf_counter()
        sizes = [(1920, 1080), (1080, 1920)]
        recs = [Recorder(work / f"{i}.mkv", w, h, 60, 12000, "h264_nvenc", start) for i, (w, h) in enumerate(sizes)]
        for rec in recs:
            rec.start()
        frames = {size: bytes(size[0] * size[1] * 3 // 2) for size in sizes}
        for n in range(240):  # 4 s at 60 fps
            t = start + n / 60
            while time.perf_counter() < t:
                time.sleep(0.001)
            for rec, size in zip(recs, sizes):
                rec.push_frame(Frame("c", size[0], size[1], frames[size], t, "nv12"))
        during = process.memory_info()
        for rec in recs:
            rec.stop()
        errors = [rec.error for rec in recs if rec.error]
        print(json.dumps({
            "ram": (during.rss - before.rss) / 1e6,
            "private": (getattr(during, "private", 0) - getattr(before, "private", 0)) / 1e6,
            "dropped": sum(rec.dropped for rec in recs),
            "error": "; ".join(errors),
        }))
    finally:
        shutil.rmtree(work, ignore_errors=True)


def compare_encoders() -> None:
    """Run each NVENC setup in its own process and print what two encoders cost in each."""
    import json
    import subprocess

    print("Two NVENC encoders (1080p60 horizontal + vertical), memory they add:")
    for variant, label in ENCODER_VARIANTS.items():
        out = subprocess.run([sys.executable, "-c", f"from sprigcast.tools.memcheck import _encoder_probe; _encoder_probe({variant!r})"],
                             capture_output=True, text=True, timeout=120)
        lines = [line for line in out.stdout.splitlines() if line.startswith("{")]
        result = json.loads(lines[-1]) if lines else {"error": (out.stderr.strip().splitlines() or ["no output"])[-1]}
        if result.get("error"):
            print(f"  {variant:<6} {label:<45} failed: {result['error']}")
        else:
            print(f"  {variant:<6} {label:<45} +{result['ram']:5.0f} MB  private +{result['private']:5.0f} MB"
                  f"  dropped {result['dropped']}", flush=True)


def main() -> None:
    if "--encoders" in sys.argv:
        compare_encoders()
        return
    from PySide6.QtCore import QCoreApplication, Qt

    meter = _Meter()
    meter.take("Python started")
    QCoreApplication.setApplicationName("SprigCast")
    QCoreApplication.setOrganizationName("Slothbloom")
    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    import av  # noqa: F401
    import moderngl  # noqa: F401
    import numpy  # noqa: F401

    from sprigcast.app.main import config_dir, load_collection
    from sprigcast.app.main_window import MainWindow
    from sprigcast.app.theme import apply_theme

    meter.take("Qt, FFmpeg and SprigCast loaded")
    apply_theme(app)
    work = Path(tempfile.mkdtemp(prefix="sprigcast-memcheck-"))
    try:
        real = config_dir() / "scenes.json"
        scenes = work / "scenes.json"
        if real.exists():
            shutil.copy(real, scenes)  # the window may save its copy; your real file is never touched
        collection = load_collection(scenes)
        print(f"  ({len(collection.scenes)} scenes, {len(collection.sources)} sources: "
              f"{', '.join(sorted({s.kind for s in collection.sources}))})", flush=True)
        window = MainWindow(collection, scenes, None)
        window._save_layout = lambda: None  # keep your saved dock layout as it is
        window._recordings_folder = lambda: work
        window.show()

        def wait(seconds: float) -> None:
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                app.processEvents()
                time.sleep(0.01)

        wait(WAIT["window"])
        meter.take("Window open, sources running")
        from sprigcast.output.recorder import codec_label, nvenc_hwaccel, pick_codec

        codec = pick_codec()
        shared = ", one shared GPU context" if codec == "h264_nvenc" and nvenc_hwaccel() is not None else ""
        print(f"  (recording with {codec_label(codec)}{shared}, canvases: {', '.join(collection.recording.canvases)}, "
              f"{collection.recording.bitrate_kbps} kbps)", flush=True)
        window._start_recording()
        wait(WAIT["recording"])
        meter.take("+ recording")
        window._toggle_replay(True)
        wait(WAIT["replay"])
        meter.take(f"+ replay buffer ({collection.recording.replay_seconds} s)")
        window._toggle_replay(False)
        window._stop_recording()
        wait(WAIT["stopped"])
        meter.take("Recording and replay stopped")
        meter.biggest_files()
        window.hotkeys.stop()
        window.meter.stop()
        window.timer.stop()
        window.engine.stop()
    finally:
        shutil.rmtree(work, ignore_errors=True)
    sys.stdout.flush()
    os._exit(0)  # skip Qt's teardown: nothing left to save


if __name__ == "__main__":
    main()
