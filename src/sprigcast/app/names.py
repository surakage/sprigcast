"""Short, readable names and status lines.

Device names and capture statuses are long ("Elgato Facecam MK.2", "capturing 1920x1080 @ 120, using 30,
shrunk to 1280x720"). The Inside list and inspector show a short version; the full text stays in tooltips.
"""

from __future__ import annotations
from sprigcast.app.modal import modal

import re

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from sprigcast.app import theme
from sprigcast.app.card import BOX_HEIGHT, PRIMARY_PILL, CardDialog

# Brand words dropped from device names: the thing you plugged in matters, not who made it.
BRANDS = {"elgato", "logitech", "razer", "microsoft", "avermedia", "insta360", "obsbot", "anker", "creative",
          "corsair", "hyperx", "steelseries", "rode", "røde", "shure", "blue", "nvidia", "amd", "intel", "realtek",
          "usb", "hd", "fhd", "uhd", "4k", "1080p", "720p", "video", "device", "camera"}
MAX = 18


def _is_model(word: str) -> bool:
    """C920, MK.2, Wave:3, XLR, v2: short codes with a digit (or all-caps codes) that say nothing to a viewer."""
    bare = word.strip(".,:;-_")
    if not bare:
        return True
    if re.fullmatch(r"(mk|v|gen)\.?\d+", bare, re.IGNORECASE):
        return True
    return any(c.isdigit() for c in bare) and len(bare) <= 6


def short_name(original: str, fallback: str) -> str:
    """'Elgato Facecam MK.2' -> 'Facecam'; 'Logitech HD Pro Webcam C920' -> 'Pro Webcam'. Never empty."""
    text = re.sub(r"\(.*?\)|\[.*?\]", " ", original or "")
    words = [w for w in re.split(r"\s+", text) if w]
    kept = [w for w in words if w.lower().strip(".,:") not in BRANDS and not _is_model(w)]
    name = " ".join(kept).strip(" -·:") or fallback
    if len(name) > MAX:
        name = name[:MAX].rsplit(" ", 1)[0] if " " in name[:MAX] else name[: MAX - 1] + "…"
    return name


def sound_name(kind: str, device: str) -> str:
    """The name SprigCast gives a mic or desktop sound for a device ("Mic", "Mic (Wave)")."""
    base = "Desktop audio" if kind == "audio_output" else "Mic"
    device = str(device or "default")
    return base if device == "default" else short_name(device, base)


def sound_name_is_ours(name: str, kind: str, device: str) -> bool:
    """True when a mic or desktop sound still has a name SprigCast made for its device, so a device change may
    rename it; a name the streamer chose ("[AIC] Microphone", which Streamer.bot steps use) never changes."""
    base = "Desktop audio" if kind == "audio_output" else "Mic"
    device = str(device or "default")
    return name in (base, f"{base} ({device})", short_name(device, base))


def window_name(title: str, process: str = "") -> str:
    """A window is named after its app ('chrome.exe' -> 'Chrome'), else the last part of its title."""
    app = re.sub(r"\.exe$", "", (process or "").rsplit("\\", 1)[-1].rsplit("/", 1)[-1], flags=re.IGNORECASE)
    if app:
        return app[:1].upper() + app[1:] if app.islower() else app
    part = (title or "").rsplit(" - ", 1)[-1].strip()
    return short_name(part, "Window") if part else "Window"


def window_app(title: str, process: str = "", app: str = "") -> str:
    """The app a window belongs to, for the window lists: its own name ("Google Chrome", "Calculator"), else the
    program's ('chrome.exe' -> 'Chrome')."""
    return (app or "").strip() or window_name(title, process)


_TAIL = re.compile(r"\s+[-\u2013\u2014|]\s+([^-\u2013\u2014|]+)$")


def window_detail(title: str, app: str, process: str = "") -> str:
    """Which window of that app: its title without the app's name on the end ("YouTube — Mozilla Firefox" ->
    "YouTube"), or "" when the title is only the app's name."""
    text = (title or "").strip()
    words = {w for w in re.findall(r"[a-z0-9]+", f"{app} {re.sub(r'(?i)[.]exe$', '', process or '')}".lower())
             if len(w) > 2}
    tail = _TAIL.search(text)
    if tail and words & set(re.findall(r"[a-z0-9]+", tail.group(1).lower())):
        text = text[:tail.start()].strip()
    return "" if text.lower() == (app or "").strip().lower() else text


_CAPTURE = re.compile(r"capturing (\d+)x(\d+)(?: @ (\d+))?(?:, using (\d+))?(?:, shrunk to (\d+)x(\d+))?")


def _res(w: int, h: int, fps: str | None) -> str:
    """1920x1080 @ 60 -> '1080p60'; odd sizes keep W×H."""
    tall = min(w, h) if w * 9 == h * 16 or h * 9 == w * 16 or w * 3 == h * 4 else None
    label = f"{tall}p" if tall else f"{w}×{h}"
    return label + (fps or "")


