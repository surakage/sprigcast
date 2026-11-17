"""Finished frames handed to SprigCast's own NVENC encoder on the graphics card, without reading them back.

Today an encoder's frame is drawn as NV12 (render/compositor.py _draw_nv12), copied back into memory
(1080p60: about 187 MB a second per canvas), queued, and copied into NVENC's input buffer. OBS skips the
trip through memory: its picture stays on the card and the encoder reads it there (plugins/obs-nvenc). OBS
draws with Direct3D 11 on Windows; SprigCast draws with OpenGL, and OBS's OpenGL route goes through CUDA,
whose context costs about 100 MB. This one doesn't:

1. A Direct3D 11 device is shared by OpenGL (WGL_NV_DX_interop2) and SprigCast's NVENC sessions
   (output/nvenc_d3d11.py shared_device).
2. Each tick OpenGL copies the NV12 target's two planes (Y: one channel, CbCr: two) into two Direct3D
   textures it shares (a copy on the card, glCopyImageSubData).
3. Direct3D draws those planes into a free NV12 texture of a small pool (R8 and R8G8 views of the NV12
   texture's two planes): the same pixels, now in the one layout NVENC takes.
4. The encoder's thread gets a GpuFrame naming that texture, registers it with its session once, maps it and
   encodes it. When the frame object is let go (encoded, dropped or skipped) its texture goes back to the pool.

Windows with an NVIDIA card drawing OpenGL only. Anything that fails turns it off for the rest of the run
(logged once) and frames go back to the read-back path as before; a pool with no free texture (the encoder
is behind) sends that tick's frame by the read-back path too. SPRIGCAST_NO_GPU_SHARE=1 turns it off.
"""

from __future__ import annotations

import ctypes
import itertools
import logging
import os
import sys
import threading
import time
from ctypes import POINTER, Structure, byref, c_float, c_int, c_uint32, c_void_p

log = logging.getLogger(__name__)

POOL_SIZE = 5  # NV12 textures per output size: one being encoded, one waiting, one held for repeats, spares
SLOW_MS = 8.0  # a hand-off slower than this is counted (and the longest logged when the share closes)

# DXGI formats, bind flags and the rest of what's used from d3d11.h / dxgiformat.h.
DXGI_FORMAT_R8G8_UNORM, DXGI_FORMAT_R8_UNORM, DXGI_FORMAT_NV12 = 49, 61, 103
BIND_SHADER_RESOURCE, BIND_RENDER_TARGET = 0x8, 0x20
USAGE_DEFAULT = 0
RTV_DIMENSION_TEXTURE2D = 4
TOPOLOGY_TRIANGLELIST = 4
FORMAT_SUPPORT_RENDER_TARGET = 0x4000
# Positions in the COM tables (checked against Wine's d3d11.idl, 2026-11-17).
DEV_CREATE_TEXTURE2D, DEV_CREATE_SRV, DEV_CREATE_RTV = 5, 7, 9
DEV_CREATE_VS, DEV_CREATE_PS, DEV_CHECK_FORMAT, DEV_REMOVED_REASON, DEV_IMMEDIATE_CONTEXT = 12, 15, 29, 39, 40
CTX_PS_SET_SRV, CTX_PS_SET_SHADER, CTX_VS_SET_SHADER, CTX_DRAW = 8, 9, 11, 13
CTX_IA_SET_LAYOUT, CTX_GS_SET_SHADER, CTX_IA_SET_TOPOLOGY = 17, 23, 24
CTX_OM_SET_TARGETS, CTX_OM_SET_BLEND, CTX_OM_SET_DEPTH, CTX_RS_SET_STATE, CTX_RS_SET_VIEWPORTS = 33, 35, 36, 43, 44
CTX_FLUSH = 111
BLOB_POINTER, BLOB_SIZE = 3, 4
# WGL_NV_DX_interop
GL_TEXTURE_2D = 0x0DE1
WGL_ACCESS_READ_WRITE_NV, WGL_ACCESS_WRITE_DISCARD_NV = 0x1, 0x2
GL_VENDOR = 0x1F00

