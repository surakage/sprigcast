"""Chat on your stream: the same mixed chat as the Chat panel, drawn straight onto the canvas.

No browser and no overlay site: lines come from the shared chat feed (chat/feed.py) that the chat
hub fills, and are drawn with Pillow into a box you size and place like any other source. Put it in
a bloom on both canvases and set its size per shape (tall and narrow for vertical, say). It only
redraws when chat changes, and sleeps with the rest when nothing on screen shows it.

Animated emotes move (user 2026-11-27): the box's words and bubbles are drawn once per change as before, and
ANIMATE_PER_S times a second only the moving emotes are pasted onto a copy of that picture, so a moving chat costs a
copy and a few small pastes, not a redraw. Only while one is on screen; "animate_emotes" off keeps them still.

Settings: width, height, size, hide_after (seconds, 0 = never), platform_marks, animate_emotes. Style tab (core/style.py):
chat_look (LOOKS: Garden card = bubbles, Clean outline = outlined text and no bubbles, Minimal = no bubbles or
marks and tight lines, Big text = cards at one and a half times the size), font, text_color, bubble_color,
background_color, corners (the bubbles') and custom_look (text, card = the bubbles, font, size, corners, border;
hide: marks).
"""

from __future__ import annotations

import logging
import threading
import time
import urllib.request
from collections import OrderedDict
from typing import Any

import numpy as np

from sprigcast.chat import PLATFORMS, ChatMessage, readable_colour
from sprigcast.chat.feed import FEED, Line
from sprigcast.core import painted, style
from sprigcast.core.source import Source, register
from sprigcast.sources.color import parse_color

log = logging.getLogger(__name__)

DEFAULTS = {
    "width": 480,
    "height": 640,
    "font": "segoeui.ttf",
    "size": 26,
    "text_color": "#ffffffff",
    "bubble_color": "#0e1311c8",  # a soft dark bubble behind each message ("#00000000" = none)
    "background_color": "#00000000",  # the whole box ("#00000000" = see-through)
    "hide_after": 0,  # seconds before a message fades away; 0 keeps them
    "platform_marks": True,  # a small bar in the platform's colour (Twitch purple, YouTube red)
    "animate_emotes": True,  # animated emotes move (off: their first picture)
    "corners": "round",  # the bubbles'
    "chat_look": "card",
    "custom_look": "",
}
# On-stream looks (user 2026-11-27): the same chat, four ways. key -> (name, bubbles, marks, size x, outline)
LOOKS = {
    "big": ("Big text", True, True, 1.5, False),
    "card": ("Garden card", True, True, 1.0, False),
    "minimal": ("Minimal", False, False, 1.0, False),
    "outline": ("Clean outline", False, True, 1.0, True),
}
TWITCH_EMOTE = "https://static-cdn.jtvnw.net/emoticons/v2/{id}/default/dark/2.0"
CHECK_EVERY = 0.2  # seconds between looks at the feed
ANIMATE_PER_S = 15  # how often moving emotes move on
MOVING_SIDE = 56  # an animated emote's pictures are kept at most this tall...
MOVING_FRAMES = 40  # ...and at most this many of them (a longer one skips pictures evenly)
MOVING_KEEP = 40  # animated emotes kept moving; the ones used longest ago go back to their first picture
MOVING_SHOWN = 30  # moving at once in one box; any more show their first picture
NOTICE_TEXT = (191, 243, 214, 255)
NOTICE_BUBBLE = (29, 52, 40, 220)


# Emote pictures: fetched off the render thread, kept small ---------------------------------------

class Moving:
    """An animated emote: its pictures (RGBA) and how long each shows."""

    def __init__(self, frames: list, seconds: list[float]):
        self.frames = frames
        self.ends: list[float] = []
        total = 0.0
        for s in seconds:
            total += max(0.02, s)
            self.ends.append(total)
        self.total = total
        self.width, self.height = frames[0].width, frames[0].height

    def at(self, now: float):
        """The picture showing at this time (every box and canvas shows the same one)."""
        from bisect import bisect_right

        return self.frames[min(len(self.frames) - 1, bisect_right(self.ends, now % self.total))]