def short_status(status: str) -> str:
    """The quiet tag for a row: '' when it's working, else one or two words."""
    s = (status or "").lower()
    if not s or s == "ok" or s.startswith(("capturing", "monitoring", "listening", "sound plays on desktop")):
        return ""
    if s.startswith("video can't play"):  # the page works; one video on it doesn't (sources/browser.py)
        return "no video"
    if s.startswith(("loading", "getting")):  # getting ready (a plugin fetching what it needs)
        return "starting"
    for words, tag in (("paused", "asleep"), ("not found", "not found"), ("disconnected", "unplugged"),
                       ("lost", "lost"), ("closed", "closed"), ("busy", "busy"), ("waiting", "waiting"),
                       ("starting", "starting")):
        if words in s:
            return tag
    return "problem"


def plain_problem(status: str, kind: str) -> tuple[str, str] | None:
    """What's wrong with a source, in plain words, and the fix to offer: "pick" (choose it again),
    "retry" (start it again) or "" (nothing to press: it sorts itself out). None when it's fine."""
    tag = short_status(status)
    if not tag or tag in ("asleep", "starting"):
        return None
    thing = {"camera": "camera", "window_capture": "window", "game_capture": "game", "display_capture": "screen", "browser": "page",
             "audio_input": "mic", "audio_output": "sound device", "app_audio": "app sound", "image": "picture"}.get(kind, "source")
    if tag == "busy":
        return (f"Another app is using this {thing}. Close it there (Camera app, Teams, Discord, OBS…), "
                "then try again.", "retry")
    if tag == "unplugged":
        return f"The {thing} was unplugged. Plug it back in and it comes back by itself.", "pick"
    if tag == "closed" or (tag == "not found" and kind in ("window_capture", "game_capture")):
        return "That window isn't open. Open the app, or pick another window.", "pick"
    if tag == "not found":
        if kind == "image":
            return "The picture file was moved or deleted. Pick it again.", "pick"
        return f"SprigCast can't find this {thing}. Is it plugged in and switched on?", "pick"
    if tag == "lost":
        return f"Lost the {thing} for a moment. SprigCast keeps trying; you can also try now.", "retry"
    if tag == "waiting":
        return f"Waiting for the {thing} to start.", ""
    if tag == "no video":
        if "h.264" in (status or "").lower():
            return ("A video on this page can't play: it's H.264 (MP4), which SprigCast's page engine can't play "
                    "yet. The rest of the page still shows."), ""
        return "A video on this page can't play: its format isn't supported. The rest of the page still shows.", ""
    detail = (status or "").strip().rstrip(".")
    return f"Something's wrong with this {thing}: {detail}.", "retry"


def status_summary(status: str, size: tuple[int, int] | None = None) -> str:
    """One short line for the inspector: '1080p120 → 720p30', 'Working · 1280×720', 'Not found'."""
    m = _CAPTURE.search(status or "")
    if m:
        w, h, fps, using, sw, sh = m.groups()
        got = _res(int(w), int(h), fps)
        if using or sw:
            out_w, out_h = (int(sw), int(sh)) if sw else (int(w), int(h))
            return f"{got} → {_res(out_w, out_h, using or fps)}"
        return got
    tag = short_status(status)
    if tag:
        return tag[:1].upper() + tag[1:]
    if size and size[0]:
        return f"{size[0]}×{size[1]}"
    return ""


class NameDialog(CardDialog):
    """After adding: use the short name (editable) or keep the device's original name."""

    def __init__(self, short: str, original: str, parent: QWidget | None = None):
        super().__init__("Name it", parent, width=460)
        self.original = original
        root = self.body
        hint = QLabel(original)
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {theme.MUTED}; background: transparent;")
        root.addWidget(hint)
        self.edit = self.line_edit()
        self.edit.setMinimumHeight(BOX_HEIGHT)
        self.edit.setText(short)
        self.edit.selectAll()
        root.addWidget(self.edit)
        row = QHBoxLayout()
        row.setSpacing(8)
        keep = QPushButton("Keep full name")
        keep.setStyleSheet(theme.button_style(17, "9px 18px"))
        keep.setAutoDefault(False)
        keep.clicked.connect(self._keep)
        use = QPushButton("Use this name")
        use.setStyleSheet(PRIMARY_PILL)
        use.setDefault(True)
        use.clicked.connect(self.accept)
        for button in (keep, use):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
        row.addStretch(1)
        row.addWidget(keep)
        row.addWidget(use)
        root.addSpacing(4)
        root.addLayout(row)
        self.choice = short
        self.edit.setFocus()

    def _keep(self) -> None:
        self.edit.setText(self.original)
        self.accept()

    def name(self) -> str:
        return self.edit.text().strip() or self.original


def ask_name(parent: QWidget | None, short: str, original: str) -> str:
    """Offer the short name; returns what to call it. Skipped when the short name is the original."""
    if not original or short == original:
        return short or original
    dialog = NameDialog(short, original, parent)
    return dialog.name() if modal(dialog) else short


def sort_key(name: str) -> tuple:
    """A name's place in A–Z order as people read it: a leading emoji or symbol is skipped ("🟣 Elgato" under E),
    case doesn't count, and numbers count as numbers ("Chat 2" before "Chat 10"). The whole name breaks ties."""
    import re

    text = name.casefold()
    start = next((i for i, ch in enumerate(text) if ch.isalnum()), len(text))
    words = [(0, int(part), "") if part.isdigit() else (1, 0, part)
             for part in re.split(r"(\d+)", text[start:].strip()) if part]
    return (words, text)
