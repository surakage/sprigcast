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

### Live screen

- Live gets its own screen: a big stage, then Blooms, Sound and Transitions as cards along the bottom (#119, #160, #196).
- Chat sits on the right beside the stage, with one Go live card under it for Record, Clips and Destinations (#174, #177).
- Destinations have on/off switches and short names; editing one no longer adds a new one (#124, #125, #126).
- Bloom buttons show their hotkey and a small picture (#183).
- A "You're live!" card with the sloth when a stream starts (#163, #187-#195).
- Right-click menus on the stage, bloom buttons and destination cards (#99), and Full screen on another monitor (#131).
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

### Get started

- A two-step Get started card on a new install (and any time from Tools → Get started): what you make, go live or record,
  which shape, where you go live (Twitch, YouTube, Kick, TikTok, X), then your camera, capture card and mic (PC sound is split by type for everyone).
  It names a garden after the show and a path after where it goes, adds a place per pick, and makes the blooms that fit.
  A capture card fills the Gaming bloom and brings its own sound.
- X is a streaming service choice.

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
- Turning the virtual camera on sets it up the first time (one Windows prompt, no trip to Repair), checks Windows'
  camera privacy first and, when it blocks desktop apps, says so and offers to open the right Settings page.
- A sprig nothing has shown for a few seconds lets go of its full-canvas texture (they stayed until SprigCast closed),
  and removing a plugin stops its sources straight away instead of at the next restart. Stopping a recording or
  stream logs the app's memory, so long sessions can be compared.
- The first frame is drawn before the frame clock starts, so start-up no longer has a one-second hitch.
- App sound's once-a-second app check no longer leaves objects behind. Over a night they piled up into millions, so
  Python's clean-up pauses grew from 36 ms to 289 ms (16 missed frames each) and memory crept up.

### Sources and effects

- Transitions: slide, swipe, wipe, leaf, bloom and stinger clips (#145), a transition for particular bloom switches (#151),
  and a Transitions card with a pop-up for the details (#149, #162, #164, #166).
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
- Death count store plugin: a mossy gravestone grows out of the ground, flips to the new count and sinks back. Hotkeys, right-click and `deaths.*` API actions for Streamer.bot; the count survives a restart.

### Chat

- One Chat panel that mixes Twitch, YouTube and Kick from just channel names (#144, #148, #150).
- TikTok chat through TikFinity or directly (#181).
- Show chat on your stream, and reply on Twitch (#181).
- Fixed YouTube live lookup and Kick's chat key (#185).

### Gardens and paths

- Gardens: whole sets of blooms, sprigs and sources for different shows (OBS's scene collections). Paths: where and how you go out, with places, encoder and recording settings (OBS's profiles), e.g. Twitch only, Multistream, Recording only. Each has its own menu: New, Duplicate, Rename, Remove, Import, Export, and Gardens has Missing files. Your setup becomes the "Main" garden and the "Main" path. The window title shows which are in use. Works through the control API and the OBS door, so Streamer.bot can switch them.
- Import scene collections from OBS, Streamlabs and Meld into gardens: Gardens → Import… takes their files, and Import from OBS / Import from Streamlabs lists the ones on this PC. Scenes nothing else uses become blooms, nested scenes and groups become sprigs, divider scenes become folders (anything not under one is sorted by what it holds: Main, Browser, Media, Audio, Capture or Nested Scenes), a vertical canvas (OBS 31 / Aitum Vertical, Streamlabs dual output) comes along with "Name (Vertical)" linked to "Name", and positions, crops, bounds, volumes, filters and stingers carry over. A short list says what to check afterwards. Tested on a real 200-source OBS collection and a real Meld Studio Session.json (Spark Elements become browser pages at their Meld size, Text and Groups come along, the vertical canvas links to the main one, Mic/Game/Music tracks become sounds with Voice Clarity as noise suppression and a sidechain compressor as Ducking). Streamlabs: checked against 33 real scene collections (themes and a hand-built one); newer collections' vertical canvas lands right (Streamlabs keeps it as if 1080x1920), a scene shown on the vertical canvas uses its vertical items, Import from Streamlabs lists them by name A-Z and skips deleted ones, and Streamlabs .overlay files (File → Export Overlay) import with their videos, pictures and stinger, unpacked to Documents\SprigCast\Media; widgets come over as pages to paste your dashboard address into, and the notes group what to check.
- Imports don't double your sound: OBS's mic or desktop sound on the default device (or its only mic) becomes your Mic or Desktop audio with OBS's volume, mute and filters; a sound whose device isn't on this PC starts muted. Sounds play only in the blooms that held them in OBS (Plays in). OBS scene and mute hotkeys come along, and text is measured so centred titles land in place.
- A chip in the header shows the garden and path in use and switches them; switching garden while live asks first. Hotkeys can switch gardens and paths, and they work from every garden. Missing files has Look in a folder… to find everything that moved at once. A broken or missing garden file at start no longer resets your path.
- Gardens → Export with files… packs a garden with every picture, video, page (with its folder), look, stinger and slide show folder it uses into one .sprigpack file, so it moves to another PC complete. Import… takes packs: the files unpack to Documents\SprigCast\Media\<garden> and the garden points at them. Unpacking never writes outside that folder, checks free space and refuses damaged or oversized packs.
- OBS Composite Blur filters (e.g. THSV Pixelate) come over as SprigCast's Blur, in Pixelate or Blur mode, and an OBS compressor with a sidechain comes over as Ducking.
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

### Tests

- The test suite passes on Windows 11 too (#210, #214).
- Recordings no longer skip their second frame, which also fixed a flaky test (#216, #218).

## 0.1.0

The build guide, milestones M0 to M11: GPU canvases for horizontal and vertical at once, blooms and sprigs, display,
window and camera capture, image, color and text sources, audio mixer, recording, streaming to several places,
transitions, hotkeys, stats, filters, plugins and a dark theme.
