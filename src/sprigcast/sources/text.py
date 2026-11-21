"""Text: titles, labels, a live clock, or the words in a text file that change live (a death counter, "now playing").

Settings (all optional; anything missing uses DEFAULTS):
    text                        the words; {time}, {time12} and {date} are filled in and refreshed every second
    font, size, bold, italic    a font file ("segoeui.ttf" or a full path), px; Bold and Italic use the family's own
                                bold or italic file, or draw it thicker or slanted when there isn't one
    letter_case                 "as_typed", "upper" or "lower"
    color                       #RRGGBBAA; its alpha is the text's opacity
    gradient, gradient_color, gradient_direction   a second color the text fades to, "down" or "across"
    outline_width, outline_color
    shadow, shadow_color, shadow_distance, shadow_blur
    box, box_color, box_padding, box_corners       a rounded backing card behind the words
    align                       "left", "center" or "right" (lines of more than one line)
    wrap_width                  px; longer lines wrap at a space (0: never)
    line_spacing                px between lines
    file, read_file, file_lines a text file whose words replace `text`, followed live (core/file_watch.py: within
                                a quarter second of it changing); file_lines "all", "first" or "last"
"""

from __future__ import annotations

import math
import threading
import time
import weakref
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

import numpy as np

from sprigcast.core import file_watch
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
    "read_file": True,
    "file_lines": "all",
    "bold": False,
    "italic": False,
    "letter_case": "as_typed",
    "gradient": False,
    "gradient_color": "#4fd88fff",
    "gradient_direction": "down",
    "shadow": False,
    "shadow_color": "#000000b3",
    "shadow_distance": 4,
    "shadow_blur": 6,
    "box": False,
    "box_color": "#0e1311cc",
    "box_padding": 16,
    "box_corners": 14,
    "align": "left",
    "wrap_width": 0,
    "line_spacing": 4,
}
# The keys added with the text leaf's own settings window (2026-11-21). A plugin made from TextSource keeps its short
# form: these are drawn from their defaults there unless the plugin sets them (app/settings_dialog.py form_hides).
LOOK_KEYS = tuple(k for k in DEFAULTS if k not in ("text", "font", "size", "color", "outline_color", "outline_width",
                                                   "file"))
FALLBACK_FONTS = ["segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"]
DRAW_ASIDE = True  # a text that changes is drawn again on a worker, not on the render thread (tests turn it off)
ITALIC_SLANT = 0.21  # how far a slanted (made-up italic) letter leans: about 12 degrees
CLOCK_TOKENS = ("{time}", "{time12}", "{date}")


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


def pick_lines(words: str, which: str) -> str:
    """All of a file's words, or just its first or last line with something on it."""
    if which not in ("first", "last"):
        return words
    lines = [line.strip() for line in words.split("\n") if line.strip()]
    if not lines:
        return ""
    return lines[0] if which == "first" else lines[-1]


def set_case(text: str, letter_case: str) -> str:
    return text.upper() if letter_case == "upper" else text.lower() if letter_case == "lower" else text


def wrap(text: str, font: ImageFont.ImageFont, width: float) -> str:
    """Lines longer than `width` px broken at a space (a word longer than the width stays whole)."""
    if width <= 0:
        return text
    out: list[str] = []
    for paragraph in text.split("\n"):
        line = ""
        for word in paragraph.split(" "):
            candidate = f"{line} {word}" if line else word
            if line and font.getlength(candidate) > width:
                out.append(line)
                line = word
            else:
                line = candidate
        out.append(line)
    return "\n".join(out)


@dataclass(frozen=True)
class Look:
    """Everything about how the words are drawn, at one scale (px already multiplied)."""

    color: tuple
    outline_color: tuple
    outline: int
    thicken: int  # made-up bold: the letters drawn with a stroke of their own color
    slant: bool
    gradient: tuple | None  # the color the fill fades to
    across: bool
    shadow: tuple | None
    shadow_distance: int
    shadow_blur: int
    box: tuple | None
    box_padding: int
    box_corners: int
    align: str
    spacing: int


def _text_box(text: str, font, look: Look) -> tuple[int, int, int, int]:
    from PIL import Image, ImageDraw

    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    return probe.multiline_textbbox((0, 0), text or " ", font=font, stroke_width=look.outline + look.thicken,
                                    align=look.align, spacing=look.spacing)