# A triangle covering the target, and a pixel shader that copies each texel as it is. Load() reads one texel
# exactly (no filtering, no sampler). Rows aren't flipped: OpenGL and Direct3D see the same memory, and the
# NV12 target is drawn top row first (it's what the read-back path reads).
VERTEX_HLSL = b"""
float4 main(uint id : SV_VertexID) : SV_Position {
    float2 uv = float2((id << 1) & 2, id & 2);
    return float4(uv * float2(2, -2) + float2(-1, 1), 0, 1);
}
"""
PIXEL_HLSL = b"""
Texture2D plane : register(t0);
float4 main(float4 pos : SV_Position) : SV_Target {
    return plane.Load(int3(pos.xy, 0));
}
"""

_broken: str | None = None  # why sharing was turned off for this run (None: still allowed)
_gl_ok = False  # the compositor's OpenGL can share with Direct3D (set by probe() on the render thread)
_lock = threading.Lock()
_ids = itertools.count(1)


def turned_off() -> bool:
    return bool(os.environ.get("SPRIGCAST_NO_GPU_SHARE")) or sys.platform != "win32"


def usable() -> bool:
    """Whether encoders should open ready for GpuFrames (the compositor's OpenGL can share, nothing failed)."""
    return _gl_ok and _broken is None and not turned_off()


def give_up(reason: str) -> None:
    """Turn sharing off for the rest of the run: frames go back through memory, as before."""
    global _broken
    with _lock:
        if _broken is None:
            _broken = reason
            log.warning("GPU hand-off to the encoder turned off: %s (frames go through memory as before)", reason)


def broken() -> str | None:
    return _broken


# COM helpers ---------------------------------------------------------------------------------------


_PROTOTYPES: dict = {}


def _method(obj, index: int, restype, *argtypes):
    """obj's COM method at vtable position index, ready to call with the other arguments."""
    key = (restype, argtypes)
    proto = _PROTOTYPES.get(key)
    if proto is None:
        proto = _PROTOTYPES[key] = ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)  # type: ignore[attr-defined]
    vtable = ctypes.cast(obj, POINTER(POINTER(c_void_p))).contents
    fn = proto(vtable[index])
    return lambda *args: fn(obj, *args)


def _release(obj) -> None:
    if obj:
        _method(obj, 2, c_uint32)()


def _check(hr: int, what: str) -> None:
    if hr < 0:
        raise OSError(f"{what} failed (0x{hr & 0xFFFFFFFF:08x})")


class TextureDesc(Structure):
    _fields_ = [("Width", c_uint32), ("Height", c_uint32), ("MipLevels", c_uint32), ("ArraySize", c_uint32),
                ("Format", c_uint32), ("SampleCount", c_uint32), ("SampleQuality", c_uint32), ("Usage", c_uint32),
                ("BindFlags", c_uint32), ("CPUAccessFlags", c_uint32), ("MiscFlags", c_uint32)]


class RtvDesc(Structure):  # D3D11_RENDER_TARGET_VIEW_DESC: format, dimension, then a 12-byte union
    _fields_ = [("Format", c_uint32), ("ViewDimension", c_uint32), ("MipSlice", c_uint32), ("_pad", c_uint32 * 2)]


class Viewport(Structure):
    _fields_ = [("TopLeftX", c_float), ("TopLeftY", c_float), ("Width", c_float), ("Height", c_float),
                ("MinDepth", c_float), ("MaxDepth", c_float)]


def _texture(device, w: int, h: int, fmt: int, bind: int) -> c_void_p:
    desc = TextureDesc(Width=w, Height=h, MipLevels=1, ArraySize=1, Format=fmt, SampleCount=1,
                       Usage=USAGE_DEFAULT, BindFlags=bind)
    tex = c_void_p()
    _check(_method(device, DEV_CREATE_TEXTURE2D, c_int, c_void_p, c_void_p, c_void_p)(
        byref(desc), None, byref(tex)), "making a texture")
    return tex


def _srv(device, tex) -> c_void_p:
    view = c_void_p()
    _check(_method(device, DEV_CREATE_SRV, c_int, c_void_p, c_void_p, c_void_p)(tex, None, byref(view)),
           "making a shader view")
    return view


