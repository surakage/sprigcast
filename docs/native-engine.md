# The native engine

SprigCast's engine is moving from Python to C++, one part at a time. Everything that touches frames or sound
moves into `sprigcore.dll` (`native/core`) and runs on the engine's own threads. Python keeps the screens,
settings, plugins, Gardens and Paths import and the Control API, and only sends the engine commands. It is the
same split as OBS: libobs in C and C++, the Qt screens on top.

Why: Python runs one piece of Python at a time (the GIL), so rendering, encoding, sound, the screens and plugins
all take turns on one lock. Most of the hard stalls (QPainter in plugins, the process scan at start, #459's
switch interval, the heavy-run read-backs) were that lock. In the engine, a slow screen or plugin can't drop a
frame.

## Rules

- **Our own code.** The engine is SprigCast's own Python converted to C++. OBS is studied for ideas only; no OBS
  code is copied, and SprigCast is not an OBS clone.
- **Frozen snapshots.** The engine draws from its own copy of what is shown (`render/native_snapshot.py` builds it
  on each change), never from Python's live objects, so no Python lock is held per frame.
- **Crash gates.** Every exported function catches C++ exceptions and, built with `/EHa`, Windows faults. A device
  that faulted is shut off (`SC_ERR_FAULTED`), a lost graphics card answers `SC_ERR_DEVICE_LOST`, and Python
  makes a new device; the app goes on. Each later phase adds its own gates (thread guards, watchdogs, helper
  processes, encoder fallback); see the plan.
- **The engine never waits on Python.** It never calls into Python. Its log lines wait in a queue that
  `sprigcore.drain_log()` moves into SprigCast's log.
- **No frames or sound cross into Python** on the frame path. Reading a canvas back is for tests and screenshots
  only.
- **One C interface**, `native/core/include/sprigcore.h`, reached only from `src/sprigcast/sprigcore.py` with
  ctypes (which lets go of the GIL for each call). Every function returns `SC_OK` or an error code, with
  `sc_last_error()` saying what went wrong. No C++ exception crosses it. Bump `SC_INTERFACE_VERSION` and
  `sprigcore.INTERFACE_VERSION` together when it changes.
- **The app always works.** Each part that moves has an old and a new path. `SPRIGCAST_ENGINE=native` picks the
  engine; anything else, a missing DLL or an older one, means the Python engine. The Python path for a part is
  deleted only after the native one passes its PC tests.
- **Old PCs first.** Direct3D 11 at feature level 10.0 or better, no runtime to install (static C runtime), and
  every part checked on the AMD E2-7110 and the 2013 all-in-one as well as the main PC.
- **Safety nets.** Warnings are errors, every check also runs under AddressSanitizer in CI, Release builds keep a
  `.pdb` (CI artifact) so crash dumps (`app/crash_dump.py`) name engine functions.

## Building and checking

```
cmake -S native/core -B build/core -A x64
cmake --build build/core --config Release
ctest --test-dir build/core -C Release --output-on-failure
set SPRIGCAST_CORE_DLL=%CD%\build\core\Release\sprigcore.dll
uv run pytest tests/test_sprigcore.py
```

`tests/test_native_compositor.py` draws the cases in `tests/native_cases.py` with the engine and compares them
with `tests/native_golden/`, today's compositor's drawing of the same cases. If a change to
`render/compositor.py` is meant, remake those pictures with `python tests/native_cases.py` and change the engine's
shader (`native/core/src/shader_compositor.h`) to match.

