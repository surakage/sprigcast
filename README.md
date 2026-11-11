# SprigCast

A lightweight, open-source streaming app for Windows, in the spirit of OBS, Meld and Streamlabs.
SprigCast renders a **horizontal** and a **vertical** canvas at the same time, and each output
(Twitch, TikTok, a recording) streams from the canvas you choose.

Status: all milestones of the build guide (M0 to M11) are in: a GPU canvas with live previews,
scenes built from color, image and text sources, display and window capture, webcams, mouse editing
in the previews, an audio mixer, recording, live streaming, transitions, hotkeys, stats, plugins and a dark theme.

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

- **Previews**: the horizontal canvas (1920x1080) on the left, the vertical canvas (1080x1920) on the right.
  Edit the scene shown on each canvas directly with the mouse:
  - Click an item to select it, then drag to move it. Edges and centre snap to the canvas; hold Ctrl to stop snapping.
  - Drag a corner handle to resize. Hold Alt while dragging a corner to crop instead.
  - Arrow keys nudge the selected item 1 px, or 10 px with Shift.
  - Tick *Locked* in Properties for backgrounds, so clicks pass through them to the items on top.
- **Scenes dock**: scenes grouped by canvas. Select one to edit it; double-click or press *Show* to put it on its canvas.
  A scene linked to another switches together with it, so one click changes both canvases.
  Under the scene list, pick the *Transition*: *Fade* (crossfade, 300 ms by default) or *Cut*.
- **Hotkeys** (*File → Hotkeys…*): give any scene, *Go live* or *Start recording* a key combo such as Ctrl+Alt+1.
  They work while a game has focus. Pick combos your games don't use.
- **Stats** (*View → Stats*, Ctrl+Shift+S): render fps, CPU and memory, free disk space, and for every stream
  and recording the frames sent and dropped, encoding time per frame, and time spent waiting on the network.
- **Sources dock**: add a display, window, color or image source to the selected scene, tick to show or hide,
  and use *Up* and *Down* to change which item draws on top.
  - *+ Display* captures a whole monitor, cursor included, and fits it to the canvas.
  - *+ Window* captures one window even when other windows cover it. SprigCast remembers the window's
    program and title, so a game that restarts or a browser that changes tabs is found again automatically.
  - *+ Camera* adds a webcam or capture card as a facecam in the bottom-right corner. Pick the device,
    mode (for example 1920x1080 at 30 fps) and mirroring. Cameras are remembered by device, so
    replugging into another USB port is fine, and an unplugged camera reconnects on its own.
- **Audio dock**: a level meter for every audio source plus the master mix.
  - *+ Mic* adds a microphone, *+ Desktop* adds what your speakers or headphones play (WASAPI loopback).
    Both default to whatever device Windows uses, so switching headsets in Windows just works.
  - Drag a slider to set the volume (-60 to +12 dB), click *Mute* to silence it,
    and use *Device…* to pin a source to one device or *Remove* to delete it.
  - Audio sources belong to the whole collection, not one scene, so they keep playing when you switch scenes.
- **Output dock**: go live and record.
  - *Stream…* sets up where you stream: Twitch, YouTube, Kick, TikTok or any RTMP server. Picking a service
    fills in its preset (Twitch: horizontal 1920×1080 60 fps 6000 kbps; TikTok: vertical 720×1280 30 fps
    4000 kbps), and you can change the canvas, resolution, frame rate and bitrate per output.
    Stream keys are saved in Windows Credential Manager, never in `scenes.json`.
  - Twitch has a *Bandwidth test* option: the stream reaches Twitch so you can check it, but viewers never see it.
  - Tick the places to stream to and press *Go live*. Each one gets its own NVENC encoder (constant bitrate,
    keyframe every 2 s), so Twitch can take the horizontal canvas while TikTok takes the vertical one.
    Each output shows a health dot: green is fine, yellow is falling behind, red is dropping frames or
    disconnected, with the reason (upload too slow for the bitrate, or encoder overloaded).
    A dropped connection reconnects on its own.
  - *Start recording* writes each ticked canvas to its own file at the same time,
    for example `SprigCast 2026-11-10 19-30-05 horizontal.mkv` and `... vertical.mkv`, both with the mixed audio.
    *Recording…* picks the folder (default `Videos\SprigCast`), bitrate and MKV or MP4; MKV stays playable
    if the PC crashes. *Folder* opens your recordings. Streaming and recording can run together.
- **Text sources** (*+ Text*): titles and labels with a font, size, colour and outline.
  `{time}`, `{time12}` and `{date}` in the text show a live clock. Set *File* to a text file and its
  contents are shown instead and follow its changes, handy for "now playing" tools.
- **Properties dock**: name, position, scale and rotation of the selected item, its color, image, monitor or
  window, *Fit to canvas*, and a status line (for example "waiting for target" while a game is closed).

Capture uses Windows Graphics Capture. SprigCast hides the yellow capture border on Windows 11;
Windows 10 always shows it.

The window remembers its size and where you put the docks. SprigCast uses a dark Slothbloom theme;
set the environment variable `SPRIGCAST_THEME=system` before launching to keep the plain Windows look.

Scenes save automatically to `scenes.json` in your AppData folder
(*File → Show scenes file location* shows the exact path).

## Plugins

A plugin is a Python file that adds a new source type. *File → Open plugins folder* opens the folder
(`%LOCALAPPDATA%\Slothbloom\SprigCast\plugins`). Every `.py` file there loads at startup; files whose
name starts with `_` are skipped. Plugin sources appear under *Sources → + More*.

The first launch puts an example there, `countdown_example.py`, a "Starting in 4:59" countdown built on
the text source. A minimal plugin looks like this:

```python
from sprigcast.core.source import register
from sprigcast.sources.text import TextSource

@register("shout")
class ShoutSource(TextSource):
    label = "Shout"                                   # name in the + More menu
    defaults = {**TextSource.defaults, "text": "hi"}  # fields in its Settings dialog

    def _current_text(self):
        return self.settings.get("text", "").upper() + "!"
```

For anything other than text, subclass `sprigcast.core.source.Source` and implement `size` and
`video_frame()` (return an RGBA NumPy array when the picture changes, `None` otherwise) or
`audio_samples()`. Installed packages can also register plugins through a `sprigcast.plugins` entry point.
A plugin that fails to load is named in the status bar and skipped. Plugins run with the same rights
as SprigCast, so only install ones you trust.

## Project layout

```
src/sprigcast/
  core/      scene collection model (canvases, sources, scenes, outputs), Source base class, mailbox, events
  render/    moderngl compositor and the render engine thread
  sources/   built-in sources: color, image, text, display and window capture, camera, mic and desktop audio
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
such as chroma key and colour correction, scene switching from chat or a Stream Deck, and replay buffer.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
