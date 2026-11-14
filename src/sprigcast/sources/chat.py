"""Chat on your stream: the same mixed chat as the Chat panel, drawn straight onto the canvas.

No browser and no overlay site: lines come from the shared chat feed (chat/feed.py) that the chat
hub fills, and are drawn with Pillow into a box you size and place like any other source. Put it in
a bloom on both canvases and set its size per shape (tall and narrow for vertical, say). It only
redraws when chat changes, and sleeps with the rest when nothing on screen shows it.

Settings: width, height, font, size, text_color, bubble_color, background_color, hide_after
(seconds, 0 = never), platform_marks.
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
    "platform_marks": True,  # a small bar in the platform's colour (Twitch purple, YouTube red, Kick green)
}
TWITCH_EMOTE = "https://static-cdn.jtvnw.net/emoticons/v2/{id}/default/dark/2.0"
CHECK_EVERY = 0.2  # seconds between looks at the feed
NOTICE_TEXT = (191, 243, 214, 255)
NOTICE_BUBBLE = (29, 52, 40, 220)


# Emote pictures: fetched off the render thread, kept small ---------------------------------------

class _Emotes:
    """url -> RGBA picture. Fetches on one helper thread so drawing never waits on the network."""

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
                    image = Image.open(BytesIO(reply.read()))
                    image.seek(0)  # first frame of an animated emote
                    picture = image.convert("RGBA")
            except Exception as exc:  # unreachable, not a picture...
                log.debug("emote %s: %s", url, exc)
                picture = None
            with self._lock:
                self._done[url] = picture
                while len(self._done) > self.LIMIT:
                    self._done.popitem(last=False)
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
        self.width = max(120, int(get("width")))
        self.height = max(80, int(get("height")))
        self.size = max(10, int(get("size")))
        self.font = load_font(str(get("font")), self.size)
        bold_name = str(get("font")).replace(".ttf", "b.ttf") if str(get("font")).endswith(".ttf") else ""
        self.bold = load_font(bold_name, self.size) if bold_name else self.font
        self.colour = self._colour(get("text_color"), (255, 255, 255, 255))
        self.bubble = self._colour(get("bubble_color"), (14, 19, 17, 200))
        self.background = self._colour(get("background_color"), (0, 0, 0, 0))
        self.marks = bool(get("platform_marks"))
        self.line_h = round(self.size * 1.32)
        self.space = self.font.getlength(" ")

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
        for kind, value in _pieces(message):
            if kind == "emote":
                runs.append(("emote", value, None, float(self.line_h)))
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

    def draw(self, lines: list[Line]) -> np.ndarray:
        from PIL import Image, ImageDraw

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
            bubble = NOTICE_BUBBLE if message.notice else self.bubble
            if bubble[3]:
                draw.rounded_rectangle((x0, top, right, y), radius=min(14, h // 2), fill=bubble)
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
                            scaled = picture.resize((max(1, round(side * picture.width / max(1, picture.height))), side))
                            image.alpha_composite(scaled, (round(tx), round(ty + 1)))
                            width = scaled.width
                        else:
                            width = self.line_h
                    else:
                        font = self.bold if kind == "name" else self.font
                        draw.text((tx, ty + (self.line_h - self.size) * 0.35), value, font=font, fill=colour)
                    tx += width
                ty += self.line_h
            y = top - self.GAP
        return np.asarray(image, dtype=np.uint8).copy()


@register("chat")
class ChatSource(Source):
    label = "Chat"
    defaults = DEFAULTS
    sleeps = True  # off screen: no drawing at all

    def __init__(self, source_id: str, settings: dict[str, Any]):
        super().__init__(source_id, settings)
        self._box: ChatBox | None = None
        self._frame: np.ndarray | None = None
        self._seen: tuple | None = None
        self._next = 0.0
        self.status = "waiting for chat"

    def update(self, settings: dict[str, Any]) -> None:
        super().update(settings)
        self._box, self._seen, self._next = None, None, 0.0

    def wake(self) -> None:
        self._seen, self._next = None, 0.0

    def sleep(self) -> None:
        self._frame = None

    @property
    def size(self) -> tuple[int, int]:
        return (max(120, int(self.settings.get("width", DEFAULTS["width"]))),
                max(80, int(self.settings.get("height", DEFAULTS["height"]))))

    def _shown_lines(self, lines: list[Line], now: float) -> list[Line]:
        hide = float(self.settings.get("hide_after", DEFAULTS["hide_after"]) or 0)
        return [line for line in lines if not hide or now - line.at < hide] if hide else lines

    def video_frame(self) -> np.ndarray | None:
        now = time.monotonic()
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
        self._frame = self._box.draw(shown)
        self._seen, self._drawn = key, visible_key
        self.status = "ok" if lines else "waiting for chat"
        return self._frame
