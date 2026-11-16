"""Draw the Stream Deck plugin's images (keys, action icons, plugin icons) from SVG.

Run from the repo root with SprigCast's environment:  python integrations/streamdeck/tools/make_images.py
Writes PNGs (1x and @2x) into com.sprigcast.sprigcast.sdPlugin/imgs.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

HERE = Path(__file__).resolve().parent.parent
IMGS = HERE / "com.sprigcast.sprigcast.sdPlugin" / "imgs"
BRAND = HERE.parent.parent / "brand"

TILE = "#123826"  # brand: dark green tile
MINT = "#4fd88f"  # brand: the S
DIM = "#6f8f7e"
PINK = "#ff5c93"  # only for live and recording
WHITE = "#ffffff"

# Glyphs drawn in a 0..100 box, rounded strokes like the app's own icons, {c} for the colour and {bg} for the
# tile behind (for cut-outs). Every key has its own picture; leaves and petals keep them in the sprig family.
SW = 'stroke="{c}" stroke-width="7" stroke-linecap="round" stroke-linejoin="round" fill="none"'
GLYPHS = {
    # Show bloom: a bloom on the screen (puts it on air).
    "bloom": f'<rect x="8" y="16" width="84" height="56" rx="10" {SW}/><path d="M38 88h24M50 72v16" {SW}/>'
             '<g fill="{c}"><circle cx="50" cy="31" r="8.5"/><circle cx="61" cy="39" r="8.5"/><circle cx="57" cy="52" r="8.5"/>'
             '<circle cx="43" cy="52" r="8.5"/><circle cx="39" cy="39" r="8.5"/></g><circle cx="50" cy="43" r="5" fill="{bg}"/>',
    # Show or hide: an eye, and the eye shut.
    "eye": f'<path d="M10 50q40-42 80 0q-40 42-80 0z" {SW}/><circle cx="50" cy="50" r="12" fill="{{c}}"/>',
    "eye-off": f'<path d="M10 46q40 40 80 0" {SW}/><path d="M24 60l-8 10M50 66v12M76 60l8 10" {SW}/>',
    # Garden: a stack of blooms with a sprout (a whole set of blooms, switched at once).
    "garden": f'<rect x="26" y="10" width="62" height="44" rx="9" {SW}/><rect x="12" y="26" width="62" height="44" rx="9" fill="{{bg}}" stroke="{{c}}" stroke-width="7"/>'
              f'<path d="M43 62V46" stroke="{{c}}" stroke-width="5" stroke-linecap="round"/>'
              f'<path d="M43 52q-13 0-15-13q13 0 15 13zM43 48q11 0 13-11q-11 0-13 11z" fill="{{c}}"/>',
    # Path: set sliders (where and how you go out: places, encoder, recording).
    "path": f'<path d="M18 26h64M18 50h64M18 74h64" {SW}/>'
            '<circle cx="36" cy="26" r="9" fill="{c}"/><circle cx="64" cy="50" r="9" fill="{c}"/><circle cx="44" cy="74" r="9" fill="{c}"/>',
    # Mute sound: a speaker with waves, and with a cross.
    "sound": f'<path d="M14 40h14l20-16v52L28 60H14z" fill="{{c}}"/><path d="M62 38q8 12 0 24M72 28q16 22 0 44" {SW}/>',
    "muted": f'<path d="M14 40h14l20-16v52L28 60H14z" fill="{{c}}"/><path d="M62 38l22 24M84 38L62 62" {SW}/>',
    # Sound volume: a level knob with its arc (the dial).
    "volume": f'<path d="M24 76a34 34 0 1 1 52 0" {SW}/><circle cx="50" cy="52" r="16" fill="{{c}}"/>'
              f'<path d="M50 52L38 38" stroke="{{bg}}" stroke-width="7" stroke-linecap="round"/>',
    # Go live: a broadcast dot with waves.
    "live": f'<circle cx="50" cy="50" r="11" fill="{{c}}"/><path d="M34 34a22 22 0 0 0 0 32M66 34a22 22 0 0 1 0 32'
            f'M22 22a40 40 0 0 0 0 56M78 22a40 40 0 0 1 0 56" {SW}/>',
    # Record: the record ring and dot.
    "record": f'<circle cx="50" cy="50" r="32" {SW}/><circle cx="50" cy="50" r="18" fill="{{c}}"/>',
    # Clips: a rewind arrow round a dot (the last seconds kept ready).
    "clips": f'<path d="M24 50a26 26 0 1 0 8-19" {SW}/><path d="M22 18v16h16" {SW}/><circle cx="50" cy="50" r="9" fill="{{c}}"/>',
    # Save clip: a film strip with a save arrow.
    "clip": f'<rect x="16" y="14" width="68" height="48" rx="9" {SW}/>'
            '<g fill="{c}"><rect x="24" y="21" width="8" height="7" rx="2"/><rect x="24" y="48" width="8" height="7" rx="2"/>'
            '<rect x="68" y="21" width="8" height="7" rx="2"/><rect x="68" y="48" width="8" height="7" rx="2"/></g>'
            f'<path d="M50 30v36M36 54l14 14 14-14" {SW}/><path d="M22 88h56" {SW}/>',
    # Virtual camera: a webcam sending SprigCast out to other apps.
    "camera": f'<circle cx="40" cy="42" r="26" {SW}/><circle cx="40" cy="42" r="10" fill="{{c}}"/><path d="M28 84h24M40 68v16" {SW}/>'
              f'<path d="M70 34h18M80 26l8 8-8 8" {SW}/>',
    # Screenshot: viewfinder corners round a dot.
    "screenshot": f'<path d="M14 32V20a6 6 0 0 1 6-6h12M68 14h12a6 6 0 0 1 6 6v12M86 68v12a6 6 0 0 1-6 6H68M32 86H20'
                  f'a6 6 0 0 1-6-6V68" {SW}/><circle cx="50" cy="50" r="14" fill="{{c}}"/>',
    # Media: a play button in a frame, and paused.
    "media": f'<rect x="12" y="18" width="76" height="64" rx="14" {SW}/><path d="M41 35l24 15-24 15z" fill="{{c}}"/>',
    "media-on": f'<rect x="12" y="18" width="76" height="64" rx="14" {SW}/>'
                '<rect x="37" y="35" width="9" height="30" rx="3" fill="{c}"/><rect x="54" y="35" width="9" height="30" rx="3" fill="{c}"/>',
    # Filter: a wand with sparkles (an effect on a source).
    "filter": f'<path d="M20 82L62 40" {SW}/><path d="M62 40l8-8" stroke="{{c}}" stroke-width="13" stroke-linecap="round"/>'
              '<path d="M74 10l3 8 8 3-8 3-3 8-3-8-8-3 8-3zM30 18l2 5 5 2-5 2-2 5-2-5-5-2 5-2zM84 52l2 5 5 2-5 2-2 5-2-5-5-2 5-2z" fill="{c}"/>',
    # Transition: one frame sliding into the next.
    "transition": f'<rect x="8" y="22" width="44" height="56" rx="9" {SW}/><rect x="48" y="22" width="44" height="56" rx="9" fill="{{c}}"/>'
                  f'<path d="M58 50h22M72 42l8 8-8 8" stroke="{{bg}}" stroke-width="6" stroke-linecap="round" '
                  'stroke-linejoin="round" fill="none"/>',
    # Sprig state: a switch with a sprout (flips a sprig to another Live state).
    "sprig": f'<rect x="10" y="34" width="80" height="40" rx="20" {SW}/><circle cx="70" cy="54" r="12" fill="{{c}}"/>'
             '<path d="M26 64q-4-20 18-22q2 20-18 22z" fill="{c}"/>',
    # Death count: a gravestone on its mound.
    "deaths": f'<path d="M28 80V40q0-22 22-22t22 22v40" {SW}/><path d="M50 32v24M40 42h20" {SW}/>'
              '<path d="M12 86q38-14 76 0z" fill="{c}"/>',
    # Run action: braces round a play mark (runs any SprigCast action).
    "run": f'<path d="M30 16q-12 0-12 12v10q0 12-10 12q10 0 10 12v10q0 12 12 12M70 16q12 0 12 12v10q0 12 10 12q-10 0-10 12v10q0 12-12 12" {SW}/>'
           '<path d="M42 36l22 14-22 14z" fill="{c}"/>',
}

# action -> (state 0 glyph, colour), (state 1 glyph, colour) or None
KEYS = {
    "bloom": (("bloom", DIM), ("bloom", MINT)),
    "mute": (("sound", MINT), ("muted", DIM)),
    "item": (("eye", MINT), ("eye-off", DIM)),
    "live": (("live", DIM), ("live", PINK)),
    "record": (("record", DIM), ("record", PINK)),
    "clip": (("clip", MINT), None),
    "volume": (("volume", MINT), None),
    "clips": (("clips", DIM), ("clips", MINT)),
    "camera": (("camera", DIM), ("camera", MINT)),
    "media": (("media", MINT), ("media-on", MINT)),
    "filter": (("filter", DIM), ("filter", MINT)),
    "transition": (("transition", DIM), ("transition", MINT)),
    "garden": (("garden", DIM), ("garden", MINT)),
    "path": (("path", DIM), ("path", MINT)),
    "screenshot": (("screenshot", MINT), None),
    "sprig-state": (("sprig", MINT), None),
    "deaths": (("deaths", MINT), None),
    "run": (("run", MINT), None),
}


def svg_key(glyph: str, colour: str, on: bool) -> str:
    bg = "#0f2a1d" if not on else TILE
    body = GLYPHS[glyph].format(c=colour, bg=bg)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 144 144"><rect width="144" height="144" fill="{bg}"/>'
            f'<g transform="translate(19 12) scale(1.06)">{body}</g></svg>')


def svg_icon(glyph: str) -> str:
    """White on black: render_icon turns black see-through (so cut-outs show the list behind, as Elgato asks)."""
    body = GLYPHS[glyph].format(c=WHITE, bg="#000000")
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><rect width="100" height="100" fill="#000000"/>'
            f'{body}</svg>')


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


def render_icon(svg: str, size: int, path: Path) -> None:
    """A white glyph on transparent: brightness becomes alpha."""
    render(svg, size, path)
    image = QImage(str(path)).convertToFormat(QImage.Format.Format_ARGB32)
    for y in range(image.height()):
        for x in range(image.width()):
            level = image.pixelColor(x, y).red()
            image.setPixelColor(x, y, QColor(255, 255, 255, level))
    image.save(str(path))


def both(svg: str, size: int, base: Path, draw=render) -> None:
    draw(svg, size, base.with_suffix(".png"))
    draw(svg, size * 2, base.with_name(base.name + "@2x.png"))


def main() -> None:
    QGuiApplication(sys.argv)
    for action, states in KEYS.items():
        for n, state in enumerate(states):
            if state is not None:
                both(svg_key(state[0], state[1], n == 1), 72, IMGS / "actions" / action / f"key{n}")
        both(svg_icon(states[0][0]), 20, IMGS / "actions" / action / "icon", render_icon)
    tile = (BRAND / "sprigcast-app-icon.svg").read_text(encoding="utf-8")
    both(tile, 256, IMGS / "plugin" / "marketplace")
    render(tile, 288, IMGS / "plugin" / "store-icon-288.png")
    logo = (BRAND / "sprigcast-s-white.svg").read_text(encoding="utf-8")  # the S, not the sloth
    both(logo, 28, IMGS / "plugin" / "category-icon")
    print("images written to", IMGS)


if __name__ == "__main__":
    main()
