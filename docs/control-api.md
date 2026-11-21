# SprigCast control API

Let stream tools, control decks, bots, scripts and plugins drive SprigCast.

> **Preview.** This API is still taking shape: action names, fields and events may change between
> versions. `hello` says `"api": 0, "preview": true` until it settles at `"api": 1`.

Turn it on in **Settings → Connections**, then connect a WebSocket to:

```
ws://127.0.0.1:4455/sprigcast
```

The port and password are the ones shown in Settings → Connections. Apps on other devices can connect only when "Let phones and other computers on my network connect" is on (an Expert settings row).

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

`auth` is `null` when no password is set. Then send `{"events": "all"}` (or a list, or `"none"`) to start. Add `"name": "My app"` to that first message and SprigCast's log names your app by it (an app paired with a code is named by its pairing instead). It's only for the log.

The password answer is `base64(sha256(base64(sha256(password + salt)) + challenge))`. This is the same as obs-websocket's, so existing client code can be reused.

### Pair a device (no password to copy)

In SprigCast, **Settings → Connections → Pair a device** shows a 6-digit code for 2 minutes. The app sends it once, instead of `auth`, with a name for itself:

```
app    → {"pair": "123456", "name": "Stream Deck", "events": "all"}
server → {"paired": {"key": "sc_…", "name": "Stream Deck"}}
server → {"welcome": {"api": 0, "events": [...]}}
```

Keep the `key`: from then on use it exactly where the password goes (the `auth` answer, `Authorization: Bearer`, `?password=`, and the OBS-compatible door). Each device has its own key; **Remove** next to it in Settings → Connections stops it at once without changing the password for anything else. A wrong or expired code closes with `4011`. Five wrong codes within a minute lock pairing for a minute: the code is cancelled and every try is refused (web links answer `429`) until the minute is up, then press Pair a device for a new code.

Web links can pair too, even while **Allow web links** is off (the key also opens the WebSocket):

```
GET http://127.0.0.1:4455/api/pair?code=123456&name=Streamer.bot
→ {"ok": true, "result": {"key": "sc_…", "name": "Streamer.bot"}}
```

### Apps on this PC

While the API runs, SprigCast writes `%LOCALAPPDATA%\Slothbloom\SprigCast\api.json` (`app`, `port`, `path`, `password`) so apps on the same PC connect with nothing typed, the way Elgato's OBS plugin reads obs-websocket's settings. The `password` there is this PC's own key, not your password, and it changes when the password does. The file is removed when the API stops.

It also writes `%APPDATA%\SprigCast\connection.json` for apps that find SprigCast by themselves (THSV StreamBridge): `app`, `version`, `port`, `path` (`/sprigcast`), `obsCompatible` (OBS-style connection on), `passwordSet` and `pid`. It never holds the password or a key, so those apps still ask you for the password. It is removed when the API stops.

The password and paired keys are stored encrypted for your Windows account (DPAPI), not as plain text.

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
| `not_found` | no such bloom, sprig, item, sound, source, filter or place; the error also has `what` (bloom, sprig, source, item, filter, shape, state, place, hotkey, kind) and `name` (what was asked for) |
| `not_allowed` | it can't be done here, e.g. putting a sprig on air |
| `busy` | already doing it, e.g. already live |
| `unknown_action` | no such action |
| `doctor_warnings` | going live or recording would have asked first (the Stream doctor, low space): nothing started; `warnings` lists why, and `force: true` goes ahead |
| `places_differ` | stream.start was given `places` and the places switched on aren't exactly those: nothing started; `places` lists the ones switched on |
| `plugin_missing` | the action comes with a store plugin that isn't added; the message names it |
| `failed` | something went wrong inside SprigCast |

These codes are never renamed; new ones may be added. `app.actions` returns every action with its fields, so a plugin can build its menus from it.

### Features

`app.info` has `features`: names of what this SprigCast can do, so an app can check instead of guessing from the shape of an answer. Names are only ever added. `api` stays 0 (preview) until SprigCast's first public release; the features list is what to check. `app.info` also has `canvases`: each canvas's `id`, `width`, `height`, `fps` and `enabled`, for placing things in canvas pixels.

