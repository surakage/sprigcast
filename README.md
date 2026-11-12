# SprigCast

A lightweight, open-source streaming app for Windows, in the spirit of OBS, Meld and Streamlabs.
SprigCast renders a **horizontal** and a **vertical** canvas at the same time, and each output
(Twitch, TikTok, a recording) streams from the canvas you choose.

Status: all milestones of the build guide (M0 to M11) are in: a GPU canvas with live previews,
scenes built from color, image and text sources, display and window capture, webcams, mouse editing
in the previews, an audio mixer, recording, live streaming, transitions, hotkeys, stats, filters, plugins and a dark theme.

## Requirements

- Windows 10 or 11, 64-bit
- An NVIDIA GPU with a recent driver (other GPUs work for the previews; NVENC is used for streaming later)
- [uv](https://docs.astral.sh/uv/), Git, and the FFmpeg CLI for the test stream:

```powershell
winget install astral-sh.uv Git.Git Gyan.FFmpeg
```

## Run it

```powershell
git clone https://github.com/surakage/sprigcast
cd sprigcast
uv sync                 # installs Python 3.12 and every library into .venv
uv run sprigcast-check  # M0: confirms PyAV can encode on your NVIDIA GPU
uv run sprigcast        # opens the app
```

To prove your stream key and network work before streaming from the app itself:

```powershell
uv run sprigcast-teststream --service twitch            # 1280x720 test pattern and tone
uv run sprigcast-teststream --service twitch --vertical # 720x1280
```

The key is typed at a hidden prompt and is never saved.

## Using the app

The layout follows OBS: the previews on top, and along the bottom the *Scenes*, *Sources*, *Audio Mixer*,
*Scene Transitions* and *Controls* docks. Every dock can be dragged to another edge, stacked into tabs,
floated as its own window or closed; the *Docks* menu brings closed ones back, *Lock docks* stops accidental
drags and *Reset layout* puts everything back. Under each list is a row of small buttons:
**+** add, **−** remove, **⚙** settings, **▲ ▼** reorder. Right-click a scene or source for the same actions.

- **Previews**: the horizontal canvas (1920x1080) on the left, the vertical canvas (1080x1920) on the right.
  Edit the scene shown on each canvas directly with the mouse:
  - Click an item to select it, then drag to move it. It snaps to the canvas edges and centre and to other
    sources; *Settings → General → Source alignment snapping* changes this. Hold Ctrl to stop snapping.
  - Drag a corner handle to resize. Hold Alt while dragging a corner to crop instead.
  - Arrow keys nudge the selected item 1 px, or 10 px with Shift.
  - Right-click a background and pick *Lock*, so clicks pass through them to the items on top.
- **Scenes dock**: scenes grouped by canvas. Select one to edit it; double-click or press *▶ Show* to put it on its canvas.
  A scene linked to another switches together with it, so one click changes both canvases.
- **Blooms and sprigs**: SprigCast has two kinds of scene. A *bloom* is what goes on air (Starting soon,
  Gaming, Be right back). A *sprig* is a reusable piece, like a webcam frame, an alerts box or a brand bar,
  that you drop into blooms. Build a sprig once and use it on both canvases: change it, and every bloom
  using it changes too.
  - Scenes **+ → New sprig…** makes one: give it a name, pick its shape, and tick *Add it to "…" now* to drop
    it straight into the bloom you're working on. Sprigs are listed under *Sprigs* in the Scenes dock with where
    they're used, for example "❧ Webcam frame (horizontal + vertical · used in 2)". Right-click a sprig there
    to add it to the bloom on either canvas.
  - **Turn into a sprig**: already built something in a bloom? Select its sources in the Sources dock
    (Ctrl-click or Shift-click for several), right-click and pick *Turn into a sprig…*. They move into a new
    sprig right where they were, so nothing moves on screen, and the sprig is ready to reuse.
  - Select a sprig to open it in the *sprig editor* in place of the canvases. The see-through areas show as a
    checkerboard, and the header lists every bloom that uses it. *Done: back to the canvases* closes it.
  - **One sprig, two shapes.** A sprig starts with a layout for the canvas you picked. The buttons at the top of
    the sprig editor switch layouts: click *+ Vertical layout* (or *+ Horizontal layout*) and SprigCast copies
    the layout over, scaled to fit and kept in the same spot (a top-right webcam frame stays top-right). Then
    arrange it however that shape needs. Vertical blooms use the vertical layout and horizontal blooms the
    horizontal one. Without a second layout, both canvases use the one layout, scaled to fit.
    *Remove this layout* goes back to one layout. The Sprigs list says which layouts a sprig has.
  - **Edit in place**: double-click a sprig in a bloom's preview (or double-click it in the Sources dock, or
    right-click → *Edit sprig in place*). The bloom stays on screen, dimmed, with the sprig drawn bright on top,
    and the mouse, arrow keys and Sources dock now work on the sprig's own items. Changes land in the sprig,
    so they appear everywhere it's used. Press Esc or double-click outside the sprig to go back to the bloom.
    You edit the layout for that canvas, so editing in the vertical bloom changes the sprig's vertical layout.
    A rotated placement opens the sprig editor instead.
  - **Just in this bloom**: right-click a sprig in the Sources dock → *Just in this bloom* and untick
    items to hide them in that bloom only, e.g. the "Live now" badge on your BRB webcam frame. The sprig
    and its other placements keep showing them, and edits to the sprig still reach every bloom. The source
    row says "N hidden here"; *Show everything* undoes the tweaks.
  - **Live states**: right-click a sprig in Scenes → *Live states…* (or *Live states…* in the sprig editor)
    to give it looks like "Hidden" and "Follower alert", each ticking which items show. In Live, each sprig
    with states gets a row of buttons (plus *As built*); a click fades the change in on every bloom that uses
    it. Hotkeys can switch states too (Settings → Hotkeys).
  - Sources **+ → Sprig** places a sprig in the selected bloom (or inside another sprig). Sprigs appear as
    "❧ name [sprig]" in the Sources dock, and hovering one in a preview says so. Move, resize and crop it
    like any source. SprigCast won't let a sprig end up inside itself.
  - A sprig is drawn once per frame however many blooms and canvases use it. Removing a sprig warns you where
    it's used. Sprigs never appear in Live mode, which lists blooms only.
