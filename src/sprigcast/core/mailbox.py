"""Single-slot hand-offs between threads: the reader only ever wants the newest item."""

from __future__ import annotations

import threading
from typing import Generic, TypeVar

import numpy as np

T = TypeVar("T")


class Mailbox(Generic[T]):
    def __init__(self) -> None:
        self._item: T | None = None
        self._lock = threading.Lock()

    def put(self, item: T) -> None:
        """Store an item, replacing any item the reader has not taken yet."""
        with self._lock:
            self._item = item

    def take(self) -> T | None:
        """Return the newest item and empty the slot, or None if nothing new arrived."""
        with self._lock:
            item, self._item = self._item, None
        return item

    def peek(self) -> T | None:
        """Return the newest item without emptying the slot."""
        with self._lock:
            return self._item


class FrameRing:
    """A mailbox for big image frames that reuses three buffers instead of allocating one per frame.

    Screen capture can deliver a full-screen image at the monitor's refresh rate. Copying each one into
    a fresh array means the OS hands out and zeroes megabytes of new memory every few milliseconds.
    Here the writer copies into a buffer that is neither waiting to be read nor still held by the reader
    (who is done with a frame once it asks for the next one), so three buffers are always enough.
    """

    def __init__(self) -> None:
        self._buffers: list[np.ndarray] = []
        self._ready: np.ndarray | None = None
        self._lent: np.ndarray | None = None
        self._lock = threading.Lock()

    def put(self, image: np.ndarray) -> None:
        """Copy image (any strides) into a free buffer and make it the newest frame. One writer thread."""
        with self._lock:
            busy = (self._ready, self._lent)
            fits = [b for b in self._buffers if b.shape == image.shape and b.dtype == image.dtype]
            if len(fits) < len(self._buffers):  # the size changed: let the old buffers go
                self._buffers = fits
            target = next((b for b in fits if not any(b is x for x in busy)), None)
            if target is None:
                target = np.empty(image.shape, image.dtype)
                self._buffers.append(target)
        np.copyto(target, image)  # outside the lock: the reader can't see this buffer yet
        with self._lock:
            self._ready = target

    def take(self) -> np.ndarray | None:
        """The newest frame, or None if nothing new. Valid until the next take()."""
        with self._lock:
            item, self._ready = self._ready, None
            if item is not None:
                self._lent = item
        return item

    def peek(self) -> np.ndarray | None:
        """The newest frame without taking it (None if nothing new)."""
        with self._lock:
            return self._ready
