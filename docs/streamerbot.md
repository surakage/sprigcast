# SprigCast and Streamer.bot

SprigCast is built to work hand in hand with [Streamer.bot](https://streamer.bot): a redeem, raid, alert or Stream Deck button in Streamer.bot can switch blooms, mute sounds, change text, spin a sprig or take a screenshot in SprigCast.

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
| Raid: take a screenshot (with Snap added, it prints as a polaroid on stream) | `http://127.0.0.1:4455/api/screenshot.take?password=…` |
| Redeem: pixelate the camera for 15 s | `http://127.0.0.1:4455/api/hide.pixelate?source=Facecam&seconds=15&password=…` |
| Redeem: black out the game for 10 s | `http://127.0.0.1:4455/api/hide.blackout?source=Game%20capture&seconds=10&password=…` |
| Redeem: barrel roll the camera frame | `http://127.0.0.1:4455/api/roll.spin?sprig=Cam%20frame&seconds=15&password=…` |
| Death: one more on the death count (the gravestone pops up) | `http://127.0.0.1:4455/api/deaths.add?password=…` |
| Undo a death counted by mistake | `http://127.0.0.1:4455/api/deaths.remove?password=…` |
| New game: death count back to 0 | `http://127.0.0.1:4455/api/deaths.reset?password=…` |
| Chat command `!deaths`: show the count on stream | `http://127.0.0.1:4455/api/deaths.show?password=…` |
| Change some text on screen | `http://127.0.0.1:4455/api/source.set?source=Goal%20text&settings=…&password=…` (or POST JSON, below) |
| Switch bloom | `http://127.0.0.1:4455/api/bloom.show?bloom=Just%20Chatting&password=…` |
| Mute the mic | `http://127.0.0.1:4455/api/sound.mute?sound=Mic&password=…` |
| Switch garden (a whole set of blooms) | `http://127.0.0.1:4455/api/garden.switch?garden=Gaming&password=…` |
| Switch path before going live (Twitch only, Multistream…) | `http://127.0.0.1:4455/api/path.switch?path=Twitch%20only&password=…` |

Store plugins (Hide it, Barrel Roll, Snap, Death count) must be added first in Settings → Plugins.

### Death count with Streamer.bot

Add **Death count** in Settings → Plugins, then + → Plugins → Death count in a bloom. Point a Stream Deck button or a mod-only `!death` command at `deaths.add`. Every `deaths.*` link answers `{"ok": true, "result": {"count": 7}}`, so a Fetch URL sub-action can save the answer to a variable and post it in chat (for example "That's death number 7"). `deaths.get` reads the count without popping it up. To keep a Streamer.bot global in step, listen for the `deaths.changed` event (below).

To put words a viewer typed on screen, give a Text source a name and set it: `source.set` with `{"text": "…"}` (POST it as JSON, below).

### Sending JSON instead

For fields that are objects (like `settings`), send a POST with a JSON body from an Execute C# Code sub-action, or any tool that can POST:

```
POST http://127.0.0.1:4455/api/source.set
Authorization: Bearer YOUR_PASSWORD
{"source": "Goal text", "settings": {"text": "Sub goal: 42 / 50"}}
```

The answer is JSON: `{"ok": true, "result": {…}}`, or `{"ok": false, "error": {"code": "…", "message": "…"}}`.

## Hearing back from SprigCast

To react in Streamer.bot when something happens in SprigCast (a bloom shown, a clip saved, a plugin's event), connect a WebSocket to `ws://127.0.0.1:4455/sprigcast` and ask for events (see [Events](control-api.md#events)). Events that plugins send are in the `plugin` group.

## For Streamer.bot developers

Everything SprigCast can be asked to do is discoverable, so an integration doesn't need to hard-code a list:

- `hello` → `auth` (same password scheme as obs-websocket 5) → `welcome`. See [Connecting](control-api.md#connecting).
- `app.actions` lists every action with a one-line description and its fields (`?` = optional). Plugin actions appear here while the plugin is added.
- `plugins.list` groups those actions by plugin.
- `blooms.list`, `sprigs.list`, `sources.list`, `sounds.list` and `hotkey.list` give the names to show in pickers.
- `{"steps": [...]}` runs several actions in order, with waits between them.
- Errors have a stable `code` (`bad_request`, `not_found`, `not_allowed`, `busy`, `unknown_action`, `failed`).

The OBS-compatible door (`ws://127.0.0.1:4455`, obs-websocket 5) also works with Streamer.bot's existing OBS sub-actions, but new features only come to the SprigCast API.