def _rtv(device, tex, fmt: int) -> c_void_p:
    desc = RtvDesc(Format=fmt, ViewDimension=RTV_DIMENSION_TEXTURE2D)
    view = c_void_p()
    _check(_method(device, DEV_CREATE_RTV, c_int, c_void_p, c_void_p, c_void_p)(tex, byref(desc), byref(view)),
           "making a plane view")
    return view


def _compile(source: bytes, target: bytes) -> bytes:
    comp = ctypes.WinDLL("d3dcompiler_47.dll")  # type: ignore[attr-defined]
    fn = comp.D3DCompile
    fn.restype = c_int
    fn.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p, c_void_p, c_void_p, ctypes.c_char_p,
                   ctypes.c_char_p, c_uint32, c_uint32, POINTER(c_void_p), POINTER(c_void_p)]
    code, errors = c_void_p(), c_void_p()
    hr = fn(source, len(source), b"sprigcast", None, None, b"main", target, 0, 0, byref(code), byref(errors))
    if errors:
        _release(errors)
    _check(hr, f"compiling the {target.decode()} shader")
    try:
        start = _method(code, BLOB_POINTER, c_void_p)()
        size = _method(code, BLOB_SIZE, ctypes.c_size_t)()
        return ctypes.string_at(start, size)
    finally:
        _release(code)


# OpenGL side ---------------------------------------------------------------------------------------


WGL = {  # name: (result, arguments)
    "wglDXOpenDeviceNV": (c_void_p, [c_void_p]),
    "wglDXCloseDeviceNV": (c_int, [c_void_p]),
    "wglDXRegisterObjectNV": (c_void_p, [c_void_p, c_void_p, c_uint32, c_uint32, c_uint32]),
    "wglDXUnregisterObjectNV": (c_int, [c_void_p, c_void_p]),
    "wglDXLockObjectsNV": (c_int, [c_void_p, c_int, POINTER(c_void_p)]),
    "wglDXUnlockObjectsNV": (c_int, [c_void_p, c_int, POINTER(c_void_p)]),
    "glCopyImageSubData": (None, [c_uint32, c_uint32, c_int, c_int, c_int, c_int,
                                  c_uint32, c_uint32, c_int, c_int, c_int, c_int, c_int, c_int, c_int]),
    "glGenTextures": (None, [c_int, POINTER(c_uint32)]),
    "glDeleteTextures": (None, [c_int, POINTER(c_uint32)]),
    "glGetString": (ctypes.c_char_p, [c_uint32]),
}


class _GL:
    def __init__(self, resolvers=None):
        from sprigcast.render.gl_unlocked import UnlockedGL, _resolvers

        resolvers = resolvers if resolvers is not None else _resolvers()
        for name, (result, arguments) in WGL.items():
            address = UnlockedGL._find(name, resolvers)
            if not address:
                raise LookupError(f"{name} not found")
            setattr(self, name, ctypes.WINFUNCTYPE(result, *arguments)(address))  # type: ignore[attr-defined]


def probe(resolvers=None) -> bool:
    """On the render thread with the compositor's context current: can this OpenGL share with Direct3D?
    Only looks (no device made): the bridge is made when an encoder first wants a frame."""
    global _gl_ok
    if turned_off():
        return False
    try:
        gl = _GL(resolvers)
        vendor = (gl.glGetString(GL_VENDOR) or b"").decode(errors="replace")
        _gl_ok = "NVIDIA" in vendor.upper()
        if not _gl_ok:
            log.info("GPU hand-off to the encoder: not on this card (%s)", vendor or "unknown")
    except Exception as exc:  # noqa: BLE001 (an old driver without the extension: frames go through memory)
        log.info("GPU hand-off to the encoder unavailable: %s", exc)
        _gl_ok = False
    return _gl_ok


# The pieces ----------------------------------------------------------------------------------------


