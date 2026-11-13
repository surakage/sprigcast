"""Memory check: `uv run sprigcast-memcheck`. Shows what each part of SprigCast adds to its memory.

It opens SprigCast with a copy of your scenes, then records and runs the replay buffer for a few
seconds each (into a temporary folder that is deleted afterwards), and prints how much memory each
step added. Nothing in your scenes, settings or recordings folder is changed. Paste the output back
so we can see where the memory goes on your PC.

`uv run sprigcast-memcheck --one-canvas` keeps only the first canvas (to compare with a one-canvas OBS).
`uv run sprigcast-memcheck --encoders` instead compares the ways NVENC can reach the GPU.
`uv run sprigcast-memcheck --gpu` shows what the graphics driver keeps in RAM for each kind of texture.
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
        self._last_label = label
        last = self.rows[-2][1] if len(self.rows) > 1 else 0
        extra = f"  Task Manager {_mb(task_manager)}" if task_manager is not None else ""
        print(f"{label:<34}{_mb(info.rss)}  ({'+' if info.rss >= last else '-'}{_mb(abs(info.rss - last)).strip()}){extra}",
              flush=True)

    def private(self) -> float:
        try:
            return self.process.memory_full_info().uss
        except Exception:
            return self.process.memory_info().rss

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
    "delay0": ("no B-frames, no output delay", {"b_frames": 0}, {"delay": "0"}),
    "min": ("no B-frames, delay 0, 2 surfaces", {"b_frames": 0}, {"delay": "0", "surfaces": "2"}),
    "no-aq": ("no B-frames, no adaptive quant", {"b_frames": 0, "adaptive_quant": False}, {}),
}

# What does the GPU connection itself cost? OBS talks to NVENC through Direct3D 11, with no CUDA at all.
GPU_VARIANTS = {
    "one": ("one encoder only (horizontal)", {"b_frames": 0}, {"_canvases": "1"}, {}),
    "lazy": ("CUDA loads its modules lazily", {"b_frames": 0}, {}, {"CUDA_MODULE_LOADING": "LAZY"}),
    "d3d11": ("Direct3D 11 like OBS (no CUDA)", {"b_frames": 0}, {}, {"SPRIGCAST_NVENC_DEVICE": "d3d11"}),
    "d3d1x": ("Direct3D 11, one encoder", {"b_frames": 0}, {"_canvases": "1"}, {"SPRIGCAST_NVENC_DEVICE": "d3d11"}),
}


def _encoder_probe(variant: str, settings: dict | None = None, extra: dict | None = None) -> None:
    """Child process: run two NVENC encoders (both canvases) for a few seconds; print the memory they add."""
    import json

    if variant == "own":
        os.environ["SPRIGCAST_NVENC_DEVICE"] = "own"
    else:
        os.environ.setdefault("SPRIGCAST_NVENC_DEVICE", "cuda")
    extra = dict(extra or {})
    canvases = int(extra.pop("_canvases", 2))
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
        sizes = [(1920, 1080), (1080, 1920)][:canvases]
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
    import av.logging
    import numpy as np

    from sprigcast.output.recorder import _make_hwaccel

    av.logging.set_level(av.logging.VERBOSE)
    with av.logging.Capture(False) as logs:  # FFmpeg's own explanation is usually in its log, not the error
        try:
            ctx = av.CodecContext.create("h264_nvenc", "w", hwaccel=_make_hwaccel())
            ctx.width, ctx.height, ctx.pix_fmt = 256, 256, "nv12"
            ctx.time_base, ctx.framerate = Fraction(1, 30), 30
            frame = av.VideoFrame.from_ndarray(np.zeros((256, 256, 3), dtype=np.uint8), format="rgb24")
            ctx.encode(frame.reformat(format="nv12"))
            ctx.encode(None)
            why = "the test frame failed"
        except Exception as exc:
            why = f"{type(exc).__name__}: {exc}"
    said = [str(line[2]).strip() for line in logs if line[0] <= av.logging.WARNING and str(line[2]).strip()]
    return why + (" | FFmpeg: " + " / ".join(said[-4:]) if said else "")


def compare_encoders() -> None:
    """Run each NVENC setup in its own process and print what two encoders cost in each."""
    import json
    import subprocess

    def run(name: str, label: str, settings: dict, extra: dict, env: dict | None = None) -> None:
        code = f"from sprigcast.tools.memcheck import _encoder_probe; _encoder_probe({name!r}, {settings!r}, {extra!r})"
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120,
                             env={**os.environ, **(env or {})})
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
    print("Today's settings (no B-frames, delay 0, 2 surfaces), other ways to reach the GPU:")
    for name, (label, settings, extra, env) in GPU_VARIANTS.items():
        run(name, label, settings, extra, env)


def _window_steps(meter):
    """Wrap the main window's build steps so their memory can be listed. Returns the list it fills."""
    import functools

    from sprigcast.app import main_window as mw
    from sprigcast.render.engine import Engine

    steps: list[tuple[str, float]] = []
    last = [meter.private()]

    def mark(label: str) -> None:
        now = meter.private()
        steps.append((label, now - last[0]))
        last[0] = now

    def wrap(owner, name: str, label: str) -> None:
        original = getattr(owner, name)

        @functools.wraps(original)
        def wrapped(*args, **kwargs):
            mark("(other window setup)")
            try:
                return original(*args, **kwargs)
            finally:
                mark(label)
        setattr(owner, name, wrapped)

    for name in dir(mw.MainWindow):
        if name.startswith("_build_") or name == "_refresh_all":
            wrap(mw.MainWindow, name, name.strip("_").replace("_", " "))
    original_start = Engine.start

    def start_and_settle(self):
        mark("(other window setup)")
        original_start(self)
        time.sleep(3)  # the renderer sets up on its own thread: give it time so its memory lands here
        mark("renderer start")
    Engine.start = start_and_settle
    wrap(mw.Hotkeys, "__init__", "global hotkeys")
    wrap(mw.ResourceMeter, "__init__", "resource meter")
    steps.append(("start", 0.0))
    return steps, mark


