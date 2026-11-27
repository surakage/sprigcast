# SprigCast API conventions

How the SprigCast API names things, how it changes over time, and what a program can count on. For how to
connect, see [the control API](../control-api.md). Every action and event is in [the reference](reference.md),
and programs can read the same thing as [protocol.json](protocol.json).

## The description for programs

`protocol.json` describes the whole API: every action with its fields (type, needed or not, default, range, the
words it takes, what it names), every event with its group and fields, every error code, error reason and close
code. A program can build its menus, check its calls or make a client from it.

- On the WebSocket: `{"do": "app.protocol"}`. This one includes the actions of plugins that are added.
- As a web link: `GET http://127.0.0.1:4455/api/protocol.json`. No password is needed, even with web links off,
  because it holds nothing of yours: only SprigCast's own actions, events and codes.
- In these docs: [protocol.json](protocol.json), written from the code, so it can't fall behind.

| Action | What it does | Fields |
| --- | --- | --- |
| `app.protocol` | The whole API described for programs: every action with typed fields, every event, error code, error reason and close code. | none |
| `sound.toggle` | Mute it if it's on, unmute it if it's muted. | `sound`: name or id |
| `media.toggleLoop` | Loop it if it doesn't, stop looping if it does. | `media`: name or id |
| `item.toggleLock` | Lock it if unlocked, unlock it if locked. | `item`: name or id, `bloom` (optional), `sprig` (optional), `shape` (optional) |
| `camera.toggle` | Turn SprigCast Camera on if it's off, off if it's on. | none |

## Names

The API is settling on one style (chosen 2026-11-27). New actions, fields and events follow it now. Older
names are kept working; later versions add the new spellings next to them.

- **Fields are camelCase**: `bloomId`, `volumeDb`, `bitrateKbps`. Units go in the name when a number has one
  (`Db`, `Kbps`, `Ms`). Every older snake_case field also answers to its camelCase spelling: `scaleX` is
  `scale_x`, `boundsWidth` is `bounds_width`, `bandwidthTest` is `bandwidth_test`, and a step list takes
  `oneStep` and `dryRun`. The snake_case spellings keep working too. Answers still use today's spellings.
- **Actions are `noun.verb`**: `bloom.show`, `sound.mute`. One thing takes the singular noun; only lists take
  the plural (`blooms.list`). The verbs are a small set: get, set, list, new, remove, show, hide, toggle, start,
  stop, play, pause, save, take, run.
- **Events are `noun.pastTense`**: `bloom.shown`, `item.moved`, `clip.saved`.
- **Ids and names**: everything has an `id` that never changes and a `name` that can. Fields like `bloom` take
  either. `bloomId`, `sprigId`, `sourceId`, `itemId`, `soundId`, `mediaId`, `filterId` and `placeId` take only
  an id, so a thing whose name happens to look like another's id is never picked by mistake:
  `{"do": "item.hide", "bloomId": "chat-h", "itemId": "capture-3"}`. Giving `bloom` and `bloomId` naming
  different things is a `bad_request` with reason `conflict`. Save the id; show the name. (Store plugins'
  actions take the `*Id` spellings too, and look them up as they do a name or id.)
- **Toggling**: new actions always need the value they set; toggling is its own `.toggle` action
  (`sound.toggle`, `media.toggleLoop`, `item.toggleLock`, `camera.toggle`, `item.toggle`, `filter.toggle`...).
  Leaving the value out of `sound.mute`, `media.loop`, `item.lock` or `camera.set` still toggles, as before, but a
  retried message then flips it back, so prefer the `.toggle` action or send the value.
- **Older action names** that don't follow the rule have a newer one next to them: `record.togglePause` is
  `record.toggle_pause`, `filter.share` is `filters.share`. Both always work.

## Versions

- `api` (in `hello`, `welcome` and `app.info`) goes up only when something an app relies on changes. It is 0
  while the API is a preview and becomes 1 when it settles.
- New actions, fields, events and error codes are added within a version. `app.info` `features` names each one,
  and names are only ever added, so check a feature rather than a version number.
- Error codes are never renamed. Old action, field and event names keep working.

## API 1

An app chooses its API version with `"api"` in its first message (the one with `auth`, `pair` or `events`).
`hello` lists the versions SprigCast speaks in `apis`. Leaving `api` out means 0: everything stays exactly as it
was. Asking for one SprigCast doesn't speak closes the connection with 4010.

```
server → {"hello": {"app": "SprigCast", "api": 0, "apis": [0, 1], "auth": {...}}}
app    → {"api": 1, "auth": "<answer>", "events": ["bloom", "output"]}
server → {"welcome": {"api": 1, "events": ["bloom", "output"]}}
```

With API 1:

- **Fields go under `data`**, so they never clash with the message's own keys:
  `{"id": "7", "do": "bloom.show", "data": {"bloom": "BRB"}}`. Steps in a step list are the same:
  `{"id": "a", "do": "item.hide", "data": {...}}`. The answer is still `{"id", "ok", "result"}` or
  `{"id", "ok": false, "error"}`.
- **Events** come as `{"event": "bloom.shown", "group": "bloom", "data": {...}, "at": "2026-11-27T02:30:00.120Z"}`.
  `app.closing` has `group` null, since every app gets it.
- **Answers and events are camelCase**: `scaleX`, `onAir`, `sourceSize`, `sameOnBothCanvases`, `dryRun`... A
  source's own `settings` (and `data` you broadcast) keep the names they have, because `source.set` takes them as
  they are. protocol.json lists every rename under `api1`.
- **Set actions need their value**: `sound.mute` needs `muted`, `media.loop` and `camera.set` need `on`,
  `item.lock` needs `locked`. Without it the answer is `bad_request` naming the field; use `sound.toggle`,
  `media.toggleLoop`, `camera.toggle` or `item.toggleLock` to toggle.

Web links are one request each, so they stay as they are (the Streamer.bot pack uses them, so it needs no
change). The Stream Deck plugin speaks API 1 when `hello` offers it and API 0 otherwise: do the same, so an app
works with an older SprigCast too. API 1 is a preview until SprigCast Version 1, when it
becomes the settled API.

## Events by name

`events` (in the first message, or sent again at any time to change what you get) takes event groups
(`"bloom"`), high-volume groups asked for by name (`"meters"`), and single events by name
(`"place.changed"`). The `welcome` lists what you'll get. Anything it didn't recognize is listed in `unknown`,
and nothing is sent for it:

```
app    → {"events": ["bloom", "place.changed", "scenes"]}
server → {"welcome": {"api": 0, "events": ["bloom", "place.changed"], "unknown": ["scenes"]}}
```

## Errors

Every failed answer has `error.code` (see [the reference](reference.md#error-codes)) and `error.message` (for
people). A `bad_request` that's about one field also says which one and why, and so does a `busy` for a name
that's taken:

```json
{"id": "7", "ok": false, "error": {"code": "bad_request", "message": "ms must be 50 to 20000.",
 "field": "ms", "reason": "out_of_range"}}
{"id": "8", "ok": false, "error": {"code": "busy", "message": "There's already a bloom called 'BRB'.",
 "field": "name", "reason": "already_exists"}}
```

`reason` is one of `missing`, `empty`, `wrong_type`, `out_of_range`, `not_one_of`, `already_exists`,
`already_running` and `conflict`. More may be added; act on the `code` when the reason is one you don't know.

## Step ids

A step in a step list can have its own `id`. Its answer comes back with that id, so you don't have to count:

```
app    → {"id": "9", "steps": [{"id": "hide", "do": "item.hide", "bloom": "Main", "item": "Alert"},
                               {"id": "pause", "wait": 5000},
                               {"id": "show", "do": "item.show", "bloom": "Main", "item": "Alert"}]}
server → {"id": "9", "ok": true, "result": {"steps": [{"id": "hide", "ok": true, …},
                               {"id": "pause", "ok": true, "waited": 5000}, {"id": "show", "ok": true, …}]}}
```

## Close codes

| Code | Why |
| --- | --- |
| 1001 | SprigCast closed, or its API was turned off (`app.closing` came first). Try again in a second or two. |
| 4002 | A message wasn't a JSON object. |
| 4009 | Wrong password or key. |
| 4010 | The app asked for an API version SprigCast doesn't speak (`hello`'s `apis` lists them). |
| 4011 | Wrong or expired pairing code, or too many wrong codes (pairing locks for a minute). |

## Features

| Feature | What it means |
| --- | --- |
| `app.protocol` | `app.protocol` and `/api/protocol.json`: every action's typed fields, every event, error code, error reason and close code |
| `events.by_name` | `events` takes single events by name; `welcome` lists `unknown` ones |
| `errors.field` | `bad_request` errors (and `busy` for a taken name) carry `field` and `reason` |
| `steps.ids` | A step's own `id` comes back with its answer |
| `fields.camelCase` | Every snake_case field also answers to its camelCase spelling (`scaleX`, `dryRun`) |
| `fields.ids` | `bloomId`, `sprigId`, `sourceId`, `itemId`, `soundId`, `mediaId`, `filterId`, `placeId` take only an id |
| `actions.toggles` | `sound.toggle`, `media.toggleLoop`, `item.toggleLock`, `camera.toggle` |
| `api.v1` | `"api": 1` in the first message: fields under `data`, camelCase answers and events, events with `group` and `at`, set actions need their value |
| `actions.aliases` | `record.togglePause` and `filter.share`, newer names for `record.toggle_pause` and `filters.share` |
