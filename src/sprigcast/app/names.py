"""Short, readable names and status lines.

Device names and capture statuses are long ("Elgato Facecam MK.2", "capturing 1920x1080 @ 120, using 30,
shrunk to 1280x720"). The Inside list and inspector show a short version; the full text stays in tooltips.
"""

from __future__ import annotations

import re

from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget

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


def window_name(title: str, process: str = "") -> str:
    """A window is named after its app ('chrome.exe' -> 'Chrome'), else the last part of its title."""
    app = re.sub(r"\.exe$", "", (process or "").rsplit("\\", 1)[-1].rsplit("/", 1)[-1], flags=re.IGNORECASE)
    if app:
        return app[:1].upper() + app[1:] if app.islower() else app
    part = (title or "").rsplit(" - ", 1)[-1].strip()
    return short_name(part, "Window") if part else "Window"


_CAPTURE = re.compile(r"capturing (\d+)x(\d+)(?: @ (\d+))?(?:, using (\d+))?(?:, shrunk to (\d+)x(\d+))?")


def _res(w: int, h: int, fps: str | None) -> str:
    """1920x1080 @ 60 -> '1080p60'; odd sizes keep W×H."""
    tall = min(w, h) if w * 9 == h * 16 or h * 9 == w * 16 or w * 3 == h * 4 else None
    label = f"{tall}p" if tall else f"{w}×{h}"
    return label + (fps or "")


def short_status(status: str) -> str:
    """The quiet tag for a row: '' when it's working, else one or two words."""
    s = (status or "").lower()
    if not s or s == "ok" or s.startswith("capturing") or s.startswith("monitoring"):
        return ""
    for words, tag in (("paused", "asleep"), ("not found", "not found"), ("disconnected", "unplugged"),
                       ("lost", "lost"), ("closed", "closed"), ("busy", "busy"), ("waiting", "waiting"),
                       ("starting", "starting")):
        if words in s:
            return tag
    return "problem"


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


class NameDialog(QDialog):
    """After adding: use the short name (editable) or keep the device's original name."""

    def __init__(self, short: str, original: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Name it")
        self.setMinimumWidth(320)
        self.original = original
        root = QVBoxLayout(self)
        root.setSpacing(10)
        hint = QLabel(f"Full name: {original}")
        hint.setWordWrap(True)
        root.addWidget(hint)
        self.edit = QLineEdit(short)
        self.edit.selectAll()
        root.addWidget(self.edit)
        row = QHBoxLayout()
        keep = QPushButton("Keep full name")
        keep.clicked.connect(self._keep)
        use = QPushButton("Use this name")
        use.setDefault(True)
        use.clicked.connect(self.accept)
        row.addWidget(keep)
        row.addStretch(1)
        row.addWidget(use)
        root.addLayout(row)
        self.choice = short

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
    return dialog.name() if dialog.exec() else short