class GpuFrame:
    """A finished canvas as an NV12 texture on the card (fmt "gpu"), for SprigCast's own NVENC encoder.

    Its texture goes back to the pool when the last reference to the frame goes (encoded, dropped, replaced).
    """

    __slots__ = ("canvas_id", "width", "height", "time", "fmt", "canvas_size", "pool", "index", "__weakref__")

    def __init__(self, canvas_id: str, width: int, height: int, now: float, pool: "Pool", index: int):
        self.canvas_id, self.width, self.height, self.time = canvas_id, width, height, now
        self.fmt = "gpu"
        self.canvas_size = (width, height)
        self.pool, self.index = pool, index

    data = None  # no bytes: see texture

    @property
    def texture(self) -> int:
        return self.pool.textures[self.index].value

    @property
    def key(self) -> tuple[int, int]:
        """Stays the same for the same texture for the whole run (encoders register each texture once)."""
        return self.pool.id, self.index

    def __del__(self):
        pool, self.pool = self.pool, None
        if pool is not None:
            pool.give_back(self.index)


class Pool:
    """POOL_SIZE NV12 textures with a view of each plane. Freed when nothing refers to it any more: the share
    that fills it, the frames out on loan, and the encoders that registered its textures."""

    def __init__(self, device, w: int, h: int):
        self.id = next(_ids)
        self.size = (w, h)
        self.textures: list[c_void_p] = []
        self.views: list[tuple[c_void_p, c_void_p]] = []  # (Y view, CbCr view) per texture
        self._free = list(range(POOL_SIZE))
        self._lock = threading.Lock()
        try:
            for _ in range(POOL_SIZE):
                tex = _texture(device, w, h, DXGI_FORMAT_NV12, BIND_RENDER_TARGET)
                self.textures.append(tex)
                self.views.append((_rtv(device, tex, DXGI_FORMAT_R8_UNORM), _rtv(device, tex, DXGI_FORMAT_R8G8_UNORM)))
        except Exception:
            self._close()
            raise

    def take(self) -> int | None:
        with self._lock:
            return self._free.pop(0) if self._free else None

    def give_back(self, index: int) -> None:
        with self._lock:
            if index not in self._free:
                self._free.append(index)

    def in_use(self) -> int:
        with self._lock:
            return POOL_SIZE - len(self._free)

    def _close(self) -> None:
        for y, uv in self.views:
            _release(y)
            _release(uv)
        for tex in self.textures:
            _release(tex)
        self.views, self.textures = [], []

    def __del__(self):
        try:
            self._close()
        except Exception:  # noqa: BLE001 (closing the app: the driver lets go of it anyway)
            pass


