"""A solid color rectangle. Settings: color ("#RRGGBB" or "#RRGGBBAA"), width, height."""

from __future__ import annotations

from typing import Any

import numpy as np

from sprigcast.core.source import Source, register


def parse_color(text: str) -> tuple[int, int, int, int]:
    text = text.strip().lstrip("#")
    if len(text) == 6:
        text += "ff"
    if len(text) != 8:
        raise ValueError(f"color must be #RRGGBB or #RRGGBBAA, got {text!r}")
    return tuple(int(text[i : i + 2], 16) for i in range(0, 8, 2))  # type: ignore[return-value]


@register("color")
class ColorSource(Source):
    def __init__(self, source_id: str, settings: dict[str, Any]):
        super().__init__(source_id, settings)
        self._dirty = True

    def update(self, settings: dict[str, Any]) -> None:
        super().update(settings)
        self._dirty = True

    @property
    def size(self) -> tuple[int, int]:
        return int(self.settings.get("width", 1920)), int(self.settings.get("height", 1080))

    def video_frame(self) -> np.ndarray | None:
        if not self._dirty:
            return None
        self._dirty = False
        # A colour source is a single colour, so a tiny texture stretched by the compositor would do,
        # but uploading at full size keeps crop maths identical to every other source.
        w, h = self.size
        frame = np.empty((h, w, 4), dtype=np.uint8)
        frame[:] = parse_color(str(self.settings.get("color", "#000000")))
        return frame
