# SprigCast

![SprigCast: one show, every shape](brand/sprigcast-banner.png)

A lightweight, open-source streaming app for Windows, in the spirit of OBS, Meld and Streamlabs.
SprigCast renders a **horizontal** and a **vertical** canvas at the same time, and each output
(Twitch, TikTok, a recording) streams from the canvas you choose. One show, every shape.

Version 0.2.0. See [CHANGELOG.md](CHANGELOG.md) for what changed and what has been tried on a real PC.

## Install

The easiest way is the Windows installer, `SprigCast-Setup-<version>.exe`. It is built on GitHub
(Actions, *Windows installer* workflow) and asks you to close SprigCast before it installs or uninstalls.
To run from the source code instead, follow the steps below.

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

SprigCast has two screens. Switch with the buttons at the top left, or Ctrl+1 and Ctrl+2.

- **Blooms and sprigs.** A *bloom* is a scene that goes on air (Starting soon, Gaming, Be right back).
  A *sprig* is a reusable part, like a webcam frame or an alerts box, that you drop into blooms.
  Change a sprig once and every bloom that uses it changes, on both canvases. Sprigs can hold other sprigs.
- **Live (Ctrl+1)** is for running the show. The stage shows what's on air. Under it are three cards:
  *Blooms* (one button per bloom, with its hotkey), *Sound* (your sounds grouped as Voice, Game, Alerts,
  Music and Other) and *Transitions*. On the right are *Chat* and the *Go live* card with Record, Clips
  and your Destinations. Right-click the stage for *Full screen on…* another monitor.
- **Build (Ctrl+2)** is for making blooms. The library on the left has Horizontal and Vertical tabs, your
  blooms and sprigs, and the shared Sound list. The middle shows the bloom you're editing, which is not on
  air until you pick it in Live. On the right, *Inside* lists what the bloom holds and the inspector folds
  open with position and size. Drag to move, drag a handle to resize, hold Alt to crop. Undo and redo work
  as usual. New here? The *starter kit* (Tools menu) builds a first set of blooms from a few choices.
- **Sources**: display and window capture, camera (with color looks and shapes), image, color, text,
  browser page (right-click *Interact…* to click in it), media files (video and sound), chat, and sprigs.
- **Sound** belongs to the whole show, not one bloom, so it never plays twice when both canvases are live.
  Each sound has a *Tune* page with filters, and you pick which tracks it goes to: Stream, Recording,
  Voice or Clips. Turn on *Mic first* to dip other sounds while you talk.
- **Chat** mixes Twitch and YouTube in one panel. You can reply on Twitch and put chat on
  your stream as a source.
- **Settings** (Ctrl+,): General, Stream, Output, Audio, Video, Hotkeys, Connections, Advanced and Plugins.
  Stream keys are saved in Windows Credential Manager, never in the scenes file.
- **Optimize for this PC** runs on first start. It checks your PC and upload and suggests the heaviest
  settings that will run clean. Run it again any time from the Tools menu.
- **Stream doctor** (Ctrl+Shift+D) checks your setup before you go live. **Stats** is Ctrl+Shift+S, and
  the command palette (Ctrl+K) finds any bloom, setting or action by typing.
- **Control from other apps**: Streamer.bot, Stream Deck and your own scripts can drive SprigCast. See
  [docs/control-api.md](docs/control-api.md).

Logs, a report for each stream and crash reports go to `Documents\SprigCast\Logs`.
Scenes save automatically to `scenes.json` in your AppData folder.

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
A plugin that fails to load is named in the status bar and in *Tools → Plugin store*, and skipped. Plugins run with the same rights
as SprigCast, so only install ones you trust.


## Project layout

```
src/sprigcast/
  core/      scene collection model (canvases, sources, blooms, outputs), Source base class, filters, events
  render/    moderngl compositor and the render engine thread
  sources/   built-in sources: capture, camera, image, color, text, browser, media, chat, sound, sprigs
  audio/     mixer that combines sounds into 48 kHz stereo tracks
  output/    recorder, replay and clips, streamer, our own NVENC encoder, Twitch Enhanced Broadcasting
  chat/      Twitch and YouTube chat
  control/   the control API (SprigCast's own and the obs-websocket compatibility mode)
  app/       PySide6 windows: Live and Build screens, settings, wizard, theme and art
  plugins.py loads source plugins from the plugins folder and entry points
  tools/     sprigcast-check, sprigcast-teststream and sprigcast-memcheck
  data/      default scene collection used on first run
packaging/   Windows installer (Inno Setup) and its build script
tests/       pytest suite (GPU tests skip when no OpenGL context is available)
```

Run the tests with `uv run pytest`.

## Releases

Each release is listed in [CHANGELOG.md](CHANGELOG.md). To make one: bump the version in `pyproject.toml`
and `src/sprigcast/__init__.py`, move the changelog's "not released yet" to the date, then push a tag like
`v0.2.0`. The tag builds the installer on GitHub.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