| Feature | What it means |
| --- | --- |
| `bloom.new` | bloom.new, bloom.remove, source.new, item.add |
| `places.status` | stream.status places, place.start, place.stop |
| `screenshot.source` | screenshot.take with source or bloom |
| `plugins.enabled` | plugins.list: id, enabled, store plugins not added |
| `plugin.missing` | plugin_missing error |
| `effects.autoend` | hide.*, roll.spin, freeze.* end by themselves after seconds (1-600); a new call restarts the time |
| `source.rotate` | source.rotate |
| `start.warnings` | stream.start / record.start answer doctor_warnings, force: true goes ahead |
| `canvas.size` | app.info canvases: id, width, height, fps, enabled |
| `item.scale_xy` | item.place scale_x / scale_y |
| `events.effect` | effect.ended |
| `events.plugins` | plugins.changed |
| `events.lists` | sources.changed, items.changed |
| `notfound.what` | not_found errors carry what and name |
| `page.api` | Web page sources: window.sprigcast (app, version, visible, bloom...) and its events |
| `effects.freeze` | freeze.start / freeze.stop / freeze.toggle (Freeze plugin), effect.ended "freeze.start" |
| `record.pause` | record.pause / resume / toggle_pause / chapter, record.status paused, record.paused event |
| `place.bandwidth_test` | place.set bandwidth_test (Twitch test mode); places.list says it where there is one |
| `place.bitrate` | place.set bitrateKbps (500-50000) for the next Go live; places.list says each place's bitrateKbps |
| `screenshot.bloom_off_air` | screenshot.take {bloom, shape}: any bloom on its canvas without putting it on air |
| `item.place_full` | item.place crop, bounds, bounds_width/height, bounds_align, align |
| `item.edit` | item.lock, item.remove, item.duplicate, source.remove, source.refresh |
| `edit.undo` | edit.undo / edit.redo; {"steps", "one_step": true}: all or none, one undo step |
| `steps.dry_run` | {"steps", "dry_run": true}: nothing kept; answers the layouts and overlaps it would make |
| `events.item_moved` | item.moved when something is moved, sized, cropped or turned |
| `filters.share` | a placed sprig's filters follow on its other canvases; filters.share, filters.list same_on_both_canvases |
| `layout.get` | layout.get: every item's place, crop, bounds, source size and canvas box; flat, overlaps |
| `source.state` | source.get loaded, state ("capturing 1920x1080", "camera not found, retrying"), size, cameraSize, asleep |
| `places.size` | places.list size [w, h] and fps (what is sent: the place's own, else its shape's), and enhanced on Twitch |
| `screenshot.skipped` | screenshot.take {bloom}: every shape it's in, skipped names a shape left out; canvas pictures are on black |
| `places.ready` | places.list name, ready, problem ("Needs a stream key"): why a place can't go live yet |
| `app_audio.pids` | app_audio settings pids: [process id, ...] catches those processes' trees instead of apps |
| `browser.h264` | Only when Web pages play H.264/AAC themselves (video tags, streaming players); without it MP4 files still play, converted |

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

#### Several changes as one

Add `"one_step": true` to a step list and its steps are taken together: all of them work, or nothing changes. They are saved and drawn once, and are one step for undo (Ctrl+Z in SprigCast, or `edit.undo`). The answer's `result` has `applied` (true or false) next to `steps`.

Add `"dry_run": true` instead and nothing is kept at all. The answer says where things would end up: `result.layouts` holds a `layout.get` answer (with `overlaps`) for each bloom or sprig the steps name.

```
app    → {"id": "9", "dry_run": true, "steps": [
            {"do": "item.place", "bloom": "Main", "item": "Chat", "shape": "vertical", "x": 40, "y": 1200}]}
server → {"id": "9", "ok": true, "result": {"steps": [{"ok": true, …}], "applied": false, "dry_run": true,
            "layouts": [{"bloom": "Main", "shapes": {"vertical": {"canvas": {…}, "items": […], "overlaps": […]}}}]}}
```

Only changes to the garden can be taken as one, so they can all be taken back: `item.add`, `item.place`, `item.order`, `item.show`, `item.hide`, `item.toggle`, `item.lock`, `item.remove`, `item.duplicate`, `source.new`, `source.set`, `source.rename`, `source.rotate`, `source.remove`, `filter.add`, `filter.set`, `filter.remove`, `filter.move`, `sound.mute`, `sound.volume`, `sound.balance`, plus `items.list` and `layout.get`. Anything else, or a `wait`, is refused with `bad_request` and nothing is done.

`edit.undo` and `edit.redo` are SprigCast's own undo and redo (the last 50 changes to the garden, whoever made them). They answer `{"undone": true}` or `{"redone": true}`, false when there was nothing to undo or redo.

## Events

Events arrive as `{"event": "<name>", …}`. Apps get the groups they asked for:

| Group | Events |
| --- | --- |
| `bloom` | `bloom.shown` (bloom, bloomId, shape), `blooms.changed` |
| `item` | `item.changed` (bloom or sprig, shape, item, itemId, visible; inside a sprig also `blooms`: the blooms whose own sprig it is), `items.changed` (bloom or sprig, shape, items: [{item, itemId}], top first) when something is added to or taken out of a bloom or sprig, `item.moved` (bloom or sprig, shape, item, itemId, x, y, scale_x, scale_y, rotation, align, crop, bounds) when something is moved, sized, cropped or turned, by hand or by an app |
| `sprig` | `sprig.state` (sprig, sprigId, state) |
| `sound` | `sound.changed` (id, name, muted, volumeDb, balance) |
| `filter` | `filter.changed` (source, id, name, kind, on, settings, added), `filter.removed`, `effect.ended` (source, sourceId, effect: hide.pixelate, hide.blackout, roll.spin or freeze.start, reason: ended or replaced) |
| `source` | `source.changed` (source, sourceId, settings), `sources.changed` (sources: [{id, name, kind}], added, removed, renamed: [{from, to}]) |
| `output` | `stream.changed`, `record.changed`, `clips.changed`, `camera.changed` (on), `record.paused` (paused), `clip.saved` (files) |
| `transition` | `transition.changed` (kind, ms) |
| `media` | `media.changed` (media, id, file, loop, state: playing, paused, stopped or ended, position, duration) |
| `garden` | `garden.changed` (garden), `gardens.changed` (gardens) |
| `path` | `path.changed` (path), `paths.changed` (paths) |
| `custom` | `custom` (data), sent by another app with `app.broadcast` |
| `plugin` | `plugins.changed` (plugins, added, removed: plugin ids) when a plugin is added or removed, and events sent by added plugins (`<plugin>.<what>`, see below) |
| `meters` | `meters` (levels per sound: peakDb, rmsDb), about 15 a second, only when asked for by name |

Every app also gets `app.closing` just before SprigCast closes (or turns its API off), whatever groups it asked
for. The connection closes next. Look again in a second or two: it's often a restart.

