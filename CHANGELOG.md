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

### Build screen

- Build gets its own screen: a library of blooms and sprigs on the left, what's inside on the right, and a folding inspector (#120, #173, #186).
- Edit a bloom without putting it on air, drag to reorder, nest sprigs inside sprigs (#121).
- Side handles, Alt to crop, zoom and pan, free shape (#122, #123).
- Horizontal and Vertical tabs, one shared Sound list, undo and redo, a smart + Add button (#186).
- Starter kit: pick what you want and SprigCast builds the first blooms for you (#186, #188, #191, #194).
- Bloom pictures, drag a sprig in, source icons, and "used in" links (#175).

### Settings

- A new look: a pill menu, titled pages and soft cards (#165, #170, #171).
- Output page: every encoder your PC has (NVENC, AMD, Intel Quick Sync, H.264, HEVC and AV1, x264), replay and clip settings (#184).
- Video page: base size and output size are separate, with a choice of downscale filter (#184).
- Stream page with padded cards for each place you go live (#126, #127).
- Menu bar: File, Edit, View, Panels, Tools, Help (#137). Docks are replaced by Panels (#140, #156).
- Audio page in tabs: Devices, Tracks, Sounds and Bitrate, with one switch for global devices (#203, #209, #212).
- Hotkeys page: grouped cards, search, and keys for sounds, clips, Studio mode and Build/Live (#204).
- Advanced page: process priority, recording file names, MP4 copies, colour, and a stream delay (#202, #206).
  While the delay runs, the Go live card says "Going out in N s" instead of Connecting (#215).

### Sound

- Sound card groups sounds into Voice, Game, Alerts, Music and Other, and folds the quiet ones away (#130, #135, #155, #172).
- Tune page for each sound, with a filters pop-up and five new audio filters (#130, #135, #158).
- "Mic first" ducking: other sounds dip while you talk (#135).
- Named tracks: Stream, Recording, Voice and Clips, chosen per sound (#154).
- Tracks 1 to 5, including a VOD-safe track, and each recording picks its tracks (#203).
- Source record: record one sound or source on its own (#209).
- Browser sources can play sound (#147).
- Cheaper level meters (#199).

### Streaming, recording and encoding

- SprigCast's own NVENC encoder, with no B-frames by default (#107, #108, #109).
- The graphics card's encoder on AMD and Intel PCs too (#95).
- Streams that want the same picture share one encoder (#116), and recording keeps its own encoder beside a stream (#114).
- Built-in clips: 15, 30, 45 or 60 seconds, horizontal, vertical or both (#152).
- The stream summary shows the bitrate actually sent (#118, #136).
- Logbook: daily logs, a report for each stream, and crash reports in Documents\SprigCast\Logs (#129, #134, #138).

### Optimize wizard

- First-run wizard that checks your PC and upload and suggests settings (#110, #112, #113, #141).
- Upload check with a hidden Twitch bandwidth test (#115), and it remembers the heaviest stream that ran clean (#189).
- Light settings for a slower PC, offered once (#96, #101).

### Lighter on your PC

- Frames come off the graphics card without the CPU waiting, which halved the time to draw a frame (#167).
- Less memory: a smaller GPU-usage reader (#91), tiny textures for solid colors (#93), no full-size previews at start (#94),
  a lighter font engine (#100), and no hidden browser for empty browser sources (#193, #197).
- `sprigcast-memcheck` shows where memory and CPU go, and runs an hour-long soak test (#90, #92, #97, #98, #102, #111).

### Sources and effects

- Transitions: slide, swipe, wipe, leaf, bloom and stinger clips (#145), a transition for particular bloom switches (#151),
  and a Transitions card with a pop-up for the details (#149, #162, #164, #166).
- Browser source on Chromium 140 (#139), and right-click Interact to click around in the page (#153).
- Color looks (LUTs) and shapes for any source (#168).
- Media source: video and sound files, with play and pause from menus, hotkeys and the API (#198).
- SprigCast Camera: a Windows 11 virtual camera, set up in Output → Virtual camera (#208).

### Chat

- One Chat panel that mixes Twitch, YouTube and Kick from just channel names (#144, #148, #150).
- TikTok chat through TikFinity or directly (#181).
- Show chat on your stream, and reply on Twitch (#181).
- Fixed YouTube live lookup and Kick's chat key (#185).

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

### Tests

- The test suite passes on Windows 11 too (#210, #214).

## 0.1.0

The build guide, milestones M0 to M11: GPU canvases for horizontal and vertical at once, blooms and sprigs, display,
window and camera capture, image, color and text sources, audio mixer, recording, streaming to several places,
transitions, hotkeys, stats, filters, plugins and a dark theme.
