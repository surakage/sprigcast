# Revisit later

Features that were taken out, or are on watch, because they weren't steady enough to ship.
Anything here comes back only after it's rebuilt or proven on real PCs. The bar is the launch bar:
it must run on any Windows 10/11 PC, old ones included, without crashes, freezes or dropped frames.

Each entry says why it's here and what it would need to come back.

## Taken out

### TikTok chat
- **Status:** being removed (decided 2026-11-17). Rebuild from scratch; don't revive the old code.
- **Why:** it caused an access-violation crash while SprigCast sat idle (2026-11-16). It ran on the
  unofficial `TikTokLive` library (TikTok has no public chat API), with its own asyncio thread inside
  the app, and it had to be kept off the UI thread twice (#371, #377).
- **To come back:** a fresh design that runs outside the SprigCast process (a helper process, like
  Live captions' model), so a crash there can never take the stream down. Or lean on TikFinity /
  Streamer.bot, which already read TikTok and can call SprigCast's API.

## Under review (decision pending)

These were flagged in the stability review of 2026-11-17. Each one is still in the app until a
decision is made; once one is taken out, move it to "Taken out" above.

### Kick chat
- **Why:** reads kick.com's private channel API and its Pusher chat feed (`chat/kick.py`). Neither is an
  official API, and kick.com sits behind Cloudflare, which already refused SprigCast's sign-in (1010/403,
  #420, #425). Same kind of risk as TikTok chat.
- **To come back:** Kick's official developer API, if it ever offers chat to desktop apps.

### YouTube and Kick sign-in (fetch the stream key)
- **Why:** the Google app is still in Testing mode, so only listed test users can sign in; outside
  testers would see "access blocked". Kick still answers 403 (#425). Closing Settings mid sign-in caused
  heap corruption (#420, fixed). Pasting the key always works.
- **To come back:** Google verification of the `youtube` scope, a working Kick sign-in, and a run with
  a tester who isn't on the test-user list.

### Twitch Enhanced Broadcasting (multitrack)
- **Why:** built in #19 and checked only against a local FFmpeg listener; never tried on a real Twitch
  stream. It runs one encoder per quality, which is the opposite of "easy on the PC", and Twitch drops
  the connection if the broadcast metrics are off. Off by default.
- **To come back:** a real Twitch test, on a PC with room to spare.

### Elgato Wave Link sync
- **Why:** talks to Wave Link's private WebSocket (`audio/wavelink.py`); only tested against a fake
  server. Off by default.
- **To come back:** a test with Wave Link 3 on the user's PC, and a check after each Wave Link update.

### Live captions (store plugin)
- **Why:** the largest plugin (about 1,100 lines), with a speech model, model downloads and a helper
  process. It froze the stream for 1.4 s before #412, and the graphics-card (DirectML) path hasn't run on
  a real PC. It needs onnxruntime-directml in the installer.
- **To come back:** a PC test on both graphics card and processor, and on an older PC.

### Background remover (store plugin)
- **Why:** v2 (model in a helper process, #428) has never run on a real PC. AI on every camera frame is
  heavy for older PCs. With Live captions, it's the only reason onnxruntime ships, so shelving both
  shrinks the install.
- **To come back:** a PC test with the Facecam, frame times with Better edges, and memory after it parks.

### VST3 sound filters
- **Why:** a VST3 plug-in is someone else's native code loaded inside SprigCast. A bad plug-in can crash
  the whole app the same way TikTok chat did. Never tried with a real plug-in (#253).
- **To come back:** run plug-ins in a helper process, or test a set of common free plug-ins (ReaPlugs,
  NVIDIA Broadcast) on the user's PC.

### Now playing (store plugin)
- **Why:** reads Windows' media session through `winrt`; tested only against a stand-in, never on real
  Windows (#390). Adds two Windows-only packages.
- **To come back:** a PC test with Spotify and a browser playing.

## Keep, but check in the next PC test

Not removal candidates. They stay, and each needs a pass on real hardware.

- **Idle preview copy stall:** at idle after a show, one frame took 330 ms on the preview copy and the
  window stopped answering for 0.9 s (log of 52ad3a7). Being fixed separately.
- **Graphics driver reset recovery** (#428): never triggered on a real PC.
- **SprigCast's own NVENC encoder** (`output/nvenc_d3d11.py`): talks to the NVIDIA driver directly and is
  the default for recordings and the replay buffer. Proven on the user's RTX card only; it checks itself
  with a test frame and falls back to FFmpeg. Try it on another NVIDIA card.
- **Untested store plugins and features:** Instant replay, Move transition, Auto clip, Stream timer,
  PNGtuber. If one misbehaves in the test, move it to "Under review".
- **Paired devices:** the cause of StreamBridge's lost pairing is still unknown; it survived the last
  update. Read the "paired" log lines if it happens again.
