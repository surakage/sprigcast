# SprigCast control API

Let stream tools, control decks, bots, scripts and plugins drive SprigCast.

> **Preview.** This API is still taking shape: action names, fields and events may change between
> versions. `hello` says `"api": 0, "preview": true` until it settles at `"api": 1`.

Turn it on in **Settings → Connections**, then connect a WebSocket to:

```
ws://127.0.0.1:4455/sprigcast
```

The port and password are the ones shown in Settings → Connections. Apps on other devices can connect only when "Let phones and other computers on my network connect" is on.

## Words

| Word | Meaning |
| --- | --- |
| bloom | a show you put on air (a scene) |
| sprig | a reusable part placed inside blooms, with optional Live states |
| item | one thing inside a bloom or sprig |
| shape | a canvas: `horizontal` or `vertical` |
| sound | anything in the mixer |
| place | somewhere you stream to (Twitch, YouTube…) |
| garden | a whole set of blooms, sprigs and sources (OBS's scene collection) |
| path | where and how you go out: places, encoder and recording settings (OBS's profile) |

Things are named by **name or id**. Any action that changes a bloom takes `shape`: `both` (the default), or one shape.

## Connecting

```
server → {"hello": {"app": "SprigCast", "version": "0.1.0", "api": 0, "preview": true, "auth": {"salt": "…", "challenge": "…"}}}
app    → {"auth": "<answer>", "events": ["bloom", "sound"]}
server → {"welcome": {"api": 0, "events": ["bloom", "sound"]}}
```

`auth` is `null` when no password is set. Then send `{"events": "all"}` (or a list, or `"none"`) to start.

The password answer is `base64(sha256(base64(sha256(password + salt)) + challenge))`. This is the same as obs-websocket's, so existing client code can be reused.

## Asking

```
app    → {"id": "7", "do": "bloom.show", "bloom": "BRB", "shape": "both"}
server → {"id": "7", "ok": true, "result": {…}}
server → {"id": "7", "ok": false, "error": {"code": "not_found", "message": "No bloom called 'BRB'."}}
```

Error codes:

| Code | Meaning |
| --- | --- |
| `bad_request` | a field is missing or has the wrong type |
| `not_found` | no such bloom, sprig, item, sound, source, filter or place |
| `not_allowed` | it can't be done here, e.g. putting a sprig on air |
| `busy` | already doing it, e.g. already live |
| `unknown_action` | no such action |
| `failed` | something went wrong inside SprigCast |

`app.actions` returns every action with its fields, so a plugin can build its menus from it.

### Step lists

Send several actions in one message, with waits between them. One answer comes back when the
last step is done:

```
app    → {"id": "8", "steps": [
            {"do": "item.show", "bloom": "Main", "item": "Alert"},
            {"wait": 5000},
            {"do": "item.hide", "bloom": "Main", "item": "Alert"}]}
server → {"id": "8", "ok": true, "result": {"steps": [{"ok": true, …}, {"ok": true, "waited": 5000}, {"ok": true, …}]}}
```

- `wait` is in milliseconds (all the waits together: at most 10 minutes). SprigCast keeps running while it waits.
- A list holds at most 100 steps.
- A failing step stops the list. Send `"stopOnError": false` to carry on.

## Events

Events arrive as `{"event": "<name>", …}`. Apps get the groups they asked for:

| Group | Events |
| --- | --- |
| `bloom` | `bloom.shown` (bloom, bloomId, shape), `blooms.changed` |
| `item` | `item.changed` (bloom or sprig, shape, item, itemId, visible) |
| `sprig` | `sprig.state` (sprig, sprigId, state) |
| `sound` | `sound.changed` (id, name, muted, volumeDb, balance) |
| `filter` | `filter.changed` (source, id, name, kind, on, settings, added), `filter.removed` |
| `source` | `source.changed` (source, sourceId, settings) |
| `output` | `stream.changed`, `record.changed`, `clips.changed`, `camera.changed` (on), `clip.saved` (files) |
| `transition` | `transition.changed` (kind, ms) |
| `media` | `media.changed` (media, id, file, loop, state: playing, paused, stopped or ended, position, duration) |
| `garden` | `garden.changed` (garden), `gardens.changed` (gardens) |
| `path` | `path.changed` (path), `paths.changed` (paths) |
| `custom` | `custom` (data), sent by another app with `app.broadcast` |
| `plugin` | events sent by added plugins (`<plugin>.<what>`, see below) |
| `meters` | `meters` (levels per sound: peakDb, rmsDb), about 15 a second, only when asked for by name |

## Examples

```json
{"do": "bloom.show", "bloom": "Just Chatting"}
{"do": "bloom.show", "bloom": "Shorts", "shape": "vertical"}
{"do": "item.toggle", "bloom": "Gaming", "item": "Webcam"}
{"do": "sprig.state", "sprig": "Cam frame", "state": "Alert showing"}
{"do": "sound.mute", "sound": "Mic"}
{"do": "sound.volume", "sound": "Music", "change": -6}
{"do": "filter.set", "source": "Facecam", "filter": "Blur", "on": true}
{"do": "filter.set", "source": "Facecam", "filter": "Color Correction", "settings": {"saturation": 0.4}}
{"do": "source.set", "source": "Goal text", "settings": {"text": "Sub goal: 42 / 50"}}
{"do": "clip.save"}
{"do": "media.restart", "media": "Intro video"}
{"do": "media.play", "media": "Hype music"}
{"do": "screenshot.take", "shape": "vertical", "width": 540, "save": false, "data": true}
{"do": "stream.toggle"}
```

## Every action

### app

| Action | What it does | Fields |
| --- | --- | --- |
| `app.actions` | Every action with what it does and its fields (fields starting with ? are optional). | none |
| `app.broadcast` | Send your own event to every connected app. | `data`: any object |
| `app.info` | Version, API version and the shapes that are turned on. | none |
| `app.stats` | Frames per second, render time and memory. | none |
| `plugins.list` | The plugins that are added, each with the actions it brings. | none |

### bloom

| Action | What it does | Fields |
| --- | --- | --- |
| `bloom.current` | The bloom on air in each shape. | none |
| `bloom.show` | Put a bloom on air. | `bloom`: name or id, `shape` (optional): both (default), horizontal or vertical |
| `blooms.list` | Every bloom, its shapes and which shapes it's on air in. | none |

### camera

| Action | What it does | Fields |
| --- | --- | --- |
| `camera.set` | Turn SprigCast Camera (the virtual camera) on or off (no on: toggle). It must be set up once in Settings first. | `on`: optional, true or false |
| `camera.status` | Whether SprigCast Camera is on. | none |

### clip

| Action | What it does | Fields |
| --- | --- | --- |
| `clip.save` | Save the last seconds as a clip (gets clips ready first if they're off). | none |
| `clips.set` | Keep clips ready (on) or stop (off). | `on`: true or false |
| `clips.status` | Whether clips are ready and how long a clip is. | none |

### filter

| Action | What it does | Fields |
| --- | --- | --- |
| `filter.add` | Add a filter to a source. | `source`: name or id, `kind`: kind from filters.kinds, `name` (optional): its name, `settings` (optional): {key: value}, `on` (optional): true (default) or false |
| `filter.move` | Move a filter to a new place in the list (0 = top). | `source`: name or id, `filter`: name, id or kind, `index`: new position |
| `filter.remove` | Remove a filter from a source. | `source`: name or id, `filter`: name, id or kind |
| `filter.set` | Turn a filter on or off and/or change some of its settings (others stay). | `source`: name or id, `filter`: filter name, id or kind, `on` (optional): true or false, `settings` (optional): {key: value} |
| `filter.toggle` | Turn a filter on if off, off if on. | `source`: name or id, `filter`: name, id or kind |
| `filters.kinds` | Filter kinds you can add, with their settings and limits. | none |
| `filters.list` | A source's filters, top to bottom. | `source`: name or id |

### garden

| Action | What it does | Fields |
| --- | --- | --- |
| `garden.new` | Make a garden and switch to it: empty, or a copy of one. | `name`: name for it, `copy` (optional): garden to copy |
| `garden.switch` | Switch to another garden. | `garden`: name |
| `gardens.list` | Every garden (a whole set of blooms) A-Z, and the one in use. | none |

### hotkey

| Action | What it does | Fields |
| --- | --- | --- |
| `hotkey.list` | Hotkey actions and what they do. | none |
| `hotkey.run` | Do what a hotkey does (hotkey.list shows them). Answers `{"hotkey", "note"}`: note says why when it couldn't, e.g. "Not in this bloom: Facecam", else null. | `hotkey`: hotkey action id |

### item

| Action | What it does | Fields |
| --- | --- | --- |
| `item.hide` | Hide something inside a bloom or sprig. | `item`: name or id, `bloom` (optional): bloom it's in, `sprig` (optional): sprig it's in, `shape` (optional): both (default), horizontal or vertical |
| `item.order` | Bring something forward or send it back. | `item`: name or id, `bloom` (optional): bloom it's in, `sprig` (optional): sprig it's in, `shape` (optional): both (default), horizontal or vertical, `to`: top, bottom, up (one step) or down (one step) |
| `item.place` | Move, size or turn something inside a bloom or sprig (only the fields you give change). | `item`: name or id, `bloom` (optional): bloom it's in, `sprig` (optional): sprig it's in, `shape` (optional): both (default), horizontal or vertical, `x` (optional): left edge, canvas pixels, `y` (optional): top edge, `scale` (optional): size, 1 = as big as the source, `rotation` (optional): degrees clockwise, `dx` (optional): pixels to move right (negative: left), `dy` (optional): pixels to move down (negative: up) |
| `item.show` | Show something inside a bloom or sprig. | `item`: name or id, `bloom` (optional): bloom it's in, `sprig` (optional): sprig it's in, `shape` (optional): both (default), horizontal or vertical |
| `item.toggle` | Show it if hidden, hide it if shown. | `item`: name or id, `bloom` (optional): bloom it's in, `sprig` (optional): sprig it's in, `shape` (optional): both (default), horizontal or vertical |
| `items.list` | What's inside a bloom or sprig, per shape. | `bloom` (optional): name or id, `sprig` (optional): name or id |

### media

| Action | What it does | Fields |
| --- | --- | --- |
| `media.list` | Every video and sound file, with what it's doing (playing, paused, stopped, ended). | none |
| `media.loop` | Loop it or not (no on: toggle). | `media`: name or id, `on` (optional): true or false |
| `media.pause` | Pause where it is. | `media`: name or id |
| `media.play` | Play (from the start if it ended or was stopped). | `media`: name or id |
| `media.restart` | Play from the start. | `media`: name or id |
| `media.seek` | Jump to a time. | `media`: name or id, `seconds` (optional): time from the start, `change` (optional): seconds to move (negative: back) |
| `media.status` | What a video or sound file is doing, where it is and how long it is. | `media`: name or id |
| `media.stop` | Stop and show nothing (play starts from the beginning). | `media`: name or id |
| `media.toggle` | Pause if playing, else play. | `media`: name or id |

### place

| Action | What it does | Fields |
| --- | --- | --- |
| `place.set` | Switch a place on or off for the next Go live. | `place`: id from places.list, `on`: true or false |
| `places.list` | Where the stream goes (Twitch, YouTube...), each one's shape and if it's on. | none |

### path

| Action | What it does | Fields |
| --- | --- | --- |
| `path.new` | Make a path and switch to it: empty, or a copy of one. | `name`: name for it, `copy` (optional): path to copy |
| `path.switch` | Switch to another path (not while live or recording). | `path`: name |
| `paths.list` | Every path (where and how you go out: places, encoder, recording) A-Z, and the one in use. | none |

### record

| Action | What it does | Fields |
| --- | --- | --- |
| `record.start` | Start recording. | none |
| `record.status` | Recording or not, for how long, and the file. | none |
| `record.stop` | Stop recording. | none |
| `record.toggle` | Start or stop recording. | none |

### screenshot

| Action | What it does | Fields |
| --- | --- | --- |
| `screenshot.take` | A picture of what's on air in each shape. | `shape` (optional): both (default), horizontal or vertical, `width` (optional): shrink to this width in pixels (keeps the shape), `save` (optional): true (default): save PNGs in the recordings folder, under Screenshots, `data` (optional): true: send the picture back as base64 PNG (for bots and pages; large) |

### sound

| Action | What it does | Fields |
| --- | --- | --- |
| `sound.balance` | Left/right: -1 left, 0 middle, 1 right. | `sound`: name or id, `balance`: -1 to 1 |
| `sound.micFirst` | Lower everything else while your mic talks (ducking). | `on`: true or false |
| `sound.mute` | Mute or unmute a sound (no muted: toggle). | `sound`: name or id, `muted` (optional): true or false |
| `sound.volume` | Set a sound's volume in dB, or change it by some dB. | `sound`: name or id, `db` (optional): new volume, -100 to 26, `change` (optional): dB to add (negative to lower) |
| `sounds.list` | Every sound in the mixer with its volume and mute. | none |

### source

| Action | What it does | Fields |
| --- | --- | --- |
| `source.get` | A source's settings. | `source`: name or id |
| `source.rename` | Give a source a new name. | `source`: name or id, `name`: new name |
| `source.set` | Change some of a source's settings: text, a browser page's url, an image file, a colour... | `source`: name or id, `settings`: {key: value} |
| `sources.list` | Every source (cameras, text, browser pages, images, sounds...). | none |

### sprig

| Action | What it does | Fields |
| --- | --- | --- |
| `sprig.state` | Switch a sprig to one of its Live states (null: as built). | `sprig`: name or id, `state`: state name or null |
| `sprigs.list` | Every sprig, its Live states and the state showing. | none |

### stream

| Action | What it does | Fields |
| --- | --- | --- |
| `stream.start` | Go live to every place that's switched on. | none |
| `stream.status` | Live or not, for how long, and dropped frames. | none |
| `stream.stop` | End the stream. | none |
| `stream.toggle` | Go live, or end the stream. | none |

### transition

| Action | What it does | Fields |
| --- | --- | --- |
| `transition.get` | The usual transition and its length. | none |
| `transition.set` | Change the usual transition and/or its length. | `kind` (optional): cut, fade, slide, swipe, wipe, leaf, bloom, stinger, `ms` (optional): length in milliseconds, 50 to 20000 |

## Store plugins

Plugins added from **Settings → Plugins** bring their own actions. They're in `app.actions` and `plugins.list` only while the plugin is added; without it they answer `unknown_action`.

| Action | Plugin | What it does | Fields |
| --- | --- | --- | --- |
| `hide.pixelate` | Hide it | Pixelate a source for a while. | `source`, `seconds` (optional; 0 = until shown again), `size` (optional block size) |
| `hide.blackout` | Hide it | Black a source out for a while. | `source`, `seconds` (optional) |
| `hide.toggle` | Hide it | Hide it until toggled back, or show it. | `source`, `how` (optional): pixelate or blackout |
| `hide.show` | Hide it | Show it again now. | `source` |
| `roll.spin` | Barrel Roll | Spin a sprig (everywhere it's shown) or one thing, then it lands where it was. | `sprig` or `source`, `seconds`, `turns`, `direction` (all optional) |
| `deaths.add` | Death count | One more death (or `amount` more). The gravestone pops up with the new count. | `amount` (optional, default 1), `source` (optional) |
| `deaths.remove` | Death count | One less death (or `amount` less), never below 0. | `amount` (optional, default 1), `source` (optional) |
| `deaths.set` | Death count | Set the count to a number. | `count`, `source` (optional) |
| `deaths.reset` | Death count | Back to 0. | `source` (optional) |
| `deaths.show` | Death count | Pop the gravestone up with the count as it is. | `source` (optional) |
| `deaths.get` | Death count | The count, without showing it. | `source` (optional) |

**Death count** answers every `deaths.*` action with `{"count": N}` and sends a `deaths.changed` event (`{"count": N}`, `plugin` group) after each change. Without `source` it changes every Death count you've added. The count is saved with your blooms, so it survives a restart. Adding a Death count (+ → Plugins → Death count) makes a sprig of its own on top of the bloom, like Snap: a gravestone grows out of the ground at the bottom, flips to the new number, holds for a few seconds (its Seconds setting) and sinks back.

**Snap** has no actions of its own: it turns every saved screenshot (`screenshot.take`, the button or its hotkey) into a polaroid animation on stream. A camera pops up, flashes and prints the screenshot. Adding a Snap (+ → Plugins → Snap) makes a sprig of its own that fills the canvas, on top of the bloom in every shape, so it overlays the stream without any placing.

Using Streamer.bot? See [Streamer.bot](streamerbot.md). Building an integration? See [Building on SprigCast](integrating.md). Stream Deck: see [the SprigCast plugin](../integrations/streamdeck/README.md).

## Web links

For buttons and scripts that can only open a link. Turn on **Allow web links** in Settings → Connections. A password is always needed.

```
http://127.0.0.1:4455/api                                  every action
http://127.0.0.1:4455/api/bloom.show?bloom=BRB&password=…   run one, fields in the address
curl -H "Authorization: Bearer …" -X POST http://127.0.0.1:4455/api/filter.set \
     -d '{"source": "Facecam", "filter": "Blur", "on": true}'
```

Values in the address are read the way they look: `true`, `false`, `null` and numbers become those, and anything else is text. The answer is the same JSON as on the WebSocket, without `id`. The HTTP status matches the error: 400 bad request, 401 password, 403 off, 404 not found, 409 not allowed or busy.

Requests made by web pages on other sites are refused, so a page you visit can't drive SprigCast.

## The OBS-compatible door

Apps that can only talk to OBS can connect to `ws://127.0.0.1:4455` (no `/sprigcast`), using the obs-websocket 5 protocol. Blooms appear as scenes and sprigs as nested scenes. Gardens are scene collections and paths are profiles (GetSceneCollectionList, SetCurrentSceneCollection, CreateSceneCollection, GetProfileList, SetCurrentProfile, CreateProfile, RemoveProfile, and their events). It also takes the requests TikFinity, Streamer.bot and Stream Deck's OBS actions send most: SetInputSettings (change a text, a browser page's url, an image or video file; OBS setting names like `file` and `local_file` are understood), GetInputDefaultSettings, SetInputName, PressInputPropertiesButton (`refreshnocache` reloads a browser page), GetSourceFilterList, GetSourceFilter, SetSourceFilterEnabled, SetSourceFilterSettings, SetSourceFilterName (with the SourceFilterEnableStateChanged event), GetMediaInputStatus, TriggerMediaInputAction, SetMediaInputCursor, OffsetMediaInputCursor, and GetSourceScreenshot / SaveSourceScreenshot (png, of a bloom that's on air; a picture up to a second old is reused), and the virtual camera (GetVirtualCamStatus, StartVirtualCam, StopVirtualCam, ToggleVirtualCam, VirtualcamStateChanged). Beyond these, new features come to the SprigCast API only. It can be switched off with **Compatibility mode** in Settings → Connections.

## Adding actions from a plugin

A plugin in the plugins folder can add its own actions:

```python
from sprigcast.control.native import register_action

def burst(api, data):
    return {"amount": data.get("amount", 1)}

register_action("confetti.burst", "Throw confetti.", {"amount": "?how much"}, burst)
```

Apps then call `{"do": "confetti.burst", "amount": 3}`, and the action shows up in `app.actions`.

A plugin can tell apps something happened. They get it in the `plugin` event group:

```python
from sprigcast.control.native import send_event

send_event(api, "confetti.thrown", {"amount": 3})   # → {"event": "confetti.thrown", "amount": 3}
```
