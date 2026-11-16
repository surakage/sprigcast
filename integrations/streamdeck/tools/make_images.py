"""Draw the Stream Deck plugin's images (keys, action icons, plugin icons) from SVG.

Run from the repo root with SprigCast's environment:  python integrations/streamdeck/tools/make_images.py
Writes PNGs (1x and @2x) into com.sprigcast.sprigcast.sdPlugin/imgs.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

HERE = Path(__file__).resolve().parent.parent
IMGS = HERE / "com.sprigcast.sprigcast.sdPlugin" / "imgs"
BRAND = HERE.parent.parent / "brand"

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


# Groups for the Colour style (each kind of key its own colour; pink stays for live and recording).
GROUP = {"bloom": "scene", "item": "scene", "garden": "scene", "path": "scene", "transition": "scene", "sprig-state": "scene",
         "mute": "sound", "volume": "sound", "media": "sound",
         "live": "live", "record": "live",
         "clips": "capture", "clip": "capture", "screenshot": "capture", "camera": "capture",
         "filter": "fx", "deaths": "fx", "run": "fx"}
GROUP_TILE = {"scene": ("#1f7a4d", "#0b3322"), "sound": ("#1f6f86", "#0a2b38"), "capture": ("#8a6a1e", "#3a2a08"),
              "fx": ("#5b4a9a", "#221a45"), "live": ("#b4305f", "#4a0f26")}

# Key styles the user can pick in the key settings (Key style). The first is the one in the manifest.
STYLES = ("canopy", "colour", "graphite")


def rgb(colour: str) -> tuple[int, int, int]:
    colour = colour.lstrip("#")
    return tuple(int(colour[i:i + 2], 16) for i in (0, 2, 4))


def raster(svg: str, size: int, box: tuple[float, float, float, float] | None = None) -> QImage:
    renderer = QSvgRenderer(QByteArray(svg.encode()))
    image = QImage(size, size, QImage.Format.Format_RGBA8888)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter, QRectF(*(box or (0, 0, size, size))))
    painter.end()
    return image


def to_pil(image: QImage):
    from PIL import Image

    size = image.width()
    return Image.fromarray(np.frombuffer(image.constBits(), np.uint8).reshape(size, size, 4).copy(), "RGBA")


def glyph_mask(glyph: str, size: int, inset: float, width: float = 7):
    """The glyph as a mask (cut-outs stay cut out)."""
    body = GLYPHS[glyph].replace('stroke-width="7"', f'stroke-width="{width}"').format(c=WHITE, bg="#000000")
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><rect width="100" height="100" fill="#000"/>'
           f'{body}</svg>')
    return to_pil(raster(svg, size, (inset, inset, size - 2 * inset, size - 2 * inset))).convert("L")


def gradient(size: int, top: str, bottom: str) -> np.ndarray:
    t = np.linspace(0, 1, size)[:, None, None]
    a, b = np.array(rgb(top), float), np.array(rgb(bottom), float)
    return np.repeat(a + (b - a) * t, size, axis=1)


def radial(size: int, inner: str, outer: str, cy: float = 0.25) -> np.ndarray:
    y, x = np.mgrid[0:size, 0:size] / size
    d = np.clip(np.hypot(x - 0.5, y - cy) / 0.95, 0, 1)[..., None]
    a, b = np.array(rgb(inner), float), np.array(rgb(outer), float)
    return a + (b - a) * d


def layer(colour: tuple[int, int, int], alpha: np.ndarray, size: int):
    from PIL import Image

    out = Image.new("RGBA", (size, size), colour + (255,))
    out.putalpha(Image.fromarray(np.clip(alpha, 0, 255).astype(np.uint8)))
    return out


def finish(tile: np.ndarray, mask, top: str, bottom: str, shadow: float, glow: str | None, gloss: float, rim: tuple[str, int]):
    """Tile + soft shadow + glow + glyph (lit from the top) + gloss + thin rim."""
    from PIL import Image, ImageDraw, ImageFilter

    size = tile.shape[0]
    out = Image.fromarray(np.clip(tile, 0, 255).astype(np.uint8), "RGB").convert("RGBA")
    m = np.asarray(mask).astype(float)
    blur = np.asarray(mask.filter(ImageFilter.GaussianBlur(size * 0.035))).astype(float)
    out.alpha_composite(layer((0, 0, 0), blur * shadow, size), (0, int(size * 0.025)))
    if glow:
        halo = np.asarray(mask.filter(ImageFilter.GaussianBlur(size * 0.06))).astype(float)
        out.alpha_composite(layer(rgb(glow), halo * 0.55, size))
    fill = Image.fromarray(gradient(size, top, bottom).astype(np.uint8), "RGB").convert("RGBA")
    fill.putalpha(Image.fromarray(m.astype(np.uint8)))
    out.alpha_composite(fill)
    y, x = np.mgrid[0:size, 0:size] / size
    out.alpha_composite(layer((255, 255, 255), np.clip(1 - np.hypot((x - 0.5) * 0.9, (y + 0.15) * 1.6), 0, 1) ** 1.6 * 255 * gloss, size))
    edge = Image.new("RGBA", (size, size))
    ImageDraw.Draw(edge).rounded_rectangle([0, 0, size - 1, size - 1], size * 0.153, outline=rgb(rim[0]) + (rim[1],),
                                           width=max(1, size // 72))
    out.alpha_composite(edge)
    return out.convert("RGB")


def style_canopy(glyph: str, lit: bool, pink: bool, _group: str, size: int):
    tile = radial(size, "#1c5a3c" if lit else "#163b2b", "#07160f")
    if lit:
        top, bottom, glow = ("#ff8fb4", "#ff3d7f", PINK) if pink else ("#b6ffd6", "#3ecf7f", MINT)
    else:
        top, bottom, glow = "#9fb3a8", "#5f7f6e", None
    return finish(tile, glyph_mask(glyph, size, size * 0.2), top, bottom, 0.7, glow, 0.08, ("#4fd88f", 40))


def style_colour(glyph: str, lit: bool, _pink: bool, group: str, size: int):
    top, bottom = GROUP_TILE[group] if lit else ("#3a4540", "#151b18")
    return finish(gradient(size, top, bottom), glyph_mask(glyph, size, size * 0.21), WHITE, "#dfe8e3" if lit else "#9aa8a0",
                  0.6, None, 0.18, ("#ffffff", 50))


def style_graphite(glyph: str, lit: bool, pink: bool, _group: str, size: int):
    colour = (PINK if pink else MINT) if lit else None
    return finish(radial(size, "#2a2f2d", "#101312", cy=0.2), glyph_mask(glyph, size, size * 0.22, 5.5),
                  colour or "#e8ecea", colour or "#b9c2bd", 0.5, None, 0.05, ("#ffffff", 22))


DRAW = {"canopy": style_canopy, "colour": style_colour, "graphite": style_graphite}


def offline_image(glyph: str, size: int):
    """While SprigCast is closed or gone: every key goes dark (one look for all styles), with a small grey ring
    in the corner, so it's plain the keys aren't connected."""
    from PIL import ImageDraw

    out = finish(radial(size, "#161a18", "#070908", cy=0.2), glyph_mask(glyph, size, size * 0.22, 5.5),
                 "#4c5651", "#353d39", 0.0, None, 0.0, ("#ffffff", 10))
    draw = ImageDraw.Draw(out)
    r, c, w = size * 0.07, size * 0.84, max(1, size // 48)
    draw.ellipse([c - r, size * 0.16 - r, c + r, size * 0.16 + r], outline=rgb("#6b7570"), width=w)
    draw.line([c - r * 0.7, size * 0.16 + r * 0.7, c + r * 0.7, size * 0.16 - r * 0.7], fill=rgb("#6b7570"), width=w)
    return out


def key_image(style: str, action: str, glyph: str, colour: str, size: int):
    return DRAW[style](glyph, colour != DIM, colour == PINK, GROUP[action], size)


def svg_icon(glyph: str) -> str:
    """Thin white lines on black: render_icon turns black see-through (cut-outs show the list behind, as Elgato asks)."""
    body = GLYPHS[glyph].replace('stroke-width="7"', 'stroke-width="4.2"').format(c=WHITE, bg="#000000")
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><rect width="100" height="100" fill="#000000"/>'
            f'<g transform="translate(4 4) scale(0.92)">{body}</g></svg>')


def render(svg: str, size: int, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raster(svg, size).save(str(path))


def render_icon(svg: str, size: int, path: Path) -> None:
    """A white glyph on transparent, a touch soft like the other list icons: brightness becomes alpha."""
    image = to_pil(raster(svg, size * 2)).resize((size, size), resample=3)  # drawn big, shrunk smooth
    alpha = np.asarray(image.convert("L")).astype(float) * 0.86
    path.parent.mkdir(parents=True, exist_ok=True)
    layer((255, 255, 255), alpha, size).save(str(path))


def both(svg: str, size: int, base: Path, draw=render) -> None:
    draw(svg, size, base.with_suffix(".png"))
    draw(svg, size * 2, base.with_name(base.name + "@2x.png"))


def key_folder(style: str, action: str) -> Path:
    """The first style lives with the manifest's images; the others under imgs/styles/<style>."""
    return IMGS / "actions" / action if style == STYLES[0] else IMGS / "styles" / style / action


def main() -> None:
    QGuiApplication(sys.argv)
    for action, states in KEYS.items():
        for n, state in enumerate(states):
            if state is None:
                continue
            for style in STYLES:
                folder = key_folder(style, action)
                folder.mkdir(parents=True, exist_ok=True)
                for size, name in ((72, f"key{n}.png"), (144, f"key{n}@2x.png")):
                    key_image(style, action, state[0], state[1], size).save(str(folder / name))
        both(svg_icon(states[0][0]), 20, IMGS / "actions" / action / "icon", render_icon)
        offline = IMGS / "offline"
        offline.mkdir(parents=True, exist_ok=True)
        offline_image(states[0][0], 144).save(str(offline / f"{action}.png"))
    tile = (BRAND / "sprigcast-app-icon.svg").read_text(encoding="utf-8")
    both(tile, 256, IMGS / "plugin" / "marketplace")
    render(tile, 288, IMGS / "plugin" / "store-icon-288.png")
    logo = (BRAND / "sprigcast-s-white.svg").read_text(encoding="utf-8")  # the S, not the sloth
    both(logo, 28, IMGS / "plugin" / "category-icon")
    print("images written to", IMGS)


if __name__ == "__main__":
    main()
