# SprigCast for Stream Deck

Run SprigCast from a Stream Deck. The keys light up to match SprigCast: a bloom key lights while that
bloom is on air, a sound key shows when it's muted, and Go live and Record turn pink while they're on.

| Key | Does | Lights |
| --- | --- | --- |
| Show bloom | puts a bloom on air (both shapes, or one) | while it's on air |
| Mute sound | toggles, mutes or unmutes a sound | while muted |
| Show or hide | toggles, shows or hides an item in a bloom | while hidden |
| Go live | starts or stops the stream | pink while live |
| Record | starts or stops recording | pink while recording |
| Save clip | saves a clip (Clips on) | ✓ on success |
| Screenshot | takes a screenshot (Snap prints it on stream) | ✓ on success |
| Sprig state | switches a sprig to one of its Live states | ✓ on success |
| Death count | add, remove, reset or show (Death count plugin) | the count as its title |
| Run action | any SprigCast action with JSON fields | ✓ on success |

## Light on the PC

The plugin runs inside the Stream Deck app. It opens one connection to SprigCast only while a SprigCast key is
on screen, and closes it when none is. SprigCast sends changes as they happen, so nothing polls. When SprigCast
isn't running, the plugin looks again after 2, 5, 10, then every 30 seconds.

## Set up

1. In SprigCast: Settings → Connections → turn on the API and set a password. Note the port (4455, or e.g. 4456
   next to OBS).
2. Double-click `com.sprigcast.sprigcast.streamDeckPlugin` to install it (Stream Deck 7.1 or newer).
3. Drag any SprigCast key onto your Stream Deck. Under **Connection** enter the port and password once (every
   SprigCast key shares them). Names (blooms, sounds...) fill in from SprigCast while it runs.

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