class Bridge:
    """One per compositor (its OpenGL context): the shared Direct3D device opened for OpenGL, the copy
    shaders and the device's immediate context. Render thread only."""

    def __init__(self, resolvers=None):
        from sprigcast.output.nvenc_d3d11 import shared_device

        self.gl = _GL(resolvers)
        self.handle = None
        self.vs = self.ps = self.context = self.guard = c_void_p()
        self.device = shared_device()
        context = c_void_p()
        _method(self.device, DEV_IMMEDIATE_CONTEXT, None, c_void_p)(byref(context))
        self.context = context
        try:
            support = c_uint32()
            _check(_method(self.device, DEV_CHECK_FORMAT, c_int, c_uint32, c_void_p)(
                DXGI_FORMAT_NV12, byref(support)), "asking about NV12")
            if not support.value & FORMAT_SUPPORT_RENDER_TARGET:
                raise OSError("this card can't draw into NV12 textures")
            vs_code, ps_code = _compile(VERTEX_HLSL, b"vs_4_0"), _compile(PIXEL_HLSL, b"ps_4_0")
            vs, ps = c_void_p(), c_void_p()
            _check(_method(self.device, DEV_CREATE_VS, c_int, c_void_p, ctypes.c_size_t, c_void_p, c_void_p)(
                vs_code, len(vs_code), None, byref(vs)), "making the vertex shader")
            self.vs = vs
            _check(_method(self.device, DEV_CREATE_PS, c_int, c_void_p, ctypes.c_size_t, c_void_p, c_void_p)(
                ps_code, len(ps_code), None, byref(ps)), "making the pixel shader")
            self.ps = ps
            self.handle = self.gl.wglDXOpenDeviceNV(self.device)
            if not self.handle:
                raise OSError("OpenGL couldn't open the Direct3D device (wglDXOpenDeviceNV)")
            ctx = self.context
            self._set_vs = _method(ctx, CTX_VS_SET_SHADER, None, c_void_p, c_void_p, c_uint32)
            self._set_ps = _method(ctx, CTX_PS_SET_SHADER, None, c_void_p, c_void_p, c_uint32)
            self._set_gs = _method(ctx, CTX_GS_SET_SHADER, None, c_void_p, c_void_p, c_uint32)
            self._set_srv = _method(ctx, CTX_PS_SET_SRV, None, c_uint32, c_uint32, POINTER(c_void_p))
            self._set_layout = _method(ctx, CTX_IA_SET_LAYOUT, None, c_void_p)
            self._set_topology = _method(ctx, CTX_IA_SET_TOPOLOGY, None, c_uint32)
            self._set_targets = _method(ctx, CTX_OM_SET_TARGETS, None, c_uint32, POINTER(c_void_p), c_void_p)
            self._set_blend = _method(ctx, CTX_OM_SET_BLEND, None, c_void_p, c_void_p, c_uint32)
            self._set_depth = _method(ctx, CTX_OM_SET_DEPTH, None, c_void_p, c_uint32)
            self._set_raster = _method(ctx, CTX_RS_SET_STATE, None, c_void_p)
            self._set_viewports = _method(ctx, CTX_RS_SET_VIEWPORTS, None, c_uint32, POINTER(Viewport))
            self._draw = _method(ctx, CTX_DRAW, None, c_uint32, c_uint32)
            self._flush = _method(ctx, CTX_FLUSH, None)
            self.removed = _method(self.device, DEV_REMOVED_REASON, c_int)
            # The device's own lock (ID3D10Multithread Enter/Leave): an encoder thread's NVENC calls wait while
            # the planes are drawn, so nothing lands between setting the pipeline up and drawing.
            from sprigcast.output.nvenc_d3d11 import MULTITHREAD_IID, _guid

            guard, iid = c_void_p(), _guid(*MULTITHREAD_IID)
            _check(_method(self.device, 0, c_int, c_void_p, c_void_p)(byref(iid), byref(guard)),
                   "finding the device's lock")
            self.guard = guard
            self._enter = _method(guard, 3, None)
            self._leave = _method(guard, 4, None)
        except Exception:
            self.release()
            raise

    def copy_planes(self, planes, nv12_views, size) -> None:
        """Draw the shared Y and CbCr textures into an NV12 texture's two planes (Direct3D, same device)."""
        w, h = size
        self._enter()
        try:
            self._draw_planes(planes, nv12_views, w, h)
        finally:
            self._leave()

    def _draw_planes(self, planes, nv12_views, w: int, h: int) -> None:
        self._set_layout(None)
        self._set_topology(TOPOLOGY_TRIANGLELIST)
        self._set_vs(self.vs, None, 0)
        self._set_gs(None, None, 0)
        self._set_ps(self.ps, None, 0)
        self._set_blend(None, None, 0xFFFFFFFF)
        self._set_depth(None, 0)
        self._set_raster(None)
        for (srv, _tex), view, (pw, ph) in zip(planes, nv12_views, ((w, h), (w // 2, h // 2))):
            target = c_void_p(view.value)
            self._set_targets(1, byref(target), None)
            self._set_viewports(1, byref(Viewport(0, 0, pw, ph, 0, 1)))
            source = c_void_p(srv.value)
            self._set_srv(0, 1, byref(source))
            self._draw(3, 0)
        nothing = c_void_p()
        self._set_srv(0, 1, byref(nothing))  # nothing left bound that OpenGL or NVENC will touch next
        self._set_targets(0, None, None)
        self._flush()

    def release(self) -> None:
        if self.handle:
            try:
                self.gl.wglDXCloseDeviceNV(self.handle)
            except Exception:  # noqa: BLE001 (a lost context)
                pass
            self.handle = None
        for shader in (self.vs, self.ps, self.guard):
            _release(shader)
        self.guard = c_void_p()
        self.vs = self.ps = c_void_p()
        _release(self.context)
        self.context = c_void_p()
        _release(self.device)  # this bridge's reference: the shared device itself stays for the run
        self.device = c_void_p()


class Share:
    """One NV12 target's way to the encoder: two Direct3D textures OpenGL writes the planes into, and the
    pool of NV12 textures handed out. Render thread only (it calls OpenGL)."""

    def __init__(self, bridge: Bridge, size: tuple[int, int]):
        self.bridge = bridge
        self.size = size
        w, h = size
        self.planes: list[tuple[c_void_p, c_void_p]] = []  # (shader view, texture): Y, then CbCr
        self.names: list[int] = []  # their OpenGL texture names
        self.handles: list[c_void_p] = []  # their interop handles
        self.pool: Pool | None = None
        self.handed = 0  # frames handed over
        self.full = 0  # ticks with no free texture (the encoder is behind): sent through memory instead
        self.slow = 0  # hand-offs over SLOW_MS
        self.longest_ms = 0.0
        gl = bridge.gl
        try:
            for pw, ph, fmt in ((w, h, DXGI_FORMAT_R8_UNORM), (w // 2, h // 2, DXGI_FORMAT_R8G8_UNORM)):
                tex = _texture(bridge.device, pw, ph, fmt, BIND_SHADER_RESOURCE | BIND_RENDER_TARGET)
                self.planes.append((_srv(bridge.device, tex), tex))
                name = c_uint32()
                gl.glGenTextures(1, byref(name))
                self.names.append(name.value)
                handle = gl.wglDXRegisterObjectNV(bridge.handle, tex, name.value, GL_TEXTURE_2D,
                                                  WGL_ACCESS_WRITE_DISCARD_NV)
                if not handle:
                    raise OSError("OpenGL couldn't share a Direct3D texture (wglDXRegisterObjectNV)")
                self.handles.append(c_void_p(handle))
            self._locked = (c_void_p * 2)(*self.handles)
            self.pool = Pool(bridge.device, w, h)
        except Exception:
            self.release()
            raise

    def push(self, y_glo: int, uv_glo: int, canvas_id: str, now: float) -> GpuFrame | None:
        """Hand this tick's NV12 target (its two OpenGL textures) to the pool. None when every texture is out
        on loan. Raises when the card or driver refuses (the caller turns sharing off)."""
        index = self.pool.take()
        if index is None:
            self.full += 1
            return None
        began = time.perf_counter()
        try:
            gl, w, h = self.bridge.gl, *self.size
            if not gl.wglDXLockObjectsNV(self.bridge.handle, 2, self._locked):
                raise OSError("OpenGL couldn't lock the shared textures")
            try:
                for src, dst, (pw, ph) in ((y_glo, self.names[0], (w, h)), (uv_glo, self.names[1], (w // 2, h // 2))):
                    gl.glCopyImageSubData(src, GL_TEXTURE_2D, 0, 0, 0, 0, dst, GL_TEXTURE_2D, 0, 0, 0, 0, pw, ph, 1)
            finally:
                gl.wglDXUnlockObjectsNV(self.bridge.handle, 2, self._locked)
            self.bridge.copy_planes(self.planes, self.pool.views[index], self.size)
            reason = self.bridge.removed()
            if reason < 0:
                raise OSError(f"the Direct3D device was lost (0x{reason & 0xFFFFFFFF:08x})")
        except Exception:
            self.pool.give_back(index)
            raise
        took = (time.perf_counter() - began) * 1000
        self.longest_ms = max(self.longest_ms, took)
        if took > SLOW_MS:
            self.slow += 1
        self.handed += 1
        return GpuFrame(canvas_id, w, h, now, self.pool, index)

    def release(self) -> None:
        gl = self.bridge.gl
        if self.handed:
            log.info("GPU hand-off %dx%d: %d frames, %d sent through memory (encoder behind), %d slow, longest "
                     "%.1f ms", *self.size, self.handed, self.full, self.slow, self.longest_ms)
        for handle in self.handles:
            try:
                gl.wglDXUnregisterObjectNV(self.bridge.handle, handle)
            except Exception:  # noqa: BLE001
                pass
        self.handles = []
        for name in self.names:
            try:
                gl.glDeleteTextures(1, byref(c_uint32(name)))
            except Exception:  # noqa: BLE001
                pass
        self.names = []
        for srv, tex in self.planes:
            _release(srv)
            _release(tex)
        self.planes = []
        self.pool = None  # freed once the frames out on loan and the encoders let go of it too