def _extent(text: str, font, look: Look) -> tuple[int, int, int, int, int, int]:
    """(width, height of the whole picture, left, top of the words' box, words' width, height)."""
    left, top, right, bottom = _text_box(text, font, look)
    left, top = math.floor(left), math.floor(top)
    tw, th = max(1, math.ceil(right) - left), max(1, math.ceil(bottom) - top)
    if look.slant:
        tw += math.ceil(th * ITALIC_SLANT)
    w, h = tw, th
    if look.shadow is not None:
        w += look.shadow_distance + 2 * look.shadow_blur
        h += look.shadow_distance + 2 * look.shadow_blur
    if look.box is not None:
        w += 2 * look.box_padding
        h += 2 * look.box_padding
    return max(1, w), max(1, h), left, top, tw, th


def text_size(text: str, font: ImageFont.ImageFont, outline_width: int, look: Look | None = None) -> tuple[int, int]:
    """The size render_text's picture has for this text (at least 1x1)."""
    look = look or plain_look(outline_width)
    w, h, *_ = _extent(text, font, look)
    return w, h


def plain_look(outline_width: int = 0, color=(255, 255, 255, 255), outline_color=(0, 0, 0, 255)) -> Look:
    return Look(color, outline_color, outline_width, 0, False, None, False, None, 0, 0, None, 0, 0, "left", 4)