def _encoder_queues(window) -> None:
    """Frames waiting for each encoder: a full queue holds ~3 MB per 1080p frame, so it shows up in RAM."""
    parts = []
    for enc in window.encoders.encoders.values():
        size = enc.out_width * enc.out_height * 3 // 2
        parts.append(f"{enc.canvas_id}: {enc._video.qsize()} waiting (peak {enc.peak_queue}, "
                     f"~{enc.peak_queue * size / 1e6:.0f} MB), {enc.dropped} dropped, "
                     f"longest write stall {enc.longest_stall * 1000:.0f} ms")
    if parts:
        print(f"  (encoder queues: {'; '.join(parts)})", flush=True)


def _report_sources(window, collection) -> None:
    """Each source's state, which camera path is in use, and whether (and why) OpenCV got loaded."""
    from sprigcast.core import lazy_cv2

    names = {s.id: s.name for s in collection.sources}
    for source in list(window.engine._sources.values()):
        backend = getattr(source, "backend", "")
        print(f"  {names.get(source.id, source.id)[:28]:<28} {source.status}{f' (via {backend})' if backend else ''}", flush=True)
        if hasattr(source, "modes_seen"):
            print(f"    opened with: {source.opened_with}", flush=True)
            seen = sorted({f"{m[1]} {m[2]}x{m[3]} {m[4]:g}-{m[5]:g} fps" for m in source.modes_seen})
            print(f"    the camera offers ({len(seen)}): {'; '.join(seen) or 'nothing listed'}", flush=True)
            from sprigcast.sources import camera as cam

            print(f"    listing: {len(cam.listing_lines)} log lines, error: {'; '.join(cam.listing_errors)[:200]}",
                  flush=True)
            for line in cam.listing_lines[:12]:
                print(f"      | {line.rstrip()[:110]}", flush=True)
            for err in source.open_errors[:8]:
                print(f"    tried {err[:150]}", flush=True)
    cv2 = sys.modules.get("cv2")
    if cv2 is None or isinstance(cv2, lazy_cv2._LazyCv2):
        print("  OpenCV: not loaded", flush=True)
    else:
        print(f"  OpenCV: loaded ({lazy_cv2.loaded_by or 'by the camera fallback or a library'})", flush=True)


