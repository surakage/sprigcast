"""Hello sprig: the smallest SprigCast plugin that shows every basic part. Start here.

It adds a card that says "Hello, NAME!" and counts waves. Press a hotkey (or call the API) and it waves again.

To try it:
1. Copy this file into SprigCast's plugins folder (File → Open plugins folder).
2. Restart SprigCast.
3. Add it with + → Plugins → Hello sprig. Double-click it to change the name and colour.
4. Give "Hello sprig: Wave" a key in Tools → Hotkeys → Plugins, or call `hello_sprig.wave` from Streamer.bot.

The file has four parts, in this order:
  1. The source: what shows on the canvas (its id, its name, its settings)
  2. Drawing: turning the settings into a picture
  3. An action: something Streamer.bot, Stream Deck or a script can call
  4. A hotkey: the same thing from a key
"""

import numpy as np
from PIL import Image, ImageColor, ImageDraw

from sprigcast.control.native import ApiError, register_action, send_event
from sprigcast.core.source import Source, register
from sprigcast.plugin_hooks import register_hotkeys
from sprigcast.sources.text import load_font

# The kind id. SprigCast saves it with every bloom that uses this source, so:
#   - make it unique (start it with your own name, so it can't clash with a built-in or someone else's plugin)
#   - never change it once people use your plugin, or their cards stop loading.
KIND = "example_hello_sprig"

WIDTH, HEIGHT = 640, 160  # the card's size in pixels


# ---------------------------------------------------------------------------------------------------------------
# 1. The source
# ---------------------------------------------------------------------------------------------------------------

@register(KIND)  # this one line is what makes it show up under + → Plugins
class HelloSprig(Source):
    label = "Hello sprig"  # its name under + → Plugins

    # Its settings. Each key becomes a field in the source's Settings (double-click it), and the value here is
    # what a new card starts with. The type of the value picks the field: text → a text box, a whole number →
    # a number box, True/False → a tick box. A key that ends in "color" gets a Pick… button.
    defaults = {
        "name": "friend",
        "color": "#2f6b4fff",  # the card colour, as #RRGGBBAA
        "waves": 0,
    }

    # Nothing on screen shows it? Then SprigCast puts it to sleep, and it costs nothing. Our picture doesn't
    # change while it sleeps, so SprigCast can keep it too.
    sleeps = True
    keeps_picture = True

    def __init__(self, source_id, settings):
        super().__init__(source_id, settings)  # keeps the settings in self.settings
        self._redraw = True  # draw once at the start

    def update(self, settings):
        """SprigCast calls this when the settings change (the Settings dialog, an action, an undo)."""
        super().update(settings)
        self._redraw = True  # draw again on the next frame

    @property
    def size(self):
        """How big the picture is: (width, height)."""
        return WIDTH, HEIGHT

    def video_frame(self):
        """Called for every frame (30 or 60 times a second) on SprigCast's drawing thread.

        Return a new picture only when something changed, and None the rest of the time: None means "keep
        showing the last one", which costs nothing. Never wait for anything in here (a file, the network),
        or the stream stutters.
        """
        if not self._redraw:
            return None
        self._redraw = False
        return draw_card(self.settings)


# ---------------------------------------------------------------------------------------------------------------
# 2. Drawing
# ---------------------------------------------------------------------------------------------------------------

def draw_card(settings):
    """The card as an H × W × 4 RGBA picture (a NumPy array), which is what video_frame() returns.

    Any drawing library works as long as you hand back that array. Pillow ships with SprigCast, so it's used here.
    """
    try:
        color = ImageColor.getcolor(str(settings.get("color", "")), "RGBA")
    except ValueError:  # someone typed a colour that isn't one: fall back instead of failing
        color = ImageColor.getcolor(HelloSprig.defaults["color"], "RGBA")

    picture = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))  # see-through to start with
    pen = ImageDraw.Draw(picture)
    pen.rounded_rectangle((0, 0, WIDTH - 1, HEIGHT - 1), radius=36, fill=color)

    name = str(settings.get("name", "")).strip() or "friend"
    pen.text((40, 28), f"Hello, {name}!", font=load_font("segoeui.ttf", 56), fill="#f2f7f3")
    pen.text((40, 100), f"Waves: {_waves(settings)}", font=load_font("segoeui.ttf", 30), fill="#f2f7f3c0")

    return np.asarray(picture)


def _waves(settings):
    try:
        return max(0, int(settings.get("waves", 0)))
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------------------------------------------
# 3. An action
# ---------------------------------------------------------------------------------------------------------------
# Actions are what apps call: Streamer.bot, Stream Deck, a web link or a script. They're listed with
# SprigCast's own, so `http://127.0.0.1:PORT/api/hello_sprig.wave?password=YOUR_KEY` works too.
#
# Every action gets `api` (SprigCast's own API) and `data` (what the caller sent, as a dict).

def wave(api, data):
    cards = [s for s in api.col.sources if s.kind == KIND]  # every Hello sprig in the user's blooms
    if not cards:
        raise ApiError("not_found", "Add a Hello sprig first (+ → Plugins → Hello sprig).")

    for card in cards:
        waves = _waves(card.settings) + 1
        # source.set changes some of a source's settings, saves them with the user's blooms and calls update()
        # on the card. Change settings this way, never the card object itself: the card lives on the drawing
        # thread, and this runs on the window's thread.
        api.run("source.set", {"source": card.id, "settings": {"waves": waves}})

    send_event(api, "hello_sprig.waved", {"waves": waves})  # apps listening to the "plugin" group hear this
    return {"waves": waves}  # the answer the caller gets


# register_action(name, what it does, its fields, the function). Name it "<plugin>.<what>".
register_action("hello_sprig.wave", "Wave on every Hello sprig card.", {}, wave)


# ---------------------------------------------------------------------------------------------------------------
# 4. A hotkey
# ---------------------------------------------------------------------------------------------------------------
# Two small functions: one lists the hotkeys (id, name), the other runs one when its key is pressed.
# They show up in Tools → Hotkeys → Plugins, where the user picks the key.

def list_hotkeys(collection):
    return [("hello_sprig:wave", "Hello sprig: Wave")]


def press_hotkey(api, hotkey_id):
    if hotkey_id == "hello_sprig:wave":
        wave(api, {})  # if it raises an ApiError (no card yet), SprigCast shows its message in the status bar


register_hotkeys(list_hotkeys, press_hotkey)