- **Scene Transitions dock**: *Fade* (crossfade, 300 ms by default) or *Cut*.
- **Hotkeys** (*Settings → Hotkeys*): give any scene, *Go live* or *Start recording* a key combo such as Ctrl+Alt+1.
  They work while a game has focus. Pick combos your games don't use.
- **Stats** (*Docks → Stats*, Ctrl+Shift+S): render fps, CPU and memory, free disk space, and for every stream
  and recording the frames sent and dropped, encoding time per frame, and time spent waiting on the network.
- **Sources dock**: **+** adds a source to the selected scene (display, window, camera, image, color, text,
  or a plugin). Tick to show or hide, **▲ ▼** change which item draws on top, double-click or **⚙** opens its
  settings, *Filters* opens the source's filters, and *Transform* opens the Properties dock.
  - *Display capture* captures a whole monitor, cursor included, and fits it to the canvas.
  - *Window capture* captures one window even when other windows cover it. SprigCast remembers the window's
    program and title, so a game that restarts or a browser that changes tabs is found again automatically.
  - *Camera* adds a webcam or capture card as a facecam in the bottom-right corner. Pick the device,
    mode (for example 1920x1080 at 30 fps) and mirroring. Cameras are remembered by device, so
    replugging into another USB port is fine, and an unplugged camera reconnects on its own.
- **Audio Mixer dock**: a level meter for every audio source plus the master mix.
  - **+ → Microphone** adds a mic, **+ → Desktop audio** adds what your speakers or headphones play (WASAPI loopback).
    Both default to whatever device Windows uses, so switching headsets in Windows just works.
  - Drag a slider to set the volume (-60 to +12 dB), click *Mute* to silence it,
    click a source's name, then **⚙** pins it to one device or **−** removes it.
  - Audio sources belong to the whole collection, not one scene, so they keep playing when you switch scenes.
  - **⇅** switches between rows and side-by-side vertical strips (remembered).
  - **🎧 Monitor**: hear a source on your speakers or headphones (default playback device). Off or on, it
    always goes to the stream and recording. Desktop audio already plays on your speakers, so it has none.
  - **Hide**: right-click a source → *Hide from mixer*. It keeps playing; "1 hidden · show" brings it back.
  - **Ducking**: when you talk, everything else glides down so your voice stays clear, then comes back a
    moment after you stop. Turn it on with *Ducking* under the mixer and set how much quieter, how sensitive,
    and how fast. Mics lead by default; right-click a source to change that. While it's working, the
    master line shows "Ducking −12 dB".
- **Filters** (*Filters* in the Sources or Audio Mixer dock, or right-click a source → *Filters…*), like OBS:
  **+** adds a filter, the tick switches it off without removing it, **▲ ▼** reorder, and the sliders apply live.
  - Video: *Color Correction* (gamma, contrast, brightness, saturation, hue shift, opacity, color multiply),
    *Chroma Key* (green screen: key color, similarity, smoothness, spill reduction), *Luma Key*, *Sharpen*
    and *Scroll*. They run on the GPU inside the compositor, so they cost next to nothing. Sources with
    filters show "✦ filters" in the Sources list.
  - Audio: *Noise Gate*, *Compressor*, *Gain* and *Limiter*, run top to bottom on the source before the mixer.