GPU_EXPERIMENTS = {
    "context": "the OpenGL context itself",
    "targets": "8 x 1080p canvases drawn on the GPU",
    "images": "8 x 1080p images uploaded at once",
    "images-finish": "8 x 1080p images, waiting after each",
    "images-pbo": "8 x 1080p images through one upload buffer",
    "rgba": "screen capture: 1080p RGBA, 120 frames",
    "rgba-pbo": "screen capture through an upload buffer",
    "bgr": "webcam: 1080p BGR (3 bytes), 120 frames",
    "bgra": "webcam as BGRA (4 bytes), 120 frames",
    "readback": "1080p canvas read back 120 times",
}


def _gpu_probe(name: str) -> None:
    """Child process: one GPU experiment in a fresh OpenGL context; prints the private memory it added."""
    import json

    import numpy as np
    import psutil

    from sprigcast.render.compositor import create_context

    process = psutil.Process()

    def private() -> int:
        try:
            return process.memory_full_info().uss
        except Exception:
            return process.memory_info().rss

    if name == "context":
        before = private()
        ctx = create_context()
        ctx.finish()
        print(json.dumps({"added": (private() - before) / 1e6}))
        return
    ctx = create_context()
    w, h = 1920, 1080
    rgba = np.full((h, w, 4), 128, np.uint8)
    keep = []
    ctx.finish()
    before = private()
    if name == "targets":
        for _ in range(8):
            tex = ctx.texture((w, h), 4)
            fbo = ctx.framebuffer(color_attachments=[tex])
            fbo.use()
            fbo.clear(0.1, 0.2, 0.3, 1.0)
            keep.append((tex, fbo))
    elif name.startswith("images"):
        pbo = ctx.buffer(reserve=rgba.nbytes, dynamic=True) if name == "images-pbo" else None
        for _ in range(8):
            tex = ctx.texture((w, h), 4, alignment=1)
            if pbo is not None:
                pbo.write(rgba)
                tex.write(pbo)
            else:
                tex.write(rgba)
            if name == "images-finish":
                ctx.finish()
            keep.append(tex)
    elif name in ("rgba", "rgba-pbo", "bgr", "bgra"):
        components = 3 if name == "bgr" else 4
        frame = np.full((h, w, components), 64, np.uint8)
        tex = ctx.texture((w, h), components, alignment=1)
        pbo = ctx.buffer(reserve=frame.nbytes, dynamic=True) if name == "rgba-pbo" else None
        for i in range(120):
            frame[0, 0, 0] = i
            if pbo is not None:
                pbo.orphan()
                pbo.write(frame)
                tex.write(pbo)
            else:
                tex.write(frame)
            if i % 2:
                ctx.finish()  # a frame tick: the renderer waits for the GPU every frame
    elif name == "readback":
        tex = ctx.texture((w, h), 4)
        fbo = ctx.framebuffer(color_attachments=[tex])
        for _ in range(120):
            fbo.read(components=4, alignment=1)
    ctx.finish()
    print(json.dumps({"added": (private() - before) / 1e6}))


