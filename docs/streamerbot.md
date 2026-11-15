# SprigCast and Streamer.bot

SprigCast is built to work hand in hand with [Streamer.bot](https://streamer.bot): a redeem, raid, alert or Stream Deck button in Streamer.bot can switch blooms, mute sounds, change text, spin a sprig or snap a polaroid in SprigCast.

> The SprigCast API is a **preview**: names may still change before it settles. Everything here is in [the control API](control-api.md).

## Set up (once)

1. In SprigCast, open **Settings → Connections**, turn on the API, set a password and turn on **Allow web links**.
2. Note the port shown there (4455 unless OBS already uses it; then pick another, e.g. 4456).

## Run a SprigCast action from Streamer.bot

Add a **Fetch URL** sub-action (Core → Network) to any Streamer.bot action, with an address like:

```
http://127.0.0.1:4455/api/bloom.show?bloom=BRB&password=YOUR_PASSWORD
```

The part after `/api/` is the action; everything after `?` is its fields. Spaces and symbols in a field must be URL-encoded, so for text a viewer typed use Streamer.bot's URL-encoded variables (for example `%rawInputUrlEncoded%`).

### Recipes

| When | Fetch URL |
| --- | --- |
| Raid: snap a polaroid with the raider's name | `http://127.0.0.1:4455/api/snap.take?text=Raid%20from%20%user%!&password=…` |
| Redeem with a message: snap it onto the photo | `http://127.0.0.1:4455/api/snap.take?text=%rawInputUrlEncoded%&password=…` |
| Redeem: pixelate the camera for 15 s | `http://127.0.0.1:4455/api/hide.pixelate?source=Facecam&seconds=15&password=…` |
| Redeem: black out the game for 10 s | `http://127.0.0.1:4455/api/hide.blackout?source=Game%20capture&seconds=10&password=…` |
| Redeem: barrel roll the camera frame | `http://127.0.0.1:4455/api/roll.spin?sprig=Cam%20frame&seconds=15&password=…` |
| Change some text on screen | `http://127.0.0.1:4455/api/source.set?source=Goal%20text&settings=…&password=…` (or POST JSON, below) |
| Switch bloom | `http://127.0.0.1:4455/api/bloom.show?bloom=Just%20Chatting&password=…` |
| Mute the mic | `http://127.0.0.1:4455/api/sound.mute?sound=Mic&password=…` |

Store plugins (Hide it, Barrel Roll, Snap) must be added first in Settings → Plugins.

### Sending JSON instead

For fields that are objects (like `settings`), send a POST with a JSON body from an Execute C# Code sub-action, or any tool that can POST:

```
POST http://127.0.0.1:4455/api/source.set
Authorization: Bearer YOUR_PASSWORD
{"source": "Goal text", "settings": {"text": "Sub goal: 42 / 50"}}
```

The answer is JSON: `{"ok": true, "result": {…}}`, or `{"ok": false, "error": {"code": "…", "message": "…"}}`.

## Hearing back from SprigCast

To react in Streamer.bot when something happens in SprigCast (a bloom shown, a snap taken, a clip saved), connect a WebSocket to `ws://127.0.0.1:4455/sprigcast` and ask for events (see [Events](control-api.md#events)). Events from plugins, like `snap.taken` with the saved photo's `file`, are in the `plugin` group.

## For Streamer.bot developers

Everything SprigCast can be asked to do is discoverable, so an integration doesn't need to hard-code a list:

- `hello` → `auth` (same password scheme as obs-websocket 5) → `welcome`. See [Connecting](control-api.md#connecting).
- `app.actions` lists every action with a one-line description and its fields (`?` = optional). Plugin actions appear here while the plugin is added.
- `plugins.list` groups those actions by plugin.
- `blooms.list`, `sprigs.list`, `sources.list`, `sounds.list` and `hotkey.list` give the names to show in pickers.
- `{"steps": [...]}` runs several actions in order, with waits between them.
- Errors have a stable `code` (`bad_request`, `not_found`, `not_allowed`, `busy`, `unknown_action`, `failed`).

The OBS-compatible door (`ws://127.0.0.1:4455`, obs-websocket 5) also works with Streamer.bot's existing OBS sub-actions, but new features only come to the SprigCast API.
