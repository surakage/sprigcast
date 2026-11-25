# Elgato Marketplace submission (prepared, not submitted)

Nothing has been sent to Elgato. These are the steps and the listing, ready for when the owner decides to publish.

## Before submitting

- [ ] Try every key on a real Stream Deck with SprigCast running (see the checklist below).
- [ ] Decide the plugin UUID for good: `com.sprigcast.sprigcast`. It can't change after publishing.
- [ ] Set `Author` in the manifest to the name the listing should show, and bump `Version` (four numbers; now `0.3.0.0`).
- [ ] Sign in to the Elgato Maker Console (https://maker.elgato.com) with the account that will own the listing.
- [ ] Have a support link: the GitHub repository's issues page works.
- [ ] Confirm the licence for the plugin's files (the repository's LICENSE covers them).

## Hand test on a Stream Deck

- [ ] Install `com.sprigcast.sprigcast.streamDeckPlugin` and drag a key on: with SprigCast open it connects by itself (no port or password).
- [ ] Show bloom: switches, and the key lights for the bloom on air; switching in SprigCast moves the light.
- [ ] Mute sound: toggles; muting in SprigCast changes the key.
- [ ] Show or hide, Garden, Path, Go live, Record, Clips, Save clip, Virtual camera, Screenshot, Media, Filter,
      Transition, Sprig state, Death count, PNGtuber, Run action.
- [ ] Place: while live with two places, the key stops and starts one of them and the other carries on; pull the
      network for a moment and the key says "Retrying", then goes back to pink.
- [ ] Save clip with clips off shows "Clips starting"; a clip that couldn't be saved shows an alert, not a tick.
- [ ] Sound volume on a key, and on a Stream Deck + dial (turn, press to mute, the level shows on the screen).
- [ ] Close SprigCast: keys go dark and show "Not connected" when pressed; open it again: they work again within a few seconds.
- [ ] A wrong password typed under Connection: the Connection line says "Wrong password"; clear it and it connects by itself.
- [ ] Remove every SprigCast key from the profile: SprigCast's Connections page shows no app connected.

## Listing

**Name:** SprigCast

**Short description:** Control SprigCast, the light streaming app, from your Stream Deck.

**Description:**
Switch blooms and gardens, mute and turn sounds (with dials on Stream Deck +), go live, record, keep clips, save
clips, turn the virtual camera on, play media, flip filters, pick transitions and take screenshots in SprigCast,
right from your Stream Deck. Keys light up to match SprigCast: the bloom on air, muted sounds, and pink while you're live or recording.
SprigCast plugins work too: add to the Death count, change your PNGtuber's expression, change a sprig's Live state, or run any SprigCast action.
The plugin connects only while a SprigCast key is on screen, so it adds nothing to your PC while idle.
No setup: with SprigCast open on the same PC, the keys connect by themselves.

**Category:** Streaming / Productivity

**Platforms:** Windows 10 and newer (SprigCast is Windows-only), Stream Deck 7.1 or newer.

**Support / website:** https://sprigcast.slothbloom.com

## Assets

| Asset | Where | Notes |
| --- | --- | --- |
| Plugin icon 256×256 and 512×512 | `com.sprigcast.sprigcast.sdPlugin/imgs/plugin/marketplace.png`, `marketplace@2x.png` | the app icon |
| Store icon 288×288 | `com.sprigcast.sprigcast.sdPlugin/imgs/plugin/store-icon-288.png` | upload in the Maker Console |
| Category icon 28×28 and 56×56 | `imgs/plugin/category-icon*.png` | the white SprigCast S |
| Action icons 20×20 and 40×40, key images 72×72 and 144×144 | `imgs/actions/*` | made by `tools/make_images.py`: one picture per key, thin white lines on transparent in the list; keys come in three styles (Canopy in the manifest, Colour and Graphite under `imgs/styles/`), picked under **Key style** in any key's settings |
| Gallery pictures (1920×960 suggested) | to make | photos or renders of a Stream Deck with SprigCast keys, plus a SprigCast screenshot |
| Thumbnail | to make | from the brand banner |

## Submitting

1. `npm run build && npm run validate && npm run pack` → `dist/com.sprigcast.sprigcast.streamDeckPlugin`.
2. Maker Console → new product → Stream Deck plugin → upload the file, fill in the listing above, add the assets.
3. Submit for review. Elgato reviews and may ask for changes before it goes live.
