"""Text: titles, labels, a live clock, or the contents of a text file (for "now playing" tools).

Settings: text, font (a font file name such as "segoeui.ttf" or a full path), size (px),
color, outline_color, outline_width, file (optional path; its contents replace `text`).
{time}, {time12} and {date} in the text are filled in and refreshed every second.
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from sprigcast.core.source import Source, register
from sprigcast.sources.color import parse_color

if TYPE_CHECKING:  # Pillow loads only once a text source draws (it costs a few MB)
    from PIL import ImageFont

DEFAULTS = {
    "text": "SprigCast",
    "font": "segoeui.ttf",
    "size": 72,
    "color": "#ffffffff",
    "outline_color": "#000000ff",
    "outline_width": 0,
    "file": "",
}
FALLBACK_FONTS = ["segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"]


def load_font(name: str, size: int) -> ImageFont.ImageFont:
    from PIL import ImageFont

    for candidate in [name, *FALLBACK_FONTS]:
        if not candidate:
            continue
        try:
            return ImageFont.truetype(candidate, size)  # Pillow also looks in the Windows fonts folder
        except OSError:
            continue
    return ImageFont.load_default(size)


def fill_tokens(text: str, now: datetime) -> str:
    return (
        text.replace("{time}", now.strftime("%H:%M:%S"))
        .replace("{time12}", now.strftime("%I:%M %p").lstrip("0"))
        .replace("{date}", now.strftime("%Y-%m-%d"))
    )


def render_text(text: str, font: ImageFont.ImageFont, color, outline_color, outline_width: int) -> np.ndarray:
    """RGBA image just big enough for the text (at least 1x1), transparent elsewhere."""
    from PIL import Image, ImageDraw

    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    left, top, right, bottom = probe.multiline_textbbox((0, 0), text or " ", font=font, stroke_width=outline_width)
    w, h = max(1, right - left), max(1, bottom - top)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(img).multiline_text(
        (-left, -top), text, font=font, fill=color, stroke_width=outline_width, stroke_fill=outline_color
    )
    return np.asarray(img, dtype=np.uint8).copy()


@register("text")
class TextSource(Source):
    defaults = DEFAULTS  # tells the settings dialog which fields to show

    def __init__(self, source_id: str, settings: dict[str, Any]):
        super().__init__(source_id, settings)
        self._frame: np.ndarray | None = None
        self._shown: tuple | None = None
        self._font_key: tuple | None = None
        self._font = None
        self._next_check = 0.0
        self._pending = False  # a rendered frame the compositor hasn't received yet

    def update(self, settings: dict[str, Any]) -> None:
        super().update(settings)
        self._next_check = 0.0

    def _get(self, key: str):
        return self.settings.get(key, DEFAULTS[key])

    @property
    def size(self) -> tuple[int, int]:
        if self._frame is None:
            self._render()
        h, w = self._frame.shape[:2]
        return w, h

    def _current_text(self) -> str:
        path = str(self._get("file") or "")
        if path:
            try:
                return Path(path).read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                self.status = "file not found"
                return ""
        return str(self._get("text"))

    def _render(self) -> bool:
        """Re-render if anything visible changed. Returns True when the frame is new."""
        text = fill_tokens(self._current_text(), datetime.now())
        try:
            color = parse_color(str(self._get("color")))
            outline = parse_color(str(self._get("outline_color")))
        except ValueError as exc:
            self.status = str(exc)
            color, outline = (255, 255, 255, 255), (0, 0, 0, 255)
        size = max(4, int(self._get("size")))
        width = max(0, int(self._get("outline_width")))
        key = (text, str(self._get("font")), size, color, outline, width)
        if key == self._shown and self._frame is not None:
            return False
        font_key = (str(self._get("font")), size)
        if font_key != self._font_key:
            self._font, self._font_key = load_font(*font_key), font_key
        self._frame = render_text(text, self._font, color, outline, width)
        self._shown = key
        self._pending = True
        if not self._get("file") or self.status != "file not found":
            self.status = "ok"
        return True

    def video_frame(self) -> np.ndarray | None:
        now = time.monotonic()
        if now >= self._next_check:
            self._next_check = now + 1.0  # clocks and text files refresh once a second
            self._render()
        if not self._pending:
            return None
        self._pending = False
        return self._frame