def render_text(text: str, font: ImageFont.ImageFont, color, outline_color, outline_width: int,
                look: Look | None = None) -> np.ndarray:
    """RGBA image just big enough for the text and its shadow and box (at least 1x1), transparent elsewhere."""
    from PIL import Image, ImageChops, ImageDraw, ImageFilter

    look = look or plain_look(outline_width, color, outline_color)
    w, h, left, top, tw, th = _extent(text, font, look)
    pad = look.box_padding if look.box is not None else 0
    words = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
    draw = ImageDraw.Draw(words)
    at = (-left, -top)  # a slanted text's lean is room on the right (its tops move right)
    kw = {"font": font, "align": look.align, "spacing": look.spacing}
    if look.gradient is None and look.outline == 0 and look.thicken == 0:
        draw.multiline_text(at, text, fill=look.color, **kw)
    elif look.gradient is None:  # one color, an outline: Pillow draws the stroke under the fill itself
        stroke = look.outline + look.thicken
        draw.multiline_text(at, text, fill=look.color, stroke_width=stroke,
                            stroke_fill=look.outline_color if look.outline else look.color, **kw)
        if look.outline and look.thicken:  # made-up bold under an outline: the fill thickened over the outline
            draw.multiline_text(at, text, fill=look.color, stroke_width=look.thicken, stroke_fill=look.color, **kw)
    else:
        if look.outline:
            draw.multiline_text(at, text, fill=look.outline_color, stroke_width=look.outline + look.thicken,
                                stroke_fill=look.outline_color, **kw)
        mask = Image.new("L", (tw, th), 0)
        ImageDraw.Draw(mask).multiline_text(at, text, fill=255, stroke_width=look.thicken, stroke_fill=255, **kw)
        fill = _gradient((tw, th), look.color, look.gradient, look.across)
        fill.putalpha(ImageChops.multiply(fill.getchannel("A"), mask))
        words = Image.alpha_composite(words, fill)
    if look.slant:  # lean the letters right about their baseline-ish middle
        words = words.transform(words.size, Image.Transform.AFFINE, (1, ITALIC_SLANT, -ITALIC_SLANT * th, 0, 1, 0),
                                resample=Image.Resampling.BILINEAR)
    content = words
    if look.shadow is not None:
        d, blur = look.shadow_distance, look.shadow_blur
        content = Image.new("RGBA", (tw + d + 2 * blur, th + d + 2 * blur), (0, 0, 0, 0))
        r, g, b, a = look.shadow
        shade = Image.new("RGBA", words.size, (r, g, b, 0))
        shade.putalpha(words.getchannel("A").point(lambda v: v * a // 255))
        holder = Image.new("RGBA", content.size, (r, g, b, 0))
        holder.paste(shade, (blur + d, blur + d))
        if blur:
            holder = holder.filter(ImageFilter.GaussianBlur(blur / 2))
        content = Image.alpha_composite(holder, _placed(words, content.size, (blur, blur)))
    if look.box is not None:
        card = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        ImageDraw.Draw(card).rounded_rectangle((0, 0, w - 1, h - 1), radius=min(look.box_corners, min(w, h) // 2),
                                               fill=look.box)
        content = Image.alpha_composite(card, _placed(content, (w, h), (pad, pad)))
    return np.asarray(content, dtype=np.uint8).copy()


def _placed(image, size, at):
    from PIL import Image

    out = Image.new("RGBA", size, (0, 0, 0, 0))
    out.paste(image, at)
    return out


def _gradient(size: tuple[int, int], start: tuple, end: tuple, across: bool):
    """An RGBA picture fading from start to end, top to bottom (or left to right)."""
    from PIL import Image

    w, h = size
    steps = w if across else h
    t = np.linspace(0.0, 1.0, max(1, steps), dtype=np.float32)[:, None]
    line = (np.array(start, np.float32) * (1 - t) + np.array(end, np.float32) * t).round().astype(np.uint8)
    pixels = np.broadcast_to(line[None, :, :], (h, w, 4)) if across else np.broadcast_to(line[:, None, :], (h, w, 4))
    return Image.fromarray(np.ascontiguousarray(pixels), "RGBA")


@register("text")
class TextSource(Source):
    defaults = DEFAULTS  # tells the settings dialog which fields to show
    form_hides = LOOK_KEYS  # a plugin made from it keeps its short form (the text leaf has its own window)
    choices = {
        "align": (("center", "Center"), ("left", "Left"), ("right", "Right")),
        "letter_case": (("as_typed", "As typed"), ("lower", "Lowercase"), ("upper", "Uppercase")),
        "file_lines": (("all", "All"), ("first", "First line"), ("last", "Last line")),
        "gradient_direction": (("across", "Across"), ("down", "Down")),
    }
    sleeps = True  # a hidden clock isn't redrawn every second
    keeps_picture = True  # asleep its texture stays; woken, it's drawn again only if the words changed
    sharp = True  # drawn with the font at the size it's shown, so it stays crisp made big

    def __init__(self, source_id: str, settings: dict[str, Any]):
        super().__init__(source_id, settings)
        self._frame: np.ndarray | None = None
        self._shown: tuple | None = None
        self._font_key: tuple | None = None
        self._font = None
        self._drawn_font_key: tuple | None = None  # the font at the size it's drawn (size x detail)
        self._drawn_font = None
        self._layout: tuple[int, int] | None = None  # the text's box at its own size: what `size` says
        self._next_check = 0.0
        self._pending = False  # a rendered frame the compositor hasn't received yet
        self._aside = None  # a redraw running on a worker (DRAW_ASIDE)
        self._lock = threading.Lock()  # one drawing at a time: the render thread's first, or a worker's
        self._watch_key = f"{source_id}#{id(self)}"  # its own: a new source with the same id never shares it
        self._file_version = -1
        self._file_words = ""  # the last words read: shown while the file is missing
        weakref.finalize(self, file_watch.forget, self._watch_key)

    def update(self, settings: dict[str, Any]) -> None:
        super().update(settings)
        self._next_check = 0.0

    def stop(self) -> None:
        file_watch.forget(self._watch_key)

    def wake(self) -> None:
        self._next_check = 0.0  # look at once: a clock or a file may have changed while it slept

    def set_detail(self, detail: float) -> None:
        super().set_detail(detail)
        self._next_check = 0.0  # drawn again at the new size on the next frame

    def _get(self, key: str):
        return self.settings.get(key, DEFAULTS[key])

    def _int(self, key: str, low: int = 0, high: int = 4000) -> int:
        try:
            return min(high, max(low, int(self._get(key))))
        except (TypeError, ValueError):
            return int(DEFAULTS[key])

    def _color(self, key: str) -> tuple:
        try:
            return parse_color(str(self._get(key)))
        except ValueError as exc:
            self.status = str(exc)
            return parse_color(str(DEFAULTS[key]))

    @property
    def size(self) -> tuple[int, int]:
        if self._frame is None or self._layout is None:
            with self._lock:
                if self._frame is None or self._layout is None:
                    self._render()
        return self._layout

    def _file(self) -> str:
        """The text file it follows, or "" (none set, or Read from a file is off)."""
        if not self._get("read_file"):
            return ""
        return str(self._get("file") or "").strip()

    def _current_text(self) -> str:
        path = self._file()
        if not path:
            return str(self._get("text"))
        key = self._watch_key
        if file_watch.following(key) == path and file_watch.read_once(key):
            if not file_watch.missing(key):
                self._file_words = file_watch.text(key)
            self.status = "file not found" if file_watch.missing(key) else "ok"
        else:  # not followed yet (its first drawing, or a size asked for once): read it here, once
            try:
                self._file_words = file_watch.read_words(path)
                self.status = "ok"
            except OSError:
                self.status = "file not found"
        return pick_lines(self._file_words, str(self._get("file_lines")))

    def _follow_file(self) -> None:
        """Follow the file while it's set; called each frame on the render thread (a dict lookup, no file I/O)."""
        path = self._file()
        if not path:
            if self._file_version != -1:
                file_watch.forget(self._watch_key)
                self._file_version = -1
            return
        if file_watch.following(self._watch_key) != path:
            file_watch.follow(self._watch_key, path)
        version = file_watch.version(self._watch_key)
        if version != self._file_version:
            self._file_version = version
            self._next_check = 0.0  # the words changed: drawn again now, not on the next one-second look

    def _look(self, scale: float, font_file: str, thicken: bool, slant: bool) -> Look:
        size = max(4, self._int("size", 4))

        def px(value: int) -> int:
            return round(value * scale)

        return Look(
            color=self._color("color"),
            outline_color=self._color("outline_color"),
            outline=px(self._int("outline_width", 0, 200)),
            thicken=max(1, round(size * scale / 28)) if thicken else 0,
            slant=slant,
            gradient=self._color("gradient_color") if self._get("gradient") else None,
            across=self._get("gradient_direction") == "across",
            shadow=self._color("shadow_color") if self._get("shadow") else None,
            shadow_distance=px(self._int("shadow_distance", 0, 200)),
            shadow_blur=px(self._int("shadow_blur", 0, 100)),
            box=self._color("box_color") if self._get("box") else None,
            box_padding=px(self._int("box_padding", 0, 400)),
            box_corners=px(self._int("box_corners", 0, 400)),
            align=str(self._get("align")) if self._get("align") in ("left", "center", "right") else "left",
            spacing=px(self._int("line_spacing", -200, 400)),
        )

    def _render(self) -> bool:
        """Re-render if anything visible changed. Returns True when the frame is new."""
        from sprigcast.sources import fonts

        words = self._current_text()
        if any(token in words for token in CLOCK_TOKENS):
            words = fill_tokens(words, datetime.now())
        words = set_case(words, str(self._get("letter_case")))
        size = max(4, self._int("size", 4))
        detail = self.detail if self.detail > 0 else 1.0
        font_name = str(self._get("font"))
        bold, italic = bool(self._get("bold")), bool(self._get("italic"))
        key = (words, font_name, size, bold, italic, detail,
               tuple(str(self.settings.get(k, DEFAULTS[k])) for k in DEFAULTS if k not in ("text", "file")))
        if key == self._shown and self._frame is not None:
            return False
        font_file, thicken, slant = fonts.resolve(font_name, bold, italic) if bold or italic else (font_name, False,
                                                                                                    False)
        font_key = (font_file, size)
        if font_key != self._font_key:
            self._font, self._font_key = load_font(*font_key), font_key
        look = self._look(1.0, font_file, thicken, slant)
        wrap_width = self._int("wrap_width", 0, 20000)
        shown = wrap(words, self._font, wrap_width)
        layout = text_size(shown, self._font, look.outline, look)
        if detail == 1.0:
            frame = render_text(shown, self._font, look.color, look.outline_color, look.outline, look)
        else:  # the same text with the font made bigger (or smaller): sharp at the size it's shown
            drawn_key = (font_file, max(1, round(size * detail)))
            if drawn_key != self._drawn_font_key:
                self._drawn_font, self._drawn_font_key = load_font(*drawn_key), drawn_key
            big = self._look(detail, font_file, thicken, slant)
            drawn = wrap(words, self._drawn_font, wrap_width * detail)
            frame = render_text(drawn, self._drawn_font, big.color, big.outline_color, big.outline, big)
        self._layout, self._frame = layout, frame  # together: a worker's drawing lands whole (DRAW_ASIDE)
        self._shown = key
        self._pending = True
        if not self._file():
            self.status = "ok"
        return True

    def video_frame(self) -> np.ndarray | None:
        now = time.monotonic()
        self._follow_file()
        aside = self._aside
        if aside is not None and aside.done():
            self._aside = aside = None
        if now >= self._next_check and aside is None:
            self._next_check = now + 1.0  # clocks refresh once a second; a followed file as soon as it changes
            if self._frame is None or not DRAW_ASIDE:
                with self._lock:
                    self._render()
            else:
                # Drawn on a worker: a big clock or "now playing" line with an outline took 10-13 ms of the
                # render thread each time it changed in the heavy run (2026-11-17). The picture shown until then
                # is the one before, a frame or two longer.
                from sprigcast.core import workers

                self._aside = workers.picture(self._render_aside, name="text drawing")
        if not self._pending:
            return None
        self._pending = False
        return self._frame

    def _render_aside(self) -> None:
        with self._lock:
            self._render()
