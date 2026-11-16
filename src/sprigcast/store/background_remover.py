"""Background remover: take the room out from behind you on a camera, no green screen needed.

Add the Background remover filter to a camera (its Filters window) and pick what happens to the room:
Remove (see-through, so whatever is under the camera in the bloom shows), Blur, or a Picture.
"Better edges" uses a bigger model that keeps hair and fingers neater, for a little more work.

How it stays light: a small AI model looks at a small copy of the camera (about 256 x 144), at most 30
times a second, on its own thread. It runs on the graphics card through DirectML (NVIDIA, AMD or Intel),
or on two processor threads when that can't start. The GPU scales its mask up and mixes the picture, as
part of drawing the camera. It only runs while the camera shows on screen, and lets go of the model a
minute after it stops showing. The model downloads the first time it's needed (0.5 MB, or 16 MB for
Better edges) and is checked before use.

Models: Google's MediaPipe Selfie Segmentation (wide and square) and Selfie Multiclass Segmentation,
both Apache 2.0, converted to ONNX (see models/background/README.md in SprigCast's source).

Until the first mask is ready the room is treated as background (hidden, blurred or covered), so it never
flashes up while the model starts.

A SprigCast store plugin: add it from Settings → Plugins.
"""

from __future__ import annotations

import logging
import threading
import time

import numpy as np

from sprigcast.core.filters import Field, register_filter
from sprigcast.plugin_hooks import FrameWatcher, register_frame_watcher

PLUGIN = {"id": "background_remover", "name": "Background remover", "version": 1, "adds": "Effect",
          "about": "Remove, blur or replace the room behind you on a camera. No green screen needed.",
          "new": "New."}

log = logging.getLogger(__name__)

BASE_URL = "https://raw.githubusercontent.com/surakage/sprigcast/main/models/background/"
# name -> (file, SHA-256, bytes, input (width, height), what it gives)
MODELS = {
    "wide": ("selfie-wide-v1.onnx", "929b9aef62cd8dda08c30470c46be56a5f76ab7da998117c22ccb0eef4e3d7b0",
             449_618, (256, 144), "person"),
    "square": ("selfie-square-v1.onnx", "9162bc69b8f87477e2ac6eb02e9e03fe8959aa5e6ed1cd9b66755a371eeac136",
               450_146, (256, 256), "person"),
    "better": ("selfie-multiclass-v1.onnx", "313ccc4234242b0adbc68aa59a8e0a40c80136ece053b821b7559047685a4103",
               16_454_540, (256, 256), "classes"),
}
MAX_FPS = 30.0
SMALL_WIDTH = {"fast": 320, "better": 384}  # the copy the model and the edges work from
RETRY_AFTER_S = 30.0  # after a failed download or start

_STATUS: dict[str, str] = {}  # source id -> how it's doing, for the Filters window


def status(source_id: str) -> str:
    return _STATUS.get(source_id, "Starts when the camera is on screen")


register_filter("background", "Background remover", "video", [
    Field("mode", "Background", "remove", choices=(("remove", "Remove"), ("blur", "Blur"), ("replace", "Picture"))),
    Field("blur", "Blur", 60, 1, 100, 1, "%", show_when=("mode", ("blur",))),
    Field("picture_file", "Picture", "", file_types="Images (*.png *.jpg *.jpeg *.webp)",
          show_when=("mode", ("replace",))),
    Field("better_edges", "Better edges", False),
], status=status)(None)


# Pictures, in numpy (OpenCV costs 90 MB to load, so it isn't used here) ---------------------------------

