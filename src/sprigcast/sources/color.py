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


MOST_PIXELS = 16384


@register("color")
class ColorSource(Source):
    stretched = True  # its picture is a few pixels shown at `size` (render/engine.py tells the engine)

    def __init__(self, source_id: str, settings: dict[str, Any]):
        super().__init__(source_id, settings)
        self._dirty = True

    @staticmethod
    def check_settings(col, settings: dict[str, Any]) -> dict[str, Any]:
        """What an app may set (control/native.py): a color that reads as one, a size that is one."""
        if "color" in settings:
            if not isinstance(settings["color"], str):
                raise ValueError("color's color must be text like #RRGGBB or #RRGGBBAA")
            try:
                parse_color(settings["color"])
            except ValueError:
                raise ValueError("color's color must be #RRGGBB or #RRGGBBAA") from None
        for key in ("width", "height"):
            value = settings.get(key, 1)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 1 <= value <= MOST_PIXELS:
                raise ValueError(f"color's {key} must be a number from 1 to {MOST_PIXELS}")
        return settings

    def update(self, settings: dict[str, Any]) -> None:
        super().update(settings)
        self._dirty = True

    @property
    def size(self) -> tuple[int, int]:
        try:
            return int(self.settings.get("width", 1920)), int(self.settings.get("height", 1080))
        except (TypeError, ValueError, OverflowError):  # a saved size that isn't one: the usual size
            return 1920, 1080

    def video_frame(self) -> np.ndarray | None:
        if not self._dirty:
            return None
        self._dirty = False
        # One colour needs no more than a few pixels: the compositor stretches the texture over the
        # source's size (crop and position work in fractions of it). A full-size texture cost 8 MB of
        # RAM in the graphics driver for every 1080p background.
        frame = np.empty((2, 2, 4), dtype=np.uint8)
        try:
            frame[:] = parse_color(str(self.settings.get("color", "#000000")))
        except ValueError:  # a saved color that isn't one draws black, not an error on every change
            frame[:] = (0, 0, 0, 255)
        return frame
