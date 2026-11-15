# Elgato Marketplace submission (prepared, not submitted)

Nothing has been sent to Elgato. These are the steps and the listing, ready for when the owner decides to publish.

## Before submitting

- [ ] Try every key on a real Stream Deck with SprigCast running (see the checklist below).
- [ ] Decide the plugin UUID for good: `com.sprigcast.sprigcast`. It can't change after publishing.
- [ ] Set `Author` in the manifest to the name the listing should show, and bump `Version` (four numbers, e.g. `0.1.0.0`).
- [ ] Sign in to the Elgato Maker Console (https://maker.elgato.com) with the account that will own the listing.
- [ ] Have a support link: the GitHub repository's issues page works.
- [ ] Confirm the licence for the plugin's files (the repository's LICENSE covers them).

## Hand test on a Stream Deck

- [ ] Install `com.sprigcast.sprigcast.streamDeckPlugin`, enter the port and password once.
- [ ] Show bloom: switches, and the key lights for the bloom on air; switching in SprigCast moves the light.
- [ ] Mute sound: toggles; muting in SprigCast changes the key.
- [ ] Show or hide, Garden, Path, Go live, Record, Clips, Save clip, Virtual camera, Screenshot, Media, Filter,
      Transition, Sprig state, Death count, Run action.
- [ ] Sound volume on a key, and on a Stream Deck + dial (turn, press to mute, the level shows on the screen).
- [ ] Close SprigCast: keys show an alert when pressed; open it again: they work again within about 30 s.
- [ ] Wrong password: keys show an alert, and the Stream Deck log says "Wrong password".
- [ ] Remove every SprigCast key from the profile: SprigCast's Connections page shows no app connected.

## Listing

**Name:** SprigCast

**Short description:** Control SprigCast, the light streaming app, from your Stream Deck.

**Description:**
Switch blooms and gardens, mute and turn sounds (with dials on Stream Deck +), go live, record, keep clips, save
clips, turn the virtual camera on, play media, flip filters, pick transitions and take screenshots in SprigCast,
right from your Stream Deck. Keys light up to match SprigCast: the bloom on air, muted sounds, and pink while you're live or recording.
SprigCast plugins work too: add to the Death count, change a sprig's Live state, or run any SprigCast action.
The plugin connects only while a SprigCast key is on screen, so it adds nothing to your PC while idle.
Requires SprigCast with the API turned on (Settings → Connections).

**Category:** Streaming / Productivity

**Platforms:** Windows 10 and newer (SprigCast is Windows-only), Stream Deck 7.1 or newer.

**Support / website:** https://github.com/surakage/sprigcast

## Assets

| Asset | Where | Notes |
| --- | --- | --- |
| Plugin icon 256×256 and 512×512 | `com.sprigcast.sprigcast.sdPlugin/imgs/plugin/marketplace.png`, `marketplace@2x.png` | the app icon |
| Store icon 288×288 | `com.sprigcast.sprigcast.sdPlugin/imgs/plugin/store-icon-288.png` | upload in the Maker Console |
| Category icon 28×28 and 56×56 | `imgs/plugin/category-icon*.png` | the white sloth |
| Action icons 20×20 and 40×40, key images 72×72 and 144×144 | `imgs/actions/*` | made by `tools/make_images.py` |
| Gallery pictures (1920×960 suggested) | to make | photos or renders of a Stream Deck with SprigCast keys, plus a SprigCast screenshot |
| Thumbnail | to make | from the brand banner |

## Submitting

1. `npm run build && npm run validate && npm run pack` → `dist/com.sprigcast.sprigcast.streamDeckPlugin`.
2. Maker Console → new product → Stream Deck plugin → upload the file, fill in the listing above, add the assets.
3. Submit for review. Elgato reviews and may ask for changes before it goes live.