def shrink(frame: np.ndarray, order: str, width: int) -> np.ndarray:
    """A small RGB uint8 copy of a source frame, taking every k-th pixel. Cheap: for the render thread.

    Frames come as BGR/BGRA/RGBA/RGB arrays, or packed 4:2:0 planes ("I420/709/tv": Y rows, then U | V rows).
    """
    if order.startswith("I420"):
        from sprigcast.render.compositor import planar_layout

        layout = planar_layout(order)
        rows, w = frame.shape[:2]
        h = rows * 2 // 5 if layout[0] == 2 else rows * 2 // 3
        k = max(2, -(-w // max(1, width)) // 2 * 2)  # a step that brings it to about `width` or less (even)
        cw, ch, c = w // 2, h // 2, k // 2
        y = frame[0:h:k, 0:w:k].astype(np.float32)
        u = frame[h:h + ch:c, 0:cw:c].astype(np.float32)
        v = frame[h:h + ch:c, cw:2 * cw:c].astype(np.float32)
        n, m = min(len(y), len(u), len(v)), min(y.shape[1], u.shape[1], v.shape[1])
        y, u, v = y[:n, :m], u[:n, :m], v[:n, :m]
        kr, kb, full = layout[1]
        if full:
            y, u, v = y / 255, (u - 128) / 255, (v - 128) / 255
        else:
            y, u, v = (y - 16) / 219, (u - 128) / 224, (v - 128) / 224
        r = y + 2 * (1 - kr) * v
        b = y + 2 * (1 - kb) * u
        g = (y - kr * r - kb * b) / (1 - kr - kb)
        return (np.clip(np.dstack((r, g, b)), 0, 1) * 255).astype(np.uint8)
    w = frame.shape[1]
    k = max(1, -(-w // max(1, width)))
    small = frame[::k, ::k]
    if small.ndim == 2:
        return np.ascontiguousarray(np.dstack((small, small, small)))
    if order in ("BGR", "BGRA", "BGRX", "pBGRA"):
        return np.ascontiguousarray(small[..., 2::-1])
    return np.ascontiguousarray(small[..., :3])


def resize(a: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    """Bilinear resize of an H x W (x C) float array to (width, height), pixel centres lined up."""
    w, h = size
    ih, iw = a.shape[:2]
    if (iw, ih) == (w, h):
        return a
    ys = np.clip((np.arange(h, dtype=np.float32) + 0.5) * ih / h - 0.5, 0, ih - 1)
    xs = np.clip((np.arange(w, dtype=np.float32) + 0.5) * iw / w - 0.5, 0, iw - 1)
    y0, x0 = ys.astype(np.int32), xs.astype(np.int32)
    y1, x1 = np.minimum(y0 + 1, ih - 1), np.minimum(x0 + 1, iw - 1)
    fy, fx = ys - y0, xs - x0
    if a.ndim == 3:
        fy, fx = fy[:, None, None], fx[None, :, None]
    else:
        fy, fx = fy[:, None], fx[None, :]
    top = a[y0][:, x0] * (1 - fx) + a[y0][:, x1] * fx
    bottom = a[y1][:, x0] * (1 - fx) + a[y1][:, x1] * fx
    return (top * (1 - fy) + bottom * fy).astype(np.float32)


def box(a: np.ndarray, r: int) -> np.ndarray:
    """Mean over a (2r+1) square around each pixel (edges repeat). Running sums: the same cost at any r."""
    if r < 1:
        return a
    n = 2 * r + 1
    # Down the rows, then across the columns.
    c = np.cumsum(np.pad(a, [(r + 1, r)] + [(0, 0)] * (a.ndim - 1), mode="edge"), axis=0, dtype=np.float32)
    a = (c[n:] - c[:-n]) / n
    c = np.cumsum(np.pad(a, [(0, 0), (r + 1, r)] + [(0, 0)] * (a.ndim - 2), mode="edge"), axis=1, dtype=np.float32)
    return (c[:, n:] - c[:, :-n]) / n


def soft_blur(a: np.ndarray, sigma: float) -> np.ndarray:
    """About a Gaussian blur: three box blurs."""
    r = int(round((np.sqrt(4 * sigma * sigma + 1) - 1) / 2))
    for _ in range(3):
        a = box(a, r)
    return a


def guided(guide: np.ndarray, p: np.ndarray, r: int, eps: float) -> np.ndarray:
    """He et al.'s guided filter: the mask p, with its edges moved onto the picture's own edges."""
    mean_i, mean_p = box(guide, r), box(p, r)
    var_i = box(guide * guide, r) - mean_i * mean_i
    cov = box(guide * p, r) - mean_i * mean_p
    a = cov / (var_i + eps)
    b = mean_p - a * mean_i
    return box(a, r) * guide + box(b, r)


def smoothstep(lo: float, hi: float, x: np.ndarray) -> np.ndarray:
    t = np.clip((x - lo) / (hi - lo), 0, 1)
    return t * t * (3 - 2 * t)


def blurred_room(rgb: np.ndarray, person: np.ndarray, amount: float) -> np.ndarray:
    """The background blurred without you smeared into it, at half the small copy's size (RGB uint8)."""
    h, w = rgb.shape[:2]
    size = (max(2, w // 2), max(2, h // 2))
    room = resize(rgb.astype(np.float32), size)
    weight = (1 - resize(person, size)) ** 2
    sigma = (0.004 + max(0.0, min(100.0, amount)) / 100 * 0.03) * size[0]
    num = soft_blur(room * weight[..., None], sigma)
    den = soft_blur(weight, sigma)[..., None]
    out = num / np.maximum(den, 1e-4)
    thin = den < 0.03  # you cover nearly all of this part: no room to average, so blur what's there
    if thin.any():
        out = np.where(thin, soft_blur(room, sigma), out)
    return np.clip(out, 0, 255).astype(np.uint8)


# The model --------------------------------------------------------------------------------------------

class Model:
    """One ONNX model on the graphics card (DirectML), or the processor when that can't start."""

    def __init__(self, path, which: str):
        import onnxruntime as ort

        ort.set_default_logger_severity(3)
        self.which = which
        self.size = MODELS[which][3]
        self.classes = MODELS[which][4] == "classes"
        self.session = None
        self.where = ""
        for provider in ("DmlExecutionProvider", "CPUExecutionProvider"):
            if provider not in ort.get_available_providers():
                continue
            opts = ort.SessionOptions()
            opts.log_severity_level = 3
            if provider == "DmlExecutionProvider":
                opts.enable_mem_pattern = False  # DirectML needs these two
                opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            else:
                opts.intra_op_num_threads = 2  # easy on the PC
                opts.inter_op_num_threads = 1
            try:
                self.session = ort.InferenceSession(str(path), opts, providers=[provider])
            except Exception as exc:  # noqa: BLE001 (an old driver: try the processor)
                log.warning("background remover: %s didn't start: %s", provider, exc)
                continue
            self.where = "graphics card" if provider == "DmlExecutionProvider" else "processor"
            break
        if self.session is None:
            raise RuntimeError("no way to run the model")
        self.input = self.session.get_inputs()[0].name
        log.info("background remover: %s model on the %s", which, self.where)

    def person(self, rgb: np.ndarray) -> np.ndarray:
        """How much each point of the model's grid is a person, 0 .. 1."""
        x = resize(rgb.astype(np.float32) / 255, self.size)
        if self.classes:
            x = x * 2 - 1
        out = self.session.run(None, {self.input: x[None]})[0][0]
        if self.classes:  # background, hair, body, face, clothes, others: you are all but the first
            e = np.exp(out - out.max(axis=-1, keepdims=True))
            return 1 - e[..., 0] / e.sum(axis=-1)
        return out[..., 0]


def model_for(better: bool, aspect: float) -> str:
    if better:
        return "better"
    return "wide" if aspect >= 1.4 else "square"


def load(which: str, source_id: str, cancel: threading.Event) -> Model:
    from sprigcast import models

    file, sha, size, _, _ = MODELS[which]
    name = f"background/{file}"
    if not models.is_ready(name, sha):
        def progress(done: int, total: int) -> None:
            _STATUS[source_id] = f"Downloading {100 * done // total}%" if total else "Downloading"

        _STATUS[source_id] = "Downloading"
        path = models.ensure(name, BASE_URL + file, sha, size, progress=progress, cancel=cancel)
    else:
        path = models.path_for(name)
    _STATUS[source_id] = "Starting"
    return Model(path, which)


class Cutout(FrameWatcher):
    """Watches one camera: small copies in, smooth masks out, on its own thread."""

    def __init__(self, source_id: str, loader=load):
        self.source_id = source_id
        self._loader = loader
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._slot: tuple | None = None
        self._result: dict | None = None
        self._busy = False
        self._offered_at = 0.0
        self._thread: threading.Thread | None = None
        self.model: Model | None = None
        self._failed_at = -RETRY_AFTER_S
        self._failed_for = ""
        self._previous: np.ndarray | None = None
        _STATUS[source_id] = "Starting"

    # Render thread
    def offer(self, frame, channel_order: str, settings: dict) -> None:
        now = time.monotonic()
        if self._busy or now - self._offered_at < 1 / MAX_FPS or self._stop.is_set():
            return
        self._offered_at = now
        width = SMALL_WIDTH["better" if settings.get("better_edges") else "fast"]
        small = shrink(frame, channel_order, width)
        with self._lock:
            self._slot = (small, dict(settings))
            self._busy = True
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="sprigcast-background", daemon=True)
            self._thread.start()
        self._wake.set()

    def take(self) -> dict | None:
        with self._lock:
            result, self._result = self._result, None
        return result

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        _STATUS.pop(self.source_id, None)

    # Its own thread
    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                self._wake.wait()
                self._wake.clear()
                if self._stop.is_set():
                    break
                with self._lock:
                    slot, self._slot = self._slot, None
                if slot is None:
                    continue
                try:
                    result = self.process(*slot)
                except Exception:
                    log.exception("background remover: a frame failed")
                    result = None
                with self._lock:
                    if result is not None:
                        self._result = result
                    self._busy = False
        finally:
            self.model = None  # lets go of the model and its graphics memory
            self._previous = None

    def _model(self, which: str) -> Model | None:
        if self.model is not None and self.model.which == which:
            return self.model
        now = time.monotonic()
        if self._failed_for == which and now - self._failed_at < RETRY_AFTER_S:
            return None
        self.model = None
        try:
            self.model = self._loader(which, self.source_id, self._stop)
        except InterruptedError:
            return None
        except Exception as exc:  # noqa: BLE001 (said in the Filters window; tried again in a while)
            from sprigcast.models import ModelError

            self._failed_at, self._failed_for = now, which
            if isinstance(exc, ModelError):
                _STATUS[self.source_id] = str(exc)
            elif isinstance(exc, ImportError):
                _STATUS[self.source_id] = "This SprigCast can't run it. Update SprigCast."
            else:
                _STATUS[self.source_id] = "It didn't start (it's in the log)."
            log.warning("background remover: %s model failed: %s", which, exc)
            return None
        self._previous = None
        return self.model

    def process(self, small: np.ndarray, settings: dict) -> dict | None:
        better = bool(settings.get("better_edges"))
        h, w = small.shape[:2]
        model = self._model(model_for(better, w / max(1, h)))
        if model is None:
            return None
        person = model.person(small)
        # Snap the mask's edges onto the picture's own edges (hair, fingers, a shoulder against a wall),
        # at the small copy's size: tighter for Better edges, a gentle smoothing of the steps otherwise.
        person = resize(np.clip(person, 0, 1), (w, h))
        gray = small.astype(np.float32).mean(axis=2) / 255
        if better:
            person, (lo, hi) = guided(gray, person, max(2, w // 80), 1e-3), (0.25, 0.75)
        else:
            person, (lo, hi) = guided(gray, person, 3, 1e-2), (0.3, 0.7)
        person = np.clip(person, 0, 1).astype(np.float32)
        previous = self._previous
        if previous is not None and previous.shape == person.shape:
            # Smooth over time: still edges stop shimmering, while a big change (you moved) follows fast.
            change = np.abs(person - previous)
            follow = np.clip(0.35 + change * 0.8, 0, 1)
            person = previous + (person - previous) * follow
        self._previous = person
        mask = smoothstep(lo, hi, person)
        _STATUS[self.source_id] = f"On, using the {model.where}"
        backdrop = None
        if settings.get("mode") == "blur":
            backdrop = blurred_room(small, mask, float(settings.get("blur", 60)))
        return {"mask": (mask * 255 + 0.5).astype(np.uint8), "backdrop": backdrop}


register_frame_watcher("background", Cutout)
