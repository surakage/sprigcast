# SprigCast and Streamer.bot

SprigCast is built to work hand in hand with [Streamer.bot](https://streamer.bot): a redeem, raid, alert or Stream Deck button in Streamer.bot can switch blooms, mute sounds, change text, spin a sprig or take a screenshot in SprigCast.

> The SprigCast API is a **preview**: names may still change before it settles. Everything here is in [the control API](control-api.md).

## Set up (once)

1. In SprigCast, open **Settings → Connections**, turn on the API and turn on **Allow web links**.
2. Note the port shown there (4455 unless OBS already uses it; then pick another, e.g. 4456).
3. Press **Pair a device**. Open `http://127.0.0.1:4455/api/pair?code=CODE&name=Streamer.bot` in a browser (your port, the 6 digits shown, within 2 minutes). Copy the `key` it answers with: that's Streamer.bot's own password from now on (use it wherever this guide says password). You can also copy the password itself, but a key can be removed on its own in Settings → Connections.

## Two ways in

- **As OBS (no code).** In Streamer.bot, Stream Apps → OBS Studio → Add: host `127.0.0.1`, the port and password from SprigCast's Settings → Connections. Streamer.bot's own OBS sub-actions then work on SprigCast: switch scene (bloom), show or hide a source, set a text (Set GDI Text), turn a filter on or off, play or restart a media source, mute, start and stop the stream, recording and replay buffer, save a replay, take a source screenshot of the bloom on air. This is the quickest start.
- **The SprigCast pack (everything, including plugins).** One Execute C# Code action, below, calls any SprigCast action by name, with ready-made methods for the common ones.

## The SprigCast pack for Streamer.bot

1. In Streamer.bot add an action named **SprigCast**, add the sub-action Core → C# → **Execute C# Code**, paste all of [SprigCast.cs](streamerbot/SprigCast.cs), press **Compile** (it should say "Compiled successfully") and **Save and Compile**.
2. Variables → Global Variables → add two persisted globals: `sprigcastPort` (the port, e.g. `4456` next to OBS) and `sprigcastPassword` (the key from Pair a device, or the password).
3. In any other action, set the arguments a method needs (Core → Arguments → Set Argument), then add Core → C# → **Execute C# Method**: action **SprigCast**, method one of:

| Method | Arguments |
| --- | --- |
| `ShowBloom` | `bloom`, optional `shape` |
| `ShowItem`, `HideItem`, `ToggleItem` | `bloom`, `item` |
| `SprigState` | `sprig`, `state` |
| `MuteSound`, `UnmuteSound`, `ToggleSound` | `sound` |
| `SetText` | `source`, `text` (e.g. `%rawInput%` from a command) |
| `FilterOn`, `FilterOff`, `ToggleFilter` | `source`, `filter` |
| `PlayMedia` (from the start), `StopMedia` | `media` |
| `Screenshot`, `SaveClip`, `ToggleStream`, `ToggleRecord` | none |
| `SwitchGarden` / `SwitchPath` | `garden` / `path` |
| `DeathAdd`, `DeathRemove`, `DeathReset`, `DeathShow` | none (Death count plugin) |
| `Pixelate`, `Blackout` | `source`, optional `seconds` (Hide it plugin) |
| `BarrelRoll` | `sprig`, optional `seconds` (Barrel Roll plugin) |
| `Execute` | `sprigcastAction` (any action, e.g. `item.place`) and `sprigcastJson` (its fields as JSON) |

Every method leaves SprigCast's answer in `%sprigcastResult%` and `%sprigcastOk%`, so the next sub-action can post it in chat (Death count answers `{"ok": true, "result": {"count": 7}}`).

## TikTok gifts and likes

Let TikFinity (or Streamer.bot's own TikTok support) trigger Streamer.bot for gifts, likes and follows, and have that Streamer.bot action call SprigCast, for example `DeathAdd` or `ShowItem`. SprigCast itself doesn't pass TikTok events on.

## Run a SprigCast action from Streamer.bot with a web link

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
| Stream Deck or redeem: start or pause the stream timer | `http://127.0.0.1:4455/api/timer.toggle?password=…` |
| Redeem: one more minute on the countdown | `http://127.0.0.1:4455/api/timer.add?seconds=60&password=…` |
| Start a fresh 10-minute "Starting soon" countdown | `http://127.0.0.1:4455/api/timer.start?minutes=10&password=…` |
| Chat command `!song`: what's playing | `http://127.0.0.1:4455/api/nowplaying.get?password=…` |
| Raid or big sub train: mark it as a suggested clip (Auto clip) | `http://127.0.0.1:4455/api/autoclip.mark?password=…` |
| Change some text on screen | `http://127.0.0.1:4455/api/source.set?source=Goal%20text&settings=…&password=…` (or POST JSON, below) |
| Switch bloom | `http://127.0.0.1:4455/api/bloom.show?bloom=Just%20Chatting&password=…` |
| Mute the mic | `http://127.0.0.1:4455/api/sound.mute?sound=Mic&password=…` |
| Switch garden (a whole set of blooms) | `http://127.0.0.1:4455/api/garden.switch?garden=Gaming&password=…` |
| Switch path before going live (Twitch only, Multistream…) | `http://127.0.0.1:4455/api/path.switch?path=Twitch%20only&password=…` |

Store plugins (Auto clip, Hide it, Barrel Roll, Snap, Death count, Stream timer, Now playing) must be added first in Settings → Plugins.

`!song` answers `{"ok": true, "result": {"playing": true, "title": "…", "artist": "…", …}}`: save `title` and `artist` to variables and post them in chat. To switch blooms when the countdown ends, listen for the `timer.done` event (below).

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

The full guide for building native SprigCast support is [Building on SprigCast](integrating.md). In short:

Everything SprigCast can be asked to do is discoverable, so an integration doesn't need to hard-code a list:

- `hello` → `auth` (same password scheme as obs-websocket 5) → `welcome`. See [Connecting](control-api.md#connecting).
- `app.actions` lists every action with a one-line description and its fields (`?` = optional). Plugin actions appear here while the plugin is added.
- `plugins.list` groups those actions by plugin.
- `blooms.list`, `sprigs.list`, `sources.list`, `sounds.list` and `hotkey.list` give the names to show in pickers.
- `{"steps": [...]}` runs several actions in order, with waits between them.
- Errors have a stable `code` (`bad_request`, `not_found`, `not_allowed`, `busy`, `unknown_action`, `failed`).

The OBS-compatible door (`ws://127.0.0.1:4455`, obs-websocket 5) also works with Streamer.bot's existing OBS sub-actions (scenes, sources, text, filters, media, outputs), but new features only come to the SprigCast API.
