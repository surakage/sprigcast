"""Waiting for the graphics card without holding up the rest of SprigCast.

moderngl keeps Python's lock (the GIL) through every OpenGL call. When the graphics card falls behind (another
app's heavy work, a driver hiccup), the driver makes one of those calls wait until it catches up: on the
2026-11-16 retest a frame read-back waited 317 ms, and with the lock held the sound, encoders and window all
stopped with it ("everything waited 258 ms").

So each tick ends with a fence (glFenceSync), and a tick first waits through ctypes, which lets go of the
lock while it waits, for the fence of the tick before last. The card may work on one tick while the next is
being prepared (waiting for the last tick itself took that overlap away: a slow card, or software drawing,
then missed a frame in three), but never falls further behind; and if it is behind, only the render thread
waits.

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
WAIT_LIMIT_S = 0.04  # wait at most this long in one tick; still behind, the tick skips the card (see behind)

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

    IN_FLIGHT = 1  # ticks the card may still be working on when the next one starts

    def __init__(self, lookups=None):
        self._syncs: list = []
        self.behind = False  # the last wait() ran out with the card still behind: give it nothing new this tick
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
        """Everything this tick asked of the card so far."""
        if not self.ok:
            return
        sync = self._fence(GL_SYNC_GPU_COMMANDS_COMPLETE, 0)
        if sync:
            self._syncs.append(sync)

    def wait(self, limit_s: float = WAIT_LIMIT_S) -> float:
        """Wait (without Python's lock) until the card finished all but the last IN_FLIGHT marked ticks. Returns
        seconds waited: next to nothing when it was already done, as it nearly always is."""
        if len(self._syncs) <= self.IN_FLIGHT:
            return 0.0
        import time

        began = time.perf_counter()
        result = None
        self.behind = False
        while len(self._syncs) > self.IN_FLIGHT:
            sync = self._syncs[0]
            left = limit_s - (time.perf_counter() - began)
            # glClientWaitSync goes through ctypes, which lets go of the GIL for the call.
            result = self._client_wait(sync, GL_SYNC_FLUSH_COMMANDS_BIT, max(0, int(left * 1e9)))
            if result == GL_TIMEOUT_EXPIRED:
                # The card is stuck on something (2026-11-16: 290-790 ms now and then, inside the driver).
                # Queuing more would only make the render thread wait in the next OpenGL call: this tick
                # skips the card, and the next one looks again.
                self.behind = True
                break
            self._syncs.pop(0)
            self._delete(sync)
        waited = time.perf_counter() - began
        if result == GL_WAIT_FAILED:
            self.ok = False
            self.release()
            log.info("graphics: fence wait failed; frames read back as before")
        return waited

    def release(self) -> None:
        while self._syncs:
            self._delete(self._syncs.pop())
