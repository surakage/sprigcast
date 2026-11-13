"""Records one canvas plus the mixed audio to a video file.

Video is H.264 from NVENC on the GPU, or x264 on the CPU when NVENC is unavailable.
Audio is AAC, 48 kHz stereo, from the mixer. MKV is the default container because a
crash or power cut still leaves a playable file; MP4 is lost if the app dies mid-recording.

Each Recorder owns one thread. The render thread and the mixer hand it frames and audio
blocks through small queues; when a queue is full the item is dropped and counted rather
than ever making the render thread wait.
"""

from __future__ import annotations

import logging
import math
import os
import queue
import threading
import time
from datetime import datetime
from fractions import Fraction
from pathlib import Path

import numpy as np

from sprigcast.sources.audio import SAMPLE_RATE

log = logging.getLogger(__name__)

VIDEO_QUEUE = 15  # frames waiting for the encoder (a quarter second at 60 fps); NV12 at output size, so at most ~45 MB at 1080p
AUDIO_QUEUE = 256  # mixer blocks (about 5 s)
CONTAINERS = {"mkv": "matroska", "mp4": "mp4", "flv": "flv"}

_nvenc_ok: bool | None = None


def nvenc_works() -> bool:
    """True when h264_nvenc can open and encode a frame on this machine. Checked once per run."""
    global _nvenc_ok
    if _nvenc_ok is None:
        _nvenc_ok = _try_encoder("h264_nvenc")
    return _nvenc_ok


_shared_gpu_ok: bool | None = None


def nvenc_hwaccel():
    """Settings that make an NVENC encoder use CUDA's one shared ("primary") context, or None.

    Left to itself, each h264_nvenc encoder creates its own CUDA context, and on Windows each one
    costs about 250 MB of RAM. Recording both canvases plus the replay buffer is four encoders, so
    sharing one context saves most of a gigabyte. The frames are uploaded to the GPU once per encoder
    either way. Checked once per run by encoding a test frame; if that fails (old driver, an FFmpeg
    build without CUDA) every encoder falls back to its own context as before.
    Set SPRIGCAST_NVENC_OWN_CONTEXT=1 to turn this off.
    """
    global _shared_gpu_ok
    if nvenc_device() == "own":
        return None
    if _shared_gpu_ok is None:
        _shared_gpu_ok = nvenc_works() and _try_encoder("h264_nvenc", _make_hwaccel())
    return _make_hwaccel() if _shared_gpu_ok else None


def nvenc_device() -> str:
    """How NVENC encoders reach the GPU (SPRIGCAST_NVENC_DEVICE):
    "cuda" (default): CUDA's one shared context; "d3d11": a Direct3D 11 device, like OBS, no CUDA at all;
    "own": FFmpeg's default, a CUDA context per encoder. `sprigcast-memcheck --encoders` compares them.
    """
    if os.environ.get("SPRIGCAST_NVENC_OWN_CONTEXT"):
        return "own"
    choice = os.environ.get("SPRIGCAST_NVENC_DEVICE", "cuda").strip().lower()
    return choice if choice in ("cuda", "d3d11", "own") else "cuda"


def _make_hwaccel():
    from av.codec.hwaccel import HWAccel

    if nvenc_device() == "d3d11":
        return HWAccel("d3d11va")
    return HWAccel("cuda", options={"primary_ctx": "1"})


def encoder_hwaccel(codec: str):
    """The hwaccel to pass to add_stream() for this codec (only NVENC uses one)."""
    return nvenc_hwaccel() if codec == "h264_nvenc" else None


def _try_encoder(codec_name: str, hwaccel=None) -> bool:
    import av

    if codec_name not in av.codecs_available:
        return False
    try:
        ctx = av.CodecContext.create(codec_name, "w", hwaccel=hwaccel)
        ctx.width, ctx.height = 256, 256
        ctx.pix_fmt = "nv12" if "nvenc" in codec_name else "yuv420p"
        ctx.time_base = Fraction(1, 30)
        ctx.framerate = 30
        frame = av.VideoFrame.from_ndarray(np.zeros((256, 256, 3), dtype=np.uint8), format="rgb24")
        ctx.encode(frame.reformat(format=ctx.pix_fmt))
        ctx.encode(None)
        return True
    except Exception as exc:  # no NVIDIA GPU, old driver, encoder sessions all in use, ...
        log.info("%s unavailable%s: %s", codec_name, " with a shared GPU context" if hwaccel else "", exc)
        return False


