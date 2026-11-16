"""Waiting for the graphics card without holding up the rest of SprigCast.

moderngl keeps Python's lock (the GIL) through every OpenGL call. When the graphics card falls behind (another
app's heavy work, a driver hiccup), the driver makes one of those calls wait until it catches up: on the
2026-11-16 retest a frame read-back waited 317 ms, and with the lock held the sound, encoders and window all
stopped with it ("everything waited 258 ms").

So each tick ends with a fence (glFenceSync) and the next tick first waits for it through ctypes, which lets
go of the lock while it waits. The card is then never more than a tick behind when the render thread starts
calling OpenGL again, and if it is behind, only the render thread waits.

Needs OpenGL 3.2 sync objects (every 3.3 driver has them). Without them, nothing changes.
"""

from __future__ import annotations

import ctypes
import logging
import sys

log = logging.getLogger(__name__)

GL_SYNC_GPU_COMMANDS_COMPLETE = 0x9117
GL_SYNC_FLUSH_COMMANDS_BIT = 0x00000001
GL_TIMEOUT_EXPIRED = 0x911B
GL_WAIT_FAILED = 0x911D
WAIT_LIMIT_S = 1.0  # never wait longer than this for one tick (then carry on as before)

_FUNCTYPE = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE) if sys.platform == "win32" else ctypes.CFUNCTYPE


def _proc_address():
    """The driver's function lookup for the current context: wglGetProcAddress, eglGetProcAddress or glX."""
    if sys.platform == "win32":
        lookup = ctypes.WinDLL("opengl32").wglGetProcAddress  # type: ignore[attr-defined]
        lookup.restype, lookup.argtypes = ctypes.c_void_p, [ctypes.c_char_p]
        return [lookup]
    lookups = []
    for library, name in (("libEGL.so.1", "eglGetProcAddress"), ("libGL.so.1", "glXGetProcAddressARB")):
        try:
            lookup = getattr(ctypes.CDLL(library), name)
        except (OSError, AttributeError):
            continue
        lookup.restype, lookup.argtypes = ctypes.c_void_p, [ctypes.c_char_p]
        lookups.append(lookup)
    return lookups


class GpuWait:
    """mark() at the end of a tick, wait() at the start of the next. Call both on the render thread, with its
    OpenGL context current."""

    def __init__(self, lookups=None):
        self._sync = None
        self.ok = False
        try:
            for lookup in (lookups if lookups is not None else _proc_address()):
                fence, client_wait, delete = (lookup(name) for name in (b"glFenceSync", b"glClientWaitSync",
                                                                        b"glDeleteSync"))
                if fence and client_wait and delete:
                    self._fence = _FUNCTYPE(ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint)(fence)
                    self._client_wait = _FUNCTYPE(ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint,
                                                  ctypes.c_uint64)(client_wait)
                    self._delete = _FUNCTYPE(None, ctypes.c_void_p)(delete)
                    self.ok = True
                    break
        except Exception as exc:  # noqa: BLE001 (no OpenGL library here: keep going without it)
            log.info("graphics: no fences (%s); frames read back as before", exc)

    def mark(self) -> None:
        """Everything this tick asked of the card so far: wait() returns once it's done."""
        if not self.ok:
            return
        self._drop()
        self._sync = self._fence(GL_SYNC_GPU_COMMANDS_COMPLETE, 0) or None

    def wait(self, limit_s: float = WAIT_LIMIT_S) -> float:
        """Wait (without Python's lock) until the card finished the last marked tick. Returns seconds waited
        past a quick check: 0.0 when it was already done, as it nearly always is."""
        if self._sync is None:
            return 0.0
        import time

        began = time.perf_counter()
        # glClientWaitSync goes through ctypes, which lets go of the GIL for the call.
        result = self._client_wait(self._sync, GL_SYNC_FLUSH_COMMANDS_BIT, int(limit_s * 1e9))
        waited = time.perf_counter() - began
        self._drop()
        if result == GL_WAIT_FAILED:
            self.ok = False
            log.info("graphics: fence wait failed; frames read back as before")
        return waited

    def _drop(self) -> None:
        if self._sync is not None:
            self._delete(self._sync)
            self._sync = None

    def release(self) -> None:
        if self.ok:
            self._drop()
