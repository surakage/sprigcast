# Elgato Marketplace submission

Everything for the Marketplace listing. The pictures live in the project files under `streamdeck/marketplace/`
(made from the real key images and the real settings panel). Checked against Elgato's maker guidelines
(docs.elgato.com/guidelines/stream-deck/plugins and /guidelines/products) on 2026-11-25.

## Rules this plugin meets

- Plugin ID `com.slothbloom.sprigcast`: maker name + plugin name. Every key's ID starts with it. It can't change after publishing.
- `Author` is the maker organization name, **SlothBloom**. It must match the name in the Maker Console.
- 28 keys since 0.5.0 (Elgato allows 2 to 30). Keys that fail show the alert; list icons are white lines on transparent.
- Icons: plugin 256/512, category 28/56, key list 20/40, keys 72/144, all PNG. App icon 288×288 for the store.
- Settings save as they change. Nothing is collected, so no privacy policy is needed.
- Free, no paywall. Links go to the website and Discord only (never GitHub).
- Elgato asks makers to say when AI helped. The description's last line does that.

## Listing

**Name:** SprigCast

**Category / tags:** Streaming, Productivity

**Price:** Free

**Description** (987 characters; Elgato wants 250 to 1,500, and the first 250 show in search):

Run SprigCast, the free and light streaming app for Windows, right from your Stream Deck. Switch blooms (scenes) and gardens, show or hide anything, mute sounds and turn them up or down (with dials on Stream Deck +), go live, record, save clips, start the virtual camera, play media, flip filters and pick transitions.

Keys light up to match SprigCast: the bloom on air, muted sounds, and pink while you're live or recording. SprigCast plugins work too: Death count, PNGtuber expressions, Stream timer, Instant replay and Freeze. Pick from three key styles: Canopy, Color and Graphite.

No setup: with SprigCast open on the same PC, keys connect by themselves. To run SprigCast on another PC, pair it with a 6-digit code from Settings > Connections > Pair a device.

Requirements: Windows 10 or newer, Stream Deck 7.1 or newer, and SprigCast (free at sprigcast.slothbloom.com). The plugin only talks to SprigCast and collects no data. Made by SlothBloom with help from AI coding tools.

**Release notes (0.4.0):**
First Marketplace release. 27 keys for SprigCast: blooms, gardens, paths, sounds (dials on Stream Deck +), go live,
places, record, clips, virtual camera, screenshots, media, filters, transitions, and SprigCast plugins. Three key
styles. Keys go dark while SprigCast is closed and come back when it opens.

**Additional links:**

| Label | Link |
| --- | --- |
| Website | https://sprigcast.slothbloom.com |
| Download SprigCast | https://sprigcast.slothbloom.com/download |
| Support (Discord) | https://discord.gg/fgMD33XrhM |
| Changelog | https://sprigcast.slothbloom.com/changelog |
| Source code | https://sprigcast.slothbloom.com/source |

## Pictures (project files, `streamdeck/marketplace/`)

| Slot | File | Size |
| --- | --- | --- |
| Thumbnail | `thumbnail.png` | 1920×960 PNG |
| Gallery 1 | `gallery-1-all-keys.png` | 1920×960 PNG |
| Gallery 2 | `gallery-2-keys-light-up.png` | 1920×960 PNG |
| Gallery 3 | `gallery-3-key-styles.png` | 1920×960 PNG |
| Gallery 4 | `gallery-4-no-setup.png` | 1920×960 PNG |
| App icon | `app-icon-288.png` (same as `imgs/plugin/store-icon-288.png`) | 288×288 PNG |

Elgato needs at least 3 gallery items (up to 10). A phone photo of a real Stream Deck running these keys, cropped
to 1920×960, makes a strong fifth one.

## Before submitting

- [ ] Install the packed plugin on a real Stream Deck. The ID changed, so remove the old SprigCast plugin first
      and drag the keys on again.
- [ ] Quick key test: Show bloom, Mute sound, Go live (to a hidden test place), Record, Save clip, Virtual camera,
      dial volume on Stream Deck +, close SprigCast (keys go dark), open it again (keys come back).

## Submitting (Maker Console, maker.elgato.com)

1. Home → **Create product** → **Stream Deck plugin** → upload `com.slothbloom.sprigcast.streamDeckPlugin`.
2. Details: paste the name, description, tags, price (Free) and the links above.
3. Media and release notes: thumbnail, the 4 gallery pictures, the 288 app icon, and the release notes.
4. Leave "Automatically publish after being approved" ticked, then **Submit**.
5. Elgato reviews in 4 to 10 working days and writes from maker@elgato.com if they want changes.

## Building the file

`npm ci && npm run build && npm test && npm run validate && npm run pack` → `dist/com.slothbloom.sprigcast.streamDeckPlugin`.
(`validate` needs internet to check the website link.)

## Release notes for 0.5.0 (next upload, as a new version in the Maker Console)

New Teleprompter key: runs the THSV Teleprompter app from your Stream Deck (play or pause, start, pause, reset,
next, previous, faster, slower, bigger or smaller text, mirror, repeat section). It works with the teleprompter window in the
background, and even while SprigCast is closed.