def gpu_costs() -> None:
    """Each GPU experiment in its own process: what the graphics driver adds to SprigCast's private memory."""
    import json
    import subprocess

    print("RAM the graphics driver adds (Task Manager), each test in a fresh process:")
    for name, label in GPU_EXPERIMENTS.items():
        code = f"from sprigcast.tools.memcheck import _gpu_probe; _gpu_probe({name!r})"
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
        lines = [line for line in out.stdout.splitlines() if line.startswith("{")]
        if lines:
            print(f"  {label:<46} +{json.loads(lines[-1])['added']:5.0f} MB", flush=True)
        else:
            print(f"  {label:<46} failed: {(out.stderr.strip().splitlines() or ['no output'])[-1]}", flush=True)


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
    if "--gpu" in sys.argv:
        gpu_costs()
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
    apply_theme(app)
    work = Path(tempfile.mkdtemp(prefix="sprigcast-memcheck-"))
    try:
        real = config_dir() / "scenes.json"
        scenes = work / "scenes.json"
        if real.exists():
            shutil.copy(real, scenes)  # the window may save its copy; your real file is never touched
        collection = load_collection(scenes)
        lean_recording(collection, remember=False)  # what the app will do on its next start
        if "--one-canvas" in sys.argv:  # like OBS: only the first canvas exists, rendered and recorded
            first = next(c for c in collection.canvases if c.enabled)
            for canvas in collection.canvases:
                canvas.enabled = canvas is first
            collection.recording.canvases = [first.id]
            print(f"  (one canvas only: {first.id}; the others are switched off in this copy)", flush=True)
        print(f"  ({len(collection.scenes)} scenes, {len(collection.sources)} sources: "
              f"{', '.join(sorted({s.kind for s in collection.sources}))})", flush=True)
        steps, mark = _window_steps(meter)
        window = MainWindow(collection, scenes, None)
        mark("(other window setup)")
        window._save_layout = lambda: None  # keep your saved dock layout as it is
        window._recordings_folder = lambda: work
        window.show()
        app.processEvents()
        mark("showing the window")

        def wait(seconds: float) -> None:
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                app.processEvents()
                time.sleep(0.01)

        wait(WAIT["window"])
        mark("first seconds running")
        meter.take("Window open, sources running")
        print("  where it went (Task Manager):", flush=True)
        for label, added in steps[1:]:
            if abs(added) >= 2e6:
                print(f"    {label:<30} {'+' if added >= 0 else '-'}{abs(added) / 1e6:.0f} MB", flush=True)
        _report_sources(window, collection)
        from sprigcast.output.recorder import codec_label, nvenc_hwaccel, pick_codec

        codec = pick_codec()
        shared = ", one shared GPU context" if codec == "h264_nvenc" and nvenc_hwaccel() is not None else ""
        print(f"  (recording with {codec_label(codec)}{shared}, canvases: {', '.join(collection.recording.canvases)}, "
              f"{collection.recording.bitrate_kbps} kbps)", flush=True)
        window._start_recording()
        samples = []
        for _ in range(int(WAIT["recording"] // 2)):
            wait(2.0)
            samples.append(f"{meter.private() / 1e6:.0f}")
        print(f"  (Task Manager every 2 s while recording: {', '.join(samples)} MB)", flush=True)
        _encoder_queues(window)
        meter.take("+ recording")
        window._toggle_replay(True)
        samples = []
        for _ in range(int(WAIT["replay"] // 2)):
            wait(2.0)
            samples.append(f"{meter.private() / 1e6:.0f}")
        print(f"  (Task Manager every 2 s with replay on: {', '.join(samples)} MB)", flush=True)
        _encoder_queues(window)
        for enc in window.encoders.encoders.values():
            packets = list(enc._packets)
            in_ram = sum(len(p.data) for p in packets if p.data is not None)
            on_disk = sum(p.size for p in packets if p.data is None)
            print(f"  ({enc.canvas_id}: {len(packets)} packets, {in_ram / 1e6:.1f} MB in RAM, "
                  f"{on_disk / 1e6:.1f} MB in the replay file)", flush=True)
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
