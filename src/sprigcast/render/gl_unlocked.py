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
        try:
            resolvers = resolvers if resolvers is not None else _resolvers()
            for name, (result, arguments) in SIGNATURES.items():
                address = None
                for resolve in resolvers:
                    try:
                        address = resolve(name.encode())
                    except (AttributeError, OSError):
                        address = None
                    if address:
                        break
                if not address:
                    raise LookupError(f"{name} not found")
                setattr(self, name, _FUNCTYPE(result, *arguments)(address))
            self.ok = True
        except Exception as exc:  # noqa: BLE001 (no OpenGL library to call directly: moderngl as before)
            log.info("graphics: direct OpenGL calls unavailable (%s); using moderngl's", exc)

    def write_texture(self, texture, data) -> None:
        """texture.write(data) for a whole 8-bit texture, data a C-contiguous numpy array (alignment 1)."""
        w, h = texture.size
        self.glActiveTexture(GL_TEXTURE0 + self.ctx.default_texture_unit)  # where moderngl's own write binds
        self.glBindTexture(GL_TEXTURE_2D, texture.glo)
        self.glBindBuffer(GL_PIXEL_UNPACK_BUFFER, 0)
        self.glPixelStorei(GL_UNPACK_ALIGNMENT, 1)
        self.glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, w, h, FORMATS[texture.components], GL_UNSIGNED_BYTE,
                             data.ctypes.data)

    def read_into(self, framebuffer, target, components: int, write_offset: int = 0) -> None:
        """framebuffer.read_into(target, components, alignment=1, write_offset): target a moderngl Buffer
        (the copy is queued on the card) or a writable bytearray (filled now)."""
        w, h = framebuffer.size
        was = _i(0)
        self.glGetIntegerv(GL_READ_FRAMEBUFFER_BINDING, ctypes.byref(was))
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
        self.glBindFramebuffer(GL_READ_FRAMEBUFFER, was.value)

    def read_buffer(self, buffer) -> bytes:
        """buffer.read(): the whole buffer's bytes."""
        out = ctypes.create_string_buffer(buffer.size)
        self.glBindBuffer(GL_COPY_READ_BUFFER, buffer.glo)
        self.glGetBufferSubData(GL_COPY_READ_BUFFER, 0, buffer.size, out)
        self.glBindBuffer(GL_COPY_READ_BUFFER, 0)
        return out.raw
