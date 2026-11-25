# Building on SprigCast

For developers of stream tools (Streamer.bot, TikFinity, Stream Deck plugins, bots, overlays) who want to support SprigCast natively. Everything here is in more detail in [the control API](control-api.md).

> The API is a **preview** (`"api": 0` in `hello`). Names may still change before `"api": 1`. Pin to the version in `hello` and read `app.actions` rather than hard-coding a list.

## What SprigCast is, in OBS words

| SprigCast | OBS | Notes |
| --- | --- | --- |
| bloom | scene | what goes on air. A bloom can have a horizontal and a vertical shape (two canvases at once); switching one switches both unless `shape` says which |
| sprig | nested scene / group | a reusable part placed in blooms, with optional named states (`sprig.state`) |
| item | scene item | a thing inside a bloom or sprig, named by its source's name |
| source | input / source | camera, capture, text, image, media, browser page, sound |
| sound | audio input | anything in the mixer |
| garden | scene collection | |
| path | profile | |
| place | stream service / output | somewhere it streams to; several at once |

There is no Studio mode: a bloom shown goes straight on air.

## Connecting

1. The user turns the API on in **Settings → Connections** (off by default). The port is 4455, or another when OBS is running (users often pick 4456). Look for SprigCast on 4455 first, then 4456.
2. Connect a WebSocket to `ws://127.0.0.1:<port>/sprigcast`.
3. The server says `hello` with `auth: {salt, challenge}` (or `null` without a password). Answer with `{"auth": base64(sha256(base64(sha256(password + salt)) + challenge)), "events": [...]}`: the same scheme as obs-websocket 5.
4. `welcome` comes back. Send `{"id": "1", "do": "app.actions"}` to learn every action and its fields.

Only one connection is needed for everything. SprigCast uses no CPU for an idle connection: events are sent when something changes, nothing is polled.

## Asking

```json
{"id": "7", "do": "bloom.show", "bloom": "BRB"}
{"id": "7", "ok": true, "result": {...}}
{"id": "7", "ok": false, "error": {"code": "not_found", "message": "No bloom called 'BRB'."}}
```

Names or ids are accepted wherever a thing is named. Error codes are stable: `bad_request`, `not_found`, `not_allowed`, `busy`, `unknown_action`, `failed`.

For pickers in your UI: `blooms.list`, `sprigs.list`, `items.list`, `sources.list`, `sounds.list`, `filters.list`, `media.list`, `hotkey.list`, `gardens.list`, `paths.list`, `plugins.list`.

`{"id": "8", "steps": [{"do": ...}, {"wait": 2000}, {"do": ...}]}` runs a list of actions in order with waits, and answers once.

## Events

Ask for groups in the auth message (`"events": ["bloom", "sound"]`, `"all"` or `"none"`): `bloom`, `item`, `sprig`, `sound`, `filter`, `source`, `output`, `transition`, `media`, `custom`, `plugin`, `garden`, `path`, and `meters` (about 15 a second, only when named). SprigCast doesn't pass on platform events (TikTok gifts, Twitch follows...): tools like TikFinity and Streamer.bot already have them. Each event is one JSON object: `{"event": "bloom.shown", "bloom": "BRB", ...}`.

## Plugins

Store plugins (Death count, Hide it, Barrel Roll, Snap...) add their own actions (`deaths.add`, `hide.pixelate`, `roll.spin`) and events (`deaths.changed`). They show up in `app.actions` and `plugins.list` while added, so a native integration picks them up without changes.

## Without a WebSocket

- **Web links**: `POST http://127.0.0.1:<port>/api/<action>` with `Authorization: Bearer <password>` and the fields as a JSON body (or as query fields with `password=`). Needs "Allow web links" on. See [Web links](control-api.md#web-links).
- **The OBS door**: `ws://127.0.0.1:<port>` (no path) speaks obs-websocket 5 for apps that already support OBS. See [The OBS-compatible door](control-api.md#the-obs-compatible-door) for the requests it handles. SprigCast's extras there are vendor requests (`CallVendorRequest`, vendorName `sprigcast`) and vendor events.

## Suggested native support

A good first version of native support in a tool like Streamer.bot:

- Connection settings (host, port, password) with auto-detect of 4455/4456, and reconnect when SprigCast restarts.
- Sub-actions: show bloom, show/hide/toggle item, set sprig state, mute/volume sound, set text, filter on/off, media play/stop, screenshot, save clip, stream/record start/stop/toggle, switch garden/path, and a generic "run action" with JSON fields, filled from `app.actions`.
- Triggers from events: bloom shown, stream/record started and stopped, clip saved, and plugin events.

Questions or changes that would help your integration: ask in #feature-ideas or #help on [the SprigCast Discord](https://discord.gg/fgMD33XrhM).

## Going live on TikTok LIVE Studio at the same time

SprigCast feeds LIVE Studio through SprigCast Camera, and LIVE Studio does its own encoding and TikTok upload.

- In LIVE Studio, add a camera source and pick SprigCast Camera. With the camera on "Auto", a portrait request gets the vertical canvas.
- Sound needs no extra device, the same as with OBS (whose virtual camera carries no sound either). LIVE Studio picks up the PC's sound itself: its own desktop sound and mic, or a mix such as Wave Link's Stream mix. Leave Settings → Video → Virtual camera → Sound to off (the default). It's only for a setup that wants SprigCast's own mix sent to a virtual cable.
- It stays light: SprigCast only feeds the camera while an app is watching. When the vertical canvas is already being streamed or recorded at the size LIVE Studio asks for, the camera reuses the same frame instead of making another one.
- TikTok gifts, likes and follows go through TikFinity or Streamer.bot. They can call SprigCast's API (see [streamerbot.md](streamerbot.md)). SprigCast doesn't read or pass on TikTok events itself.