- **Settings** (the *Settings* button in Controls, the *Settings* menu, or Ctrl+,): one window with tabs down the left.
  - *General*: theme (Slothbloom dark or the plain Windows look), snapping (on/off, sensitivity, canvas edges,
    other sources, centre lines), and whether to ask before ending a stream or stopping a recording.
  - *Stream*, *Output*, *Hotkeys*: described below.
  - *Audio*: which device each mic and desktop audio source uses ("Default" follows Windows).
  - *Video*: size, frame rate and on/off for each canvas. These apply the next time SprigCast starts.
  - *Plugins*: which plugins loaded (and why any failed), what they added, and *Open plugins folder*.
  - Stream settings are locked while you're live, and recording settings while you record.
- **Live and Build modes** (the buttons at the top left, or Ctrl+1 / Ctrl+2). *Build* is the editing layout above, with a Horizontal and a Vertical tab: each shows that canvas with its own blooms and sources. On the Vertical tab, 🔗 Link (or right-click a bloom) picks the horizontal bloom it switches together with. The Vertical tab also shows **safe zones** (top right: All platforms, TikTok, YouTube Shorts, Instagram Reels or Off): tinted boxes where those apps draw their buttons and captions, and a green dashed box for the clear middle. They're an approximate guide and never appear on the stream. **Folders** keep blooms tidy: + → New folder (e.g. "Main Scenes"), then drag blooms into it (or right-click a bloom → Move to folder). Drag blooms up and down to reorder them. Folders get a colour and fold up; removing a folder keeps its blooms. **Live shows only your Live folder**: the first folder you make on a canvas is it (marked "· Live"), and right-click a folder → Show in Live changes which. Every bloom you put in it becomes a Live button. With no Live folder, Live lists every bloom.
- **Studio mode** (tick it in the Live dock): clicking a scene lines it up, with its linked scenes, in a *Next* preview above each canvas (dashed yellow buttons), and **Go ▶** (or its hotkey) sends them all on air together with the transition. Untick it to switch instantly again.
- **Replay buffer** (Controls dock): turn it on and **Save replay** (or its hotkey) writes the last few seconds of each recorded canvas into your recordings folder. Set the length in Settings → Output → Recording.
- **Stream doctor** (🩺 in Controls, or Ctrl+Shift+D): listens to your mics for a few seconds, then checks stream keys, how your last stream's upload went, the GPU and encoder, audio levels and sources, and says in one sentence what to fix. Going live runs a quick check and only asks first if something is seriously wrong.
- **Auto-pilot bitrate** (on by default, per destination in its ⚙): when your upload can't keep up, the bitrate steps down instead of frames being dropped, then climbs back to what you set once the upload recovers. The Destinations card and Stats show when it is lowered.
  *Live* is for running the show: big buttons for each canvas's blooms (linked scenes, marked ⇄, switch together),
  a large on-air clock, the Audio Mixer, Destinations and Controls; the previews can't be edited by a stray
  click. Each mode remembers its own dock layout, and SprigCast reopens in the mode you left it in.
- **Destinations dock**: a card for every place you stream to, showing what it gets (canvas, size, fps,
  bitrate, or Enhanced), whether it's ready ("Needs a stream key", "Needs a server URL"), and while live its
  bitrate, drops and a health dot. *On* includes it in the next *Go live*; ⚙ opens its settings, and
  *+ Add destination* adds a new one.
