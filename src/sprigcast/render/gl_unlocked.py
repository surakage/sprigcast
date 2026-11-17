"""The OpenGL calls that can wait on the graphics card, made without holding Python's lock.

moderngl keeps the GIL through every OpenGL call. Most return at once, but a few can wait while the driver is
busy: sending a frame to a texture (a camera's 1080p picture), reading a canvas back for an encoder or the
virtual camera, and fetching that copy. In the 2026-11-15/16 soaks these waited 300-770 ms now and then
(a camera waking, an output starting: the driver setting something up), and with the lock held everything
stopped with them: sound, encoders, the window.

The same calls made through ctypes let go of the lock while they run, so only the render thread waits. They
do exactly what moderngl's write/read_into/read do, on the objects moderngl made (by their .glo names), and
leave the bindings as moderngl expects.
"""

from __future__ import annotations

import ctypes
import logging
import sys

log = logging.getLogger(__name__)

GL_TEXTURE_2D = 0x0DE1
GL_TEXTURE0 = 0x84C0
GL_UNSIGNED_BYTE = 0x1401
GL_RED, GL_RG, GL_RGB, GL_RGBA = 0x1903, 0x8227, 0x1907, 0x1908
GL_PACK_ALIGNMENT, GL_UNPACK_ALIGNMENT = 0x0D05, 0x0CF5
GL_READ_FRAMEBUFFER = 0x8CA8
GL_READ_FRAMEBUFFER_BINDING = 0x8CAA
GL_COLOR_ATTACHMENT0 = 0x8CE0
GL_PIXEL_PACK_BUFFER, GL_PIXEL_UNPACK_BUFFER = 0x88EB, 0x88EC
GL_COPY_READ_BUFFER = 0x8F36
FORMATS = {1: GL_RED, 2: GL_RG, 3: GL_RGB, 4: GL_RGBA}

_FUNCTYPE = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE) if sys.platform == "win32" else ctypes.CFUNCTYPE
_u, _i, _p, _z = ctypes.c_uint, ctypes.c_int, ctypes.c_void_p, ctypes.c_ssize_t

SIGNATURES = {  # name: (result, arguments)
    "glActiveTexture": (None, [_u]),
    "glBindTexture": (None, [_u, _u]),
    "glPixelStorei": (None, [_u, _i]),
    "glTexSubImage2D": (None, [_u, _i, _i, _i, _i, _i, _u, _u, _p]),
    "glBindFramebuffer": (None, [_u, _u]),
    "glReadBuffer": (None, [_u]),
    "glBindBuffer": (None, [_u, _u]),
    "glReadPixels": (None, [_i, _i, _i, _i, _u, _u, _p]),
    "glGetBufferSubData": (None, [_u, _z, _z, _p]),
    "glGetIntegerv": (None, [_u, ctypes.POINTER(_i)]),
}
STREAMING = {  # uploads through a pixel buffer, so sending a picture never waits for the card (see write_texture)
    "glBufferData": (None, [_u, _z, _p, _u]),
    "glBufferSubData": (None, [_u, _z, _z, _p]),
}
GL_STREAM_DRAW = 0x88E0
FENCES = {  # OpenGL 3.2 sync objects: without them read-backs are fetched without asking first, as before
    "glFenceSync": (_p, [_u, _u]),
    "glClientWaitSync": (_u, [_p, _u, ctypes.c_uint64]),
    "glDeleteSync": (None, [_p]),
}
GL_SYNC_GPU_COMMANDS_COMPLETE = 0x9117
GL_SYNC_FLUSH_COMMANDS_BIT = 0x00000001
GL_TIMEOUT_EXPIRED = 0x911B
MAPPING = {  # OpenGL 4.4 (or ARB_buffer_storage): read-back buffers the CPU reads straight from memory
    "glGenBuffers": (None, [_i, ctypes.POINTER(_u)]),
    "glDeleteBuffers": (None, [_i, ctypes.POINTER(_u)]),
    "glBufferStorage": (None, [_u, _z, _p, _u]),
    "glMapBufferRange": (_p, [_u, _z, _z, _u]),
    "glUnmapBuffer": (ctypes.c_ubyte, [_u]),
}
GL_MAP_READ_BIT, GL_MAP_PERSISTENT_BIT, GL_MAP_COHERENT_BIT = 0x0001, 0x0040, 0x0080
GL_CLIENT_STORAGE_BIT = 0x0200
_memmove = ctypes.memmove  # a ctypes call: lets go of Python's lock while it copies


