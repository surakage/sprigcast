"""Safe zones for vertical video: where TikTok, Twitch, YouTube Shorts and Instagram Reels draw their own
buttons, captions, chat and tabs over your stream. Keep faces, text and alerts out of them.

Sizes are approximate (the apps change them now and then), measured on a 1080×1920 frame and scaled to
the canvas. Each zone is (label, x, y, width, height) in 1080×1920 pixels.

Twitch (Dual Format vertical) comes from Twitch's own safe-zone template: its top bar takes about 8% of the
frame and its chat, events and controls start about 79% down on an iPhone 13 mini. Where chat starts
differs by phone and by how tall the viewer drags it, so the box covers the usual case.
"""

from __future__ import annotations

BASE_W, BASE_H = 1080, 1920

PLATFORMS: dict[str, list[tuple[str, int, int, int, int]]] = {
    "TikTok": [
        ("Top tabs", 0, 0, 1080, 160),
        ("Like · comment · share", 940, 700, 140, 760),
        ("Caption and name", 0, 1500, 1080, 420),
    ],
    "YouTube Shorts": [
        ("Top bar", 0, 0, 1080, 140),
        ("Like · dislike · comment", 920, 860, 160, 680),
        ("Title and channel", 0, 1580, 1080, 340),
    ],
    "Twitch": [
        ("Top bar", 0, 0, 1080, 150),
        ("Chat · events · controls", 0, 1500, 1080, 420),
    ],
    "Instagram Reels": [
        ("Top bar", 0, 0, 1080, 220),
        ("Like · comment · share", 950, 1000, 130, 560),
        ("Caption and audio", 0, 1500, 1080, 420),
    ],
}
ALL = "All platforms"
CHOICES = ["Off", ALL, *sorted(PLATFORMS)]


def zones(choice: str, width: int, height: int) -> list[tuple[str, float, float, float, float]]:
    """The zones to draw on a canvas of this size. Only portrait canvases get them."""
    if choice == "Off" or width >= height:
        return []
    sx, sy = width / BASE_W, height / BASE_H
    if choice == ALL:
        boxes = _merged()
    elif choice in PLATFORMS:
        boxes = PLATFORMS[choice]
    else:
        return []
    return [(label, x * sx, y * sy, w * sx, h * sy) for label, x, y, w, h in boxes]


def _merged() -> list[tuple[str, int, int, int, int]]:
    """Every platform at once, as three boxes (top, side buttons, bottom) covering all of them. A zone is a
    top bar when it starts at the top edge, a bottom band when it reaches the bottom edge, else side buttons."""
    groups: dict[str, list[tuple[str, int, int, int, int]]] = {"Top bars": [], "Side buttons": [],
                                                                 "Captions and names": []}
    for boxes in PLATFORMS.values():
        for box in boxes:
            _, _, y, _, h = box
            key = "Top bars" if y == 0 else "Captions and names" if y + h >= BASE_H else "Side buttons"
            groups[key].append(box)
    out = []
    for label, rects in groups.items():
        if not rects:
            continue
        x0 = min(r[1] for r in rects)
        y0 = min(r[2] for r in rects)
        x1 = max(r[1] + r[3] for r in rects)
        y1 = max(r[2] + r[4] for r in rects)
        out.append((label, x0, y0, x1 - x0, y1 - y0))
    return out


def clear_area(choice: str, width: int, height: int) -> tuple[float, float, float, float] | None:
    """The biggest centred box free of every zone: a handy guide for where faces and text belong."""
    found = zones(choice, width, height)
    if not found:
        return None
    top = max((y + h for _, x, y, w, h in found if y == 0), default=0.0)
    bottom = min((y for _, x, y, w, h in found if y + h >= height - 1), default=float(height))
    right = min((x for _, x, y, w, h in found if x > 0), default=float(width))
    left = width - right  # keep it centred
    return left, top, right - left, bottom - top