def moving_from(image) -> Moving | None:
    """An opened GIF or WebP -> Moving, or None when it has one picture."""
    count = min(int(getattr(image, "n_frames", 1) or 1), 400)
    if count < 2:
        return None
    step = -(-count // MOVING_FRAMES)
    frames, seconds, waited = [], [], 0.0
    for i in range(count):
        image.seek(i)  # in order: each picture builds on the one before
        waited += float(image.info.get("duration") or 100) / 1000.0
        if i % step == step - 1 or i == count - 1:
            frame = image.convert("RGBA")
            if frame.height > MOVING_SIDE:
                frame = frame.resize((max(1, round(frame.width * MOVING_SIDE / frame.height)), MOVING_SIDE))
            frames.append(frame)
            seconds.append(waited)
            waited = 0.0
    return Moving(frames, seconds) if len(frames) > 1 else None


class _Emotes:
    """url -> RGBA picture (or Moving). Fetches on one helper thread so drawing never waits on the network."""

    LIMIT = 300

    def __init__(self):
        self._lock = threading.Lock()
        self._done: OrderedDict[str, object] = OrderedDict()  # url -> PIL image (or None: failed)
        self._queue: list[str] = []
        self._worker: threading.Thread | None = None
        self.version = 0  # moves when a picture arrives, so chat boxes redraw

    def get(self, url: str):
        with self._lock:
            if url in self._done:
                self._done.move_to_end(url)
                return self._done[url]
            if url not in self._queue:
                self._queue.append(url)
                if self._worker is None or not self._worker.is_alive():
                    self._worker = threading.Thread(target=self._run, name="chat-emotes", daemon=True)
                    self._worker.start()
        return None

    def _run(self) -> None:
        from io import BytesIO

        from PIL import Image

        while True:
            with self._lock:
                if not self._queue:
                    self._worker = None
                    return
                url = self._queue.pop(0)
            try:
                with urllib.request.urlopen(url, timeout=10) as reply:  # noqa: S310 (https emote CDNs only)
                    image = Image.open(BytesIO(reply.read(6_000_000)))
                    picture = moving_from(image)
                    if picture is None:
                        image.seek(0)
                        picture = image.convert("RGBA")
            except Exception as exc:  # unreachable, not a picture...
                log.debug("emote %s: %s", url, exc)
                picture = None
            with self._lock:
                self._done[url] = picture
                while len(self._done) > self.LIMIT:
                    self._done.popitem(last=False)
                moving = [u for u, p in self._done.items() if isinstance(p, Moving)]
                for old in moving[:-MOVING_KEEP]:  # (oldest used first) back to one picture: memory stays flat
                    self._done[old] = self._done[old].frames[0]
                self.version += 1


EMOTES = _Emotes()


def emote_url(message: ChatMessage, key: str) -> str | None:
    url = message.images.get(key) or (TWITCH_EMOTE.format(id=key) if message.platform == "twitch" else None)
    return url if url and url.startswith("https://") else None


# Laying out and drawing ---------------------------------------------------------------------

def _pieces(message: ChatMessage) -> list[tuple[str, str]]:
    """The message as ("text", words) and ("emote", url) pieces, words split so they can wrap."""
    text, out, at = message.text, [], 0
    for start, end, key in message.emotes:
        if start < at or end >= len(text):
            continue
        out.append(("text", text[at:start]))
        url = emote_url(message, key)
        out.append(("emote", url) if url else ("text", text[start:end + 1]))
        at = end + 1
    out.append(("text", text[at:]))
    pieces: list[tuple[str, str]] = []
    for kind, value in out:
        if kind == "emote":
            pieces.append((kind, value))
            continue
        for word in value.split(" "):
            if word:
                pieces.append(("text", word))
    return pieces


class ChatBox:
    """Draws chat lines into a width x height RGBA picture, newest at the bottom."""

    PAD, GAP, PX, PY = 14, 8, 12, 7  # outer padding, space between messages, bubble padding

    def __init__(self, settings: dict[str, Any]):
        from sprigcast.sources.text import load_font

        get = lambda key: settings.get(key, DEFAULTS[key])  # noqa: E731
        self.look_key = str(get("chat_look")) if str(get("chat_look")) in LOOKS else "card"
        _name, bubbles, marks, grow, outline = LOOKS[self.look_key]
        if not bubbles:  # no card: the lines sit closer, still clear of the box's edges
            self.GAP, self.PX, self.PY = 4, 4, 2
        self.width = max(120, int(get("width")))
        self.height = max(80, int(get("height")))
        look = style.resolve({"text_color": get("text_color"), "card_color": get("bubble_color"),
                              "corners": get("corners"), "custom_look": get("custom_look")},
                             {"text_color": DEFAULTS["text_color"], "card_color": DEFAULTS["bubble_color"]})
        self.size = max(10, round(int(get("size")) * look.scale * grow))
        font_file = str(get("font"))
        if look.font:  # a custom look's font: its Windows file, when it's one SprigCast knows
            font_file = style.FONT_FILES.get(look.font.lower(), font_file) or font_file
        self.font = load_font(font_file, self.size)
        bold_name = font_file.replace(".ttf", "b.ttf") if font_file.endswith(".ttf") else ""
        self.bold = load_font(bold_name, self.size) if bold_name else self.font
        self.colour = parse_color(look.text)
        self.bubble = parse_color(look.card) if bubbles else (0, 0, 0, 0)
        self.background = self._colour(get("background_color"), (0, 0, 0, 0))
        self.marks = bool(get("platform_marks")) and look.shows("marks") and marks
        self.animate = bool(get("animate_emotes"))
        self.scene: tuple | None = None  # (the picture without its moving emotes, as an array too, where they go)
        # Without a card the text needs its own edge to read over a game: a full outline, or a soft one
        self.stroke = (max(2, round(self.size / 11)), (0, 0, 0, 235)) if outline else \
            ((max(1, round(self.size / 20)), (0, 0, 0, 150)) if not bubbles else (0, None))
        self.radius = 14.0 if look.corners != "square" else 3.0
        if look.radius is not None:
            self.radius = look.radius
        self.leaf = look.corners == "leaf"
        self.border = (round(look.border_width), parse_color(look.border_color)) if look.border_width and bubbles \
            else None
        self.line_h = round(self.size * 1.32)
        self.space = self.font.getlength(" ")

    def _bubble(self, image, draw, box, h: float, fill) -> None:
        """One message's bubble: round, square or leaf (big top-left and bottom-right corners)."""
        r = max(0, min(round(self.radius), int(h // 2)))
        width, colour = self.border if self.border else (0, None)
        if not self.leaf or r < 2:
            draw.rounded_rectangle(box, radius=r, fill=fill if fill[3] else None, outline=colour, width=width)
            return
        from PIL import Image, ImageDraw, ImageFilter

        x0, y0, x1, y1 = (round(v) for v in box)
        w, hh = max(1, x1 - x0 + 1), max(1, y1 - y0 + 1)
        from PIL import ImageChops

        # One shape with two corner sizes (where both shapes are), so a see-through fill stays even.
        big, small = Image.new("L", (w, hh), 0), Image.new("L", (w, hh), 0)
        ImageDraw.Draw(big).rounded_rectangle((0, 0, w - 1, hh - 1), radius=min(r * 3 // 2, hh // 2), fill=255,
                                              corners=(True, False, True, False))
        ImageDraw.Draw(small).rounded_rectangle((0, 0, w - 1, hh - 1), radius=max(2, r // 4), fill=255,
                                                corners=(False, True, False, True))
        mask = ImageChops.darker(big, small)
        if fill[3]:
            image.alpha_composite(_layer(fill, mask), (x0, y0))
        if width:
            from PIL import ImageOps

            pad = width + 1  # (the edges of the picture count as outside, so the straight sides get a border too)
            inner = ImageOps.expand(mask, border=pad, fill=0).filter(ImageFilter.MinFilter(2 * width + 1))
            ring = ImageChops.subtract(mask, inner.crop((pad, pad, pad + w, pad + hh)))
            image.alpha_composite(_layer(colour, ring), (x0, y0))

    @staticmethod
    def _colour(value, fallback):
        try:
            return parse_color(str(value))
        except ValueError:
            return fallback

    def _wrap(self, message: ChatMessage, inner: float) -> list[list[tuple[str, str, object, float]]]:
        """Lines of (kind, value, colour/font, width) runs. The name leads the first line."""
        runs: list[tuple[str, str, object, float]] = []
        if message.notice:
            colour = NOTICE_TEXT
        else:
            name_colour = parse_color(readable_colour(message.color, message.user) + "ff")
            runs.append(("name", message.user, name_colour, self.bold.getlength(message.user)))
            colour = name_colour if message.action else self.colour
        wide = {message.images.get(key): ratio for key, ratio in message.ratios.items()}
        for kind, value in _pieces(message):
            if kind == "emote":
                # As wide as it will be drawn: a wide emote taken as square pushed the words after it out of the
                # bubble. Its picture says once it's here; until then, what its list said (chat/extra_emotes.py).
                picture = EMOTES.get(value)
                ratio = picture.width / max(1, picture.height) if picture is not None else wide.get(value, 1.0)
                runs.append(("emote", value, None, float(max(1, round((self.line_h - 2) * ratio)))))
            else:
                runs.append(("text", value, colour, self.font.getlength(value)))
        lines: list[list] = [[]]
        used = 0.0
        for run in runs:
            width = run[3]
            gap = self.space * (1.6 if lines[-1] and lines[-1][-1][0] == "name" else 1) if lines[-1] else 0
            if lines[-1] and used + gap + width > inner:
                lines.append([])
                used, gap = 0.0, 0
            if width > inner and run[0] == "text":  # one very long word: cut it to fit
                text = run[1]
                while text and self.font.getlength(text + "…") > inner:
                    text = text[:-1]
                run = ("text", text + "…", run[2], self.font.getlength(text + "…"))
                width = run[3]
            lines[-1].append(run)
            used += gap + width
        return lines

    @property
    def moving(self) -> bool:
        """Does the picture drawn last have emotes that move?"""
        return bool(self.scene and self.scene[2])

    def frame(self, now: float) -> np.ndarray:
        """The picture drawn last, with its moving emotes as they are at `now`: a copy and a few small pastes."""
        image, pixels, placed = self.scene
        out = pixels.copy()
        for picture, x, y, w, h in placed:
            x1, y1 = min(self.width, x + w), min(self.height, y + h)
            if x < 0 or y < 0 or x1 <= x or y1 <= y:
                continue
            patch = image.crop((x, y, x + w, y + h))
            patch.alpha_composite(picture.at(now).resize((w, h)))
            out[y:y1, x:x1] = np.asarray(patch, dtype=np.uint8)[:y1 - y, :x1 - x]
        return out

    def draw(self, lines: list[Line], now: float | None = None) -> np.ndarray:
        from PIL import Image, ImageDraw

        placed: list[tuple] = []
        image = Image.new("RGBA", (self.width, self.height), self.background)
        draw = ImageDraw.Draw(image, "RGBA")
        inner = self.width - 2 * self.PAD - 2 * self.PX - (6 if self.marks else 0)
        y = self.height - self.PAD
        for line in reversed(lines):
            message = line.message
            wrapped = self._wrap(message, inner)
            h = len(wrapped) * self.line_h + 2 * self.PY
            if y - h < self.PAD and y < self.height - self.PAD:
                break  # no room for a whole message: stop rather than cut one in half
            top = y - h
            x0 = self.PAD + (6 if self.marks else 0)
            widest = max(sum(r[3] for r in ln) + self.space * max(0, len(ln) - 1) * 1.1 for ln in wrapped)
            right = min(self.width - self.PAD, x0 + widest + 2 * self.PX)
            bubble = NOTICE_BUBBLE if message.notice and self.bubble[3] else self.bubble
            if bubble[3] or self.border:
                self._bubble(image, draw, (x0, top, right, y), h, bubble)
            if self.marks and message.platform in PLATFORMS:
                mark = parse_color(PLATFORMS[message.platform][1] + "ff")
                draw.rounded_rectangle((self.PAD, top + 3, self.PAD + 3, y - 3), radius=2, fill=mark)
            ty = top + self.PY
            for ln in wrapped:
                tx = x0 + self.PX
                for i, (kind, value, colour, width) in enumerate(ln):
                    if i:
                        tx += self.space * (1.6 if ln[i - 1][0] == "name" else 1)
                    if kind == "emote":
                        picture = EMOTES.get(value)
                        if picture is not None:
                            side = self.line_h - 2
                            width = max(1, round(side * picture.width / max(1, picture.height)))
                            if isinstance(picture, Moving) and self.animate and len(placed) < MOVING_SHOWN:
                                placed.append((picture, round(tx), round(ty + 1), width, side))  # pasted by frame()
                            else:
                                still = picture.frames[0] if isinstance(picture, Moving) else picture
                                image.alpha_composite(still.resize((width, side)), (round(tx), round(ty + 1)))
                        else:
                            width = self.line_h
                    else:
                        font = self.bold if kind == "name" else self.font
                        width_px, edge = self.stroke
                        draw.text((tx, ty + (self.line_h - self.size) * 0.35), value, font=font, fill=colour,
                                  stroke_width=width_px, stroke_fill=edge)
                    tx += width
                ty += self.line_h
            y = top - self.GAP
        pixels = np.asarray(image, dtype=np.uint8).copy()
        self.scene = (image, pixels, placed)
        return self.frame(time.monotonic() if now is None else now) if placed else pixels


def _layer(colour, mask):
    """A picture of one colour in the mask's shape, at the colour's own see-through-ness."""
    from PIL import Image

    layer = Image.new("RGBA", mask.size, colour[:3] + (0,))
    alpha = colour[3]
    layer.putalpha(mask if alpha >= 255 else mask.point(lambda v: v * alpha // 255))
    return layer


@register("chat")
class ChatSource(Source):
    label = "Chat"
    defaults = DEFAULTS
    choices = {"chat_look": tuple((key, LOOKS[key][0]) for key in sorted(LOOKS, key=lambda k: LOOKS[k][0]))}
    sleeps = True  # off screen: no drawing at all

    def __init__(self, source_id: str, settings: dict[str, Any]):
        super().__init__(source_id, settings)
        self._box: ChatBox | None = None
        self._frame: np.ndarray | None = None
        self._seen: tuple | None = None
        self._next = 0.0
        self._aside: tuple | None = None  # (a drawing running on the pictures lane, its box, what it shows)
        self._moved = 0.0  # when its moving emotes last moved on
        self.status = "waiting for chat"

    def update(self, settings: dict[str, Any]) -> None:
        super().update(settings)
        self._box, self._seen, self._next = None, None, 0.0

    def wake(self) -> None:
        self._seen, self._next = None, 0.0

    def sleep(self) -> None:
        self._frame = self._aside = None

    @property
    def size(self) -> tuple[int, int]:
        return (max(120, int(self.settings.get("width", DEFAULTS["width"]))),
                max(80, int(self.settings.get("height", DEFAULTS["height"]))))

    def _shown_lines(self, lines: list[Line], now: float) -> list[Line]:
        hide = float(self.settings.get("hide_after", DEFAULTS["hide_after"]) or 0)
        return [line for line in lines if not hide or now - line.at < hide] if hide else lines

    def video_frame(self) -> np.ndarray | None:
        now = time.monotonic()
        frame = self._changed(now)
        box = self._box
        if frame is None and self._aside is None and box is not None and self._frame is not None and box.moving \
                and now - self._moved >= 1.0 / ANIMATE_PER_S:
            frame = self._frame = box.frame(now)  # nothing new in chat: only the moving emotes move on
        if frame is not None:
            self._moved = now
        return frame

    def _changed(self, now: float) -> np.ndarray | None:
        """The box drawn again when chat changed (None: it hasn't, or the drawing is still running)."""
        if self._aside is not None:
            return self._landed()
        if now < self._next:
            return None
        self._next = now + CHECK_EVERY
        version, lines = FEED.version, None
        hide = float(self.settings.get("hide_after", DEFAULTS["hide_after"]) or 0)
        # What decides the picture: the feed, arrived emote pictures, and (when fading) which lines are still young.
        key = (version, EMOTES.version, int(now // 1) if hide else 0)
        if key == self._seen and self._frame is not None:
            return None
        _, lines = FEED.snapshot()
        shown = self._shown_lines(lines, now)
        visible_key = (version, EMOTES.version, tuple(id(line) for line in shown))
        if visible_key == getattr(self, "_drawn", None) and self._frame is not None:
            self._seen = key
            return None
        if self._box is None:
            self._box = ChatBox(self.settings)
        self.status = "ok" if lines else "waiting for chat"
        if self._frame is not None and painted.DRAW_ASIDE:
            # Drawn on the pictures lane, not here: wrapping and drawing a full box of messages with emotes on
            # the render thread held its tick each time a message arrived. The box shows the one before until
            # this one lands, a frame or two later.
            from sprigcast.core import workers

            self._aside = (workers.picture(self._box.draw, shown, name="chat drawing"), self._box, key, visible_key)
            return None
        self._frame = self._box.draw(shown)
        self._seen, self._drawn = key, visible_key
        return self._frame

    def _landed(self) -> np.ndarray | None:
        """The picture a worker drew, once it is done (None: still drawing, or it was for an older look)."""
        job, box, key, visible_key = self._aside
        if not job.done():
            return None
        self._aside = None
        if box is not self._box or job.exception() is not None:
            self._next = 0.0  # the settings changed meanwhile, or it failed (logged there): drawn again
            return None
        self._frame = job.result()
        self._seen, self._drawn = key, visible_key
        return self._frame
