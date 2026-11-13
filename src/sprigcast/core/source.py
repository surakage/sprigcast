"""Base class for everything that produces frames, and the registry of source kinds."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Callable

import numpy as np


class Source(ABC):
    kind: str = "base"
    channel_order: str = "RGBA"  # byte order of video_frame(); "BGRA" is swizzled on the GPU for free

    def __init__(self, source_id: str, settings: dict[str, Any]):
        self.id = source_id
        self.settings = dict(settings)
        self.status = "ok"  # short human-readable state shown in the UI
        self.asleep = False  # set by the renderer while nothing on screen shows this source

    def start(self) -> None:
        """Open devices or spawn capture threads. Called on the render thread."""

    def stop(self) -> None:
        """Release everything start() opened."""

    def update(self, settings: dict[str, Any]) -> None:
        self.settings = dict(settings)

    # Sources nothing on screen shows are put to sleep by the renderer (see Engine._rest_sources): a
    # sleeping source does no work and holds no frames, and its texture is dropped. Kinds that can
    # come back quickly set `sleeps = True`. A sleeping source may never have been started.
    sleeps = False

    def sleep(self) -> None:
        """Stop working and let go of frames. Must not block: called on the render thread."""

    def wake(self) -> None:
        """Start again and send a fresh frame. Called on the render thread."""

    @property
    @abstractmethod
    def size(self) -> tuple[int, int]:
        """Current frame size in pixels (width, height)."""

    def video_frame(self) -> np.ndarray | None:
        """Newest frame as an H x W x 4 uint8 array in `channel_order`, or None to keep the last one.

        Called on the render thread once per tick. Must never block.
        """
        return None

    def audio_samples(self) -> np.ndarray | None:
        """Float32 samples (N x 2, 48 kHz) captured since the last call. Used from M6."""
        return None


_REGISTRY: dict[str, Callable[[str, dict[str, Any]], Source]] = {}


def register(kind: str):
    def wrap(cls):
        cls.kind = kind
        _REGISTRY[kind] = cls
        return cls

    return wrap


def create_source(kind: str, source_id: str, settings: dict[str, Any]) -> Source:
    import sprigcast.sources  # noqa: F401  (registers the built-in kinds)

    try:
        factory = _REGISTRY[kind]
    except KeyError:
        raise ValueError(f"unknown source kind {kind!r}") from None
    return factory(source_id, settings)


def known_kinds() -> list[str]:
    import sprigcast.sources  # noqa: F401

    return sorted(_REGISTRY)


def source_class(kind: str):
    """The class registered for `kind`, or None. Lets the UI read a kind's `label` and `defaults`."""
    import sprigcast.sources  # noqa: F401

    return _REGISTRY.get(kind)
