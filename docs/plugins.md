# Build your own plugin

A SprigCast plugin is one Python file. It can add a new source, actions that Streamer.bot, Stream Deck or scripts can call, events they can react to, hotkeys, right-click entries, a Tools menu entry and audio filters.

Plugins run with the same rights as SprigCast, so only add ones you trust.

## Set up

1. Install SprigCast.
2. Open **File → Open plugins folder**. It's `%LOCALAPPDATA%\Slothbloom\SprigCast\plugins`.
3. Put your `.py` file there and restart SprigCast.

You don't need to install Python: SprigCast brings its own (3.12). A plugin can use Python's standard library and what SprigCast already ships with: NumPy, Pillow and PySide6 (Qt). Other packages from pip aren't there.

Files whose name starts with `_` are skipped. A plugin that fails to load is skipped too, and its error shows under **Your plugins** in Tools → Plugin store and in the log (`Documents\SprigCast\Logs`). It never stops SprigCast.

The first start puts `countdown_example.py` in the folder: a small working plugin to copy from.

## Your first plugin

A text source that shouts:

```python
from sprigcast.core.source import register
from sprigcast.sources.text import TextSource


@register("shout")
class ShoutSource(TextSource):
    label = "Shout"                                   # its name under + → Plugins
    defaults = {**TextSource.defaults, "text": "hi"}  # the fields in its Settings

    def _current_text(self):
        return self.settings.get("text", "").upper() + "!"
```

Save it as `shout.py`, restart, then add it with **+ → Plugins → Shout**. Double-click it to change the text.

The id you give `@register` is saved with the user's blooms, so never change it once people use your plugin.

## What a plugin can add

| To add | Use | Where it shows |
|---|---|---|
| A source | `@register("id")` on a `Source` class | + → Plugins |
| An action for apps | `register_action(name, about, fields, run)` | The API, `app.actions` |
| An event for apps | `send_event(api, name, data)` | The API's `plugin` event group |
| Hotkeys | `register_hotkeys(list_fn, run_fn)` | Tools → Hotkeys → Plugins |
| Right-click entries | `register_item_menu(fn)` | Right-click on something in a bloom |
| A Tools menu entry | `register_tool(label, fn)` | Tools menu |
| An audio filter | `register_filter(kind, label, "audio", fields)` | A source's Filters |
| Sound levels | `register_level_watcher(fn)` | About 15 times a second |
| Screenshot hooks | `register_on_screenshot(fn)` | After each screenshot |

`register_action` and `send_event` come from `sprigcast.control.native`. The hooks come from `sprigcast.plugin_hooks`, and `register_filter` from `sprigcast.core.filters`. Each one's docstring says exactly what it's given.

Every hook gets `api`, which is SprigCast's own API:

- `api.run("bloom.show", {"bloom": "BRB"})` runs any [API action](control-api.md), the same way an app would.
- `api.col` holds the blooms, sprigs and sources.
- `api.host.commit("sources")` saves and redraws after you change `api.col` yourself. Most of the time `api.run(...)` is simpler: it saves for you.

## A full example: Shout-out

[shoutout.py](plugins/shoutout.py) is a complete plugin to copy. It adds:

- A **Shout-out** source: a text card that says "Go follow NAME!".
- The actions `shoutout.show` (with `name`) and `shoutout.clear`.
- The events `shoutout.shown` and `shoutout.cleared`.
- A hotkey (Shout-out: Clear) and a right-click entry (Clear shout-out).

The action part looks like this:

```python
from sprigcast.control.native import ApiError, register_action, send_event


def show(api, data):
    name = str(data.get("name", "")).strip().lstrip("@")
    if not name:
        raise ApiError("bad_request", "name is empty.")
    for card in [s for s in api.col.sources if s.kind == "shoutout"]:
        api.run("source.set", {"source": card.id, "settings": {"name": name}})
    send_event(api, "shoutout.shown", {"name": name})
    return {"name": name}


register_action("shoutout.show", "Show a shout-out for someone.", {"name": "who to shout out"}, show)
```

