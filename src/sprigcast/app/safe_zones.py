"""Safe zones for vertical video: where TikTok, YouTube Shorts and Instagram Reels draw their own buttons,
captions and tabs over your stream. Keep faces, text and alerts out of them.

Sizes are approximate (the apps change them now and then), measured on a 1080×1920 frame and scaled to
the canvas. Each zone is (label, x, y, width, height) in 1080×1920 pixels.
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
    "Instagram Reels": [
        ("Top bar", 0, 0, 1080, 220),
        ("Like · comment · share", 950, 1000, 130, 560),
        ("Caption and audio", 0, 1500, 1080, 420),
    ],
}
ALL = "All platforms"
CHOICES = ["Off", ALL, *PLATFORMS]


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
    """Every platform at once, as three boxes (top, side buttons, bottom) covering all of them."""
    out = []
    for i, label in enumerate(("Top bars", "Side buttons", "Captions and names")):
        rects = [zones_[i] for zones_ in PLATFORMS.values()]
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