def _resolvers():
    """Ways to find an OpenGL function for the current context: the driver's lookup, then the library's own
    exports (Windows' wglGetProcAddress doesn't hand out the OpenGL 1.1 functions)."""
    found = []
    if sys.platform == "win32":
        gl = ctypes.WinDLL("opengl32")  # type: ignore[attr-defined]
        lookup = gl.wglGetProcAddress
        lookup.restype, lookup.argtypes = ctypes.c_void_p, [ctypes.c_char_p]
        found.append(lambda name: lookup(name) or None)
        found.append(lambda name: ctypes.cast(getattr(gl, name.decode()), ctypes.c_void_p).value)
        return found
    for library, name in (("libEGL.so.1", "eglGetProcAddress"), ("libGL.so.1", "glXGetProcAddressARB")):
        try:
            lookup = getattr(ctypes.CDLL(library), name)
        except (OSError, AttributeError):
            continue
        lookup.restype, lookup.argtypes = ctypes.c_void_p, [ctypes.c_char_p]
        found.append(lambda n, lookup=lookup: lookup(n) or None)
    return found


class UnlockedGL:
    """ok is False when a function couldn't be found: then callers use moderngl's own calls as before."""

    def __init__(self, ctx, resolvers=None):
        self.ctx = ctx
        self.ok = False
        self.fences_ok = False  # fence()/done() work: a read-back is fetched only once the card has made it
        self.streaming_ok = False  # write_texture goes through a pixel buffer (never waits for the card)
        self._upload = None  # that buffer (a moderngl Buffer: only its OpenGL name is used)
        self.mapping_ok = False  # mapped(): read-back buffers fetched with no OpenGL call at all
        try:
            resolvers = resolvers if resolvers is not None else _resolvers()
            for name, (result, arguments) in SIGNATURES.items():
                address = self._find(name, resolvers)
                if not address:
                    raise LookupError(f"{name} not found")
                setattr(self, name, _FUNCTYPE(result, *arguments)(address))
            self.ok = True
            found = {name: self._find(name, resolvers) for name in FENCES}
            if all(found.values()):
                for name, (result, arguments) in FENCES.items():
                    setattr(self, name, _FUNCTYPE(result, *arguments)(found[name]))
                self.fences_ok = True
                self.mapping_ok = self._find_mapping(resolvers)
            found = {name: self._find(name, resolvers) for name in STREAMING}
            if all(found.values()):
                for name, (result, arguments) in STREAMING.items():
                    setattr(self, name, _FUNCTYPE(result, *arguments)(found[name]))
                self.streaming_ok = True
        except Exception as exc:  # noqa: BLE001 (no OpenGL library to call directly: moderngl as before)
            log.info("graphics: direct OpenGL calls unavailable (%s); using moderngl's", exc)
        self._reset_status = None
        try:
            for name in ("glGetGraphicsResetStatus", "glGetGraphicsResetStatusARB", "glGetGraphicsResetStatusKHR"):
                address = self._find(name, resolvers)
                if address:
                    self._reset_status = _FUNCTYPE(_u)(address)
                    break
        except Exception:  # noqa: BLE001 (older drivers: a reset is still noticed by frames failing)
            pass

    def reset_status(self) -> int:
        """Non-zero once the graphics driver has reset (a driver update, a crash or Windows' 2-second hang
        recovery) and this context's work is lost; 0 when fine or the driver can't say."""
        if self._reset_status is None:
            return 0
        try:
            return int(self._reset_status())
        except Exception:  # noqa: BLE001
            return 0

    def _find_mapping(self, resolvers) -> bool:
        try:
            if self.ctx.version_code < 440 and "GL_ARB_buffer_storage" not in self.ctx.extensions:
                return False
        except Exception:  # noqa: BLE001 (a context that can't say: the usual buffers)
            return False
        found = {name: self._find(name, resolvers) for name in MAPPING}
        if not all(found.values()):
            return False
        for name, (result, arguments) in MAPPING.items():
            setattr(self, name, _FUNCTYPE(result, *arguments)(found[name]))
        return True

    def mapped(self, nbytes: int):
        """A read-back buffer kept mapped for good (MappedBuffer), or None when the driver won't make one."""
        if not self.mapping_ok:
            return None
        name = _u(0)
        try:
            self.glGenBuffers(1, ctypes.byref(name))
            self.glBindBuffer(GL_PIXEL_PACK_BUFFER, name.value)
            self.glBufferStorage(GL_PIXEL_PACK_BUFFER, nbytes, None,
                                 GL_MAP_READ_BIT | GL_MAP_PERSISTENT_BIT | GL_MAP_COHERENT_BIT | GL_CLIENT_STORAGE_BIT)
            pointer = self.glMapBufferRange(GL_PIXEL_PACK_BUFFER, 0, nbytes,
                                            GL_MAP_READ_BIT | GL_MAP_PERSISTENT_BIT | GL_MAP_COHERENT_BIT)
            self.glBindBuffer(GL_PIXEL_PACK_BUFFER, 0)
        except Exception as exc:  # noqa: BLE001
            log.info("graphics: mapped read-back buffers unavailable (%s)", exc)
            pointer = None
        if not pointer:
            if name.value:
                self.glBindBuffer(GL_PIXEL_PACK_BUFFER, 0)
                self.glDeleteBuffers(1, ctypes.byref(name))
            self.mapping_ok = False  # it failed once: the usual buffers from now on
            return None
        return MappedBuffer(self, name.value, nbytes, pointer)

    @staticmethod
    def _find(name: str, resolvers):
        for resolve in resolvers:
            try:
                address = resolve(name.encode())
            except (AttributeError, OSError):
                address = None
            if address:
                return address
        return None

    def fence(self):
        """A marker after everything asked of the card so far (None without sync objects)."""
        if not self.fences_ok:
            return None
        return self.glFenceSync(GL_SYNC_GPU_COMMANDS_COMPLETE, 0) or None

    def done(self, sync) -> bool:
        """Has the card got past this marker? Never waits."""
        if sync is None:
            return True
        result = self.glClientWaitSync(sync, GL_SYNC_FLUSH_COMMANDS_BIT, 0)
        return result != GL_TIMEOUT_EXPIRED  # signalled, or the wait failed (then read it as before)

    def forget(self, sync) -> None:
        if sync is not None:
            self.glDeleteSync(sync)

    def write_texture(self, texture, data) -> None:
        """texture.write(data) for a whole 8-bit texture, data a C-contiguous numpy array (alignment 1).

        Like OBS's dynamic textures (Map with WRITE_DISCARD): the picture goes into fresh pixel-buffer memory
        (the old memory is let go, not waited for) and the card copies it into the texture in its own time, in
        order after the draws still reading the old picture. Sent straight from our memory instead, the driver
        could wait for those draws first: 160-350 ms in the 2026-11-17 heavy run, now and then."""
        w, h = texture.size
        self.glActiveTexture(GL_TEXTURE0 + self.ctx.default_texture_unit)  # where moderngl's own write binds
        self.glBindTexture(GL_TEXTURE_2D, texture.glo)
        self.glPixelStorei(GL_UNPACK_ALIGNMENT, 1)
        if self.streaming_ok:
            if self._upload is None:
                self._upload = self.ctx.buffer(reserve=1)
            self.glBindBuffer(GL_PIXEL_UNPACK_BUFFER, self._upload.glo)
            self.glBufferData(GL_PIXEL_UNPACK_BUFFER, data.nbytes, None, GL_STREAM_DRAW)  # fresh memory
            self.glBufferSubData(GL_PIXEL_UNPACK_BUFFER, 0, data.nbytes, data.ctypes.data)
            self.glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, w, h, FORMATS[texture.components], GL_UNSIGNED_BYTE, None)
            self.glBindBuffer(GL_PIXEL_UNPACK_BUFFER, 0)
            return
        self.glBindBuffer(GL_PIXEL_UNPACK_BUFFER, 0)
        self.glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, w, h, FORMATS[texture.components], GL_UNSIGNED_BYTE,
                             data.ctypes.data)

    def release(self) -> None:
        """Let go of the upload buffer (the compositor is closing, or starting graphics again)."""
        upload, self._upload = self._upload, None
        if upload is not None:
            try:
                upload.release()
            except Exception:  # noqa: BLE001 (a lost context: nothing to let go)
                pass

    def read_into(self, framebuffer, target, components: int, write_offset: int = 0) -> None:
        """framebuffer.read_into(target, components, alignment=1, write_offset): target a moderngl Buffer
        (the copy is queued on the card) or a writable bytearray (filled now)."""
        w, h = framebuffer.size
        was = self._read_binding()
        self.glBindFramebuffer(GL_READ_FRAMEBUFFER, framebuffer.glo)
        self.glReadBuffer(GL_COLOR_ATTACHMENT0)
        self.glPixelStorei(GL_PACK_ALIGNMENT, 1)
        if isinstance(target, (bytearray, memoryview)):
            room = (ctypes.c_char * (len(target) - write_offset)).from_buffer(target, write_offset)
            self.glBindBuffer(GL_PIXEL_PACK_BUFFER, 0)
            self.glReadPixels(0, 0, w, h, FORMATS[components], GL_UNSIGNED_BYTE, ctypes.addressof(room))
        else:
            self.glBindBuffer(GL_PIXEL_PACK_BUFFER, target.glo)
            self.glReadPixels(0, 0, w, h, FORMATS[components], GL_UNSIGNED_BYTE, write_offset)
            self.glBindBuffer(GL_PIXEL_PACK_BUFFER, 0)
        self.glBindFramebuffer(GL_READ_FRAMEBUFFER, was)

    def _read_binding(self) -> int:
        """The framebuffer to put back after a read: the one moderngl last used (ctx.fbo, what moderngl's own
        reads put back). Asking the driver (glGetIntegerv) makes a threaded driver (NVIDIA's) catch up with
        everything queued first, which is a wait; only when moderngl hasn't used one yet."""
        fbo = getattr(self.ctx, "fbo", None)
        if fbo is not None:
            return fbo.glo
        was = _i(0)
        self.glGetIntegerv(GL_READ_FRAMEBUFFER_BINDING, ctypes.byref(was))
        return was.value

    def read_buffer(self, buffer) -> bytes:
        """buffer.read(): the whole buffer's bytes (a MappedBuffer's straight from memory)."""
        if isinstance(buffer, MappedBuffer):
            return buffer.read()
        out = ctypes.create_string_buffer(buffer.size)
        self.glBindBuffer(GL_COPY_READ_BUFFER, buffer.glo)
        self.glGetBufferSubData(GL_COPY_READ_BUFFER, 0, buffer.size, out)
        self.glBindBuffer(GL_COPY_READ_BUFFER, 0)
        return out.raw


class MappedBuffer:
    """A read-back buffer the CPU reads straight from memory (persistent, coherent mapping), so fetching a
    finished copy is a memory copy and no OpenGL call: none the driver can hold up (the 2026-11-16 heavy run
    waited 59 ms in glGetBufferSubData). Its fence must be done before read(); _Pbos makes sure of that.
    Has what read_into and _Pbos use of a moderngl Buffer: glo, size, read(), release()."""

    def __init__(self, gl: UnlockedGL, glo: int, size: int, pointer: int):
        self.gl, self.glo, self.size, self.pointer = gl, glo, size, pointer

    def read(self) -> bytes:
        out = ctypes.create_string_buffer(self.size)
        _memmove(out, self.pointer, self.size)
        return out.raw

    def release(self) -> None:
        if not self.glo:
            return
        name = _u(self.glo)
        self.glo, self.pointer = 0, None
        self.gl.glBindBuffer(GL_PIXEL_PACK_BUFFER, name.value)
        self.gl.glUnmapBuffer(GL_PIXEL_PACK_BUFFER)
        self.gl.glBindBuffer(GL_PIXEL_PACK_BUFFER, 0)
        self.gl.glDeleteBuffers(1, ctypes.byref(name))