## Examples

```json
{"do": "bloom.show", "bloom": "Just Chatting"}
{"do": "bloom.show", "bloom": "Shorts", "shape": "vertical"}
{"do": "item.toggle", "bloom": "Gaming", "item": "Webcam"}
{"do": "sprig.state", "sprig": "Cam frame", "state": "Alert showing"}
{"do": "sound.mute", "sound": "Mic"}
{"do": "sound.volume", "sound": "Music", "change": -6}
{"do": "filter.set", "source": "Facecam", "filter": "Mist", "on": true}
{"do": "filter.set", "source": "Facecam", "filter": "Tone", "settings": {"saturation": 0.4}}
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
| `app.info` | Version, API version, the shapes that are on, each canvas's size, event groups and features. | none |
| `app.stats` | Frames per second, render time and memory. | none |
| `plugins.list` | The plugins that are added (enabled) and the store's others (not enabled), each with the actions it brings. | none |

### bloom

| Action | What it does | Fields |
| --- | --- | --- |
| `bloom.current` | The bloom on air in each shape. | none |
| `bloom.new` | Make an empty bloom (with its own sprig, where what you add goes). | `name`: name, `shape` (optional): both (default: every canvas), horizontal or vertical |
| `bloom.remove` | Remove a bloom (in every shape). Its sources stay while other blooms use them. | `bloom`: name or id |
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
| `clip.save` | Save the last seconds as a clip (gets clips ready first if they're off). Answers with the file names at once; the clip.saved event says when they are written. | none |
| `clips.set` | Keep clips ready (on) or stop (off). | `on`: true or false |
| `clips.status` | Whether clips are ready and how long a clip is. | none |

### edit

| Action | What it does | Fields |
| --- | --- | --- |
| `edit.redo` | Redo the change that was last undone. Answers `{"redone": true}`, false when there was nothing to redo. | none |
| `edit.undo` | Undo the last change to the garden (as Ctrl+Z in SprigCast). Answers `{"undone": true}`, false when there was nothing to undo. | none |

### filter

| Action | What it does | Fields |
| --- | --- | --- |
| `filter.add` | Add a filter to a source or a placed sprig. | `kind`: kind from filters.kinds, `name` (optional): its name, `settings` (optional): {key: value}, `on` (optional): true (default) or false, `source` (optional): name or id, `sprig` (optional): a sprig instead: its filters where it's placed, `in` (optional): with sprig: only where it's placed in this bloom or sprig (both canvases) |
| `filter.move` | Move a filter to a new place in the list (0 = top). | `filter`: name, id or kind, `index`: new position, `source` (optional): name or id, `sprig` (optional): a sprig instead: its filters where it's placed, `in` (optional): with sprig: only where it's placed in this bloom or sprig (both canvases) |
| `filter.remove` | Remove a filter from a source or a placed sprig. | `filter`: name, id or kind, `source` (optional): name or id, `sprig` (optional): a sprig instead: its filters where it's placed, `in` (optional): with sprig: only where it's placed in this bloom or sprig (both canvases) |
| `filter.set` | Turn a filter on or off and/or change some of its settings (others stay). | `filter`: filter name, id or kind, `on` (optional): true or false, `settings` (optional): {key: value}, `source` (optional): name or id, `sprig` (optional): a sprig instead: its filters where it's placed, `in` (optional): with sprig: only where it's placed in this bloom or sprig (both canvases) |
| `filter.toggle` | Turn a filter on if off, off if on. | `filter`: name, id or kind, `source` (optional): name or id, `sprig` (optional): a sprig instead: its filters where it's placed, `in` (optional): with sprig: only where it's placed in this bloom or sprig (both canvases) |
| `filters.kinds` | Filter kinds you can add, with their settings and limits. | none |
| `filters.list` | A source's or a placed sprig's filters, top to bottom. | `source` (optional): name or id, `sprig` (optional): a sprig instead: its filters where it's placed, `in` (optional): with sprig: only where it's placed in this bloom or sprig (both canvases) |
| `filters.share` | A placed sprig: the same filters on both canvases (the usual), or its own on each. | `on`: true or false, `source` (optional): name or id, `sprig` (optional): a sprig instead: its filters where it's placed, `in` (optional): with sprig: only where it's placed in this bloom or sprig (both canvases) |

**Filter names.** A filter is found by its id, its name, its kind, or its kind's name today or before the one-word names (2026-11-21), so `"Chroma Key"` and `"Greenscreen"` both find a `chroma_key` filter. Kinds never change: Spin `barrel_roll`, Curtain `blackout`, Mist `blur`, Greenscreen `chroma_key`, Tone `color_correction`, Colorcut `color_key`, Mood `lut`, Lightcut `luma_key`, Mosaic `pixelate`, Drift `scroll`, Frame `shape`, Crisp `sharpen`, Outline `stroke`; Freeze, Glow and Shadow kept their names.

A sprig placed in a bloom has a placement on each canvas, and each placement has filters of its own. They're kept the same: change one (in the Filters window or with any request here) and the other canvas follows. `filters.share` with `on: false`, or unticking **Same on both canvases** in its Filters window, lets each canvas keep its own. Name the sprig with `sprig` to change its filters wherever it's placed, or add `in` for one bloom: `{"do": "filter.set", "sprig": "Elgato", "in": "Gameplay", "filter": "barrel_roll", "on": true}`. A placement that doesn't have the filter is passed over. `filters.list` says `same_on_both_canvases` for a placed sprig. A source's filters were always one list on every canvas.

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
| `item.add` | Put a source in a bloom (in its sprig) or a sprig, on top (a plugin starts readable, in a free corner). Answers the item's id. | `source`: name or id, `bloom` (optional): bloom to add to, `sprig` (optional): sprig to add to, `shape` (optional): both (default), horizontal or vertical, `visible` (optional): true (default) or false |
| `item.duplicate` | Copy something: a new source with the same settings and filters, placed the same way, on top. Not for sprigs. Answers `item`, `name`, `shapes`. | `item`: name or id, `bloom` (optional): bloom it's in, `sprig` (optional): sprig it's in, `shape` (optional): both (default), horizontal or vertical, `name` (optional): the copy's name |
| `item.hide` | Hide something inside a bloom or sprig. | `item`: name or id, `bloom` (optional): bloom it's in, `sprig` (optional): sprig it's in, `shape` (optional): both (default), horizontal or vertical |
| `item.lock` | Lock something in place in Build, or unlock it (no `locked`: toggle). | `item`: name or id, `bloom` (optional): bloom it's in, `sprig` (optional): sprig it's in, `shape` (optional): both (default), horizontal or vertical, `locked` (optional): true or false |
| `item.order` | Bring something forward or send it back. | `item`: name or id, `bloom` (optional): bloom it's in, `sprig` (optional): sprig it's in, `shape` (optional): both (default), horizontal or vertical, `to`: top, bottom, up (one step) or down (one step) |
| `item.place` | Move, size or turn something inside a bloom or sprig (only the fields you give change). | `item`: name or id, `bloom` (optional): bloom it's in, `sprig` (optional): sprig it's in, `shape` (optional): both (default), horizontal or vertical, `x` (optional): left edge, canvas pixels, `y` (optional): top edge, `scale` (optional): size, 1 = as big as the source, `scale_x` (optional): width only, 1 = as wide as the source, `scale_y` (optional): height only, 1 = as tall as the source, `rotation` (optional): degrees clockwise, `dx` (optional): pixels to move right (negative: left), `dy` (optional): pixels to move down (negative: up), `crop` (optional): `{left, top, right, bottom}` in source pixels (the sides you give change), `bounds` (optional): off, fit, fill or stretch, `bounds_width`, `bounds_height` (optional): the box in canvas pixels, `bounds_align` (optional): where the picture sits in its box, `align` (optional): the point it turns around, top_left to bottom_right (the picture stays where it is) |
| `item.remove` | Take something out of a bloom or sprig. Its source stays. | `item`: name or id, `bloom` (optional): bloom it's in, `sprig` (optional): sprig it's in, `shape` (optional): both (default), horizontal or vertical |
| `item.show` | Show something inside a bloom or sprig. | `item`: name or id, `bloom` (optional): bloom it's in, `sprig` (optional): sprig it's in, `shape` (optional): both (default), horizontal or vertical |
| `item.status` | Whether something inside a bloom or sprig is shown. | `item`: name or id, `bloom` (optional): bloom it's in, `sprig` (optional): sprig it's in, `shape` (optional): both (default), horizontal or vertical |
| `item.toggle` | Show it if hidden, hide it if shown. | `item`: name or id, `bloom` (optional): bloom it's in, `sprig` (optional): sprig it's in, `shape` (optional): both (default), horizontal or vertical |
| `items.list` | What's inside a bloom or sprig, per shape. | `bloom` (optional): name or id, `sprig` (optional): name or id |

Blooms hold sprigs, so `{"bloom": "Chatting", "item": "Cam"}` also finds the Cam inside Chatting's own sprig.

### layout

| Action | What it does | Fields |
| --- | --- | --- |
| `layout.get` | Where everything in a bloom or sprig sits, per shape, top of the stack first. | `bloom` or `sprig`: name or id, `shape` (optional): both (default), horizontal or vertical, `flat` (optional): true for only the things themselves (no sprigs), `overlaps` (optional): true to also list overlapping pairs |

`layout.get` reads the garden; it draws nothing and changes nothing, so it works for blooms that aren't on air. It answers `{"bloom" or "sprig": name, "id", "shapes": {"horizontal": {"canvas": {"width", "height"}, "items": [...]}}}`. Each item has:

- `id`, `name`, `kind` (`sprig` for a nested sprig, else the source's kind), `sprig`, `visible`, `locked`, and `shown` (visible, and so is every sprig it's inside).
- Its place as saved: `x`, `y` (top-left of its box before turning), `scale_x`, `scale_y`, `rotation` (degrees clockwise, around the `align` point), `align`, `crop` (`left`, `top`, `right`, `bottom` in source pixels) and `bounds` (`mode`: off, fit, fill or stretch; `width`, `height`; `align`).
- `source_size`: the source's own `width` and `height` in pixels. A sprig is as big as its canvas. `null` when SprigCast doesn't know yet (a source that hasn't drawn); then `size`, `corners` and `box` are `null` too.
- `size`: its box before turning. `corners`: the box's four corners on the canvas (top-left, top-right, bottom-left, bottom-right). `box`: the upright rectangle around those corners (`x`, `y`, `width`, `height`), in the canvas pixels of the bloom or sprig you asked for.
- A sprig has `items`: what's inside it. Their `x`, `y` and scales are in the sprig's own pixels, and their `corners` and `box` are where they end up on the canvas you asked for. `in` lists the sprigs an item is inside.

With `overlaps`, each shape also has `overlaps`: `[{"a", "a_name", "a_in", "b", "b_name", "b_in", "box"}]` for each pair of shown things whose boxes share more than a pixel. Boxes are upright rectangles, so two turned things may be listed when only their corners' rectangles touch.

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
| `place.set` | Switch a place on or off for the next Go live, and/or its test mode (Twitch: the stream reaches Twitch but viewers never see it). | `place`: id from places.list, `on` (optional): true or false, `bandwidth_test` (optional): true or false (only places whose service has a test mode), `bitrateKbps` (optional): 500 to 50000: its bitrate for the next Go live |
| `place.start` | While live: start sending to one more place (switched on for next time too). | `place`: id from places.list |
| `place.stop` | While live: stop sending to one place (the others keep going; the last one ends the stream). | `place`: id from places.list |
| `places.list` | Where the stream goes, each one's `service`, `name` (Twitch, YouTube, Facebook...), `shape`, `on`, `bitrateKbps`, and `ready` with `problem` saying why it can't go live yet ("Needs a stream key", "Needs a server URL", a canvas that's off), else null. `bandwidth_test` where the service has a test mode. A place added without its key stays off until it has one. | none |

### path

| Action | What it does | Fields |
| --- | --- | --- |
| `path.new` | Make a path and switch to it: empty, or a copy of one. | `name`: name for it, `copy` (optional): path to copy |
| `path.switch` | Switch to another path (not while live or recording). | `path`: name |
| `paths.list` | Every path (where and how you go out: places, encoder, recording) A-Z, and the one in use. | none |

### record

| Action | What it does | Fields |
| --- | --- | --- |
| `record.chapter` | Add a chapter mark to the recording at this moment (editors and players list them). | `name` (optional): the chapter's name (default: Chapter 1, 2...) |
| `record.pause` | Pause the recording. The file stays open; record.resume carries on in it. | none |
| `record.resume` | Carry on recording in the same file after record.pause. | none |
| `record.start` | Start recording. If the drive is nearly full, nothing starts: doctor_warnings says so. | `force` (optional): true: record anyway |
| `record.status` | Recording or not, for how long, the file, and whether it's paused. | none |
| `record.stop` | Stop recording. | none |
| `record.toggle` | Start or stop recording (starting answers like record.start). | `force` (optional): true: record anyway |
| `record.toggle_pause` | Pause, or carry on after a pause. | none |

### screenshot

| Action | What it does | Fields |
| --- | --- | --- |
| `screenshot.take` | A picture of what's on air in each shape, or of one source or bloom. | `shape` (optional): both (default), horizontal or vertical, `source` (optional): just this source (a camera...), name or id, `bloom` (optional): just this bloom, name or id, `width` (optional): shrink to this width in pixels (keeps the shape), `save` (optional): true (default): save PNGs in the recordings folder, under Screenshots, `data` (optional): true: send the picture back as base64 PNG (for bots and pages; large) |


`screenshot.take` with `bloom` pictures that bloom on the `shape` you name, whether it's on air or not. A bloom on air is pictured as its canvas. A bloom off air is drawn on its own canvas without being put on air: its sources wake for the moment it takes (up to about 4 seconds for cameras and web pages), then rest again, and its shot has `"on_air": false`. That isn't done while live or recording (`not_allowed`), so the stream never pays for it. With `shape` both (the default) you get a picture for each shape the bloom is in; a shape left out is named in `skipped` (`shape`, `why`): the bloom has no layout for that canvas (its vertical look is a bloom of its own: ask for that one by name), or it's on air on one canvas only. A canvas's or bloom's picture is on black with nothing see-through, as viewers get it; a single source's picture keeps its see-through parts. With no `shape`, a bloom named by one canvas's own id (or a name only that one has, such as the vertical bloom's) is pictured on that canvas.

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
| `source.new` | Make a source (a browser page, text, image...). Put it in a bloom with item.add. | `name`: name, `kind`: browser, text, image, color, media..., `settings` (optional): {key: value}, e.g. url, width, height |
| `source.rename` | Give a source a new name. | `source`: name or id, `name`: new name |
| `source.refresh` | Load a web page source again, now. `not_allowed` while the page isn't open (off screen): it loads fresh when it's shown. | `source`: name or id |
| `source.remove` | Remove a source, taking it out of every bloom and sprig it's in. Answers `removed`, `places`. | `source`: name or id |
| `source.rotate` | Turn a source to an angle wherever it's shown (0: upright again). | `source`: name or id, `degrees`: clockwise, -3600 to 3600 |
| `source.set` | Change some of a source's settings: text, a browser page's url, an image file, a color... | `source`: name or id, `settings`: {key: value} |
| `sources.list` | Every source (cameras, text, browser pages, images, sounds...). | none |

A Text source's settings (`source.get` shows them all, `source.set` changes the ones you give): `text`, `font`, `size`, `color`, `bold`, `italic`, `letter_case` (as_typed, lower, upper), `align` (left, center, right), `wrap_width`, `line_spacing`, `outline_color`, `outline_width`, `gradient`, `gradient_color`, `gradient_direction` (across, down), `shadow`, `shadow_color`, `shadow_distance`, `shadow_blur`, `box`, `box_color`, `box_padding`, `box_corners`. To show a text file and follow it as it changes: `file` (its path), `read_file` (true) and `file_lines` (all, first, last). Colors are `#rrggbbaa`.

