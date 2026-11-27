# SprigCast API reference

<!-- Written by `python -m sprigcast.control.spec` from the actions in the code. Don't edit by hand:
     tests/test_control_spec.py fails when this page is behind. -->

Every action, field, event and error, as SprigCast itself describes them. The same description, for programs, is [protocol.json](protocol.json); a running SprigCast also answers it as `app.protocol` and at `http://127.0.0.1:<port>/api/protocol.json`. How to connect and how messages look: [the control API](../control-api.md). Naming, versions and errors: [conventions](conventions.md).

API version 0 (preview). Store plugins' actions are listed in [the control API](../control-api.md#store-plugins).

## Actions

### app

#### `app.actions`

Every action with what it does and its fields (fields starting with ? are optional). *(looks only)*

```json
{"id": "1", "do": "app.actions"}
```

#### `app.broadcast`

Send your own event to every connected app.

| Field | Needed | Type | About |
| --- | --- | --- | --- |
| `data` | yes | object | any object |

```json
{"id": "1", "do": "app.broadcast", "data": {}}
```

#### `app.info`

Version, API version, the shapes that are on, each canvas's size, event groups and features. *(looks only)*

```json
{"id": "1", "do": "app.info"}
```

#### `app.protocol`

The whole API described for programs: every action with typed fields (type, needed, default, range, words it takes), every event, error code, error reason and close code. *(looks only)*

```json
{"id": "1", "do": "app.protocol"}
```

#### `app.stats`

Frames per second, render time and memory. *(looks only)*

```json
{"id": "1", "do": "app.stats"}
```

#### `plugins.list`

The plugins that are added (enabled) and the store's others (not enabled), each with the actions it brings. *(looks only)*

```json
{"id": "1", "do": "plugins.list"}
```

### bloom

#### `bloom.current`

The bloom on air in each shape. *(looks only)*

```json
{"id": "1", "do": "bloom.current"}
```

#### `bloom.new`

Make an empty bloom (with its own sprig, where what you add goes).

| Field | Needed | Type | About |
| --- | --- | --- | --- |
| `name` | yes | string | name |
| `shape` | no | `both` \| `horizontal` \| `vertical`, default `"both"` | both (default: every canvas), horizontal or vertical |

```json
{"id": "1", "do": "bloom.new", "name": "Name"}
```

#### `bloom.remove`

Remove a bloom (in every shape). Its sources stay while other blooms use them.

| Field | Needed | Type | About |
| --- | --- | --- | --- |
| `bloom` | yes | string | name or id |

```json
{"id": "1", "do": "bloom.remove", "bloom": "Name"}
```

#### `bloom.show`

Put a bloom on air.

| Field | Needed | Type | About |
| --- | --- | --- | --- |
| `bloom` | yes | string | name or id |
| `shape` | no | `both` \| `horizontal` \| `vertical`, default `"both"` | both (default), horizontal or vertical |

```json
{"id": "1", "do": "bloom.show", "bloom": "Name"}
```

#### `blooms.list`

Every bloom, its shapes and which shapes it's on air in. *(looks only)*

```json
{"id": "1", "do": "blooms.list"}
```

### camera

#### `camera.set`

Turn SprigCast Camera on or off (no on: toggle).

| Field | Needed | Type | About |
| --- | --- | --- | --- |
| `on` | no | boolean | true or false |

```json
{"id": "1", "do": "camera.set"}
```

#### `camera.status`

Whether SprigCast Camera (the virtual camera) is on. *(looks only)*

```json
{"id": "1", "do": "camera.status"}
```

### clip

#### `clip.save`

