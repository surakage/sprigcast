# Changelog

What changed in each SprigCast release, grouped by area. Numbers like (#184) are the pull requests on GitHub.

"Tested" means tried on a real Windows PC. Everything else passed the automated tests but still needs a try.

## 0.2.0 (not released yet)

The first named release since the build guide. SprigCast now looks and works like its own app instead of an OBS copy:
a Live screen for running the show, a Build screen for making blooms, chat, a control API, and a lot of speed work.

### Tested on Windows

- One-hour Twitch stream: no dropped frames, memory flat, CPU 3 to 5 %, about 2 ms to draw a frame.
- Chat from Twitch, YouTube and Kick together.
- The Windows installer.

### Taken out

Not steady enough to ship yet; each is listed in docs/REVISIT.md with what it needs to come back.

- TikTok chat (#431). Kick chat (since rebuilt on the official API, see Chat), VST3 sound filters and the Background remover store plugin (#430). The Live captions
  store plugin, and with it the onnxruntime library (about 25 MB of the download, 70 MB installed). Elgato Wave Link
  sync (Wave Link's channels still work as sound devices). The Auto clip store plugin, which wasn't reliable enough
  (a looping video's sound kept suggesting clips). A Background remover, Live captions or Auto clip someone had added
  is deleted at start. Once, at start, SprigCast deletes what those
  features left on the PC: downloaded models, their settings (including a captions Google key), their filters in gardens,
  and Auto clip's unkept suggested clips. Clips and the replay buffer are unchanged.

### Live screen

- Live gets its own screen: a big stage, then Blooms, Sound and Transitions as cards along the bottom (#119, #160, #196).
- Chat sits on the right beside the stage, with one Go live card under it for Record, Clips and Destinations (#174, #177).
- Destinations have on/off switches and short names; editing one no longer adds a new one (#124, #125, #126).
- Bloom buttons show their hotkey and a small picture (#183).
- A "You're live!" card with the sloth when a stream starts (#163, #187-#195).
- Right-click menus on the stage, bloom buttons and destination cards (#99), and Full screen on another monitor (#131).
- New Live layout: Go live, Record, one clock and a Garden check pill (CPU, memory, dropped frames; click for every
  number and the last streams' health) in the header; Both / Horizontal / Vertical, Clip, Screenshot and Virtual Cam
  under the canvases; Blooms as a list per canvas; Destinations with platform tiles under Chat; a shorter
  Transitions card (type, Duration, Between blooms "N active", the rest behind its gear).
- Live fits small windows (down to 1280 × 720): the header drops extra words before any button, long names end in "…"
  with the full name on hover, Transitions and Sound wrap instead of cutting off, recording warnings move into the
  Garden check, and the pills under the canvases have round ends.
- No Studio mode: clicking a bloom puts it on air. Bloom right-click: Comes in with, Save screenshot, Pop out, Rename, Set a key. Canvas right-click: Pop out, Safe zones (tall canvases), Canvas settings. OBS clients see Studio mode as off.
- A round screenshot button in the top bar, beside search and settings. Virtual Cam and its gear share one small card. Record, Clips and Virtual Cam keep their names and show on or off by colour.

### Build screen

- Build gets its own screen: a library of blooms and sprigs on the left, what's inside on the right, and a folding inspector (#120, #173, #186).
- Edit a bloom without putting it on air, drag to reorder, nest sprigs inside sprigs (#121).
- Side handles, Alt to crop, zoom and pan, free shape (#122, #123).
- Horizontal and Vertical tabs, one shared Sound list, undo and redo, a smart + Add button (#186).
- Starter kit: pick what you want and SprigCast builds the first blooms for you (#186, #188, #191, #194).
- Bloom pictures, drag a sprig in, source icons, and "used in" links (#175).
- Fill the canvas (Ctrl+Alt+F) crops a camera evenly so no empty strips show; smoother webcam dragging (#220, #221).
- Starter kit always offers Vertical versions, and Just Chatting's camera fills the canvas (#220, #221).

### Settings

- A new look: a pill menu, titled pages and soft cards (#165, #170, #171).
- Output page: every encoder your PC has (NVENC, AMD, Intel Quick Sync, H.264, HEVC and AV1, x264), replay and clip settings (#184).
- Video page: base size and output size are separate, with a choice of downscale filter (#184).
- Stream page with padded cards for each place you go live (#126, #127).
- Menu bar: File, Edit, View, Panels, Tools, Help (#137). Docks are replaced by Panels (#140, #156).
- Audio page in tabs: Devices, Tracks, Sounds and Bitrate, with one switch for global devices (#203, #209, #212).
- Hotkeys page: grouped cards, search, and keys for sounds, clips and Build/Live (#204).
- Advanced page: process priority, recording file names, MP4 copies, colour, and a stream delay (#202, #206).
  While the delay runs, the Go live card says "Going out in N s" instead of Connecting (#215).
  Stopping a delayed stream sends its last seconds instead of dropping them (#216).
- Number fields have room inside so numbers don't touch the edge (#224).
- Names with & (like "SFX & alerts") show as typed in tick boxes and menus (#231).
- Settings reorganised into six pages: General, Stream (Places, Quality), Recording (Recording, Clips), Audio,
  Video (Canvases, Virtual camera) and Connections. Hotkeys and the plugin store have their own windows (Tools menu),
  snapping moved to Build's right-click menu, and the read-only Colour box is gone.
- Each encoder leads with one Quality choice (Lightest to Best). The rest (rate control, presets, B-frames, audio
  bitrate, output scaling, ports...) show with **Expert settings** at the bottom of Settings, a tick that's remembered.
- General → This PC: the tier Optimize picked (Light, Balanced or Best) with Optimize again, process priority, what
  sources off screen do, and Preview while live (Smooth, Light at 15 fps, or Off).
- A search box finds any setting, and canvas changes that wait for a restart get a Restart now button.
- File → Export settings / Import settings: one file with your choices, never your keys, sign-ins or passwords.
- Hotkeys Windows or every app already uses (Alt+Tab, Ctrl+C, Windows-key combos) are refused with the reason.
- Recording "Same as stream" is on for new setups: one encoder while live instead of two.
- The first time the encoder can't keep up, a note offers Make it lighter right away.
- The mouse wheel no longer changes a drop-down you didn't click (it set a place's Resolution to 480p).
- Settings no longer waits for the sound-device scan to list the virtual camera's speakers (a 300 ms freeze).
- Browser sources and web docks share at most two Chromium page processes.
- From the heavy run (2026-11-17), the way OBS does it:
  - Browser sources are drawn in a helper process. Copying a page waited for the graphics card for 0.3 s at a
    time, which froze the window and held up the renderer. The helper ends 90 s after the last page closes, and
    starts again with the same pages if it stops.
  - A picture sent to the graphics card goes into fresh memory, so it never waits for the card to finish the
    last frame (up to 355 ms before).
  - Videos and cameras shown at half their size or less are decoded that small, with a quarter or a sixteenth
    of the pixels to send.
  - A looping GIF or silent clip that fits in 48 MB is decoded once, then plays from memory with its file closed.
  - When the canvas runs late, the stream and recordings repeat the last picture instead of leaving a gap, so
    they keep a steady 60 fps. Gaps over half a second, or with the encoder busy, stay as gaps.
  - A source drawn every 2nd frame to stay on time goes back to every frame only once it's well under budget,
    instead of switching back and forth.
- Settings → General → Features: switch off Clips or the virtual camera. Off, they leave Live and load nothing.

### Sound

- Sound card groups sounds into Voice, Game, Alerts, Music and Other, and folds the quiet ones away (#130, #135, #155, #172).
- Tune page for each sound, with a filters pop-up and five new audio filters (#130, #135, #158).
- "Mic first" ducking: other sounds dip while you talk (#135).
- Named tracks: Stream, Recording, Voice and Clips, chosen per sound (#154).
- Tracks 1 to 5, including a VOD-safe track, and each recording picks its tracks (#203).
- Source record: a second recording without the sources you pick, like alerts (#209).
- Capture sound: right-click a game or window capture → Sound to bring the app's own sound, or any output, as its own row (#225).
- Elgato Wave Link channels and mixes are offered first for desktop sound, named like "Game · Elgato Wave Link" (#223, #225).
- Starter kit By type: every row offers the full output list, and a Show desktop audio switch brings back the old app choices (#230).
- Cheaper app sound: one process snapshot shared by every row (#228).
- Browser sources can play sound (#147).
- Cheaper level meters (#199).

### Streaming, recording and encoding

- SprigCast's own NVENC encoder, with no B-frames by default (#107, #108, #109).
- The graphics card's encoder on AMD and Intel PCs too (#95).
- Streams that want the same picture share one encoder (#116), and recording keeps its own encoder beside a stream (#114).
- Built-in clips: 15, 30, 45 or 60 seconds, horizontal, vertical or both (#152).
- The stream summary shows the bitrate actually sent (#118, #136).
- Logbook: daily logs, a report for each stream, and crash reports in Documents\SprigCast\Logs (#129, #134, #138).
- A recording stops itself, files saved, when the drive has about a minute of room left, instead of filling it.
  Finishing a recording as a plain MP4 waits when there's no room for the second copy (the file plays as it is).
- The Stream doctor adds up every place you go live to and checks the total against your upload (Optimize's test,
  or what your last clean stream carried). Far over asks before going live.
- AMD's encoder keeps a steady bitrate on still scenes in constant bitrate mode, as NVENC already did (like OBS).
- NVIDIA: SprigCast's own NVENC now takes each frame straight from the graphics card (OpenGL shares it with
  Direct3D, as OBS's encoder reads its frames on the card), instead of copying it into memory and back: about
  187 MB a second less per 1080p60 output, and no frames waiting in memory. Anything the driver refuses turns it
  off for the rest of the run and frames go through memory as before; SPRIGCAST_NO_GPU_SHARE=1 turns it off.
  The stream report says which way frames went. First PC run: drawing went from ~5.8 to ~10.4 ms a frame
  (handing over waits on the render thread), so it's now OFF unless SPRIGCAST_GPU_SHARE=1, with a log line
  each minute saying where its time goes.
- Frames read back from the graphics card (previews, other encoders) are copied once instead of twice.
- A Stream timer or Death count hotkey no longer freezes the window: only that source's settings go to the
  renderer, not a whole copy of the garden (544 ms and ~440 skipped stream frames on the PC), and it isn't an undo step.
- Switching back to a bloom of many sources spreads their first pictures over a few frames (24 MB a frame at most)
  and the switch waits for them, so nothing pops in late; a "waking frame" log line names each slow source.
- Bloom switches got lighter again: texts, clock cards, pictures and Now playing are drawn one at a time on a
  pictures lane instead of four at once (they crowded out the render thread); Stream timer and Local time paint
  there too; the new bloom's sprigs are drawn once it shows, not while the old one is held; a Slide show keeps
  its slide while hidden; Now playing keeps listening 2 minutes off screen; Death count sends a tiny clear picture.
- Opening Settings or any pop-up no longer pauses the canvases: the app style is plain Qt (it called Python for
  each of ~3,600 widgets, each waiting its turn while streaming); drop-downs, number boxes and menus get the
  wheel guard, room and rounded corners as they're made. Folders (Logs, Recordings) open without holding the window.
- Settings (a destination, the Sound card's gear) and Hotkeys show their window at once and fill it in just after;
  the Stream, Audio, Recording and Video pages are made in parts a tick apart; a warm-up a little after start (not while live) makes
  the first open about half as long. Instant replay sends a tiny clear picture on wake (was a full frame, 47-68 ms).
- Fixed a crash while live when UI Automation (a screen reader, the PC's menu sweep) asked the window something
  during a menu: Windows handles that error and carries on, but the crash note then read every running
  thread's stack and died reading the render thread's. Crash notes on Windows now hold the crashing thread's stack
  only, and the stall watch's every-thread dump is off unless SPRIGCAST_STALL_DUMPS=1.
- Now playing (v6) with the new engine: the card is handed over as a list of pictures (cover card, words,
  sliding words in thin slices for the fade, bars), so no whole card is painted and copied to the graphics card 30
  times a second; only the small time strip is painted when it changes. Lists of pictures are several times cheaper to
  make too.
- New engine: graphics memory grew about 10 MB a minute with bloom switches (PC soak 2026-11-18). A switch that
  waits for a camera gave its outputs back to Python and made them again after, with new textures each time, and
  the recording's encoder on the graphics card keeps every texture it was handed. The engine now keeps drawing the
  bloom being held, so recordings, streams and the replay buffer keep their outputs through a switch.
  Move and stinger transitions (drawn by Python) keep them too: the recording takes Python's frames for the
  transition, then goes back to the engine's same textures.
- A late frame of about 100 ms every few minutes with Move transitions (PC soak 2026-11-18): Python's minute
  cleanup freed 55-68 thousand objects each time, 65-72 ms holding everything else. It now runs every 10 seconds,
  each a sixth as long and inside a frame's idle time. SPRIGCAST_GC_TYPES=1 logs what each slow one freed.
- New engine: display, window and game capture happen inside the engine. Windows hands each picture over on
  the engine's own graphics device and it's copied there, so no screen picture is copied into memory, through
  Python, and back (about 500 MB a second for a 1080p60 game). If the engine can't capture on a PC, the old
  capture takes over by itself; SPRIGCAST_NO_NATIVE_CAPTURE_VIDEO=1 always uses the old one.
- Now playing (v7): Cover glow's sound dots didn't move (PC test 2026-11-18). Its wide row had dots half the row
  tall, and a bar stayed a dot until the music passed that height. Bars are now at most 0.3 of the row wide and
  grow up from their dot with the level, so every style moves with quiet music too.
- Death count (v8) and Snap (v7): a death while a Snap polaroid was up waited, frozen with the rest of the screen,
  until the polaroid left, then jumped to its last number with no rise (PC test 2026-11-18). Snap's freeze now
  leaves Death count moving, so each death rises and flips in turn.
- PNGtuber: an app setting Mic (listen_to) to a sound's id or name without "sound:" was kept but quietly heard as
  Auto. It's now matched to that sound, and anything unknown is refused, saying which sounds there are.
- PNGtuber (v4) with the new engine: each of its pictures goes to the graphics card once, then a bounce or a held
  mouth only says where it goes; a mouth or blink fade sends just the part that changes.
- A crash in native code can no longer hang SprigCast: SprigCast.exe writes the crash dump itself, in plain C on a
  thread made at start, and ends the run if it isn't written within 15 s (a Python callback writing it from the
  crashing thread left the window Not Responding for 25 minutes). A fault Windows survives (one in a window's
  paint) is dumped once and the run goes on. The crash report names the last fault, not COM's own handled
  "0x8001010d" notes before it, and the Slide show's Qt fallback keeps its picture alive while Qt reads it.
- Crash dumps name each thread (Windows): every thread SprigCast starts tells Windows its name, and adds "(ending)"
  once its work is done. The 12:21 crash on 2026-11-18 was in a thread Python was tearing down, and the dump
  couldn't say which one.
- `SPRIGCAST_CRASH_TEST=<seconds>[,main]` crashes SprigCast on purpose, to test the crash guard on a PC.
- The safe-mode question after a crash no longer adds a "0x8001010d" note to crash.log while it waits.
- Pop-ups open lighter (sweep 13): the Live screen's place rows, the number fields and the previews no longer run
  Python for every event they get (the window losing focus to a pop-up reached each of them), and the Garden
  check shows its rows in one layout pass.
- The Garden check, Last stream and the command palette hand Python's lock back quickly while they open, as the
  other pop-ups do (57-76 ms), and the Plugin store makes its cards two a tick, handing it back quickly too
  (147 ms over its fill).
- A media source copies each new picture before taking the lock the canvas reads it under, and one long frame
  (a stall elsewhere) no longer has a source drawn every 4th frame for most of a second afterwards (237 ms on the
  PC). SPRIGCAST_STALL_DUMP_MS (from 200 ms) prints every thread's stack for shorter stalls when chasing one.
- Chat's ⋯ menu is kept and made ready ahead instead of made in the click (151 ms). Drop-downs and number boxes
  keep the wheel rule in their own wheel handling instead of an event filter each, and the Live lists fit their
  height from their layout instead of a filter, so opening a pop-up no longer sends every one of them through
  Python (51-63 ms). Panels → Web panels checks Qt WebEngine is there without importing it (51 ms).
- Death count (v7) makes its stone, numbers and mound ahead, off the render thread, and starts a trip's clock with
  its first picture: the first death's stone came 3.5 s late on the PC, rise and flip unseen. Quick deaths each
  flip in turn (0, 1, 2 instead of 0 then 2); a flood waits for at most three flips.
- A browser page in a bloom Snap freezes no longer remakes its texture every 3 s: with the new engine, a frozen
  page's pictures are still taken (the page never waits) but its frozen picture stays on screen.
- Pop-up cards (Garden check, Last stream, Get started, Starter kit and the rest) draw their soft shadow once per
  size instead of blurring the whole card on every repaint, and the Stream doctor lost its two blurred shadows
  (96 ms). The encoder overload check runs on a worker instead of the window's thread every meter tick (62 ms),
  Last stream fills its tiles a tick after it shows (81-90 ms), and the main window no longer watches every event
  of the stage splitter and an empty tiles area (69 ms).
- Last streams no longer opens its drop-down as a window of its own for a moment (90-133 ms). The Garden check
  fills its rows a tick after it opens (75-101 ms), a Settings page is put in its place a tick after it's made and
  General's lower cards come a tick later still (62-131 ms), the + Add menu is made ready ahead (67 ms), and labels
  that already fit skip measuring when a window resizes (Hotkeys, 52 ms).
- Settings → General remembers the process priority instead of reading it again each time (93 ms), the Last streams
  drop-down shares its page's style (95 ms), and a plugin hotkey no longer imports anything when pressed.
- Bloom tiles draw themselves once per look and then only copy that picture: a pop-up opening, or the screen
  waking, repainted every tile (496 ms during a monitor wake). While a pop-up is being made the preview waits
  for the next frame and the Live places list for the next second. The Sound gear's rows come half a row a tick,
  the Plugin store's cards a few a tick, and one-line labels fit their text only when it or their width changes.
- The Plugin store reads its list and what's installed on a worker before making its cards (113-167 ms). Last streams
  shares one style sheet and, opened on its own, doesn't fill the Garden check's "now" view too (150-253 ms).
  One-line labels no longer run Python on every style or font change, the Garden check's chips are lighter, and
  the sound meters skip a few beats while a pop-up is being made.
- Pop-ups over Build mode no longer repaint the Library and Inside lists (101-156 ms each time). The Sound gear's
  drop-downs and number boxes are set up with almost no work as they're made, the rest a moment later (243 ms),
  and its sound rows come one a tick. Settings → General no longer reads the registry for "Start SprigCast when
  Windows starts", the Garden check's one-line labels measure themselves once, and the Stream page's last few
  style sheets joined its page style.
- The first half-minute after start has fewer hitches: the Stream settings page shares one style sheet instead
  of 30-odd (245 ms of its warm-up), its Quality tab is made and put in place a tick apart, handing unused memory
  back to Windows runs off the window's thread (60-66 ms), and start-up's objects are set aside without a full
  look through all of them (44-63 ms).
- Opening or closing a pop-up no longer repaints the whole window behind it: its colours stay the same when it
  loses the focus (Windows' own palette changed them, and every bloom tile and panel painted again: 76-214 ms of
  opening Settings, the Plugin store, Web panels or Paths > New while live). The Plugin store's rows share one style
  sheet instead of six each (333 ms), Hotkeys' action names are one line each (a wrapped name was measured again on
  every step of rows) and number boxes draw their chevrons once.
- Pop-ups no longer pause while an app is connected to the Control API: it works out which blooms belong
  together in one pass instead of once per bloom (409 ms on the PC while live). The Sound gear's rows set up
  their drop-downs and track ticks without asking Qt back for each one (300 ms). Garden check and Last streams
  change their rows in place while live instead of making them again every second (110-160 ms). A PNGtuber's
  picture keeps its spot on the graphics card while it sleeps, so its bloom comes back without a 187 ms hitch.
- Bloom tiles measure their name and key once, not on every paint: a pop-up opening over the window repaints them all
  (309 ms under the Settings gear while live). Hotkeys are made four rows at a time (a card was 60-80 ms), and the
  Garden check's rows and chips share one style sheet (140 ms).
- Control API screenshots (Streamer.bot, Stream Deck) and a bloom tile's Save screenshot wait for the frame and make
  the PNG on a worker; the window keeps drawing and answering meanwhile.
- More from the second live sweep: the Audio page's sound rows are made a few per tick (263 ms); the control pictures
  are written once per run, not checked on disk at every open (137 ms on Last streams); one-line labels work out
  their size once per text (Garden check); the Plugin store shows first and fills after (94 ms); Missing files (and
  the check after a garden switch) look for the files on a worker (110 ms); the Settings warm-up after start is
  made in parts too (111 ms).
- Pop-ups from the PC's sweep while live open without holding the window: the modules they load are loaded by the
  warm-up (the Audio page was 256 ms, the plugin store 138 ms); device-name patterns are made at start (186 ms);
  Hotkeys uses one style sheet and makes its cards a tick apart (427 ms); the Transitions pop-up makes each tab when
  it's first shown (190 ms) and the Garden check makes Last streams only when shown (148 ms); sloth pictures are read
  once at 640 px on a worker and shrunk once for every pop-up (About); the Sound card asks Qt about its rows only
  when they change.
- The card after a crash (and Help → Report a problem) no longer holds the window: it opened with .exec() and froze
  it 404 and 660 ms. The report is built and sent on a thread of its own, and "See what's sent" builds it on a worker.
- Help → Export report makes the report on a worker and saves it straight to the desktop, shown in its folder
  (the save box and the build on the window's thread froze it 310 ms). For the pop-up sweep, SPRIGCAST_UI_STALL_MS=50
  logs every window stall from 50 ms with where the window's thread was (the usual watch starts at 250 ms).
- Spare sprig targets and textures past their minute (and an ended recording's copies) are freed a little each
  frame, not all at once: that held the graphics card 300+ ms about a minute after a Stop or a bloom switch.
- The PNGtuber folder is read again on a worker (the window answers from the last read), with one disk read per
  folder; Freeze hotkeys on big sprigs no longer hold the window (each sprig walked once, one commit).
- Texts, Local time and the stream timer keep their picture while their bloom is off screen, and draw again only
  if what they show changed: each was drawn again on every bloom switch (10-14 ms each on the PC).
- Switching back to a bloom full of sources: their textures wait a minute after the bloom leaves the screen and
  are reused when it comes back, instead of being made again in the switch frame (140-225 ms on the PC).
- Switching to a heavy bloom no longer costs a 100-220 ms frame: sprig targets that were let go are kept a minute
  and reused, pictures shown again are decoded on a worker (the switch waits for them, as for a camera), and a
  sprig's Live look is a light copy instead of a deep one.
- Heavy scenes draw faster: the sprigs pass works out each sprig's Live look once per change instead of copying
  every sprig every frame (7.7 to 0.7 ms a frame with 17 sprigs on two canvases in a test). SprigCast Camera
  showing the bloom on air shares that canvas's picture instead of drawing the whole bloom again. A text that
  changes (a clock, Now playing) is drawn again on a worker, not on the render thread.
- Fewer late frames while live: the once-a-minute tidy, thread count and memory line now wait for a frame to end
  and run 20 s apart (together they made a 17-20 ms late frame on the same second of the minute). A full-size
  preview keeps its read-back buffers for 3 minutes, so closing Settings no longer costs a 27 ms frame. A late
  frame's log line now names what the window was doing when a step waited for Python's lock.
- Fixed: after the Settings freeze fix, any restyled label on the Live screen kept Python's quick lock hand-over
  on for good, which doubled SprigCast's CPU and dropped the heavy run to 14-41 fps. Only a window or pop-up
  being shown starts it now, for 3 seconds at most.
- Twitch sign-in no longer signs you out when going live and sending a chat message refresh it at the same moment.

### Get started

- A two-step Get started card on a new install (and any time from Tools → Get started): what you make, go live or record,
  which shape, where you go live (Twitch, YouTube, Kick, TikTok, X), then your camera, capture card and mic (PC sound is split by type for everyone).
  It names a garden after the show and a path after where it goes, adds a place per pick, and makes the blooms that fit.
  A capture card fills the Gaming bloom and brings its own sound.
- X is a streaming service choice.
- Sign in to YouTube or Kick (in Get started, or Sign in beside the stream key) and SprigCast fills in the key, and
  Kick's server, for you. YouTube makes a reusable key if there's none. The client secrets stay out of the app:
  a small sign-in helper at signin.slothbloom.com finishes each sign-in (integrations/signin-helper).

### Optimize wizard

- First-run wizard that checks your PC and upload and suggests settings (#110, #112, #113, #141).
- Upload check with a hidden Twitch bandwidth test (#115), and it remembers the heaviest stream that ran clean (#189).
- Light settings for a slower PC, offered once (#96, #101).

### Lighter on your PC

- Frames come off the graphics card without the CPU waiting, which halved the time to draw a frame (#167).
- Less memory: a smaller GPU-usage reader (#91), tiny textures for solid colors (#93), no full-size previews at start (#94),
  a lighter font engine (#100), and no hidden browser for empty browser sources (#193, #197).
- `sprigcast-memcheck` shows where memory and CPU go, and runs an hour-long soak test (#90, #92, #97, #98, #102, #111).
- Death count and Snap draw their art once and reuse it, skip frames while they stand still, and hand frames to the GPU
  without a copy (about 4x less work while they move, none while held). In the 60-minute soak they made the canvas miss
  frames and starved the recording's encoder, so recordings and clips dropped to about 50 fps. Slide show decodes the
  next picture in the background. Store plugins you added update themselves when SprigCast does.
- Death count engraves its stone once per number instead of redrawing the text every frame, and painted plugin frames
  go to the GPU premultiplied, as QPainter leaves them, with no conversion (about half the work per moving frame).
  A full-screen popout draws its frame straight, without scaling or a copy, when it already matches the screen.
- Slide show leaves out SprigCast's own screenshots and Snap polaroids. Pointed at the Screenshots folder in the soak,
  it played pictures of the canvas back onto the canvas, which looked like trails of old frames.
- The clips buffer no longer keeps sound forever while no picture keyframe comes (a stalled video encoder): it holds
  the clip length plus 10 s. When a recording or the stream stops, the log now says what Task Manager shows, what is
  committed, how many Python objects there are (and which kinds) and how many threads, so long shows can be compared.
- Once nothing is recorded, streamed or kept for clips, SprigCast lets go of cached plugin art and hands the pages it
  no longer touches back to Windows (after a 2-hour soak it sat at ~415 MB at rest, against ~200 MB fresh). One late
  frame in a second no longer flips the status word to Busy.
- Shorter Python cleanups: a small tidy every 5 minutes freezes what survives, so the full collection that took ~70 ms
  every 10 minutes in the 2-hour soak (and made a late frame, often on a bloom change) walks only what is new. When the
  show ends, everything is looked at once so nothing from it is kept. Start-up's one-time pages go back to Windows
  20 s after SprigCast opens.
- After a show: the at-rest clean-up runs once when several outputs stop together (it ran three times), and the full
  look through Python's objects (191 ms, 22 late preview frames after the soak) waits until SprigCast is minimized.
  Two walks that ran every frame no longer leave reference cycles behind, so Python's cleanups have less to find.
- Death count's number flip moves pictures of the numbers instead of cutting text every frame (15-24 ms a frame on a
  busy PC in the soak).
- Turning the virtual camera on sets it up the first time (one Windows prompt, no trip to Repair), checks Windows'
  camera privacy first and, when it blocks desktop apps, says so and offers to open the right Settings page.
- A sprig nothing has shown for a few seconds lets go of its full-canvas texture (they stayed until SprigCast closed),
  and removing a plugin stops its sources straight away instead of at the next restart. Stopping a recording or
  stream logs the app's memory, so long sessions can be compared.
- The first frame is drawn before the frame clock starts, so start-up no longer has a one-second hitch.
- App sound's once-a-second app check no longer leaves objects behind. Over a night they piled up into millions, so
  Python's clean-up pauses grew from 36 ms to 289 ms (16 missed frames each) and memory crept up.

### Sources and effects

- A Style tab for plugins: colours, font, text size, corners (round, leaf or square), parts to show or hide, and a
  Custom look box that takes CSS-like lines (`text: pink`, `corners: leaf`, `border: 2 white`, `hide: dot`). It only
  changes the look, is never run, and a line it doesn't understand is named under the box and left at the plugin's
  own look. Local time, Countdown, Stream timer, Now playing and the Chat box have it (the clock cards keep their
  leaf cut by default and can hide the badge or the vine); plugin makers get it from `sprigcast.core.style`.
- Transitions: slide, swipe, wipe, leaf, bloom and stinger clips (#145), a transition for particular bloom switches (#151),
  and a Transitions card with a pop-up for the details (#149, #162, #164, #166).
- Move transition: what both blooms show (matched by source, then by name, even inside sprigs) glides to its new
  spot, size, turn and crop; the rest fades, scales or slides. Five easings, both canvases, and usable in Between
  blooms rules. It draws straight onto the canvas, so it costs less than a fade. OBS Move transitions import as it.
- Browser source on Chromium 140 (#139), and right-click Interact to click around in the page (#153).
- Color looks (LUTs) and shapes for any source (#168).
- Media source: video and sound files, with play and pause from menus, hotkeys and the API (#198).
- SprigCast Camera: a Windows 11 virtual camera, set up in Output → Virtual camera (#208).
- Virtual camera Shows list: match the app, a whole canvas, one bloom or one source; fixed the camera not starting in TikTok LIVE Studio (#224).
- When an older camera fails to start, SprigCast points you to Update (#227).
- Media files no longer loop with Loop off, and .webm files end cleanly (#220, #222).
- Slide show fills the canvas when added; + → Plugins entries work on the first click; Death count and Snap no longer
  show "problem" in Leaves after they run.
- Virtual camera opened at two sizes at once (TikTok LIVE Studio, or two apps) keeps feeding the first size instead of
  flipping every few seconds.
- Snap shows the whole screenshot in the polaroid (the card takes the screenshot's shape instead of cutting its edges
  off) and the polaroid stays inside the canvas the whole time. The saved file is the
  plain screenshot and a screenshot taken while a polaroid is up leaves it out. "Save a polaroid copy" also saves the
  framed polaroid; it's off by default, also for Snaps that had it on before.
- Live captions store plugin: what you say, as captions in a band along the bottom of the bloom. Runs on your PC with a
  small Whisper model (free and offline; it downloads the first time captions go on air, 75 to 480 MB), or with your
  own Google Speech-to-Text key for slower PCs (kept encrypted, never in the garden file). Pick the mic, language,
  Light / Balanced / Sharper, 1 to 3 lines and colours. It listens only while it's on air and lets the model go a
  minute after.
  How captions look on stream: Card, Outline, Shadow or Plain; any font on the PC; Small / Medium / Large; Bold;
  All caps; left, centre or right; text, card and outline colours.
  Captions run on the graphics card (NVIDIA, AMD or Intel Arc, through DirectML: nothing extra to download)
  and fall back to the processor; Run on picks. Whisper now runs through ONNX Runtime, so faster-whisper
  and its CUDA needs are gone from the installer, and the old model downloads are deleted.
  The Stream doctor no longer stops go-live because captions are Listening (waiting for you to talk), and
  blooms going on and off air reuse one listening thread instead of adding one each time.
  The speech model runs in its own low-priority helper process, so loading it (1.4 s on the PC test) never
  freezes the picture or the stream, and its memory all goes when captions leave the air. The model downloads
  when captions are added or SprigCast starts, not when they go on air.
- Instant replay store plugin: plays the last few seconds back on stream. Pick one sprig or source to replay (your game
  or Elgato capture), and optionally a second one in a corner (your camera). Its hotkey, right-click, Stream Deck or
  an app (`replay.play`) starts it: canopy bands sweep in, REPLAY shows, the replay plays with a badge and a progress
  line (normal speed or slow motion, sound slowed with it), then the bands sweep back and it hides. Apps hear
  `replay.started` and `replay.ended`. It keeps nothing while its bloom is off air, encodes only what you picked (720p,
  or 1080p with Sharp) and deletes each replay file when it ends.
- Background remover store plugin: a camera filter that removes the room behind you, blurs it, or puts a picture
  there, no green screen needed. A small model (Google's MediaPipe selfie segmentation, Apache 2.0) runs on the
  graphics card through DirectML, any brand, or the processor when it can't; "Better edges" uses a bigger one for
  hair and fingers. It downloads the first time it's used (0.5 MB, or 16 MB for Better edges), runs only while the
  camera shows and lets go a minute after. A shadow or stroke on the camera follows you, not the frame.
- PNGtuber store plugin: your own avatar pictures (PNG, GIF, WebP) talk when you talk, the mouth opening and
  shutting with each syllable, with a little bounce as each sentence starts, random blinks, a quick fade between pictures, a yell picture for loud moments, and expressions you switch with hotkeys,
  a right-click, the Stream Deck PNGtuber key or `pngtuber.*` API actions (for some seconds, or until changed). It
  follows a sound you already have in SprigCast (Auto: your mic, so muting quiets it too) or a mic straight from
  Windows, sends nothing while nothing moves, keeps pictures only at the size they show (big animated emotes keep
  fewer frames), and off air stops listening and lets the pictures go. A
  sample sloth (your own Picsart frames) comes with it.
- Stream timer store plugin: how long you've been live (or recording), a countdown (minutes, or to a time of day such
  as 8:30 PM) or a stopwatch, in a small card like Local time's. Start, pause, reset and add time with hotkeys,
  right-click, the Stream Deck (Run action) or `timer.*` API actions; apps hear `timer.done` when a countdown ends. A
  countdown can start over each time it comes on screen (for a "Starting soon" bloom), and a stopwatch carries on
  after a restart. Off screen it does nothing; on screen it's drawn once a second.
- Now playing store plugin: the song Windows says is playing (Spotify, YouTube Music, browsers and most players) with
  its cover, in a small card that fades away when the music stops. No keys or sign-in. It listens only while the card
  is on screen and only when Windows says the song changed; Only from limits it to some apps.
- Now playing has four styles (Style: Canopy card, Cover glow, Slim band, Sprig ring), a time bar with how far in the
  song is and how long it is (when the app tells Windows; Show time), and sound bars that move with the music (Sound
  bars, and the Sound they hear). A title too long for the card slides by so all of it shows (Slide speed, 1 to 5,
  default 2), instead of being cut off with "...". The card is drawn once per song and each frame adds only what
  moves, on a worker, never on the render thread; standing still, nothing is drawn. Adding it no longer freezes the
  window for 0.66 s, and the log notes each new song. nowplaying.get also answers `length` and `position`.
- A plugin, text or slide show added to a sprig that shows small in its bloom (say at 40% in a corner) starts sized
  for how big the sprig shows, inside the part of the canvas the sprig covers and clear of the others there, instead
  of at 40% of its size. Bands shrink to fit a crowded sprig; a tall sprig stacks the cards down its sides.
- Plugins put in a bloom through the Control API (item.add) or the OBS-style connection (CreateInput,
  CreateSceneItem) start at the same readable size and free corner as from the + menu, on both canvases; text starts
  the same share of the canvas at 720p, 1080p or 4K. They used to start at the size of their box in the top left corner.
- Re-adding a store plugin in the same session no longer doubles its right-click entries.
- Freeze store plugin: freeze a sprig, game or camera on the frame showing now for a set time (a redeem that
  freezes your game for 10 seconds), then it's live again by itself. Right-click, hotkeys, and `freeze.start`,
  `freeze.stop` and `freeze.toggle` for Streamer.bot and other apps, with an `effect.ended` event when it ends.
  Things that only redraw when they change (text, Death count) show their newest picture as it ends.
- Snap freezes the screen on the snapped moment while the polaroid prints, so viewers see exactly the frame in the
  polaroid; the Snap keeps moving and the screen goes live again as the polaroid slides away. On by default
  ("Freeze the screen while it prints" in the Snap's settings).
- A store plugin added again or updated no longer runs its old hooks too (Snap would have printed twice).
- Audio visualizer store plugin: bars, a wave, a circle or a line spectrum that move with a sound, in your colours
  (Night Canopy greens by default), with bar count, smoothing, mirror and sensitivity. It hears what SprigCast
  already mixes (everything viewers hear, or one mic, desktop, music or app sound), so no device is opened again,
  draws with NumPy in about a millisecond, at most 30 times a second, and only while it's on air; during silence
  nothing is drawn. `visualizer.set` changes the look from Streamer.bot.
- Death count store plugin: a mossy gravestone grows out of the ground, flips to the new count and sinks back. Hotkeys, right-click and `deaths.*` API actions for Streamer.bot; the count survives a restart.

### Chat

- One Chat panel that mixes Twitch, YouTube and Kick from just channel names (#144, #148, #150).
- Show chat on your stream, and reply on Twitch (#181).
- Fixed YouTube live lookup and Kick's chat key (#185).
- Kick chat is back, rebuilt on Kick's official API: SprigCast's small relay (integrations/kick-chat) takes Kick's
  signed chat webhooks and passes them to the app. Nobody signs in, and the app never talks to kick.com itself.
  It needs the relay deployed once (see its README).

### Gardens and paths

- Gardens: whole sets of blooms, sprigs and sources for different shows (OBS's scene collections). Paths: where and how you go out, with places, encoder and recording settings (OBS's profiles), e.g. Twitch only, Multistream, Recording only. Each has its own menu: New, Duplicate, Rename, Remove, Import, Export, and Gardens has Missing files. Your setup becomes the "Main" garden and the "Main" path. The window title shows which are in use. Works through the control API and the OBS door, so Streamer.bot can switch them.
- Import scene collections from OBS, Streamlabs and Meld into gardens: Gardens → Import… takes their files, and Import from OBS / Import from Streamlabs lists the ones on this PC. Scenes nothing else uses become blooms, nested scenes and groups become sprigs, divider scenes become folders (anything not under one is sorted by what it holds: Main, Browser, Media, Audio, Capture or Nested Scenes), a vertical canvas (OBS 31 / Aitum Vertical, Streamlabs dual output) comes along with "Name (Vertical)" linked to "Name", and positions, crops, bounds, volumes, filters and stingers carry over. A short list says what to check afterwards. Tested on a real 200-source OBS collection and a real Meld Studio Session.json (Spark Elements become browser pages at their Meld size, Text and Groups come along, the vertical canvas links to the main one, Mic/Game/Music tracks become sounds with Voice Clarity as noise suppression and a sidechain compressor as Ducking). Streamlabs: checked against 33 real scene collections (themes and a hand-built one); newer collections' vertical canvas lands right (Streamlabs keeps it as if 1080x1920), a scene shown on the vertical canvas uses its vertical items, Import from Streamlabs lists them by name A-Z and skips deleted ones, and Streamlabs .overlay files (File → Export Overlay) import with their videos, pictures and stinger, unpacked to Documents\SprigCast\Media; widgets come over as pages to paste your dashboard address into, and the notes group what to check.
- Imports don't double your sound: OBS's mic or desktop sound on the default device (or its only mic) becomes your Mic or Desktop audio with OBS's volume, mute and filters; a sound whose device isn't on this PC starts muted. Sounds play only in the blooms that held them in OBS (Plays in). OBS scene and mute hotkeys come along, and text is measured so centred titles land in place.
- A chip in the header shows the garden and path in use and switches them; switching garden while live asks first. Hotkeys can switch gardens and paths, and they work from every garden. Missing files has Look in a folder… to find everything that moved at once. A broken or missing garden file at start no longer resets your path.
- Gardens → Export with files… packs a garden with every picture, video, page (with its folder), look, stinger and slide show folder it uses into one .sprigpack file, so it moves to another PC complete. Import… takes packs: the files unpack to Documents\SprigCast\Media\<garden> and the garden points at them. Unpacking never writes outside that folder, checks free space and refuses damaged or oversized packs.
- OBS Composite Blur filters (e.g. THSV Pixelate) come over as SprigCast's Blur, in Pixelate or Blur mode, and an OBS compressor with a sidechain comes over as Ducking.
- Gardens are safer to use while live: packs pack and unpack (and Look in a folder searches) in the background, so the stream and window stay smooth; importing while live asks before switching; hotkeys and the API wait while Settings or another window is open (it would save into the wrong garden) and while a pack is working; a garden that can't be read at start says so and where its old file is kept, and that kept copy no longer shows up as a garden.
- Exports go to Documents\SprigCast\Exports with safe file names, never into SprigCast's own settings folder, and carry no stream keys (those stay in Windows Credential Manager). Imports read files with a BOM, refuse files that aren't gardens, and fit gardens from other canvas sizes. A lost gardens list is rebuilt from the files.

### Control API

- SprigCast's own control language at ws://127.0.0.1:4455/sprigcast (#176). See [docs/control-api.md](docs/control-api.md).
- Web links (/api/action) on the same port (#179), step lists with waits, item.place and item.order (#180), screenshots (#182).
- Compatibility mode for tools that speak obs-websocket 5, such as Streamer.bot and Stream Deck (#169).

### Look and brand

- Night Canopy theme: calm greens, pink only for Live and Recording (#106).
- New logo (the sprig S) and sloth mascot (#178), sloth art for empty spots and the wizard (#157, #161, #163).
- Leaf tick boxes, seed radio buttons and seed knobs (#142, #143).
- Drawn line icons instead of emoji, a rounded About box and a README banner (#205, #207).
- The new sloths in the starter blooms, Stream doctor and a Nice stream card (#211).
- Live screen spacing evened out (#213).

### Installer

- Windows installer built on GitHub (#103, #104), which asks to close SprigCast first (#105).
- Each installer build is scanned by Microsoft Defender and comes with SHA256 fingerprints (#229).
- An update deletes every file the last version installed and this one doesn't (a dropped library or plugin),
  from a list of files each installer carries, as OBS's updater does.

### Tests

- The test suite passes on Windows 11 too (#210, #214).
- Recordings no longer skip their second frame, which also fixed a flaky test (#216, #218).

## 0.1.0

The build guide, milestones M0 to M11: GPU canvases for horizontal and vertical at once, blooms and sprigs, display,
window and camera capture, image, color and text sources, audio mixer, recording, streaming to several places,
transitions, hotkeys, stats, filters, plugins and a dark theme.
