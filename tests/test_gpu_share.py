"""Frames handed to SprigCast's NVENC on the graphics card (render/gpu_share.py).

There's no Direct3D or NVIDIA card in CI: the texture pool is stood in for by plain lists, and the encoder by
x264 (as in test_direct_video.py), reading each "texture" from a dictionary. What's checked here is everything
around the card: who gets frames on the card, that every texture goes back to its pool, and that any failure
falls back to frames through memory for the rest of the run.
"""

import gc
import threading
import time
from types import SimpleNamespace

import av
import pytest

from sprigcast.output import nvenc_d3d11, recorder
from sprigcast.output.recorder import VIDEO_QUEUE, Recorder
from sprigcast.render import gpu_share
from sprigcast.render.engine import Engine, Frame, _wants_gpu

pytestmark = pytest.mark.skipif("libx264" not in av.codecs_available, reason="needs libx264 in PyAV")

PICTURES: dict = {}  # (pool id, index) -> the NV12 bytes "on the card"


def _pool(size=(320, 180)):
    pool = gpu_share.Pool.__new__(gpu_share.Pool)
    pool.id = next(gpu_share._ids)
    pool.size = size
    pool.textures = [SimpleNamespace(value=1000 + i) for i in range(gpu_share.POOL_SIZE)]
    pool.views = []
    pool._free = list(range(gpu_share.POOL_SIZE))
    pool._lock = threading.Lock()
    return pool