Save the last seconds as a clip (gets clips ready first if they're off). Answers with the file names at once; the clip.saved event says when they are written. clip is true when a clip is being saved; when it is false, note says why and when one can be.

```json
{"id": "1", "do": "clip.save"}
```

#### `clips.set`

Keep clips ready (on) or stop (off).

| Field | Needed | Type | About |
| --- | --- | --- | --- |
| `on` | yes | boolean | true or false |

```json
{"id": "1", "do": "clips.set", "on": true}
```

#### `clips.status`

Whether clips are ready and how long a clip is. *(looks only)*

```json
{"id": "1", "do": "clips.status"}
```

### edit

#### `edit.redo`

Put back the change that was just undone (as Ctrl+Y in SprigCast).

```json
{"id": "1", "do": "edit.redo"}
```

#### `edit.undo`

Undo the last change to the garden (as Ctrl+Z in SprigCast).

```json
{"id": "1", "do": "edit.undo"}
```

### filter

#### `filter.add`

Add a filter to a source or a placed sprig. *(can be in one_step)*

| Field | Needed | Type | About |
| --- | --- | --- | --- |
| `kind` | yes | string | kind from filters.kinds |
| `name` | no | string | its name |
| `settings` | no | object | {key: value} |
| `on` | no | boolean, default `true` | true (default) or false |
| `source` | no | string | name or id |
| `sprig` | no | string | a sprig instead: its filters where it's placed (name or id) |
| `in` | no | string | with sprig: only where it's placed in this bloom or sprig (both canvases) (name or id) |

```json
{"id": "1", "do": "filter.add", "kind": "Name"}
```

#### `filter.move`

Move a filter to a new place in the list (0 = top). *(can be in one_step)*

| Field | Needed | Type | About |
| --- | --- | --- | --- |
| `filter` | yes | string | name, id or kind (name or id) |
| `index` | yes | integer | new position |
| `source` | no | string | name or id |
| `sprig` | no | string | a sprig instead: its filters where it's placed (name or id) |
| `in` | no | string | with sprig: only where it's placed in this bloom or sprig (both canvases) (name or id) |

```json
{"id": "1", "do": "filter.move", "filter": "Name", "index": 1}
```

#### `filter.remove`

Remove a filter from a source or a placed sprig. *(can be in one_step)*

| Field | Needed | Type | About |
| --- | --- | --- | --- |
| `filter` | yes | string | name, id or kind (name or id) |
| `source` | no | string | name or id |
| `sprig` | no | string | a sprig instead: its filters where it's placed (name or id) |
| `in` | no | string | with sprig: only where it's placed in this bloom or sprig (both canvases) (name or id) |

```json
{"id": "1", "do": "filter.remove", "filter": "Name"}
```

#### `filter.set`

Turn a filter on or off and/or change some of its settings (others stay). *(can be in one_step)*

| Field | Needed | Type | About |
| --- | --- | --- | --- |
| `filter` | yes | string | filter name, id or kind (name or id) |
| `on` | no | boolean | true or false |
| `settings` | no | object | {key: value} |
| `source` | no | string | name or id |
| `sprig` | no | string | a sprig instead: its filters where it's placed (name or id) |
| `in` | no | string | with sprig: only where it's placed in this bloom or sprig (both canvases) (name or id) |

```json
{"id": "1", "do": "filter.set", "filter": "Name"}
```

#### `filter.toggle`

Turn a filter on if off, off if on.

| Field | Needed | Type | About |
| --- | --- | --- | --- |
| `filter` | yes | string | name, id or kind (name or id) |
| `source` | no | string | name or id |
| `sprig` | no | string | a sprig instead: its filters where it's placed (name or id) |
| `in` | no | string | with sprig: only where it's placed in this bloom or sprig (both canvases) (name or id) |

```json
{"id": "1", "do": "filter.toggle", "filter": "Name"}
```

#### `filters.kinds`

Filter kinds you can add, with their settings and limits. *(looks only)*

```json
{"id": "1", "do": "filters.kinds"}
```

#### `filters.list`

A source's or a placed sprig's filters, top to bottom. *(looks only)*

| Field | Needed | Type | About |
| --- | --- | --- | --- |
| `source` | no | string | name or id |
| `sprig` | no | string | a sprig instead: its filters where it's placed (name or id) |
| `in` | no | string | with sprig: only where it's placed in this bloom or sprig (both canvases) (name or id) |

```json
{"id": "1", "do": "filters.list"}
```

#### `filters.share`

A placed sprig: the same filters on both canvases (the usual), or its own on each.

| Field | Needed | Type | About |
| --- | --- | --- | --- |
| `on` | yes | boolean | true or false |
| `source` | no | string | name or id |
| `sprig` | no | string | a sprig instead: its filters where it's placed (name or id) |
| `in` | no | string | with sprig: only where it's placed in this bloom or sprig (both canvases) (name or id) |

```json
{"id": "1", "do": "filters.share", "on": true}
```

### garden

#### `garden.new`

Make a garden and switch to it: empty, or a copy of one.

| Field | Needed | Type | About |
| --- | --- | --- | --- |
| `name` | yes | string | name for it |
| `copy` | no | string | garden to copy (name) |

```json
{"id": "1", "do": "garden.new", "name": "Name"}
```

#### `garden.switch`

Switch to another garden.

| Field | Needed | Type | About |
| --- | --- | --- | --- |
| `garden` | yes | string | name |

```json
{"id": "1", "do": "garden.switch", "garden": "Name"}
```

#### `gardens.list`

Every garden (a whole set of blooms) A-Z, and the one in use. *(looks only)*

```json
{"id": "1", "do": "gardens.list"}
```

### hotkey

#### `hotkey.list`

Hotkey actions and what they do. *(looks only)*

```json
{"id": "1", "do": "hotkey.list"}
```

#### `hotkey.run`

Do what a hotkey does (hotkey.list shows them). note says why when it couldn't.

| Field | Needed | Type | About |
| --- | --- | --- | --- |
| `hotkey` | yes | string | hotkey action id |

```json
{"id": "1", "do": "hotkey.run", "hotkey": "Name"}
```

### item

#### `item.add`

Put a source in a bloom (in its sprig) or a sprig, on top (a plugin starts readable, in a free corner). Answers the item's id. *(can be in one_step)*

| Field | Needed | Type | About |
| --- | --- | --- | --- |
| `source` | yes | string | name or id |
| `bloom` | no | string | bloom to add to (name or id) |
| `sprig` | no | string | sprig to add to (name or id) |
| `shape` | no | `both` \| `horizontal` \| `vertical`, default `"both"` | both (default), horizontal or vertical |
| `visible` | no | boolean, default `true` | true (default) or false |

```json
{"id": "1", "do": "item.add", "source": "Name"}
```

#### `item.duplicate`

Copy something in a bloom or sprig: a new source with the same settings and filters, placed the same way, on top. Sprigs can't be copied this way. *(can be in one_step)*

| Field | Needed | Type | About |
| --- | --- | --- | --- |
| `item` | yes | string | name or id |
| `bloom` | no | string | bloom it's in (name or id) |
| `sprig` | no | string | sprig it's in (name or id) |
| `shape` | no | `both` \| `horizontal` \| `vertical`, default `"both"` | both (default), horizontal or vertical |
| `name` | no | string | the copy's name (default: its name and a number) |

```json
{"id": "1", "do": "item.duplicate", "item": "Name"}
```

#### `item.hide`

Hide something inside a bloom or sprig. *(can be in one_step)*

| Field | Needed | Type | About |
| --- | --- | --- | --- |
| `item` | yes | string | name or id |
| `bloom` | no | string | bloom it's in (name or id) |
| `sprig` | no | string | sprig it's in (name or id) |
| `shape` | no | `both` \| `horizontal` \| `vertical`, default `"both"` | both (default), horizontal or vertical |

```json
{"id": "1", "do": "item.hide", "item": "Name"}
```

#### `item.lock`

Lock something in place in Build, or unlock it (no locked: toggle). *(can be in one_step)*

| Field | Needed | Type | About |
| --- | --- | --- | --- |
| `item` | yes | string | name or id |
| `bloom` | no | string | bloom it's in (name or id) |
| `sprig` | no | string | sprig it's in (name or id) |
| `shape` | no | `both` \| `horizontal` \| `vertical`, default `"both"` | both (default), horizontal or vertical |
| `locked` | no | boolean | true or false |

```json
{"id": "1", "do": "item.lock", "item": "Name"}
```

#### `item.order`

Bring something forward or send it back. *(can be in one_step)*

| Field | Needed | Type | About |
| --- | --- | --- | --- |
| `item` | yes | string | name or id |
| `bloom` | no | string | bloom it's in (name or id) |
| `sprig` | no | string | sprig it's in (name or id) |
| `shape` | no | `both` \| `horizontal` \| `vertical`, default `"both"` | both (default), horizontal or vertical |
| `to` | yes | `top` \| `bottom` \| `up` \| `down` | top, bottom, up (one step) or down (one step) |

```json
{"id": "1", "do": "item.order", "item": "Name", "to": "top"}
```

#### `item.place`

Move, size or turn something inside a bloom or sprig (only the fields you give change). *(can be in one_step)*

| Field | Needed | Type | About |
| --- | --- | --- | --- |
| `item` | yes | string | name or id |
| `bloom` | no | string | bloom it's in (name or id) |
| `sprig` | no | string | sprig it's in (name or id) |
| `shape` | no | `both` \| `horizontal` \| `vertical`, default `"both"` | both (default), horizontal or vertical |
| `x` | no | number | left edge, canvas pixels |
| `y` | no | number | top edge |
| `scale` | no | number | size, 1 = as big as the source |
| `scale_x` | no | number | width only, 1 = as wide as the source |
| `scale_y` | no | number | height only, 1 = as tall as the source |
| `rotation` | no | number | degrees clockwise |
| `dx` | no | number | pixels to move right (negative: left) |
| `dy` | no | number | pixels to move down (negative: up) |
| `crop` | no | object | {left, top, right, bottom} in source pixels (the sides you give change) |
| `bounds` | no | `off` \| `fit` \| `fill` \| `stretch` | off, fit, fill or stretch: squeeze it into a box |
| `bounds_width` | no | number | the box's width, canvas pixels |
| `bounds_height` | no | number | the box's height |
| `bounds_align` | no | `top_left` \| `top` \| `top_right` \| `left` \| `center` \| `right` \| `bottom_left` \| `bottom` \| `bottom_right` | where the picture sits in its box (top_left ... center ... bottom_right) |
| `align` | no | `top_left` \| `top` \| `top_right` \| `left` \| `center` \| `right` \| `bottom_left` \| `bottom` \| `bottom_right` | the point it turns around (top_left ... center ... bottom_right); the picture stays where it is |

```json
{"id": "1", "do": "item.place", "item": "Name"}
```

#### `item.remove`

Take something out of a bloom or sprig. Its source stays (source.remove removes that). *(can be in one_step)*

| Field | Needed | Type | About |
| --- | --- | --- | --- |
| `item` | yes | string | name or id |
| `bloom` | no | string | bloom it's in (name or id) |
| `sprig` | no | string | sprig it's in (name or id) |
| `shape` | no | `both` \| `horizontal` \| `vertical`, default `"both"` | both (default), horizontal or vertical |

```json
{"id": "1", "do": "item.remove", "item": "Name"}
```

#### `item.show`

Show something inside a bloom or sprig. *(can be in one_step)*

| Field | Needed | Type | About |
| --- | --- | --- | --- |
| `item` | yes | string | name or id |
| `bloom` | no | string | bloom it's in (name or id) |
| `sprig` | no | string | sprig it's in (name or id) |
| `shape` | no | `both` \| `horizontal` \| `vertical`, default `"both"` | both (default), horizontal or vertical |

```json
{"id": "1", "do": "item.show", "item": "Name"}
```

#### `item.status`

Whether something inside a bloom or sprig is shown. *(looks only)*

| Field | Needed | Type | About |
| --- | --- | --- | --- |
| `item` | yes | string | name or id |
| `bloom` | no | string | bloom it's in (name or id) |
| `sprig` | no | string | sprig it's in (name or id) |
| `shape` | no | `both` \| `horizontal` \| `vertical`, default `"both"` | both (default), horizontal or vertical |

```json
{"id": "1", "do": "item.status", "item": "Name"}
```

#### `item.toggle`

Show it if hidden, hide it if shown. *(can be in one_step)*

| Field | Needed | Type | About |
| --- | --- | --- | --- |
| `item` | yes | string | name or id |
| `bloom` | no | string | bloom it's in (name or id) |
| `sprig` | no | string | sprig it's in (name or id) |
| `shape` | no | `both` \| `horizontal` \| `vertical`, default `"both"` | both (default), horizontal or vertical |

```json
{"id": "1", "do": "item.toggle", "item": "Name"}
```

#### `items.list`

What's inside a bloom or sprig, per shape. *(looks only, can be in one_step)*

| Field | Needed | Type | About |
| --- | --- | --- | --- |
| `bloom` | no | string | name or id |
| `sprig` | no | string | name or id |

```json
{"id": "1", "do": "items.list"}
```

### layout

#### `layout.get`

Where everything in a bloom or sprig sits, per shape, top of the stack first: each item's place, crop, bounds, its source's own size and its box on the canvas. Sprigs carry what's inside them. *(looks only, can be in one_step)*

| Field | Needed | Type | About |
| --- | --- | --- | --- |
| `bloom` | no | string | name or id |
| `sprig` | no | string | name or id |
| `shape` | no | `both` \| `horizontal` \| `vertical`, default `"both"` | both (default), horizontal or vertical |
| `flat` | no | boolean | true: only the things themselves (no sprigs), as they end up on the canvas |
| `overlaps` | no | boolean | true: also the pairs of shown things whose boxes overlap |

```json
{"id": "1", "do": "layout.get"}
```

### media

#### `media.list`

Every video and sound file, with what it's doing (playing, paused, stopped, ended). *(looks only)*

```json
{"id": "1", "do": "media.list"}
```

#### `media.loop`

Loop it or not (no on: toggle).

| Field | Needed | Type | About |
| --- | --- | --- | --- |
| `media` | yes | string | name or id |
| `on` | no | boolean | true or false |

```json
{"id": "1", "do": "media.loop", "media": "Name"}
```

#### `media.pause`

Pause where it is.

| Field | Needed | Type | About |
| --- | --- | --- | --- |
| `media` | yes | string | name or id |

```json
{"id": "1", "do": "media.pause", "media": "Name"}
```

#### `media.play`

Play (from the start if it ended or was stopped).

| Field | Needed | Type | About |
| --- | --- | --- | --- |
| `media` | yes | string | name or id |

```json
{"id": "1", "do": "media.play", "media": "Name"}
```

#### `media.restart`

Play from the start.

| Field | Needed | Type | About |
| --- | --- | --- | --- |
| `media` | yes | string | name or id |

```json
{"id": "1", "do": "media.restart", "media": "Name"}
```

#### `media.seek`

Jump to a time.

| Field | Needed | Type | About |
| --- | --- | --- | --- |
| `media` | yes | string | name or id |
| `seconds` | no | number | time from the start |
| `change` | no | number | seconds to move (negative: back) |

```json
{"id": "1", "do": "media.seek", "media": "Name"}
```

#### `media.status`

What a video or sound file is doing, where it is and how long it is. *(looks only)*

| Field | Needed | Type | About |
| --- | --- | --- | --- |
| `media` | yes | string | name or id |

```json
{"id": "1", "do": "media.status", "media": "Name"}
```

#### `media.stop`

Stop and show nothing (play starts from the beginning).

| Field | Needed | Type | About |
| --- | --- | --- | --- |
| `media` | yes | string | name or id |

```json
{"id": "1", "do": "media.stop", "media": "Name"}
```

#### `media.toggle`

Pause if playing, else play.

| Field | Needed | Type | About |
| --- | --- | --- | --- |
| `media` | yes | string | name or id |

```json
{"id": "1", "do": "media.toggle", "media": "Name"}
```

### path

#### `path.new`

Make a path and switch to it: empty, or a copy of one.

| Field | Needed | Type | About |
| --- | --- | --- | --- |
| `name` | yes | string | name for it |
| `copy` | no | string | path to copy (name) |

```json
{"id": "1", "do": "path.new", "name": "Name"}
```

#### `path.switch`

Switch to another path (not while live or recording).

| Field | Needed | Type | About |
| --- | --- | --- | --- |
| `path` | yes | string | name |

```json
{"id": "1", "do": "path.switch", "path": "Name"}
```

#### `paths.list`

Every path (where and how you go out: places, encoder, recording) A-Z, and the one in use. *(looks only)*

```json
{"id": "1", "do": "paths.list"}
```

### place

#### `place.set`

Switch a place on or off for the next Go live, and/or its test mode (Twitch: the stream reaches Twitch but viewers never see it).

| Field | Needed | Type | About |
| --- | --- | --- | --- |
| `place` | yes | string | id from places.list (id, name or service) |
| `on` | no | boolean | true or false |
| `bandwidth_test` | no | boolean | true or false (only places whose service has a test mode) |
| `bitrateKbps` | no | integer, 500 to 50000 | 500 to 50000: its bitrate for the next Go live |

```json
{"id": "1", "do": "place.set", "place": "Name"}
```

#### `place.start`

While live: start sending to one more place (switched on for next time too).

| Field | Needed | Type | About |
| --- | --- | --- | --- |
| `place` | yes | string | id from places.list (id, name or service) |

```json
{"id": "1", "do": "place.start", "place": "Name"}
```

#### `place.stop`

While live: stop sending to one place (the others keep going; the last one ends the stream).

| Field | Needed | Type | About |
| --- | --- | --- | --- |
| `place` | yes | string | id from places.list (id, name or service) |

```json
{"id": "1", "do": "place.stop", "place": "Name"}
```

#### `places.list`

Where the stream goes (Twitch, YouTube...), each one's shape and if it's on. *(looks only)*

```json
{"id": "1", "do": "places.list"}
```

### record

#### `record.chapter`

Add a chapter mark to the recording at this moment (editors and players list them).

| Field | Needed | Type | About |
| --- | --- | --- | --- |
| `name` | no | string | the chapter's name (default: Chapter 1, 2...) |

```json
{"id": "1", "do": "record.chapter"}
```

#### `record.pause`

Pause the recording. The file stays open; record.resume carries on in it.

```json
{"id": "1", "do": "record.pause"}
```

#### `record.resume`

Carry on recording in the same file after record.pause.

```json
{"id": "1", "do": "record.resume"}
```

#### `record.start`

Start recording. If the drive is nearly full, nothing starts: doctor_warnings says so.

| Field | Needed | Type | About |
| --- | --- | --- | --- |
| `force` | no | boolean | true: record anyway |

```json
{"id": "1", "do": "record.start"}
```

#### `record.status`

Recording or not, for how long, the file, and whether it's paused. *(looks only)*

```json
{"id": "1", "do": "record.status"}
```

#### `record.stop`

Stop recording.

```json
{"id": "1", "do": "record.stop"}
```

#### `record.toggle`

Start or stop recording (starting answers like record.start).

| Field | Needed | Type | About |
| --- | --- | --- | --- |
| `force` | no | boolean | true: record anyway |

```json
{"id": "1", "do": "record.toggle"}
```

#### `record.toggle_pause`

Pause, or carry on after a pause.

```json
{"id": "1", "do": "record.toggle_pause"}
```

### screenshot

#### `screenshot.take`

A picture of what's on air in each shape, or of one source or bloom.

| Field | Needed | Type | About |
| --- | --- | --- | --- |
| `shape` | no | `both` \| `horizontal` \| `vertical`, default `"both"` | both (default), horizontal or vertical |
| `source` | no | string | just this source (a camera...), name or id |
| `bloom` | no | string | just this bloom, name or id |
| `width` | no | integer | shrink to this width in pixels (keeps the shape) |
| `save` | no | boolean, default `true` | true (default): save PNGs in the recordings folder, under Screenshots |
| `data` | no | boolean | true: send the picture back as base64 PNG (for bots and pages; large) |

```json
{"id": "1", "do": "screenshot.take"}
```

### sound

#### `sound.balance`

Left/right: -1 left, 0 middle, 1 right. *(can be in one_step)*

| Field | Needed | Type | About |
| --- | --- | --- | --- |
| `sound` | yes | string | name or id |
| `balance` | yes | number, -1.0 to 1.0 | -1 to 1 |

```json
{"id": "1", "do": "sound.balance", "sound": "Name", "balance": 1}
```

#### `sound.micFirst`

Lower everything else while your mic talks (ducking).

| Field | Needed | Type | About |
| --- | --- | --- | --- |
| `on` | yes | boolean | true or false |

```json
{"id": "1", "do": "sound.micFirst", "on": true}
```

#### `sound.mute`

Mute or unmute a sound (no muted: toggle). *(can be in one_step)*

| Field | Needed | Type | About |
| --- | --- | --- | --- |
| `sound` | yes | string | name or id |
| `muted` | no | boolean | true or false |

```json
{"id": "1", "do": "sound.mute", "sound": "Name"}
```

#### `sound.volume`

Set a sound's volume in dB, or change it by some dB. *(can be in one_step)*

| Field | Needed | Type | About |
| --- | --- | --- | --- |
| `sound` | yes | string | name or id |
| `db` | no | number, -100.0 to 26.0 | new volume, -100 to 26 |
| `change` | no | number | dB to add (negative to lower) |

```json
{"id": "1", "do": "sound.volume", "sound": "Name"}
```

#### `sounds.list`

Every sound in the mixer with its volume and mute. *(looks only)*

```json
{"id": "1", "do": "sounds.list"}
```

### source

#### `source.get`

A source's settings. *(looks only)*

| Field | Needed | Type | About |
| --- | --- | --- | --- |
| `source` | yes | string | name or id |

```json
{"id": "1", "do": "source.get", "source": "Name"}
```

#### `source.new`

Make a source (a browser page, text, image...). Put it in a bloom with item.add. *(can be in one_step)*

| Field | Needed | Type | About |
| --- | --- | --- | --- |
| `name` | yes | string | name |
| `kind` | yes | string | browser, text, image, color, media... |
| `settings` | no | object | {key: value}, e.g. url, width, height |

```json
{"id": "1", "do": "source.new", "name": "Name", "kind": "Name"}
```

#### `source.refresh`

Load a web page source again, now.

| Field | Needed | Type | About |
| --- | --- | --- | --- |
| `source` | yes | string | name or id |

```json
{"id": "1", "do": "source.refresh", "source": "Name"}
```

#### `source.remove`

Remove a source, taking it out of every bloom and sprig it's in. *(can be in one_step)*

| Field | Needed | Type | About |
| --- | --- | --- | --- |
| `source` | yes | string | name or id |

```json
{"id": "1", "do": "source.remove", "source": "Name"}
```

#### `source.rename`

Give a source a new name. *(can be in one_step)*

| Field | Needed | Type | About |
| --- | --- | --- | --- |
| `source` | yes | string | name or id |
| `name` | yes | string | new name |

```json
{"id": "1", "do": "source.rename", "source": "Name", "name": "Name"}
```

#### `source.rotate`

Turn a source to an angle wherever it's shown (0: upright again). *(can be in one_step)*

| Field | Needed | Type | About |
| --- | --- | --- | --- |
| `source` | yes | string | name or id |
| `degrees` | yes | number, -3600.0 to 3600.0 | clockwise, -3600 to 3600 |

```json
{"id": "1", "do": "source.rotate", "source": "Name", "degrees": 1}
```

#### `source.set`

Change some of a source's settings: text, a browser page's url, an image file, a color... *(can be in one_step)*

| Field | Needed | Type | About |
| --- | --- | --- | --- |
| `source` | yes | string | name or id |
| `settings` | yes | object | {key: value} |

```json
{"id": "1", "do": "source.set", "source": "Name", "settings": {}}
```

#### `sources.list`

Every source (cameras, text, browser pages, images, sounds...). *(looks only)*

```json
{"id": "1", "do": "sources.list"}
```

### sprig

#### `sprig.remove`

Remove a sprig in every shape, and take it out of every bloom. Sources only it used go too, except sounds (mics, desktop and app sounds), which always stay. *(can be in one_step)*

| Field | Needed | Type | About |
| --- | --- | --- | --- |
| `sprig` | yes | string | name or id |

```json
{"id": "1", "do": "sprig.remove", "sprig": "Name"}
```

#### `sprig.state`

Switch a sprig to one of its Live states (null: as built).

| Field | Needed | Type | About |
| --- | --- | --- | --- |
| `sprig` | yes | string | name or id |
| `state` | yes | string or null | state name or null |

```json
{"id": "1", "do": "sprig.state", "sprig": "Name", "state": "Name"}
```

#### `sprigs.list`

Every sprig, its Live states and the state showing. *(looks only)*

```json
{"id": "1", "do": "sprigs.list"}
```

### stream

#### `stream.start`

Go live to every place that's switched on; the answer lists them (places). If the Stream doctor would warn first, nothing starts: doctor_warnings lists them. places: the ids you expect to go live; if the places switched on differ, nothing starts (places_differ lists them).

| Field | Needed | Type | About |
| --- | --- | --- | --- |
| `force` | no | boolean | true: go live past the doctor's warnings |
| `places` | no | array | list of place ids from places.list: go live only if exactly these are switched on |

```json
{"id": "1", "do": "stream.start"}
```

#### `stream.status`

Live or not, for how long, dropped frames, and each place on its own. *(looks only)*

```json
{"id": "1", "do": "stream.status"}
```

#### `stream.stop`

End the stream.

```json
{"id": "1", "do": "stream.stop"}
```

#### `stream.toggle`

Go live, or end the stream (going live answers like stream.start).

| Field | Needed | Type | About |
| --- | --- | --- | --- |
| `force` | no | boolean | true: go live past the doctor's warnings |

```json
{"id": "1", "do": "stream.toggle"}
```

### transition

#### `transition.get`

The usual transition, its length and (Move) how it moves. *(looks only)*

```json
{"id": "1", "do": "transition.get"}
```

#### `transition.set`

Change the usual transition, its length and (Move) how it moves.

| Field | Needed | Type | About |
| --- | --- | --- | --- |
| `kind` | no | `cut` \| `fade` \| `move` \| `slide` \| `swipe` \| `wipe` \| `leaf` \| `bloom` \| `stinger` | cut, fade, move, slide, swipe, wipe, leaf, bloom, stinger |
| `ms` | no | integer, 50 to 20000 | length in milliseconds, 50 to 20000 |
| `easing` | no | `smooth` \| `ease_out` \| `ease_in` \| `linear` \| `spring` | Move only: smooth, ease_out, ease_in, linear, spring |
| `unmatched` | no | `fade` \| `scale` \| `slide` | Move only, what only one bloom shows: fade, scale, slide |

```json
{"id": "1", "do": "transition.set"}
```

### viewers

#### `viewers.counts`

While live: how many watch on each platform (YouTube, Twitch, Kick), read at most once a minute; never waits. A platform not live: live false; viewers null: no number yet.

```json
{"id": "1", "do": "viewers.counts"}
```

### youtube

#### `youtube.viewers`

While live on YouTube: how many watch, in all and on each YouTube stream (read from YouTube at most once a minute; never waits). Not live there or not signed in: live false.

```json
{"id": "1", "do": "youtube.viewers"}
```

## Events

| Event | Group | Fields | About |
| --- | --- | --- | --- |
| `app.closing` | every app | none | SprigCast is closing or turning its API off; every app gets it. The connection closes next: look again in a second or two, it's often a restart. |
| `bloom.shown` | bloom | `bloom`, `bloomId`, `shape` | A bloom went on air. |
| `blooms.changed` | bloom | `blooms` | Blooms were added, removed or renamed. |
| `break.ended` | plugin | `onBreak`, `breakLeft`, `nextBreakIn`, `endIn` | A break ended. (Break & End Guard plugin) |
| `break.started` | plugin | `onBreak`, `breakLeft`, `nextBreakIn`, `endIn` | A break started. (Break & End Guard plugin) |
| `camera.changed` | output | `on` | SprigCast Camera was turned on or off. |
| `clip.saved` | output | `files` | A clip's files are written. |
| `clips.changed` | output | `on` | Clips were made ready or stopped. |
| `countdown.changed` | plugin | `source`, `seconds`, `running`, `done`, `text` | A countdown changed. (Countdown plugin) |
| `countdown.done` | plugin | `source` | A countdown reached 0 on screen. (Countdown plugin) |
| `custom` | custom | `data` | Sent by another app with app.broadcast. |
| `deaths.changed` | plugin | `count` | The death count changed. (Death count plugin) |
| `effect.ended` | filter | `source`, `sourceId`, `effect`, `reason` | A timed effect (Hide it, Barrel Roll, Freeze) ended. |
| `filter.changed` | filter | `source`, `id`, `name`, `kind`, `on`, `settings`, `added` | A filter was added, switched or changed. |
| `filter.removed` | filter | `source`, `id`, `name` | A filter was removed. |
| `garden.changed` | garden | `garden` | Another garden is in use. |
| `gardens.changed` | garden | `gardens` | Gardens were added, removed or renamed. |
| `item.changed` | item | `bloom`, `sprig`, `shape`, `item`, `itemId`, `visible`, `blooms` | Something in a bloom or sprig was shown or hidden. |
| `item.moved` | item | `bloom`, `sprig`, `shape`, `item`, `itemId`, `x`, `y`, `scale_x`, `scale_y`, `rotation`, `align`, `crop`, `bounds` | Something was moved, sized, cropped or turned, by hand or by an app. |
| `items.changed` | item | `bloom`, `sprig`, `shape`, `items` | Something was added to or taken out of a bloom or sprig. |
| `media.changed` | media | `media`, `id`, `file`, `loop`, `state`, `position`, `duration` | A video or sound file played, paused, stopped or ended. |
| `meters` | meters | `levels` | Sound levels, about 15 a second; only when asked for by name. |
| `nowplaying.changed` | plugin | `playing`, `title`, `artist`, `album`, `app`, `length`, `position` | The song changed, paused or jumped while the card is on screen. (Now playing plugin) |
| `path.changed` | path | `path` | Another path is in use. |
| `paths.changed` | path | `paths` | Paths were added, removed or renamed. |
| `place.changed` | output | `place`, `name`, `service`, `shape`, `state`, `was`, `reconnects`, `dropped` | One place started, dropped, is trying again, came back or stopped. |
| `plugins.changed` | plugin | `plugins`, `added`, `removed` | A plugin was added or removed. |
| `pngtuber.expression` | plugin | `expression` | The expression changed. (PNGtuber plugin) |
| `record.changed` | output | `on` | Recording started or stopped. |
| `record.paused` | output | `paused` | The recording was paused or carried on. |
| `replay.ended` | plugin | `source`, `sourceId`, `reason` | A replay ended. (Instant replay plugin) |
| `replay.started` | plugin | `source`, `sourceId`, `seconds`, `speed` | A replay started playing. (Instant replay plugin) |
| `snap.taken` | plugin | none | A screenshot became a polaroid on stream. (Snap plugin) |
| `sound.changed` | sound | `id`, `name`, `muted`, `volumeDb`, `balance` | A sound's mute, volume or balance changed. |
| `source.changed` | source | `source`, `sourceId`, `settings` | A source's settings changed. |
| `sources.changed` | source | `sources`, `added`, `removed`, `renamed` | Sources were added, removed or renamed. |
| `sprig.state` | sprig | `sprig`, `sprigId`, `state` | A sprig switched Live state. |
| `stream.changed` | output | `on` | The stream started or ended. |
| `stream.limit` | plugin | none | The stream limit is close. (Break & End Guard plugin) |
| `timer.changed` | plugin | `source`, `mode`, `seconds`, `running`, `done`, `text` | A stream timer changed. (Stream timer plugin) |
| `timer.done` | plugin | `source` | A countdown reached 0 on screen. (Stream timer plugin) |
| `transition.changed` | transition | `kind`, `ms` | The usual transition changed. |

## Error codes

| Code | About |
| --- | --- |
| `bad_request` | A field is missing or has the wrong type or value; field and reason say which and why. |
| `not_found` | No such bloom, sprig, item, sound, source, filter or place; what and name say what was asked for. |
| `not_allowed` | It can't be done here, e.g. putting a sprig on air. |
| `busy` | Already doing it (already live), or the name is taken (reason already_exists). |
| `unknown_action` | No such action. |
| `doctor_warnings` | Going live or recording would have asked first: nothing started; warnings lists why, force: true goes ahead. |
| `places_differ` | stream.start was given places and the places switched on aren't exactly those: nothing started; places lists the ones switched on. |
| `plugin_missing` | The action comes with a store plugin that isn't added; the message names it. |
| `failed` | Something went wrong inside SprigCast. |

## Error reasons

| Reason | About |
| --- | --- |
| `missing` | A field that's needed wasn't sent. |
| `empty` | A field was sent empty. |
| `wrong_type` | A field has the wrong type (text where a number goes...). |
| `out_of_range` | A number is outside what the field takes. |
| `not_one_of` | A word the field doesn't take. |
| `already_exists` | Something already has that name. |
| `already_running` | It's already going (live, recording). |

## Close codes

| Code | About |
| --- | --- |
| `1001` | SprigCast closed, or its API was turned off (app.closing came first). |
| `4002` | A message wasn't a JSON object. |
| `4009` | Wrong password or key. |
| `4011` | Wrong or expired pairing code, or too many wrong codes (pairing locks for a minute). |