def pick_codec() -> str:
    return "h264_nvenc" if nvenc_works() else "libx264"


def codec_label(codec: str) -> str:
    return "NVIDIA NVENC (GPU)" if codec == "h264_nvenc" else "x264 (CPU, NVENC not available)"


def recording_path(folder: Path, canvas_id: str, when: datetime, container: str) -> Path:
    """`SprigCast 2026-11-10 19-30-05 horizontal.mkv`, with a counter if that name is taken."""
    base = f"SprigCast {when:%Y-%m-%d %H-%M-%S} {canvas_id}"
    path = folder / f"{base}.{container}"
    n = 2
    while path.exists():
        path = folder / f"{base} ({n}).{container}"
        n += 1
    return path


X264_PRESETS = {"p1": "ultrafast", "p2": "superfast", "p3": "veryfast", "p4": "veryfast", "p5": "veryfast",
                "p6": "faster", "p7": "fast"}  # CPU encoding: keep it light, games need the CPU too
LOOKAHEAD_FRAMES = 16


def rate_limits(codec: str, enc, bitrate_kbps: int) -> tuple[int, int]:
    """(max rate, buffer size) in kbps that video_options() sets for this bitrate; (0, 0) when none."""
    k = bitrate_kbps
    if enc.rate_control == "cbr":
        return k, k
    if enc.rate_control == "vbr" and codec == "h264_nvenc":
        return int(k * 1.5), k * 2
    return 0, 0


def video_options(codec: str, enc, bitrate_kbps: int, hrd: bool = True) -> dict[str, str]:
    """FFmpeg encoder options for Settings → Output's choices (core.scene.EncoderSettings).

    hrd=False leaves out x264's strict CBR signalling, which would stop the bitrate changing mid-stream.
    """
    k = bitrate_kbps
    if codec == "h264_nvenc":
        opts = {"preset": enc.preset, "tune": enc.tuning, "profile": enc.profile,
                "multipass": enc.multipass, "spatial-aq": "1" if enc.adaptive_quant else "0"}
        if enc.rate_control == "cqp":
            opts.update(rc="constqp", qp=str(enc.cq_level))
        elif enc.rate_control == "cbr":
            opts.update(rc="cbr", maxrate=f"{k}k", bufsize=f"{k}k")
        else:
            opts.update(rc="vbr", maxrate=f"{int(k * 1.5)}k", bufsize=f"{k * 2}k")
        if enc.lookahead:
            opts["rc-lookahead"] = str(LOOKAHEAD_FRAMES)
        elif not getattr(enc, "b_frames", 2):
            # Without B-frames or lookahead NVENC never needs to hold frames back: take each packet as soon
            # as it's done and keep two input surfaces instead of FFmpeg's default pool. Measured on an
            # RTX card: 26 MB less for two 1080p60 encoders, no extra dropped frames.
            opts.update(delay="0", surfaces="2")
        return opts
    opts = {"preset": X264_PRESETS.get(enc.preset, "veryfast"), "profile": enc.profile}
    if enc.tuning in ("ll", "ull"):
        opts["tune"] = "zerolatency"
    if enc.rate_control == "cqp":
        opts["crf"] = str(enc.cq_level)
    elif enc.rate_control == "cbr":
        opts.update({"maxrate": f"{k}k", "bufsize": f"{k}k"})
        if hrd:
            opts["nal-hrd"] = "cbr"
    if not enc.adaptive_quant:
        opts["aq-mode"] = "0"
    return opts