def _hand(pool, i, w=320, h=180, start=1000.0, fps=30):
    index = pool.take()
    assert index is not None, "the pool ran dry: a texture wasn't given back"
    PICTURES[(pool.id, index)] = bytes([(i * 8) % 256]) * (w * h) + bytes([128]) * (w * h // 2)
    return gpu_share.GpuFrame("horizontal", w, h, start + i / fps, pool, index)


@pytest.fixture
def card(monkeypatch):
    from test_direct_video import StandInEncoder

    class OnCard(StandInEncoder):
        fail_at = None

        def encode_texture(self, frame, keyframe=False):
            assert self.settings.get("shared") is True
            if OnCard.fail_at is not None and self.n >= OnCard.fail_at:
                raise nvenc_d3d11.NvencError("NVENC mapping a texture failed: MAP_FAILED")
            return self.encode_frame(PICTURES[frame.key], keyframe)

    OnCard.made = StandInEncoder.made = []
    monkeypatch.setattr(nvenc_d3d11, "D3D11Encoder", OnCard)
    monkeypatch.setattr(recorder, "direct_nvenc_ok", lambda: True)
    monkeypatch.setattr(gpu_share, "turned_off", lambda: False)
    monkeypatch.setattr(gpu_share, "_gl_ok", True)
    monkeypatch.setattr(gpu_share, "_broken", None)
    return OnCard


def _recorder(tmp_path, name="g.mkv"):
    from sprigcast.core.scene import EncoderSettings

    return Recorder(tmp_path / name, 320, 180, 30, 3000, "h264_nvenc", 1000.0, "mkv",
                    encoder=EncoderSettings(rate_control="cbr", b_frames=0))


def _wait_queue(rec):
    while rec._video.qsize() > 1:
        time.sleep(0.001)


def test_a_recording_takes_its_frames_on_the_card_and_gives_every_texture_back(tmp_path, card):
    pool = _pool()
    rec = _recorder(tmp_path)
    assert not rec.wants_gpu_frames()  # nothing open yet: frames through memory until the encoder is ready
    rec.start()
    assert rec._opened.wait(10)
    assert rec.wants_gpu_frames() and card.made[0].settings["shared"] is True
    for i in range(45):
        rec.push_frame(_hand(pool, i))
        _wait_queue(rec)
    rec.stop()
    assert rec.error is None and rec.frames == 45 and rec.dropped == 0
    assert not rec.wants_gpu_frames()  # finished: nothing more on the card
    gc.collect()
    assert pool.in_use() == 0  # every texture back, the last frame kept for repeats too
    with av.open(str(tmp_path / "g.mkv")) as f:
        assert sum(1 for _ in f.decode(video=0)) == 45


def test_a_refused_texture_turns_sharing_off_and_frames_come_through_memory(tmp_path, card):
    card.fail_at = 10
    pool = _pool()
    rec = _recorder(tmp_path)
    rec.start()
    assert rec._opened.wait(10)
    for i in range(20):
        frame = _hand(pool, i) if rec.wants_gpu_frames() else \
            Frame("horizontal", 320, 180, PICTURES[(pool.id, 0)], 1000.0 + i / 30, "nv12")
        rec.push_frame(frame)
        _wait_queue(rec)
    rec.stop()
    # Only the refused frame is lost, and one already queued on the card behind it if the encoder was behind.
    assert rec.error is None and 1 <= rec.dropped <= 2 and rec.frames + rec.dropped == 20
    assert rec._gpu_failed and gpu_share.broken() and not gpu_share.usable()
    gc.collect()
    assert pool.in_use() == 0


def test_an_encoder_opened_after_a_failure_stays_off_the_card(tmp_path, card):
    gpu_share.give_up("test")
    rec = _recorder(tmp_path)
    rec.start()
    assert rec._opened.wait(10)
    assert not rec.wants_gpu_frames() and card.made[0].settings["shared"] is False
    rec.stop()


def test_frames_pushed_after_the_end_are_let_go(tmp_path, card):
    pool = _pool()
    rec = _recorder(tmp_path)
    rec.start()
    assert rec._opened.wait(10)
    rec.stop()
    rec.push_frame(_hand(pool, 1, start=2000.0))  # a tick that raced the stop
    gc.collect()
    assert pool.in_use() == 0 and rec._video.qsize() == 0


def test_a_texture_goes_back_when_the_last_holder_lets_go():
    pool = _pool()
    a = _hand(pool, 0)
    b = a  # a repeat of the same frame in the queue
    assert pool.in_use() == 1
    del a
    assert pool.in_use() == 1
    del b
    assert pool.in_use() == 0
    for _ in range(gpu_share.POOL_SIZE):
        PICTURES.setdefault("held", []).append(_hand(pool, 0))
    assert pool.take() is None  # every texture out on loan: the caller sends that tick through memory
    PICTURES.pop("held")
    assert pool.in_use() == 0


class _Owner:
    def __init__(self, gpu):
        self.gpu, self.got = gpu, []

    def wants_gpu_frames(self):
        return self.gpu

    def push_frame(self, frame):
        self.got.append(frame)


def test_the_engine_hands_frames_on_the_card_only_to_encoders_that_want_them():
    shared, readbacks = [], []

    class Compositor:
        def share_nv12(self, canvas_id, size, now):
            shared.append(size)
            return SimpleNamespace(fmt="gpu", size=size)

    fake = SimpleNamespace(_frame=lambda c, canvas, size, now, made: readbacks.append(size) or
                           made.setdefault(size, SimpleNamespace(fmt="nv12")))
    canvas, comp, made = SimpleNamespace(id="horizontal"), Compositor(), {}
    on_card, on_card_too, through_memory = _Owner(True), _Owner(True), _Owner(False)
    a = Engine._output_frame(fake, comp, canvas, on_card.push_frame, (1280, 720), 1.0, made)
    b = Engine._output_frame(fake, comp, canvas, on_card_too.push_frame, (1280, 720), 1.0, made)
    c = Engine._output_frame(fake, comp, canvas, through_memory.push_frame, (1280, 720), 1.0, made)
    assert a is b and a.fmt == "gpu" and c.fmt == "nv12"
    assert shared == [(1280, 720)] and readbacks == [(1280, 720)]  # one hand-off and one read-back, shared
    # Only encoders asking: the read-back is skipped altogether.
    shared.clear(), readbacks.clear()
    Engine._output_frame(fake, comp, canvas, on_card.push_frame, (1280, 720), 2.0, {})
    assert shared == [(1280, 720)] and readbacks == []
    # Nothing on the card this tick (pool dry, or sharing off): the read-back frame instead.
    comp.share_nv12 = lambda *a: None
    assert Engine._output_frame(fake, comp, canvas, on_card.push_frame, (1280, 720), 3.0, {}).fmt == "nv12"
    # RGBA outputs (previews, the virtual camera) never ask.
    assert not _wants_gpu(lambda frame: None) and not _wants_gpu(print)


def test_sharing_is_off_where_it_cannot_work(monkeypatch):
    monkeypatch.setattr(gpu_share, "_broken", None)
    monkeypatch.setattr(gpu_share, "_gl_ok", True)
    monkeypatch.setenv("SPRIGCAST_NO_GPU_SHARE", "1")
    assert not gpu_share.usable()
    monkeypatch.delenv("SPRIGCAST_NO_GPU_SHARE")
    if gpu_share.sys.platform != "win32":
        assert not gpu_share.usable()  # Windows only
        assert gpu_share.probe() is False


def test_off_unless_asked_for(monkeypatch):
    """The first PC run drew ~10.4 ms a frame with it against ~5.8 ms without: off until it's faster."""
    monkeypatch.setattr(gpu_share.sys, "platform", "win32")
    monkeypatch.delenv("SPRIGCAST_NO_GPU_SHARE", raising=False)
    monkeypatch.delenv("SPRIGCAST_GPU_SHARE", raising=False)
    assert gpu_share.turned_off()
    monkeypatch.setenv("SPRIGCAST_GPU_SHARE", "1")
    assert not gpu_share.turned_off()
    monkeypatch.setenv("SPRIGCAST_NO_GPU_SHARE", "1")
    assert gpu_share.turned_off()