The checks draw with Windows' software renderer (WARP), so they give the same answer on every PC and in CI
(`.github/workflows/core.yml`). The installer build puts the DLL in `sprigcast\engine\`.

## The frame loop (`sc_engine_*`)

The engine draws on its own thread, paced by a high-resolution timer (Windows 10 1803+, else a 1 ms system
tick). Python's calls only copy work into a queue (pictures, looks, shapes, the snapshot, canvas sizes, previews);
the frame thread takes the whole queue at the start of a frame, so Python never waits on the graphics card and the
frame thread never waits on Python. A source uploaded twice before a frame only sends the newest picture. A frame
that runs long skips the frame times it missed (counted as `late`) instead of queueing them. With no canvases and
no previews the thread sleeps.

Previews are swap chains on the windows Python gives (flip model where Windows has it), drawn fitted with bars
and shown without waiting: a window that isn't ready skips that frame. `max_fps` lowers a preview's rate.

Crash gates in the loop: a C++ exception, an access violation or a lost graphics card in a frame drops that
device; the next frame builds a new one and hands it back everything kept (pictures uploaded with
`SC_UPLOAD_KEEP`, looks, shapes, the snapshot; video sends its next frame anyway). More than five rebuilds in a
minute, or no device after ten tries, and the engine stops and reports `faulted`, for Python to switch to its own
engine. A watchdog thread reports `hung` while one frame has taken more than 2 s. `sc_engine_debug_fault`
triggers each of these in the checks.

To try the gates on a real PC, start SprigCast with `SPRIGCAST_ENGINE_FAULT` set as well (never in normal use):
`kind@seconds` items separated by commas, each optionally `xN` to repeat. `3@60` loses the graphics card a minute
in (expect "rebuilding the device" then "the device is rebuilt" in the log, and the picture back within a
moment); `2@60` is an access violation; `4@60` a stuck frame (the log says so, then that it is going again);
`1@60x7` faults seven times running, so the engine stops and the log says the Python compositor draws again.
Recordings and streams should carry on through all of them.

## In the app (`SPRIGCAST_ENGINE=native`)

render/native_bridge.py: the render thread hands every new source picture to the native engine too, and a
snapshot of what is on air (blooms and every sprig layout, as their Live state shows them) whenever that or the
filters change. The native engine draws the canvases and copies preview-sized pictures back without waiting on
the card (`sc_engine_canvas_watch` / `_take`); the screens show those, so Python's compositor no longer draws a
canvas only to show it. Transitions (fade, slide, swipe, wipe, leaf, bloom) run in the native engine on its own
clock (`sc_engine_canvas_transition`; the outgoing bloom is in the snapshot while one runs), drawn by a port of
the Python transition shader and pixel-checked against it (tests/native_golden/t_*.png).

Recordings and streams take their frames from the native engine too (`sc_engine_output_set` / `_take`): each
frame it scales the canvas to every output size wanted (up to four a canvas) and turns it into NV12, BT.709
limited range, with a port of the Python NV12 shader and its four scaling filters (pixel-checked,
tests/native_golden/nv12_*.png), and copies it back without waiting on the card. Finished frames wait in a short
queue, each stamped with the performance counter when it was drawn (Python's `time.perf_counter()`, the clock
recorders time frames by), so a late Python tick doesn't lose a frame: it hands on every frame that queued up.
Past four waiting frames the oldest is dropped and counted.

SprigCast's own NVENC takes its frames on the card instead (`SC_OUTPUT_SHARE`): while the native engine draws,
a recorder opens its NVENC session on the engine's Direct3D device (`sc_engine_device`, render/gpu_share.py
`native_device`) and its output is drawn straight into one of five NV12 textures (R8 and R8G8 views of the
texture's two planes), which the encoder's thread registers once, maps and encodes
(output/nvenc_d3d11.py `encode_texture`, as for the OpenGL hand-off). Nothing is copied back and nothing
waits: the frame thread only draws, and a texture is the encoder's until its frame is let go
(`sc_engine_output_give_back`); with every texture out the frame is missed and counted, never overwritten.
Fallbacks: a card that can't draw into NV12 textures, an encoder that won't open on the device or a frame it
refuses (the engine rebuilt its device since) all send that output through memory as above. Other encoders
(x264, AMF, QSV, FFmpeg's NVENC) take the frames from memory. The encoder session itself is still Python's
ctypes one; it moves into the engine with recording and streaming (phase 4).

Views something records (the virtual camera, Instant replay keeping a camera) are the engine's too: a view of
the bloom on air rides on that canvas's outputs (the same picture, transitions included), any other view is
drawn in a free canvas slot of its own (`view:<name>`, after the canvases; when none is free, Python draws it).

Python's compositor still draws stinger and move transitions, Build's edited bloom, the Part editor, screenshots,
source record, full-size RGBA taps (a projector showing a view) and a canvas's outputs while it draws one of those (a frame older than one already handed on is never sent). If the native engine stops
(faulted), Python's compositor draws the previews and outputs again by itself.

## Video, GIF and sound files (`sc_media_*`)

With the switch on, a "Video or sound file" source is played by the engine (`native/core/src/media.cpp`): it is
`sources/media.py`'s player moved to C++ method for method (the same clock, read-ahead, loop, seek, speed, sleep
and wake rules, and the same packed 4:2:0 pictures, checked byte for byte against the Python player's). A thread
of the engine's own reads and decodes; `MediaSource` only passes controls on and hands the renderer and the mixer
what is ready. None of the decoding, packing, shrinking or resampling runs under Python's lock any more.

- **One FFmpeg.** The engine links no FFmpeg. It loads the libraries PyAV already brings (`site-packages/av.libs`)
  at run time and checks their major versions against the headers it was built with
  (`native/third_party/ffmpeg`, FFmpeg 9.0.2 for PyAV 19). If they can't be found or don't match, Python plays
  files as before and the log says why. When PyAV is updated to another FFmpeg, update those headers with it.
- **Crash gate.** A fault while reading or decoding (FFmpeg or our code) ends that file: its status becomes
  "can't play: the decoder faulted", the log says so, and SprigCast goes on. The Python player has no such gate.
  A file whose sound has no decoder plays silent instead of not at all.
- **Straight to the compositor.** While the engine also draws the canvases, a video's source is attached to its
  player (`sc_engine_source_media`): each frame the frame thread takes the picture due from the player itself and
  sends it to the card, so pictures go from the decoder thread to the screen without touching Python or its lock,
  at the engine's own frame times. Python's render thread asks for a picture only on ticks where its own
  compositor still draws something (a screenshot, a stinger, Build's edited bloom); each taker remembers the
  picture it took last, so whoever starts looking gets the one on screen at once. A sleeping or frozen source is
  detached and keeps the picture it has. The engine holds the player for as long as it is attached, so a source
  closing its player first is safe.
- **Stinger clips.** A stinger transition's clip is played by the same player (`render/stinger.py`
  `NativeStinger`): asked for RGBA pictures with straight alpha, shrunk to fit the canvas while decoding
  (`sc_media_set_picture`), which is what the transition draws. It waits paused with its first pictures decoded
  and starts its clock with the transition's first frame, so picture and sound stay in step. A clip that can't be
  played, or faults the decoder, falls back to a fade as before.
- **Instant replay.** The replay itself plays through the same player (`store/instant_replay.py`
  `NativePlayer`): B, G, R, A pictures fitted to the replay's box, from where the kept seconds start, at the
  replay's speed with its sound slowed to match. The wipes, the badge and the progress line are still drawn in
  Python on the picture it hands over.
- **Decoding on the graphics card.** A source with `hw_decode` is played by the engine too (`sc_media_set_decode`):
  its decoder decodes on the card through one Direct3D 11 device shared by every such player, and each picture
  is copied to memory as NV12 and packed as usual. A see-through WebM (libvpx), a codec the card lacks, or a PC
  without it decodes on the CPU, and the log says so once. Turning it on or off reopens the file with the other
  decoder.
- **Pictures stay on the card (zero-copy step 2).** The engine gives its own Direct3D device to the players
  (`sc::media_card`, made with video support where the card has it), so a `hw_decode` video decodes on it and,
  while only the engine takes its pictures, each one stays in the decoder's texture: `media_due` hands the
  texture slice to `sc::source_nv12`, which copies it on the card. No picture crosses memory. The decoder keeps
  `AHEAD_FRAMES + 3` spare surfaces for pictures waiting to be shown. Memory again, picture by picture, when
  Python asks for the same video (a projector) within a second (the one on screen is copied for it once), after a device rebuild until the file reopens
  on the new device, for odd sizes, and for good if the engine can't take a picture on the card (logged once).
- **NV12 sources (`SC_FMT_NV12`, `native/core/src/nv12_source.cpp`).** The compositor takes NV12 pictures:
  copied onto the card as NV12 and repacked there by a small shader into its I420 layout, line for line what an
  I420 upload of the same picture gives. A card that can't read NV12 textures in shaders gets them repacked in
  memory instead (logged once). `sc::source_nv12` takes a texture already on the card (a slice of a decoder's
  texture array): how the card's decoder hands pictures over.
- **Browser pages (`sc_engine_source_shared`).** A page runs in the browser helper process and writes its
  pictures into a shared memory block (`app/browser_helper.py` `FrameSlot`: two slots and a counter). On the
  native engine the frame thread opens that block itself and copies each new picture straight onto the card, so
  a page's pictures never pass through Python (`BrowserSource.engine_player` gives the block's name, and
  `engine_tick` only notes the first paint and the sound status). A picture overwritten while it was copied is
  taken again the next frame; a device rebuild takes the newest again. Pages drawn in SprigCast's own process
  (`SPRIGCAST_BROWSER_IN_PROCESS`) go through Python as before.
- **Browser pages on the card (`native/core/src/page_texture.cpp`, `app/browser_quick.py`).** With the native
  engine, SprigCast starts the helper with the engine's card (`SPRIGCAST_BROWSER_CARD`, its LUID from
  `sc_d3d_device_luid`; `NativeCanvases` tells `sources/browser.use_card`, and a helper already running starts
  again with the same pages). The helper then draws each page with Qt Quick on Direct3D 11 (`QuickPage`: a
  `WebEngineView` in a `QQuickRenderControl`) into a texture it makes on that card (`sc_page_target_*`), shared
  by name as `<block>-tex<n>` with a keyed mutex: key 0 the helper draws, key 1 a picture waits for the engine.
  The block's header says slot 2 ("on the card") and n; the engine's `sc::source_page` copies the picture on the
  card into the page's source (premultiplied). Chromium's picture never leaves the card. A resized page gets a
  new texture on the same device. While SprigCast reads a page itself (its own compositor, a projector),
  `RemoteHost.take` asks for memory pictures (`memory`, every 0.5 s) and the page writes them to the block as
  before for 2 s at a time. A page the engine stops taking for 1 s gets a new texture. If the card can't be used
  (no Qt Quick WebEngine, the texture can't be made, Qt Quick doesn't start on the device) the page is a widget
  page in memory, as before.
- **Checks:** `tests/test_native_media.py` plays real files made with PyAV: a video with sound, a GIF looping
  from memory, a see-through WebM, a sound file, seeking, pausing, speed, shrink, sleep and wake, broken and
  missing files, the crash gate, and (with the DLL) a video shown on an engine canvas with no picture sent by Python. The player needs no graphics card, so on Linux the test builds it alone
  with g++ and runs the same checks there; in CI they run against `sprigcore.dll`.

## Phase 4: recording and streaming (`native/core/src/writer.cpp`)

- **The writer (`sc_writer_*`, step 1).** A file or stream written by a thread of the engine's own: NV12
  pictures and 48 kHz stereo sound are queued (a picture that finds the queue full is dropped and counted), and
  the writer thread encodes them with any FFmpeg encoder (x264, NVENC, AMF, QSV), adds up to six AAC tracks,
  muxes and writes, never holding Python's lock. Opening, a stream's connecting included, happens on that
  thread; giving up interrupts FFmpeg's waiting. A fault inside FFmpeg ends that output (state "failed", with
  why), never the app. FFmpeg's table is shared with the player (`src/ffmpeg.h`); like the player the writer
  builds on Linux, so `tests/test_native_writer.py` writes and reads back real files there and in CI.
- **Recordings through the writer (step 2, `output/recorder.py` `uses_writer`).** On the native engine a
  recording to a file whose video isn't SprigCast's own NVENC (x264, AMF, Quick Sync, NVENC with B-frames or
  lookahead) is written by the engine's writer: the recorder's thread only hands each picture (NV12, converted on
  the card or from RGBA) and the sound over. Muxer options (MP4 in fragments), track titles and B-frames come
  with it (interface 17). The writer's counts become the recorder's (frames, drops, bytes, time writing); a full
  writer queue makes the recorder's own queue wait, so easing off and drops work as before. If the writer can't
  start, the recording goes through PyAV as before. Streams and replay buffers still go through PyAV.
  `SPRIGCAST_NO_WRITER=1` turns it off.
- **Outputs linked to a writer (step 3, `sc_engine_output_link`, interface 18).** Once such a recording's writer
  is open and Python's own frames for it are written, the render thread links the canvas's output to the writer
  (`render/native_bridge.py` `link_taps`): from then on the engine's frame thread hands every finished frame to
  the writer itself, numbered by when it was drawn exactly as the recorder numbers them (a draw a little late
  still gets the next number, spare draws of a faster canvas are skipped, a hole up to 5 s is filled with the
  next picture as one queue entry, `sc_writer_video_span`). No frame passes through Python and the recorder's
  thread only hands sound over. The writer never goes back in time (`SC_WRITER_LATE`), so Python and the engine
  can take turns: a stinger or move transition, a pause, the end of the recording or a fault unlinks it, and
  Python's frames go on after the last frame number the writer took. The engine holds a share of the writer, so
  a writer closed while linked just refuses frames and the link ends by itself. Other outputs of the same size
  still take the frames (the link keeps them queued). `SPRIGCAST_NO_LINK=1` turns linking off.
- **Frames on the card into the writer (step 4, interface 19).** A recording on SprigCast's own NVENC goes
  through the writer too: it opens on the engine's Direct3D 11 device (`sc_writer_config.d3d11_device`) with
  FFmpeg's NVENC taking D3D11 textures, and the canvas's on-card output is linked to it. The frame thread then
  hands each finished texture to the writer (`writer_feed_texture`): no copy through memory and no Python per
  frame. The texture goes back to the engine's pool once the encoder is done with it; the writer holds a
  reference to every texture it has seen until the encoder is closed, because NVENC remembers registered
  textures by pointer (the #521 crash). Only a writer on the card takes an on-card output, and only alone: a
  stream of the same size on SprigCast's own NVENC sharing it, a refused link or a rebuilt engine device
  (`Recorder.card_lost`) send the recording memory frames instead, which the writer copies up to its card. If
  the writer can't open there (no NVENC, the device mid-rebuild) the recording goes through PyAV with
  SprigCast's own NVENC as before. `SPRIGCAST_NO_CARD_WRITER=1` keeps those recordings in Python. 
- **An encoder that fails once starts again (interface 20).** If the writer's video encoder fails mid-file (a
  graphics driver reset, a lost card), the old encoder is flushed and let go of entirely (its frames on the card,
  their textures, its FFmpeg contexts) and a new one carries on from memory into the same file; the engine's link
  to the card ends and the recording takes memory frames (`sc_writer_stats.on_card` 0, `Recorder.card_lost`).
  Once per writer: a second failure ends the file cleanly (`recoveries`). Writing errors (disk full) never
  restart anything. `SPRIGCAST_WRITER_FAULT=20` (or `10,30`) fails the encoder at those pictures for tests;
  `tests/test_native_writer.py` runs it, also under AddressSanitizer locally.
- **Shared encoders on the writer: packets mode (step 5, interface 21).** Recordings that share a canvas's
  encoder with clips, clips themselves, Instant replay's buffer and every stream (`output/shared.py`
  `CanvasEncoder`, `output/shared_stream.py` `SharedStreamEncoder`, `output/replay.py` `ReplayBuffer`) are encoded
  by a writer in packets mode (`sc_writer_config.packets`): it encodes on its own thread, writes only a small NUT
  file with the streams and their codec headers as the encoders made them (a placeholder byte a stream so a
  demuxer opens it; read once for the templates that clip files and stream muxers copy), and hands every packet
  back (`sc_writer_packet_take`). Python keeps them for clips, writes the recording file and sends the streams
  exactly as it did PyAV's packets, so reconnects, delay, backlog skipping and clips borrowing the stream's encoder
  are unchanged. A keyframe asked for (a link joining, a file starting) goes to the next picture
  (`sc_writer_keyframe`); a stream's auto-pilot steers the bitrate (`sc_writer_set_bitrate`). Linking works as for
  recordings (memory or the card), so a stream's frames don't pass through Python either. With the native engine
  a lone stream gets a shared encoder too (`output/stream.py` `engine_encodes_streams`). If the video encoder fails
  once, the new one puts its codec headers in its keyframes, so the streams' muxers go on. `SPRIGCAST_NO_PACKET_WRITER=1`
  keeps these encoders in PyAV (and a lone stream on its own `Streamer`). Tests: `tests/test_packet_writer.py`
  runs the shared-encoder and stream tests on the writer.
- Next: Enhanced Broadcasting's tracks and the virtual camera fed by the engine.

## Phases

Ordered by how much heavy-run stalling each removes. The plan: https://claude.ai/artifact/3zTFrUsXtNhCzszAczL7r7

| Phase | What moves | State |
| --- | --- | --- |
| 1 | Groundwork: build, checks, Python link, engine switch, first canvas | Done |
| 2 | Frozen scene snapshot; Direct3D 11 compositor and previews, feeding NVENC, AMF and QSV on the graphics card | In progress: compositor, snapshot, transitions and NV12 drawn like today's (pixel-checked); behind the switch the engine's own frame loop draws previews and makes recording and stream frames. NVENC takes the engine's frames on the card. Next: the PC test with the switch on |
| 3 | Media and GIF decode and reformatting, browser pages as shared textures | In progress: the engine plays video, GIF and sound files itself behind the switch (`sc_media_*`, below). and its compositor takes their pictures straight from the player. Stinger clips and instant replays play through it too, and it decodes on the graphics card when asked. Pictures from the card's decoder stay on the card; browser pages go from the helper's memory straight to the engine |
| 4 | Recording, replay (with its sound writing), streaming, Enhanced Broadcasting, virtual camera | |
| 5 | Sound: devices, mixer, filters, sync | |
| 6 | Per-frame plugin painting (store plugins native, your Python plugins in a helper process) | |
| 7 | Capture and camera inside the engine | |
| 8 | Old Python engine and its packages removed | |
| 9 | Only if needed: Control API into the engine, screens to Qt C++ | |
