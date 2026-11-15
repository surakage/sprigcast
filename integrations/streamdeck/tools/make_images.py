"""Draw the Stream Deck plugin's images (keys, action icons, plugin icons) from SVG.

Run from the repo root with SprigCast's environment:  python integrations/streamdeck/tools/make_images.py
Writes PNGs (1x and @2x) into com.sprigcast.sprigcast.sdPlugin/imgs.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

HERE = Path(__file__).resolve().parent.parent
IMGS = HERE / "com.sprigcast.sprigcast.sdPlugin" / "imgs"
BRAND = HERE.parent.parent / "brand"

TILE = "#123826"  # brand: dark green tile
MINT = "#4fd88f"  # brand: the S
DIM = "#6f8f7e"
PINK = "#ff5c93"  # only for live and recording
WHITE = "#ffffff"

# Glyphs drawn in a 0..100 box, stroke-based, with {c} for the colour.
GLYPHS = {
    "bloom": '<g fill="{c}"><circle cx="50" cy="30" r="13"/><circle cx="69" cy="44" r="13"/><circle cx="62" cy="66" r="13"/>'
             '<circle cx="38" cy="66" r="13"/><circle cx="31" cy="44" r="13"/></g><circle cx="50" cy="50" r="9" fill="{bg}"/>',
    "sound": '<path d="M22 40h14l18-15v50L36 60H22z" fill="{c}"/><path d="M64 36q10 14 0 28M72 28q18 22 0 44" fill="none" '
             'stroke="{c}" stroke-width="6" stroke-linecap="round"/>',
    "muted": '<path d="M22 40h14l18-15v50L36 60H22z" fill="{c}"/><path d="M64 38l20 24M84 38L64 62" stroke="{c}" '
             'stroke-width="7" stroke-linecap="round"/>',
    "eye": '<path d="M12 50q38-40 76 0q-38 40-76 0z" fill="none" stroke="{c}" stroke-width="7" stroke-linejoin="round"/>'
           '<circle cx="50" cy="50" r="11" fill="{c}"/>',
    "eye-off": '<path d="M12 50q38-40 76 0q-38 40-76 0z" fill="none" stroke="{c}" stroke-width="7" stroke-linejoin="round"/>'
               '<path d="M22 80L78 20" stroke="{c}" stroke-width="7" stroke-linecap="round"/>',
    "live": '<rect x="14" y="30" width="72" height="40" rx="20" fill="none" stroke="{c}" stroke-width="7"/>'
            '<circle cx="33" cy="50" r="8" fill="{c}"/><path d="M50 40v20h12M68 40v20" stroke="{c}" stroke-width="6" '
            'stroke-linecap="round" fill="none"/>',
    "record": '<circle cx="50" cy="50" r="30" fill="none" stroke="{c}" stroke-width="7"/><circle cx="50" cy="50" r="17" fill="{c}"/>',
    "clip": '<rect x="18" y="26" width="64" height="48" rx="10" fill="none" stroke="{c}" stroke-width="7"/>'
            '<path d="M44 38l18 12-18 12z" fill="{c}"/>',
    "screenshot": '<rect x="16" y="32" width="68" height="46" rx="10" fill="none" stroke="{c}" stroke-width="7"/>'
                  '<path d="M38 32l6-10h12l6 10" fill="none" stroke="{c}" stroke-width="7" stroke-linejoin="round"/>'
                  '<circle cx="50" cy="55" r="12" fill="none" stroke="{c}" stroke-width="7"/>',
    "sprig": '<path d="M50 86V40" stroke="{c}" stroke-width="7" stroke-linecap="round"/><path d="M50 52q-26 2-30-24q26-2 30 24z'
             'M50 44q22-2 28-26q-24-2-28 26z" fill="{c}"/>',
    "deaths": '<path d="M28 84V40q0-22 22-22t22 22v44z" fill="none" stroke="{c}" stroke-width="7" stroke-linejoin="round"/>'
              '<path d="M50 34v26M40 44h20" stroke="{c}" stroke-width="6" stroke-linecap="round"/>'
              '<path d="M16 86h68" stroke="{c}" stroke-width="7" stroke-linecap="round"/>',
    "run": '<path d="M56 12L24 56h22l-6 32 34-46H52z" fill="{c}" stroke="{c}" stroke-width="3" stroke-linejoin="round"/>',
}

# action -> (state 0 glyph, colour), (state 1 glyph, colour) or None
KEYS = {
    "bloom": (("bloom", DIM), ("bloom", MINT)),
    "mute": (("sound", MINT), ("muted", DIM)),
    "item": (("eye", MINT), ("eye-off", DIM)),
    "live": (("live", DIM), ("live", PINK)),
    "record": (("record", DIM), ("record", PINK)),
    "clip": (("clip", MINT), None),
    "screenshot": (("screenshot", MINT), None),
    "sprig-state": (("sprig", MINT), None),
    "deaths": (("deaths", MINT), None),
    "run": (("run", MINT), None),
}


def svg_key(glyph: str, colour: str, on: bool) -> str:
    bg = "#0f2a1d" if not on else TILE
    body = GLYPHS[glyph].format(c=colour, bg=bg)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 144 144"><rect width="144" height="144" fill="{bg}"/>'
            f'<g transform="translate(25 20) scale(0.94)">{body}</g></svg>')


def svg_icon(glyph: str) -> str:
    body = GLYPHS[glyph].format(c=WHITE, bg="#000000")
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">{body}</svg>'


def render(svg: str, size: int, path: Path) -> None:
    renderer = QSvgRenderer(QByteArray(svg.encode()))
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter, QRectF(0, 0, size, size))
    painter.end()
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(str(path))


def both(svg: str, size: int, base: Path) -> None:
    render(svg, size, base.with_suffix(".png"))
    render(svg, size * 2, base.with_name(base.name + "@2x.png"))


def main() -> None:
    QGuiApplication(sys.argv)
    for action, states in KEYS.items():
        for n, state in enumerate(states):
            if state is not None:
                both(svg_key(state[0], state[1], n == 1), 72, IMGS / "actions" / action / f"key{n}")
        both(svg_icon(states[0][0]), 20, IMGS / "actions" / action / "icon")
    tile = (BRAND / "sprigcast-app-icon.svg").read_text(encoding="utf-8")
    both(tile, 256, IMGS / "plugin" / "marketplace")
    render(tile, 288, IMGS / "plugin" / "store-icon-288.png")
    sloth = (BRAND / "sprigcast-sloth-white.svg").read_text(encoding="utf-8")
    both(sloth, 28, IMGS / "plugin" / "category-icon")
    print("images written to", IMGS)


if __name__ == "__main__":
    main()