Rules for actions and events:

- Name them `<plugin>.<what>`: `shoutout.show`, `shoutout.shown`.
- In `fields`, start a description with `?` when the field is optional: `{"seconds": "?how long"}`.
- Fail with `ApiError(code, message)`. Use the codes in [the control API](control-api.md#asking): `bad_request`, `not_found`, `not_allowed`, `busy`.
- What `run` returns is the answer apps get, so return a dict.
- Values from web links arrive as text, so `"3"` may come in where you expect `3`. Accept both.
- Don't rename an action or event once people use it. Add new ones instead.

## Use it from Streamer.bot

Your actions work everywhere SprigCast's own do. Streamer.bot can pass any text along, such as a chat user's name.

**With the SprigCast pack** ([Streamer.bot](streamerbot.md)): add two arguments, then run the SprigCast action's `Execute` method.

```
sprigcastAction   shoutout.show
sprigcastJson     {"name": "%user%"}
```

**With a web link** (turn on **Allow web links** in Settings → Connections), use a Fetch URL sub-action:

```
http://127.0.0.1:PORT/api/shoutout.show?name=%user%&password=YOUR_KEY
```

PORT is the port in Settings → Connections.

**To react to your events**, connect a WebSocket to `ws://127.0.0.1:PORT/sprigcast` and ask for the `plugin` group. Each event arrives as one JSON object, for example `{"event": "shoutout.shown", "name": "Ana"}`. See [Events](control-api.md#events).

## Give it a Style tab

Put the shared look keys in your `defaults` and your Settings gets a Style tab, the same one Now playing, Chat
and the clocks have: `text_color`, `card_color`, `accent_color`, `font`, `text_scale`, `corners`, any `show_...`
part, and `custom_look` (the box where people type their own look, like CSS). Then read the look once when
you draw:

```python
from sprigcast.core import style

look = style.resolve(self.settings, {"text_color": "#f2f7f3", "card_color": "#151c19e0"})  # your own colours
p.setFont(style.qfont(look, 48))                  # their font, at their text size
style.draw_card(p, rect, 36, look)                # round, leaf or square, with their border and shadow
if look.shows("date", self.settings): ...         # show_date, or "hide: date" in a custom look
```

`resolve()` never raises and is cached, so call it whenever you draw. Anything someone types that isn't
right falls back to your own look. Put `look.key` in what decides when you draw again.

## Keep it light

SprigCast draws every frame on one thread. A slow plugin makes the stream stutter.

- `video_frame()` must never wait. Return a new picture (an H × W × 4 RGBA NumPy array) only when it changed, and `None` the rest of the time. Draw new pictures only when something changes, not every frame.
- Do slow work (files, the network, big pictures) on a thread of your own, and hand the result over for `video_frame()` to pick up.
- Never keep a Qt widget on another thread.
- Stop everything that isn't needed. Set `sleeps = True` on your source and stop work in `sleep()`, so a source nobody can see costs nothing.
- Hooks, actions, hotkeys and right-click entries run on the window's thread. Keep them quick.
- Level watchers run about 15 times a second. Do almost nothing in them.

For anything that isn't text, subclass `sprigcast.core.source.Source` and give it `size` (width, height) and `video_frame()`. The `Source` class in `sprigcast/core/source.py` explains each method.

## Test it

1. Restart SprigCast after each change to your file.
2. Check **Tools → Plugin store → Your plugins**: a ✓ means it loaded, a ✗ shows the error.
3. Add your source and try its settings.
4. Call your actions from Streamer.bot, or open `http://127.0.0.1:PORT/api?password=YOUR_KEY` to see every action, yours included (web links must be on).
5. If something goes wrong, the log in `Documents\SprigCast\Logs` has the full error.

## Share it

Send people your `.py` file. They put it in their plugins folder and restart SprigCast. If your plugin uses pictures or sounds, keep them in a folder next to the file and find them with `Path(__file__).parent`.

The plugins in Tools → Plugin store ship with SprigCast itself. Plugins made by others aren't listed there.
