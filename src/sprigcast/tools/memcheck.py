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
        try:
            task_manager = self.process.memory_full_info().uss  # Windows: private working set, Task Manager's "Memory"
        except Exception:
            task_manager = None
        self.rows.append((label, info.rss, task_manager))
        last = self.rows[-2][1] if len(self.rows) > 1 else 0
        extra = f"  Task Manager {_mb(task_manager)}" if task_manager is not None else ""
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
    "own": ("FFmpeg default (a CUDA context per encoder)", {}, {}),
    "cuda": ("one shared CUDA context (current default)", {}, {}),
}
# Same shared context, different NVENC settings: which ones cost memory?
SETTING_VARIANTS = {
    "no-b": ("no B-frames", {"b_frames": 0}, {}),
    "p1": ("fastest preset (p1)", {"preset": "p1"}, {}),
    "ll": ("low-latency tuning", {"tuning": "ll"}, {}),
    "surf4": ("4 input surfaces", {}, {"surfaces": "4"}),
    "lean": ("no B-frames + p4 + 4 surfaces", {"b_frames": 0, "preset": "p4"}, {"surfaces": "4"}),
}


def _encoder_probe(variant: str, settings: dict | None = None, extra: dict | None = None) -> None:
    """Child process: run two NVENC encoders (both canvases) for a few seconds; print the memory they add."""
    import json

    os.environ["SPRIGCAST_NVENC_DEVICE"] = "own" if variant == "own" else "cuda"
    import psutil

    from sprigcast.output.recorder import Recorder, nvenc_hwaccel, nvenc_works
    from sprigcast.render.engine import Frame

    process = psutil.Process()

    def task_manager() -> int:
        try:
            return process.memory_full_info().uss
        except Exception:
            return process.memory_info().rss

    if not nvenc_works():
        print(json.dumps({"error": "NVENC not available"}))
        return
    if variant != "own" and nvenc_hwaccel() is None:
        print(json.dumps({"error": _why_not(variant)}))
        return

    class Probe(Recorder):
        def _video_options(self):
            return {**super()._video_options(), **(extra or {})}

    encoder = Recorder.default_encoder().model_copy(update=settings or {})
    before = (process.memory_info().rss, task_manager())
    work = Path(tempfile.mkdtemp(prefix="sprigcast-enc-"))
    try:
        start = time.perf_counter()
        sizes = [(1920, 1080), (1080, 1920)]
        recs = [Probe(work / f"{i}.mkv", w, h, 60, 12000, "h264_nvenc", start, encoder=encoder)
                for i, (w, h) in enumerate(sizes)]
        for rec in recs:
            rec.start()
        frames = {size: bytes(size[0] * size[1] * 3 // 2) for size in sizes}
        for n in range(240):  # 4 s at 60 fps
            t = start + n / 60
            while time.perf_counter() < t:
                time.sleep(0.001)
            for rec, size in zip(recs, sizes):
                rec.push_frame(Frame("c", size[0], size[1], frames[size], t, "nv12"))
        during = (process.memory_info().rss, task_manager())
        for rec in recs:
            rec.stop()
        errors = [rec.error for rec in recs if rec.error]
        print(json.dumps({
            "ram": (during[0] - before[0]) / 1e6,
            "task_manager": (during[1] - before[1]) / 1e6,
            "dropped": sum(rec.dropped for rec in recs),
            "error": "; ".join(errors),
        }))
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _why_not(variant: str) -> str:
    """The error a test frame gives with this setup, for the report."""
    from fractions import Fraction

    import av
    import numpy as np

    from sprigcast.output.recorder import _make_hwaccel

    try:
        ctx = av.CodecContext.create("h264_nvenc", "w", hwaccel=_make_hwaccel())
        ctx.width, ctx.height, ctx.pix_fmt = 256, 256, "nv12"
        ctx.time_base, ctx.framerate = Fraction(1, 30), 30
        frame = av.VideoFrame.from_ndarray(np.zeros((256, 256, 3), dtype=np.uint8), format="rgb24").reformat(format="nv12")
        ctx.encode(frame)
        ctx.encode(None)
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    return "the test frame failed"


def compare_encoders() -> None:
    """Run each NVENC setup in its own process and print what two encoders cost in each."""
    import json
    import subprocess

    def run(name: str, label: str, settings: dict, extra: dict) -> None:
        code = f"from sprigcast.tools.memcheck import _encoder_probe; _encoder_probe({name!r}, {settings!r}, {extra!r})"
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
        lines = [line for line in out.stdout.splitlines() if line.startswith("{")]
        result = json.loads(lines[-1]) if lines else {"error": (out.stderr.strip().splitlines() or ["no output"])[-1]}
        if result.get("error"):
            print(f"  {name:<6} {label:<45} failed: {result['error']}", flush=True)
        else:
            print(f"  {name:<6} {label:<45} +{result['ram']:5.0f} MB  Task Manager +{result['task_manager']:5.0f} MB"
                  f"  dropped {result['dropped']}", flush=True)

    print("Two NVENC encoders (1080p60 horizontal + vertical), memory they add:")
    for name, (label, settings, extra) in ENCODER_VARIANTS.items():
        run(name, label, settings, extra)
    print("With the shared context, by setting:")
    for name, (label, settings, extra) in SETTING_VARIANTS.items():
        run(name, label, settings, extra)


def _report_sources(window, collection) -> None:
    """Each source's state, which camera path is in use, and whether (and why) OpenCV got loaded."""
    from sprigcast.core import lazy_cv2

    names = {s.id: s.name for s in collection.sources}
    for source in list(window.engine._sources.values()):
        backend = getattr(source, "backend", "")
        print(f"  {names.get(source.id, source.id)[:28]:<28} {source.status}{f' (via {backend})' if backend else ''}", flush=True)
    cv2 = sys.modules.get("cv2")
    if cv2 is None or isinstance(cv2, lazy_cv2._LazyCv2):
        print("  OpenCV: not loaded", flush=True)
    else:
        print(f"  OpenCV: loaded ({lazy_cv2.loaded_by or 'by the camera fallback or a library'})", flush=True)


def _source_costs(window, collection, meter, wait) -> None:
    """Switch the sources off one at a time (in the engine only) and show what each frees."""
    from sprigcast.core.events import ReplaceCollection

    print("\nSwitching sources off one at a time (your scenes aren't changed):", flush=True)
    order = ["camera", "display_capture", "window_capture", "image", "text", "audio_input", "audio_output"]
    remaining = list(collection.sources)
    for source in sorted(collection.sources, key=lambda s: order.index(s.kind) if s.kind in order else 99):
        remaining = [s for s in remaining if s.id != source.id]
        window.engine.send(ReplaceCollection(collection.model_copy(update={"sources": remaining}, deep=True)))
        wait(2.5)
        meter.take(f"  - {source.name[:20]} ({source.kind})")


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

    from sprigcast.app.main import config_dir, lean_recording, load_collection
    from sprigcast.app.main_window import MainWindow
    from sprigcast.app.theme import apply_theme

    meter.take("Qt, FFmpeg and SprigCast loaded")
    from sprigcast.render.compositor import create_context

    probe = create_context()  # what the graphics driver alone costs (the engine makes its own below)
    meter.take("OpenGL context (driver)")
    probe.release()
    apply_theme(app)
    work = Path(tempfile.mkdtemp(prefix="sprigcast-memcheck-"))
    try:
        real = config_dir() / "scenes.json"
        scenes = work / "scenes.json"
        if real.exists():
            shutil.copy(real, scenes)  # the window may save its copy; your real file is never touched
        collection = load_collection(scenes)
        lean_recording(collection, remember=False)  # what the app will do on its next start
        print(f"  ({len(collection.scenes)} scenes, {len(collection.sources)} sources: "
              f"{', '.join(sorted({s.kind for s in collection.sources}))})", flush=True)
        from sprigcast.render.engine import Engine

        renderer = Engine(collection.model_copy(update={"sources": []}, deep=True))
        renderer.start()  # renderer on its own: GL context, canvases, previews at full size, no sources or UI
        time.sleep(4)
        meter.take("Renderer alone (no sources/UI)")
        renderer.stop()
        time.sleep(1)
        meter.take("Renderer stopped")
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
        _report_sources(window, collection)
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
        _source_costs(window, collection, meter, wait)
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