class Recorder:
    """Encodes frames handed to push_frame() and audio handed to push_audio() into one file."""

    def __init__(
        self,
        path: Path,
        width: int,
        height: int,
        fps: int,
        bitrate_kbps: int,
        codec: str,
        start_time: float,
        container: str = "mkv",
        out_size: tuple[int, int] | None = None,
        encoder=None,
    ):
        self.encoder = encoder or self.default_encoder()
        self.path = path
        self.width, self.height, self.fps = width, height, fps  # canvas size; fps of the output
        self.out_width, self.out_height = out_size or (width, height)  # encoded size (scaled if different)
        self.bitrate_kbps = bitrate_kbps
        self.codec = codec
        self.container_kind = container
        self.start_time = start_time  # perf_counter() value that becomes timestamp 0
        self.frames = 0  # frames written
        self.dropped = 0  # frames the encoder could not keep up with
        self.peak_queue = 0  # most frames ever waiting at once (each one is a full frame in RAM)
        self.error: str | None = None
        self.bytes_out = 0  # bytes muxed so far, for the bitrate readout
        self.mux_seconds = 0.0  # time spent handing packets to the file or network
        self.encode_seconds = 0.0  # time spent converting and encoding video frames
        self.longest_stall = 0.0  # longest single wait to hand a packet to the file or network
        self.started_at = time.monotonic()
        self._video: queue.Queue = queue.Queue(VIDEO_QUEUE)
        self._audio: queue.Queue = queue.Queue(AUDIO_QUEUE)
        self._stop = threading.Event()
        self._last_pts = -1
        self._last_queued = -1
        self._samples = 0
        self._force_key = False  # make the next video frame a keyframe (set on the encoder thread)
        self._thread = threading.Thread(target=self._run, name=f"sprigcast-{path.stem}", daemon=True)

    # Called from other threads ---------------------------------------------------

    def start(self) -> None:
        self._thread.start()

    def _slot(self, frame) -> int:
        """The output frame number this render belongs to.

        floor(x + 0.25) rather than round(): at half the render rate (30 from 60) it keeps every
        other frame evenly instead of alternating, and still tolerates a tick arriving a little late.
        """
        return math.floor((frame.time - self.start_time) * self.fps + 0.25)

    def push_frame(self, frame) -> None:
        """frame has .data (RGBA bytes, top row first) and .time (perf_counter when rendered).

        Renders that fall in an output frame slot already taken (a 60 fps canvas feeding a 30 fps
        output) are skipped here, before the queue, so they neither take queue space nor count as drops.
        """
        if self.error is not None or self._stop.is_set():
            return
        slot = self._slot(frame)
        if slot <= self._last_queued:
            return
        self._last_queued = slot
        try:
            self._video.put_nowait(frame)
            self.peak_queue = max(self.peak_queue, self._video.qsize())
        except queue.Full:
            if self._counts_drops():
                self.dropped += 1

    def _counts_drops(self) -> bool:
        return True

    def push_audio(self, block: np.ndarray) -> None:
        """block is float32 of shape (n, 2) at 48 kHz."""
        if self.error is not None or self._stop.is_set():
            return
        try:
            self._audio.put_nowait(block)
        except queue.Full:
            pass

    def stop(self, timeout: float = 15.0) -> None:
        """Finish the file. Waits for frames already queued to be encoded."""
        self._stop.set()
        self._thread.join(timeout)

    @property
    def running(self) -> bool:
        return self._thread.is_alive()

    def stats(self) -> dict:
        """Numbers for the stats panel."""
        total = self.frames + self.dropped
        running = max(1e-6, time.monotonic() - self.started_at)
        return {
            "frames": self.frames,
            "dropped": self.dropped,
            "dropped_pct": 100 * self.dropped / total if total else 0.0,
            "encode_ms": 1000 * self.encode_seconds / max(1, self.frames),
            "budget_ms": 1000 / self.fps,
            "network_pct": 100 * self.mux_seconds / running,
            "longest_stall_s": self.longest_stall,
            "megabytes": self.bytes_out / 1e6,
            "backlog_s": getattr(self, "backlog_seconds", lambda: 0.0)(),
            "size": f"{self.out_width}×{self.out_height}",
            "fps": self.fps,
        }

    def measure_kbps(self) -> float:
        """Average output bitrate since the previous call (call it about once a second)."""
        now, sent = time.monotonic(), self.bytes_out
        last_t, last_b = getattr(self, "_measure", (now, sent))
        self._measure = (now, sent)
        return 0.0 if now <= last_t else (sent - last_b) * 8 / 1000 / (now - last_t)

    # Recorder thread ---------------------------------------------------------------

    def _open_container(self):
        import av

        self.path.parent.mkdir(parents=True, exist_ok=True)
        return av.open(str(self.path), "w", format=CONTAINERS[self.container_kind])

    @staticmethod
    def default_encoder():
        from sprigcast.core.scene import EncoderSettings

        return EncoderSettings(rate_control="vbr")

    def _video_options(self) -> dict[str, str]:
        return video_options(self.codec, self.encoder, self.bitrate_kbps)

    def _open(self):
        from av.video.reformatter import VideoReformatter

        container = self._open_container()
        video = container.add_stream(self.codec, rate=self.fps, hwaccel=encoder_hwaccel(self.codec))
        video.width, video.height = self.out_width, self.out_height
        video.pix_fmt = "nv12"  # what the GPU hands over (render/compositor.py read_nv12); x264 takes it too
        video.bit_rate = self.bitrate_kbps * 1000
        ctx = video.codec_context
        ctx.time_base = Fraction(1, self.fps)
        ctx.gop_size = self.fps * (self.encoder.keyint_sec or 2)  # 2 s is what streaming sites expect
        ctx.max_b_frames = max(0, min(4, self.encoder.b_frames))
        # Tag the colours so players read them as HD video (BT.709, limited range).
        ctx.colorspace, ctx.color_primaries, ctx.color_trc, ctx.color_range = 1, 1, 1, 1
        video.options = self._video_options()
        audio = container.add_stream("aac", rate=SAMPLE_RATE)
        audio.layout = "stereo"
        audio.bit_rate = self.encoder.audio_kbps * 1000
        self._reformatter = VideoReformatter()
        return container, video, audio

    def _run(self) -> None:
        try:
            self._session()
        except Exception as exc:
            log.exception("recording %s failed", self.path)
            self.error = str(exc) or exc.__class__.__name__

    def _session(self) -> None:
        """Open the output, encode until stop() is called, then flush and close. Raises on failure."""
        container, finished = None, False
        try:
            container, video, audio = self._open()
            while True:
                stopping = self._stop.is_set()
                self._write_audio(container, audio)
                try:
                    frame = self._video.get(timeout=0.05)
                except queue.Empty:
                    if stopping:
                        break
                    continue
                self._write_video(container, video, frame)
            self._write_audio(container, audio)
            for stream in (video, audio):
                self._mux(container, stream.encode(None))
            finished = True
        finally:
            if container is not None and self._before_close(container):
                try:
                    container.close()
                except Exception as exc:
                    if finished:
                        raise  # the file's ending could not be written (disk full, drive removed)
                    log.warning("closing %s failed: %s", self.path, exc)

    def _before_close(self, container) -> bool:
        """Hook for subclasses that write from another thread. Return False to skip closing."""
        return True

    def _mux(self, container, packets) -> None:
        for packet in packets:
            began = time.perf_counter()
            container.mux(packet)
            waited = time.perf_counter() - began
            self.mux_seconds += waited
            self.longest_stall = max(self.longest_stall, waited)
            self.bytes_out += packet.size

    @property
    def gpu_size(self) -> tuple[int, int] | None:
        """The size to ask the engine for NV12 frames at (converted and scaled on the GPU); None = RGBA."""
        if os.environ.get("SPRIGCAST_CPU_CONVERT"):
            return None  # escape hatch: convert on the CPU as before, in case a driver gets NV12 wrong
        w, h = self.out_width, self.out_height
        return (w, h) if w % 2 == 0 and h % 2 == 0 else None

    def _nv12_frame(self, frame):
        """An av.VideoFrame from the GPU's NV12 bytes, copying row by row only if the planes are padded."""
        import av

        w, h = frame.width, frame.height
        vf = av.VideoFrame(w, h, "nv12")
        y_size = w * h
        data = memoryview(frame.data)  # slices of a memoryview don't copy
        for plane, data, rows in ((vf.planes[0], data[:y_size], h), (vf.planes[1], data[y_size:], h // 2)):
            if plane.line_size == w:
                plane.update(data)
            else:
                padded = np.zeros((rows, plane.line_size), dtype=np.uint8)
                padded[:, :w] = np.frombuffer(data, dtype=np.uint8).reshape(rows, w)
                plane.update(padded.tobytes())
        return vf

    def _write_video(self, container, stream, frame) -> None:
        import av
        from av.video.reformatter import ColorRange, Colorspace

        pts = self._slot(frame)
        if pts <= self._last_pts:
            return  # rendered before the start, or a second tick in the same output frame slot
        self._last_pts = pts
        began, muxed = time.perf_counter(), self.mux_seconds
        if getattr(frame, "fmt", "rgba") == "nv12" and (frame.width, frame.height) == (self.out_width, self.out_height):
            vf = self._nv12_frame(frame)  # already converted and scaled on the GPU: nothing to do here
            vf.pts = pts
            self._mux(container, self._encode_video(stream, vf))
            self.frames += 1
            self.encode_seconds += (time.perf_counter() - began) - (self.mux_seconds - muxed)
            return
        vf = av.VideoFrame(self.width, self.height, "rgba")
        if vf.planes[0].line_size == self.width * 4:
            vf.planes[0].update(frame.data)  # one memcpy: about 3x faster than going through NumPy at 1080p
        else:
            rgba = np.frombuffer(frame.data, dtype=np.uint8).reshape(self.height, self.width, 4)
            vf = av.VideoFrame.from_ndarray(rgba, format="rgba")
        vf = self._reformatter.reformat(
            vf,
            width=self.out_width,
            height=self.out_height,
            interpolation="BICUBIC",
            format="nv12",  # not stream.pix_fmt: with a shared GPU context that reads "cuda" once open
            dst_colorspace=Colorspace.ITU709,
            dst_color_range=ColorRange.MPEG,
        )
        vf.pts = pts
        self._mux(container, self._encode_video(stream, vf))
        self.frames += 1
        # Time spent converting and encoding this frame, not counting waiting on the file or network.
        self.encode_seconds += (time.perf_counter() - began) - (self.mux_seconds - muxed)

    def _encode_video(self, stream, vf):
        if self._force_key:
            from av.video.frame import PictureType

            vf.pict_type = PictureType.I
            self._force_key = False
        return stream.encode(vf)

    def _write_audio(self, container, stream) -> None:
        import av

        for _ in range(self._audio.qsize()):  # only what is queued now, so audio can never starve video
            try:
                block = self._audio.get_nowait()
            except queue.Empty:
                return
            planar = np.ascontiguousarray(block.T, dtype=np.float32)  # (2, n)
            af = av.AudioFrame.from_ndarray(planar, format="fltp", layout="stereo")
            af.sample_rate = SAMPLE_RATE
            af.pts = self._samples
            self._samples += block.shape[0]
            self._mux(container, stream.encode(af))


class RecordingSession:
    """Records the chosen canvases at once, each to its own file, sharing the mixed audio."""

    def __init__(self, engine, recorders: list[tuple[str, Recorder]]):
        self.engine = engine
        self.recorders = recorders  # (canvas id, recorder)
        self.started_at = time.monotonic()

    @classmethod
    def start(
        cls,
        engine,
        canvases: list,
        folder: Path,
        bitrate_kbps: int,
        container: str = "mkv",
        codec: str | None = None,
        encoder=None,
    ) -> "RecordingSession":
        codec = codec or pick_codec()
        now = datetime.now()
        start_time = time.perf_counter()
        recorders = []
        for canvas in canvases:
            path = recording_path(folder, canvas.id, now, container)
            rec = Recorder(path, canvas.width, canvas.height, canvas.fps, bitrate_kbps, codec, start_time, container,
                           encoder=encoder)
            rec.start()
            recorders.append((canvas.id, rec))
        session = cls(engine, recorders)
        for canvas_id, rec in recorders:
            engine.add_frame_tap(canvas_id, rec.push_frame, rec.gpu_size)
            engine.mixer.add_tap(rec.push_audio)
        return session

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    @property
    def dropped(self) -> int:
        return sum(rec.dropped for _, rec in self.recorders)

    @property
    def errors(self) -> list[str]:
        return [f"{canvas_id}: {rec.error}" for canvas_id, rec in self.recorders if rec.error]

    @property
    def paths(self) -> list[Path]:
        return [rec.path for _, rec in self.recorders]

    def stop(self) -> None:
        for canvas_id, rec in self.recorders:
            self.engine.remove_frame_tap(canvas_id, rec.push_frame)
            self.engine.mixer.remove_tap(rec.push_audio)
        for _, rec in self.recorders:
            rec.stop()