### sprig

| Action | What it does | Fields |
| --- | --- | --- |
| `sprig.remove` | Remove a sprig in every shape, and take it out of every bloom. Sources only it used go too, except sounds (mics, desktop and app sounds), which always stay. | `sprig`: name or id |
| `sprig.state` | Switch a sprig to one of its Live states (null: as built). | `sprig`: name or id, `state`: state name or null |
| `sprigs.list` | Every sprig, its Live states and the state showing. | none |

### stream

| Action | What it does | Fields |
| --- | --- | --- |
| `stream.start` | Go live to every place that's switched on; the answer lists them (places). If the Stream doctor would warn first, nothing starts: doctor_warnings lists them. places: the ids you expect to go live; if the places switched on differ, nothing starts (places_differ lists them). | `force` (optional): true: go live past the doctor's warnings; `places` (optional): list of place ids from places.list: go live only if exactly these are switched on |
| `stream.status` | Live or not, for how long, dropped frames, and each place on its own. | none |
| `stream.stop` | End the stream. | none |
| `stream.toggle` | Go live, or end the stream (going live answers like stream.start). | `force` (optional): true: go live past the doctor's warnings |

### transition

| Action | What it does | Fields |
| --- | --- | --- |
| `transition.get` | The usual transition, its length and (Move) how it moves. | none |
| `transition.set` | Change the usual transition, its length and (Move) how it moves. | `kind` (optional): cut, fade, move, slide, swipe, wipe, leaf, bloom, stinger, `ms` (optional): length in milliseconds, 50 to 20000, `easing` (optional): Move only: smooth, ease_out, ease_in, linear, spring, `unmatched` (optional): Move only, what only one bloom shows: fade, scale, slide |