- **Controls dock**: *Go live*, *Start recording* and *Settings*. The ticks choose which canvases record.
  - *Settings → Stream* sets up where you stream: Twitch, YouTube, Kick, TikTok or any RTMP server. Picking a service
    fills in its preset (Twitch: horizontal 1920×1080 60 fps 6000 kbps; TikTok: vertical 720×1280 30 fps
    4000 kbps), and you can change the canvas, resolution, frame rate and bitrate per output.
    The page is grouped like OBS: *Destination*, *Video*, *Multitrack Video* (Twitch) and *Advanced options*.
    Unless *Ignore streaming service setting recommendations* is ticked, the bitrate is capped at what the
    service accepts (Twitch: 6000 kbps). Stream keys are saved in Windows Credential Manager, never in `scenes.json`.
  - Twitch: *Connect Twitch…* signs in with a code at twitch.tv/activate (no password goes through
    SprigCast) and fills in your stream key. With *Get the stream key from my Twitch account* ticked,
    SprigCast asks Twitch for the current key each time you go live, so resetting it on Twitch just works;
    if Twitch can't be reached it uses the saved key. Only the "read stream key" permission is requested.
  - Twitch *Enhanced Broadcasting*: tick it and SprigCast asks Twitch which qualities suit your PC
    (it sends your GPU, CPU, memory and Windows version, like OBS does), then encodes them all at once on
    the GPU and sends them over one connection, so viewers get quality options without Twitch transcoding.
    *Maximum streaming bandwidth* and *Maximum video tracks* (Auto or a limit) cap what Twitch picks, and
    *Additional canvas* adds Twitch's dual-format vertical stream from the vertical canvas.
    If Twitch can't set it up, SprigCast offers to go live with a single quality instead.
  - Twitch has a *Bandwidth test* option: the stream reaches Twitch so you can check it, but viewers never see it.
  - *Settings → Output* has the encoder settings, like OBS: rate control (CBR, VBR or constant quality),
    keyframe interval, preset P1 to P7, tuning, multipass, profile, look-ahead, adaptive quantization,
    B-frames and audio bitrate, once for streaming and once for recording. The defaults match what SprigCast
    always used (CBR, P5, high quality, 2 s keyframes). A banner says when Enhanced Broadcasting is
    choosing the Twitch output's settings instead.
  - Tick the places to stream to and press *Go live*. Each one gets its own NVENC encoder, so Twitch can take the horizontal canvas while TikTok takes the vertical one.
    Each output shows a health dot: green is fine, yellow is falling behind, red is dropping frames or
    disconnected, with the reason (upload too slow for the bitrate, or encoder overloaded).
    A dropped connection reconnects on its own.
  - *Start recording* writes each ticked canvas to its own file at the same time,
    for example `SprigCast 2026-11-10 19-30-05 horizontal.mkv` and `... vertical.mkv`, both with the mixed audio.
    *Settings → Output → Recording* picks the folder (default `Videos\SprigCast`), bitrate and MKV or MP4; MKV stays
    playable if the PC crashes. *File → Open recordings folder* opens your recordings. Streaming and recording can run together.
- **Text sources** (**+ → Text**): titles and labels with a font, size, colour and outline.
  `{time}`, `{time12}` and `{date}` in the text show a live clock. Set *File* to a text file and its
  contents are shown instead and follow its changes, handy for "now playing" tools.
- **Properties dock** (hidden until you press *Transform* or right-click → *Transform…*): name, position,
  scale and rotation of the selected item, *Fit to canvas*, *Locked*, and a status line (for example "waiting for target" while a game is closed).

Capture uses Windows Graphics Capture. SprigCast hides the yellow capture border on Windows 11;
Windows 10 always shows it.

The window remembers its size and where you put the docks. SprigCast uses a dark Slothbloom theme;
pick *System* under *Settings → General* to keep the plain Windows look.

Scenes save automatically to `scenes.json` in your AppData folder
(*File → Show scenes file location* shows the exact path).

## Plugins

A plugin is a Python file that adds a new source type. *File → Open plugins folder* opens the folder
(`%LOCALAPPDATA%\Slothbloom\SprigCast\plugins`). Every `.py` file there loads at startup; files whose
name starts with `_` are skipped. Plugin sources appear under *Sources → + → Plugins*.

The first launch puts an example there, `countdown_example.py`, a "Starting in 4:59" countdown built on
the text source. A minimal plugin looks like this:

```python
from sprigcast.core.source import register
from sprigcast.sources.text import TextSource

@register("shout")
class ShoutSource(TextSource):
    label = "Shout"                                   # name in Sources → + → Plugins
    defaults = {**TextSource.defaults, "text": "hi"}  # fields in its Settings dialog

    def _current_text(self):
        return self.settings.get("text", "").upper() + "!"
```

For anything other than text, subclass `sprigcast.core.source.Source` and implement `size` and
`video_frame()` (return an RGBA NumPy array when the picture changes, `None` otherwise) or
`audio_samples()`. Installed packages can also register plugins through a `sprigcast.plugins` entry point.
Plugins can add audio filters too, with `sprigcast.core.filters.register_filter` (see the docstring there).
A plugin that fails to load is named in the status bar and in *Settings → Plugins*, and skipped. Plugins run with the same rights
as SprigCast, so only install ones you trust.

## Project layout

```
src/sprigcast/
  core/      scene collection model (canvases, sources, scenes, outputs), Source base class, filters, mailbox, events
  render/    moderngl compositor and the render engine thread
  sources/   built-in sources: color, image, text, display and window capture, camera, mic and desktop audio, sprigs
  audio/     mixer that combines audio sources into one 48 kHz stereo stream
  output/    recorder (MKV/MP4) and RTMP streamer: H.264 (NVENC or x264) and AAC, stream keys via keyring
  app/       PySide6 main window, canvas previews, dialogs and theme
  plugins.py loads source plugins from the plugins folder and entry points
  tools/     sprigcast-check and sprigcast-teststream
  data/      default scene collection used on first run
tests/       pytest suite (GPU tests skip when no OpenGL context is available)
```

Run the tests with `uv run pytest`.

## Roadmap

All planned milestones are done. Ideas for later: more transition types (slide, stinger), filters
such as chroma key and colour correction, and scene switching from chat or a Stream Deck.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
