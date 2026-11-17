# Revisit later

Features that were taken out, or are on watch, because they weren't steady enough to ship.
Anything here comes back only after it's rebuilt or proven on real PCs. The bar is the launch bar:
it must run on any Windows 10/11 PC, old ones included, without crashes, freezes or dropped frames.

Each entry says why it's here and what it would need to come back. To bring one back, start from
the git history named in its entry, but rebuild it to today's rules rather than restoring it as it was.

## Taken out

### TikTok chat
- **Removed:** 2026-11-17 (#431). Rebuild from scratch; don't revive the old code.
- **Why:** it caused an access-violation crash while SprigCast sat idle (2026-11-16). It ran on the
  unofficial `TikTokLive` library (TikTok has no public chat API), with its own asyncio thread inside
  the app, and it had to be kept off the UI thread twice (#371, #377).
- **To come back:** a fresh design that runs outside the SprigCast process (a helper process), so a crash there can never take the stream down. Or lean on TikFinity /
  Streamer.bot, which already read TikTok and can call SprigCast's API.

### Kick chat
- **Removed:** 2026-11-17 (#430). The code was `src/sprigcast/chat/kick.py`. Streaming to Kick and Kick
  sign-in stay.
- **Why:** it read kick.com's private channel API and its Pusher chat feed. Neither is an official API,
  and kick.com sits behind Cloudflare, which already refused SprigCast's sign-in (1010/403, #420, #425).
  It was the same kind of risk as TikTok chat.
- **To come back:** Kick's official developer API, once it offers chat to desktop apps (today it only
  sends chat to a web server by webhook).

### VST3 sound filters
- **Removed:** 2026-11-17 (#430), with the `pedalboard` library. The code was the `vst` filter in
  `src/sprigcast/core/filters.py` and its "Open plug-in" button in the Filters window. A saved VST filter
  is skipped, and the sound plays without it.
- **Why:** a VST3 plug-in is someone else's native code loaded inside SprigCast. A bad plug-in can crash
  the whole app the same way TikTok chat did. It was never tried with a real plug-in (#253).
- **To come back:** run plug-ins in a helper process, so a crash there only stops that one filter.

### Background remover (store plugin)
- **Removed:** 2026-11-17 (#430). The plugin was `src/sprigcast/store/background_remover.py`, and its
  models were in `models/background/`. A copy someone had added is deleted at start
  (`plugin_store.RETIRED`). The drawing side went too (2026-11-17): the
  `background` filter kind, `plugin_hooks.register_frame_watcher`, and the compositor's `upload_cutout` and
  `cutout` shader paths. Find them in git history before #436.
- **Why:** it cost the most of anything measured on 2026-11-17. On the processor (old PCs, or any PC
  where DirectML can't start), on a 4-core test machine:
  - Normal: about 25 % of the whole PC (one full core) plus 5 % in SprigCast, and a 80 MB helper,
    for as long as the camera is on screen.
  - Better edges: about 47 % of the PC and a 190 MB helper, and it managed only 17 masks a second.
  - SprigCast itself streams at 3 to 5 % CPU. Nothing was used while the camera was off screen,
    because the helper ended. Its graphics card (DirectML) path was never run on a real PC.
- **To come back:** a graphics card path proven on real PCs (NVIDIA, AMD, Intel) that stays off the
  processor, plus a clear "your PC can't run this" message on PCs without DirectML. The onnxruntime
  library was taken out with Live captions, so it would need to be added back.

### Live captions (store plugin)
- **Removed:** 2026-11-17, along with the onnxruntime-directml library, which was about 25 MB of the
  download and 70 MB installed. The plugin was `src/sprigcast/store/live_captions.py`. A copy someone
  had added is deleted at start (`plugin_store.RETIRED`). The shared model download
  (`src/sprigcast/models.py`) went too, since nothing else used it.
- **Why:** it used too much while it ran. With the tiny model on the processor (4-core test machine), it
  needed a 210 MB helper process, about as much as SprigCast itself uses at idle, plus about 6 % CPU while
  someone talked non-stop. PCs with 8 or more cores picked the larger base model, which costs more. The
  model was a 104 to 290 MB download. Before #412 it froze the stream for 1.4 s, and its graphics card
  (DirectML) path was never run on a real PC.
- **To come back:** a much smaller memory footprint, a graphics card path proven on real PCs, and a test on
  an older PC. The streamer's-own-Google-key mode is light, since it needs no local model, and could come
  back on its own (paid services rule: the streamer's own key only).

### Auto clip (store plugin)
- **Removed:** 2026-11-17. The plugin was `src/sprigcast/store/auto_clip.py` (#388, #406), with
  `WindowHost.save_clip_into`. Find them in git history. The generic hooks it brought
  (`plugin_hooks.register_level_watcher` and `register_tool`) stay, since docs/plugins.md offers them to plugins. A copy someone had added is deleted at start (`plugin_store.RETIRED`). Clips and the
  replay buffer are unchanged.
- **Why:** not reliable enough. It judged a moment only by loudness against a learned usual level. In the
  PC heavy runs it saved a suggestion about every minute under a loud looping video (7 in 7 minutes), and
  after #406 still made four "game +14-17 dB" suggestions from a looping Game Over webm's sound. Each
  suggestion is a full clip written to disk.
- **To come back:** a way to tell a real moment from a repeating sound that works on real streams (for
  example only the game's own app sound, plus chat), tested across a whole stream before it ships.

### Elgato Wave Link sync
- **Removed:** 2026-11-17. The code was `src/sprigcast/audio/wavelink.py` and the "Keep Wave Link in step"
  box in Settings → Sound. Capturing Wave Link's channels and mixes as sound devices stays: those are
  ordinary Windows devices.
- **Why:** it talked to Wave Link's private WebSocket (the one Elgato's Stream Deck plugin uses), which
  isn't an official API, so any Wave Link update could break it. It was only tested against a fake server.
  It was light, with one local connection and no polling.
- **To come back:** an official Wave Link API from Elgato.

## Cleanup on PCs that ran an earlier build

`src/sprigcast/app/leftovers.py` runs once at start. It deletes the models folder that Live captions
and the Background remover downloaded into, and the settings that captions, Wave Link sync and Kick chat
kept, including a captions Google key, and Auto clip's `auto_clip.json`. `clean_up_recordings` deletes,
once, the unkept suggested clips that Auto clip's `suggested.json` lists in the recordings folder's "Suggested clips"
(nothing else in that folder). An update also clears SprigCast's old code before copying the new code in (`packaging/sprigcast.iss`
`[InstallDelete]`, all but the camera folder), so a removed module or store plugin file can't linger in the install.
Before that, Auto clip still showed in the plugin store after #454, and `plugin_store.catalog` now skips `RETIRED`
ids as well. When someone takes out another feature, add its leftovers there
and bump `VERSION`. Garden filters of removed kinds (`vst`, `background`) are dropped as gardens load
(`core/scene.py` `RETIRED_FILTER_KINDS`).

## Kept, with what was measured (2026-11-17)

These were reviewed and kept. Each still needs a pass on the real PC.

### Now playing (store plugin)
- **Kept:** it adds under 1 MB to the install (the `winrt` packages). It uses one thread only while the card
  is on screen, and it waits for Windows to say the song changed instead of asking.
- **Tested** with Spotify on the PC (2026-11-17): it follows song changes with no errors. Still to check: the
  four styles, the time bar (Windows' timeline) and the sound bars on the PC, and a browser player.

### YouTube and Kick sign-in
- **Kept:** it makes setting up a stream key easier (the user's decision, 2026-11-17).
- **Risk:** the Google app is still in Testing mode, so only listed test users can sign in. Outside testers
  will see "access blocked" until Google verifies the `youtube` scope. Kick still answers 403 (#425).
  Pasting the key always works.

### Twitch Enhanced Broadcasting (multitrack)
- **Kept:** for testing while multistreaming. OBS offers the same thing through the Aitum plugin.
- **Risk:** it was built in #19 and checked only against a local FFmpeg listener. It runs one encoder per
  quality, and Twitch drops the connection if the broadcast metrics are off. It is off by default.

## Check in the next PC test

These aren't removal candidates. Each needs a pass on real hardware.

- **Graphics driver reset recovery** (#428): never triggered on a real PC.
- **SprigCast's own NVENC encoder** (`output/nvenc_d3d11.py`): it talks to the NVIDIA driver directly and is
  the default for recordings and the replay buffer. It has only been proven on the user's RTX card. It checks
  itself with a test frame and falls back to FFmpeg. Try it on another NVIDIA card.
- **Untested store plugins and features:** Instant replay, Move transition, Stream timer and
  PNGtuber. If one misbehaves in the test, review it here.
- **Paired devices:** the cause of StreamBridge's lost pairing is still unknown, though the pairing survived
  the last update. If it's lost again, read the "paired" log lines.