## Web page sources

Pages shown by a Web page source get `window.sprigcast` (the same object as `window.obsstudio`, so overlays made for OBS keep working). Fields: `app` ("SprigCast"), `version`, `visible` (shown in a bloom on air), `bloom` (the bloom on air in the shape the page is shown in), `blooms` ({shape: bloom}), `shape`, `streaming`, `recording`. Events on `window`: `sprigcastVisibleChanged` and `sprigcastStateChanged` (detail: the fields above), and OBS's `obsSourceVisibleChanged`, `obsSourceActiveChanged`, `obsSceneChanged`, `obsStreamingStarted` / `obsStreamingStopped`, `obsRecordingStarted` / `obsRecordingStopped`. `getCurrentScene` answers the bloom on air. The state is looked at about once a second.

## Store plugins

Plugins added from **Tools → Plugin store** bring their own actions. They're in `app.actions` only while the plugin is added; without it they answer `plugin_missing`, naming the plugin to add. `plugins.list` shows every store plugin with `enabled` true or false. Timed effects (`hide.blackout`, `hide.pixelate`, `roll.spin`, `freeze.start`, `seconds` up to 600) end by themselves, and a new call on the same thing restarts the time.

Hide it and Barrel Roll take `in` (a bloom's or sprig's name or id): then only the item in there changes, and the same source elsewhere stays as it is. `{"source": "Facecam", "in": "Gameplay"}` is the camera in Gameplay; `{"sprig": "Cam frame", "in": "Gameplay"}` is that sprig placed in Gameplay. Without `in`, it's the source everywhere it shows. Switching a Barrel roll filter on (`filter.set` with `on: true`, `filter.toggle`, or its tick in Filters) rolls once with the filter's own time and turns; `roll.spin` rolls every time it's called and switches the filter off again when it lands.

