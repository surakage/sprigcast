"""Waiting for the graphics card on a fence, through ctypes (which lets go of Python's lock), not in OpenGL."""

import ctypes

import pytest

from sprigcast.render.gpu_wait import GpuWait


@pytest.fixture
def compositor():
    from sprigcast.render.compositor import Compositor

    try:
        comp = Compositor()
    except Exception as exc:
        pytest.skip(f"no OpenGL context: {exc}")
    yield comp
    comp.release()


def test_a_fence_marks_a_tick_and_the_next_waits_for_it(compositor):
    gpu = GpuWait()
    assert gpu.ok
    # Foreign functions called through ctypes run without the GIL: the rest of SprigCast keeps going.
    assert isinstance(gpu._client_wait, ctypes._CFuncPtr)
    fbo = compositor.ctx.simple_framebuffer((640, 360))
    buffer = compositor.ctx.buffer(reserve=640 * 360 * 4)
    for level in (0.2, 0.6):
        fbo.use()
        fbo.clear(level, 0.0, 0.0, 1.0)
        fbo.read_into(buffer, components=4)
        gpu.mark()
    assert len(gpu._syncs) == 2
    assert 0.0 <= gpu.wait() < 1.0  # for the tick before last; the last may still be on the card
    assert len(gpu._syncs) == 1
    assert gpu.wait() == 0.0  # one tick in flight is fine
    assert buffer.read()[0] == round(0.6 * 255)
    gpu.release()
    fbo.release()
    buffer.release()


def test_the_engine_uses_fences_with_a_real_context(compositor):
    from sprigcast.render.engine import Engine

    assert Engine._gpu_wait(compositor) is not None
    assert Engine._gpu_wait(object()) is None  # a test stand-in compositor: nothing changes


def test_without_sync_objects_nothing_changes():
    gpu = GpuWait(lookups=[lambda name: 0])
    assert not gpu.ok
    gpu.mark()
    assert gpu.wait() == 0.0
    gpu.release()


def test_direct_calls_give_the_same_pixels_as_moderngl(compositor):
    import numpy as np

    from sprigcast.render.gl_unlocked import UnlockedGL

    gl = compositor.gl
    assert isinstance(gl, UnlockedGL) and gl.ok
    assert isinstance(gl.glReadPixels, ctypes._CFuncPtr)  # through ctypes: no Python lock while it runs
    ctx = compositor.ctx
    rng = np.random.default_rng(3)
    for components, size in ((1, (37, 21)), (3, (33, 17)), (4, (64, 36)), (2, (15, 9))):
        pixels = rng.integers(0, 256, (size[1], size[0], components), dtype=np.uint8)
        texture = ctx.texture(size, components, alignment=1)
        gl.write_texture(texture, np.ascontiguousarray(pixels))
        assert texture.read(alignment=1) == pixels.tobytes()
        fbo = ctx.framebuffer(color_attachments=[texture])
        out = bytearray(8 + size[0] * size[1] * components)
        gl.read_into(fbo, out, components, write_offset=8)
        assert bytes(out[8:]) == fbo.read(components=components, alignment=1)
        buffer = ctx.buffer(reserve=size[0] * size[1] * components)
        gl.read_into(fbo, buffer, components)
        assert gl.read_buffer(buffer) == buffer.read() == bytes(out[8:])
        for obj in (buffer, fbo, texture):
            obj.release()


def test_uploads_and_nv12_read_backs_match_without_direct_calls(compositor, monkeypatch):
    """The compositor's own upload and NV12 read-back give the same bytes either way."""
    import numpy as np

    from sprigcast.core.scene import Canvas

    canvas = Canvas(id="horizontal", width=64, height=36)
    frame = np.random.default_rng(5).integers(0, 256, (36, 64, 4), dtype=np.uint8)
    results = []
    for direct in (True, False):
        gl = compositor.gl
        if not direct:
            compositor.gl = None
        compositor.upload("pic", frame, "RGBA")
        results.append(compositor.textures["pic"].read(alignment=1))
        target = compositor._target(canvas)
        target.fbo.use()
        target.fbo.clear(0.2, 0.4, 0.6, 1.0)
        results.append(bytes(compositor.read_nv12("horizontal", (32, 18))))
        first = compositor.read_nv12_later("horizontal", (32, 18), "a", 0.0)
        compositor.ctx.finish()  # a real card may still be copying (it isn't waited for); a tick later it's done
        second = compositor.read_nv12_later("horizontal", (32, 18), "b", 0.01)
        assert first == (None, None) and second[1] == "a"
        results.append(bytes(second[0]))
        compositor.gl = gl
        compositor.nv12_targets.clear()
    assert results[:3] == results[3:]


def test_a_card_stuck_past_the_limit_is_left_alone_for_the_tick():
    """The card still on the tick before last after WAIT_LIMIT_S: behind is set, the fence is kept for the next
    look, and the render loop draws nothing new this tick instead of blocking in OpenGL for the rest."""
    from sprigcast.render import gpu_wait

    done = {"ready": False}
    made, deleted = [], []
    fence_t = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint)
    wait_t = ctypes.CFUNCTYPE(ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint64)
    delete_t = ctypes.CFUNCTYPE(None, ctypes.c_void_p)
    fence = fence_t(lambda kind, flags: made.append(len(made) + 1) or len(made))
    client_wait = wait_t(lambda sync, flags, ns: 0x911C if done["ready"] else gpu_wait.GL_TIMEOUT_EXPIRED)
    delete = delete_t(lambda sync: deleted.append(sync))
    keep = (fence, client_wait, delete)
    addresses = {b"glFenceSync": fence, b"glClientWaitSync": client_wait, b"glDeleteSync": delete}
    gpu = GpuWait(lookups=[lambda name: ctypes.cast(addresses[name], ctypes.c_void_p).value])
    assert gpu.ok
    gpu.mark()
    gpu.mark()
    gpu.wait(limit_s=0.001)
    assert gpu.behind and deleted == [] and len(gpu._syncs) == 2  # kept: looked at again next tick
    done["ready"] = True
    gpu.wait(limit_s=0.001)
    assert not gpu.behind and deleted == [1] and len(gpu._syncs) == 1
    del keep


def test_uploads_go_through_fresh_buffer_memory_and_leave_moderngl_working(compositor):
    """Like OBS's dynamic textures: each upload gets fresh pixel-buffer memory, so it never waits for the card
    to finish drawing the old picture. moderngl's own writes still work afterwards (nothing left bound)."""
    import numpy as np

    gl = compositor.gl
    assert gl.streaming_ok
    ctx = compositor.ctx
    texture = ctx.texture((48, 30), 4, alignment=1)
    fbo = ctx.framebuffer(color_attachments=[texture])
    rng = np.random.default_rng(9)
    for _ in range(3):  # each one replaces the last, even while the draw reading it may still be queued
        pixels = rng.integers(0, 256, (30, 48, 4), dtype=np.uint8)
        gl.write_texture(texture, pixels)
        fbo.use()  # something reads the texture between uploads
        assert texture.read(alignment=1) == pixels.tobytes()
    small = rng.integers(0, 256, (30, 48, 1), dtype=np.uint8)
    other = ctx.texture((48, 30), 1, alignment=1)
    gl.write_texture(other, small)  # a different size of buffer: still right
    assert other.read(alignment=1) == small.tobytes()
    plain = rng.integers(0, 256, (30, 48, 4), dtype=np.uint8)
    texture.write(plain.tobytes())  # moderngl's own call, after ours
    assert texture.read(alignment=1) == plain.tobytes()
    for obj in (fbo, texture, other):
        obj.release()
