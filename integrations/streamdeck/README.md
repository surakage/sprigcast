# SprigCast for Stream Deck

Run SprigCast from a Stream Deck. The keys light up to match SprigCast: a bloom key lights while that
bloom is on air, a sound key shows when it's muted, and Go live and Record turn pink while they're on.

| Key | Does | Lights | OBS plugin's |
| --- | --- | --- | --- |
| Show bloom | puts a bloom on air (both shapes, or one) | while it's on air | Scene |
| Show or hide | toggles, shows or hides an item in a bloom | while hidden | Source Visibility |
| Garden | switches garden | while in use | Scene Collection |
| Path | switches path | while in use | Profile |
| Mute sound | toggles, mutes or unmutes a sound | while muted | Audio Mixer |
| Sound volume | changes or sets a sound's volume; on a Stream Deck + dial, turn for volume and press to mute | dial shows the level | Audio Mixer |
| Go live | starts or stops the stream | pink while live | Stream |
| Record | starts or stops recording | pink while recording | Record |
| Clips | keeps clips ready, or stops | while ready | Replay Buffer |
| Save clip | saves a clip | ✓ on success | Replay Buffer Save |
| Virtual camera | SprigCast Camera on or off (set it up once in Settings) | while on | Virtual Camera |
| Screenshot | takes a screenshot (Snap prints it on stream) | ✓ on success | Screenshot |
| Media | play/pause, play, pause, restart or stop a video or sound file | while playing | Media Source Control |
| Filter | turns a filter on, off, or toggles it | while on | Filter |
| Transition | picks the transition and/or its length | while it's in use | Transition |
| Sprig state | switches a sprig to one of its Live states | ✓ on success | |
| Death count | add, remove, reset or show (Death count plugin) | the count as its title | |
| PNGtuber | shows an expression (for some seconds, or until changed), the next one, or default (PNGtuber plugin) | ✓ and the expression's name | |
| Run action | any SprigCast action with JSON fields | ✓ on success | |

Not here on purpose: Studio Mode (SprigCast has none: a bloom goes straight on air), Record Pause and Chapter
Marker (SprigCast recordings don't pause or take chapters yet).

## Light on the PC

The plugin runs inside the Stream Deck app. It opens one connection to SprigCast only while a SprigCast key is
on screen, and closes it when none is. SprigCast sends changes as they happen, so nothing polls. When SprigCast
isn't running, the plugin only checks every 2 seconds whether SprigCast's api.json is there (no connection tries),
and its keys go dark until SprigCast is back. A quiet connection is checked every 15 seconds, so a PC waking from sleep finds SprigCast again.

## Set up

1. Double-click `com.sprigcast.sprigcast.streamDeckPlugin` to install it (Stream Deck 7.1 or newer).
2. Drag any SprigCast key onto your Stream Deck. That's it: with SprigCast open, the keys connect by themselves.

While its API runs (on by default: Settings → Connections), SprigCast keeps its port and a key for this PC (not the password) in
`%LOCALAPPDATA%\Slothbloom\SprigCast\api.json`, and the plugin reads them there, the way Elgato's OBS plugin
reads obs-websocket's settings. If OBS holds port 4455, SprigCast takes the next free port and the file says so.
The plugin looks for the file every 2 seconds, so keys come alive as soon as SprigCast opens.

**Key style** in any key's settings switches every key's look: Canopy (dark green with a mint glow), Colour (each kind of key
its own colour) or Graphite (dark and plain, mint when on).

Not connecting by itself? In SprigCast press **Settings → Connections → Pair a device** and type the 6-digit code
under **Pair code** in any key's settings: the plugin trades it for its own key and keeps it (every key shares it).
A port and password typed under **Connection** still work and win over the file. The line there says whether
SprigCast is found.

## Build

```
cd integrations/streamdeck
npm install
npm test            # client tests
npm run build       # bundles src/ into com.sprigcast.sprigcast.sdPlugin/bin/plugin.js
npm run validate
npm run pack        # dist/com.sprigcast.sprigcast.streamDeckPlugin
```

For development, `npx streamdeck link com.sprigcast.sprigcast.sdPlugin` installs the folder itself, and
`npx streamdeck restart com.sprigcast.sprigcast` reloads it after a build.

Images come from `tools/make_images.py` (run it with SprigCast's Python from the repo root); they're SVG glyphs
drawn to PNG, in the brand colours.

`test/live_check.mjs <port> <password>` connects to a running SprigCast and shows a bloom, as a quick check.

Publishing to the Elgato Marketplace: see [SUBMISSION.md](SUBMISSION.md).