| Action | Plugin | What it does | Fields |
| --- | --- | --- | --- |
| `hide.pixelate` | Hide it | Pixelate a source for a while. | `source` (or `sprig` with `in`), `in` (optional), `seconds` (optional; 0 = until shown again), `size` (optional block size) |
| `hide.blackout` | Hide it | Black a source out for a while. | `source` (or `sprig` with `in`), `in` (optional), `seconds` (optional) |
| `hide.toggle` | Hide it | Hide it until toggled back, or show it. | `source` (or `sprig` with `in`), `in` (optional), `how` (optional): pixelate or blackout |
| `hide.show` | Hide it | Show it again now. | `source` (or `sprig` with `in`), `in` (optional) |
| `roll.spin` | Barrel Roll | Spin a sprig (everywhere it's shown) or one thing, then it lands where it was. | `sprig` or `source`, `in`, `seconds`, `turns`, `direction` (all optional) |
| `freeze.start` | Freeze | Hold a sprig (everywhere it's shown) or one thing still on the frame showing now, then it's live again. Called again while frozen, it keeps the picture and restarts the time. | `sprig` or `source`, `in` (optional: a bloom or sprig, to freeze only the one in there), `seconds` (optional; 0 = until `freeze.stop`) |
| `freeze.stop` | Freeze | Let it move again now. | `sprig` or `source`, `in` (optional) |
| `freeze.toggle` | Freeze | Freeze until toggled back, or let it go. | `sprig` or `source`, `in` (optional) |
| `deaths.add` | Death count | One more death (or `amount` more). The gravestone pops up with the new count. | `amount` (optional, default 1), `source` (optional) |
| `deaths.remove` | Death count | One less death (or `amount` less), never below 0. | `amount` (optional, default 1), `source` (optional) |
| `deaths.set` | Death count | Set the count to a number. | `count`, `source` (optional) |
| `deaths.reset` | Death count | Back to 0. | `source` (optional) |
| `deaths.show` | Death count | Pop the gravestone up with the count as it is. | `source` (optional) |
| `deaths.get` | Death count | The count, without showing it. | `source` (optional) |
| `pngtuber.expression` | PNGtuber | Show one of the model's expressions (case doesn't matter). | `expression`, `seconds` (optional; then back to the one before), `source` (optional) |
| `pngtuber.next` | PNGtuber | The next expression (default first, then A-Z). | `seconds` (optional), `source` (optional) |
| `pngtuber.reset` | PNGtuber | Back to the default expression. | `source` (optional) |
| `pngtuber.expressions` | PNGtuber | The model's expressions and the one showing. | `source` (optional) |
| `timer.start` | Stream timer | Start a stopwatch or countdown, or carry on after a pause. `minutes` starts a fresh countdown of that length. | `minutes` (optional), `source` (optional) |
| `timer.pause` | Stream timer | Pause it. | `source` (optional) |
| `timer.toggle` | Stream timer | Start, or pause if it's going (one button for both). | `source` (optional) |
| `timer.reset` | Stream timer | Back to the start, stopped. | `source` (optional) |
| `timer.add` | Stream timer | More time on a countdown (or a stopwatch); negative takes some off. | `seconds`, `source` (optional) |
| `timer.get` | Stream timer | Where it is, without changing it. | `source` (optional) |
| `countdown.start` | Countdown | Start the countdown, or carry on after a pause. `minutes` starts a fresh one of that length. | `minutes` (optional), `source` (optional; default the ones on air) |
| `countdown.pause` | Countdown | Pause it. | `source` (optional; default the ones on air) |
| `countdown.toggle` | Countdown | Start, or pause if it's going (one button for both). | `source` (optional; default the ones on air) |
| `countdown.reset` | Countdown | Back to the start, stopped. | `source` (optional; default the ones on air) |
| `countdown.add` | Countdown | More time; negative takes some off. | `seconds`, `source` (optional; default the ones on air) |
| `countdown.get` | Countdown | Where it is, without changing it. | `source` (optional; default the ones on air) |
| `nowplaying.get` | Now playing | The song on the Now playing card. | |
| `replay.play` | Instant replay | Play the last seconds back on stream. | `source` (optional; default every Instant replay), `seconds` (1-120, default its Seconds), `speed` (0.25-1; 0.5 is slow motion) |
| `replay.stop` | Instant replay | End a replay that's playing (it wipes out). | `source` (optional) |
| `visualizer.set` | Audio visualizer | Change the look: style, colors, sensitivity. | `style` (bars, wave, circle or line), `color`, `peak_color` (#rrggbb), `sensitivity` (0.05 to 10), `source` (optional; default every visualizer), all optional |

**Death count** answers every `deaths.*` action with `{"count": N}` and sends a `deaths.changed` event (`{"count": N}`, `plugin` group) after each change. Without `source` it changes every Death count you've added. The count is saved with your blooms, so it survives a restart. Adding a Death count (+ → Plugins → Death count) makes a sprig of its own on top of the bloom, like Snap: a gravestone grows out of the ground at the bottom, flips to the new number, holds for a few seconds (its Seconds setting) and sinks back.

**PNGtuber** answers with `{"expression": "happy", "expressions": ["default", "happy", ...]}` and sends a `pngtuber.expression` event (`{"expression": ...}`, `plugin` group) after each change. An expression set without `seconds` is saved with your blooms; one with `seconds` isn't. Its model is a folder of pictures named idle, talk, blink, talk_blink, yell and yell_blink (only idle and talk are needed); an expression is a folder inside it with its own set, or files named `happy_idle.png`. Pictures named anything else can be picked as its Idle picture and Talk picture instead. It moves with a sound you have in SprigCast (Auto: your mic), so muting the mic quiets it, or a mic straight from Windows.

**Instant replay** keeps the last Seconds (default 20) of what you picked in its settings: one sprig or source, plus an optional one in a corner (a camera). It keeps nothing while its bloom is off air or its eye is off, so `replay.play` then answers `not_allowed` ("Instant replay isn't on air."), as it does while one is already playing. It answers `{"playing": [names]}` and sends `replay.started` (`source`, `sourceId`, `seconds`, `speed`) and `replay.ended` (`source`, `sourceId`, `reason`: ended, stopped or failed) in the `plugin` group. Adding it (+ → Plugins → Instant replay) makes a sprig of its own that fills the canvas, on top of the bloom; it's see-through except while a replay plays. Its `capture` and `corner` settings (`source.set`, `source.new`) take a sprig or source by name or id and are kept as `sprig:<id>` or `source:<id>`; a name it can't find, a sound-only source or one that isn't in any bloom is refused with `bad_request` saying which.

**Stream timer** has three modes: Time live (counts up from going live, or from recording with Count from; it runs by itself, so the `timer.*` controls answer `bad_request` for it), Countdown (Minutes, or Until a time of day such as `8:30 PM`) and Stopwatch. Every `timer.*` action answers `{"source", "mode", "seconds", "running", "done", "text"}` and sends `timer.changed` with the same fields; `timer.done` (`{"source": name}`) comes when a countdown reaches 0 on screen. Without `source` they work on every stopwatch and countdown you've added. Where a stopwatch or countdown is is saved with your blooms, so it carries on after a restart. With Restart when shown on (the default), a countdown starts over each time it comes on screen, for a "Starting soon" bloom. The card sits in the top left corner of the bloom in every shape.

**Countdown** counts down Minutes, or to a time of day (Until, such as `8:30 PM`; that one runs by itself, so the controls answer `bad_request` for it). Every `countdown.*` action answers `{"source", "sources", "seconds", "running", "done", "text"}`: `source` is the countdown the numbers are about and `sources` names every countdown the action was for. Each one it changed sends `countdown.changed` (`{"source", "seconds", "running", "done", "text"}`); `countdown.done` (`{"source": name}`) comes when it reaches 0 on screen. Without `source` they work on the Countdowns on air now (in the bloom showing, eye on), and on every Countdown when none is on air; `countdown.get` answers for the one on air, else the first. Pass `source` (name or id) to pick one. With Restart when shown on (the default) it starts over each time it comes on screen, for a "Starting soon" bloom. The card sits in the top left corner of the bloom in every shape, with a vine along its bottom that shrinks as the time runs out.

**Now playing** shows the song Windows says is playing (Spotify, YouTube Music, browsers and most players; no keys or sign-in), with its cover, a time bar and sound bars, in the bottom left corner, in one of four styles. It fades away when the music pauses or stops. It listens to Windows only while the card is on screen, and only when told something changed. `nowplaying.get` answers `{"playing", "title", "artist", "album", "app", "length", "position"}` (seconds; 0 when the app doesn't say), and `nowplaying.changed` (same fields) comes when the song changes, pauses or jumps while the card is on screen.

**Audio visualizer** draws bars, a wave, a circle or a line that move with a sound SprigCast already mixes (everything viewers hear, or one sound: a mic, desktop, music, an app). It listens only while it's on air. `visualizer.set` answers with the names it changed and the style and colors now. Adding one (+ → Plugins → Audio visualizer) makes a sprig of its own along the bottom of the bloom.

**Snap** has no actions of its own: it turns every saved screenshot (`screenshot.take`, the button or its hotkey) into a polaroid animation on stream. A camera pops up, flashes and prints the screenshot. The polaroid shows the whole screenshot. The saved file is the plain screenshot, and a screenshot taken while a polaroid is up leaves it out; turn on **Save a polaroid copy** in the Snap's settings (off by default) to also save the framed polaroid next to it. Adding a Snap (+ → Plugins → Snap) makes a sprig of its own that fills the canvas, on top of the bloom in every shape, so it overlays the stream without any placing. With **Freeze the screen while it prints** on (the default), the bloom on air holds still from the snapped frame until the polaroid slides away, so viewers see exactly the moment that was snapped; the Snap itself keeps moving.

Using Streamer.bot? See [Streamer.bot](streamerbot.md). Building an integration? See [Building on SprigCast](integrating.md). Making a plugin? See [Build your own plugin](plugins.md). Stream Deck: see [the SprigCast plugin](../integrations/streamdeck/README.md).

## Web links

For buttons and scripts that can only open a link. Turn on **Allow web links** in Settings → Connections (an Expert settings row: tick Expert settings at the bottom of Settings first). A password is always needed.

```
http://127.0.0.1:4455/api                                  every action
http://127.0.0.1:4455/api/bloom.show?bloom=BRB&password=…   run one, fields in the address
curl -H "Authorization: Bearer …" -X POST http://127.0.0.1:4455/api/filter.set \
     -d '{"source": "Facecam", "filter": "Blur", "on": true}'
```

Values in the address are read the way they look: `true`, `false`, `null` and numbers become those, and anything else is text. The answer is the same JSON as on the WebSocket, without `id`. The HTTP status matches the error: 400 bad request, 401 password, 403 off, 404 not found, 409 not allowed or busy.

Requests made by web pages on other sites are refused, so a page you visit can't drive SprigCast.

## The OBS-compatible door

Apps that can only talk to OBS can connect to `ws://127.0.0.1:4455` (no `/sprigcast`), using the obs-websocket 5 protocol. Blooms appear as scenes and sprigs as nested scenes. Gardens are scene collections and paths are profiles (GetSceneCollectionList, SetCurrentSceneCollection, CreateSceneCollection, GetProfileList, SetCurrentProfile, CreateProfile, RemoveProfile, and their events). It also takes the requests TikFinity, Streamer.bot and Stream Deck's OBS actions send most: SetInputSettings (change a text, a browser page's url, an image or video file; OBS setting names like `file` and `local_file` are understood), GetInputDefaultSettings, SetInputName, PressInputPropertiesButton (`refreshnocache` reloads a browser page), GetSourceFilterList, GetSourceFilter, SetSourceFilterEnabled, SetSourceFilterSettings, SetSourceFilterName (with the SourceFilterEnableStateChanged event), GetMediaInputStatus, TriggerMediaInputAction, SetMediaInputCursor, OffsetMediaInputCursor, and GetSourceScreenshot / SaveSourceScreenshot (png, of a bloom that's on air; a picture up to a second old is reused), and the virtual camera (GetVirtualCamStatus, StartVirtualCam, StopVirtualCam, ToggleVirtualCam, VirtualcamStateChanged). Apps that build overlays can make them: CreateScene (a bloom in every shape that's on, with its own sprig), RemoveScene, CreateInput (answers `inputUuid` and a numeric `sceneItemId`), CreateSceneItem and RemoveSceneItem (a plugin starts readable in a free corner, as from the + menu); things added to a bloom go in its sprig and answer to the bloom's name. StartStream and StartRecord never wait on a SprigCast pop-up: when the Stream doctor (or low space) would ask first, they fail with the reasons in the comment, and a `force: true` field goes ahead. A browser source's OBS `shutdown` setting is SprigCast's `keep_running` turned around, both ways. A text's OBS `read_from_file` is SprigCast's `read_file` (a Text leaf follows its `file` live). Each place you stream to is an output called `sprigcast:<id>` with GetOutputStatus, StartOutput and StopOutput while live (`simple_stream` is the whole stream). Beyond these, new features come to the SprigCast API only. It can be switched off with **OBS-style connection (for Streamer.bot)** in Settings → Connections.

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
