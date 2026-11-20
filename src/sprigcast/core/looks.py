"""Colour looks (LUTs) and the shapes a source can be cut to.

A look is a 3D lookup table: for every input colour, the colour it becomes. SprigCast reads the
standard .cube format (Resolve, Premiere, Photoshop and most LUT packs export it), ships a few
built-in looks, and can bake a Color Correction filter into a .cube of your own.

Tables are float32 arrays shaped (size, size, size, 3) indexed [blue, green, red], which is the
.cube order (red changes fastest) and also the order a 3D texture wants (x = red).
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np

SIZE = 33  # grid points per side for built-in and baked looks

LOOKS: tuple[tuple[str, str], ...] = (
    ("canopy", "Canopy (rich greens)"),
    ("warm", "Warm sun"),
    ("cool", "Cool morning"),
    ("film", "Faded film"),
    ("teal_orange", "Teal and orange"),
    ("night", "Night"),
    ("vivid", "Vivid"),
    ("mono", "Black and white"),
)
FROM_FILE = "file"

SHAPES: tuple[tuple[str, str], ...] = (  # the Shape filter's presets, as listed (user, 2026-11-20: more shapes)
    ("rounded", "Rounded rectangle"),
    ("circle", "Circle"),
    ("squircle", "Squircle"),
    ("arch", "Arch"),
    ("heart", "Heart"),
    ("star", "Star"),
    ("flower", "Flower"),
    ("leaf", "Leaf"),
    ("diamond", "Diamond"),
    ("triangle", "Triangle"),
    ("pentagon", "Pentagon"),
    ("hexagon", "Hexagon"),
    ("octagon", "Octagon"),
    ("image", "My own shape (image)"),
)
# The number each shape has in the shaders (both engines). Fixed: new shapes take new numbers; 7 is the picture.
SHAPE_IDS = {"rounded": 1, "circle": 2, "hexagon": 3, "leaf": 4, "heart": 5, "star": 6, "image": 7,
             "diamond": 8, "octagon": 9, "pentagon": 10, "triangle": 11, "arch": 12, "flower": 13, "squircle": 14}

LUMA = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)


class LutError(ValueError):
    pass


def identity(size: int = SIZE) -> np.ndarray:
    ramp = np.linspace(0.0, 1.0, size, dtype=np.float32)
    b, g, r = np.meshgrid(ramp, ramp, ramp, indexing="ij")
    return np.stack([r, g, b], axis=-1)


def read_cube(path: str | Path) -> np.ndarray:
    """A .cube file as a (n, n, n, 3) table. Raises LutError if it isn't a 3D .cube."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise LutError(f"Can't open {Path(path).name}: {exc.strerror or exc}") from exc
    size = 0
    lo, hi = np.zeros(3, np.float32), np.ones(3, np.float32)
    rows: list[list[float]] = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        word = line.split()[0].upper()
        if word == "LUT_3D_SIZE":
            size = int(line.split()[1])
        elif word == "LUT_1D_SIZE":
            raise LutError("This is a 1D LUT. SprigCast needs a 3D .cube.")
        elif word == "DOMAIN_MIN":
            lo = np.array([float(v) for v in line.split()[1:4]], np.float32)
        elif word == "DOMAIN_MAX":
            hi = np.array([float(v) for v in line.split()[1:4]], np.float32)
        elif word[0].isdigit() or word[0] in "-.":
            parts = line.split()
            if len(parts) >= 3:
                rows.append([float(v) for v in parts[:3]])
    if not 2 <= size <= 128:
        raise LutError("Not a 3D .cube file (no LUT_3D_SIZE).")
    if len(rows) != size ** 3:
        raise LutError(f"The file says {size}³ colors but has {len(rows)}.")
    table = np.asarray(rows, np.float32).reshape(size, size, size, 3)
    span = np.where(hi - lo == 0, 1, hi - lo)
    return np.clip((table - lo) / span, 0.0, 1.0)


def write_cube(path: str | Path, table: np.ndarray, title: str = "SprigCast look") -> Path:
    size = table.shape[0]
    lines = [f'TITLE "{title}"', f"LUT_3D_SIZE {size}", "DOMAIN_MIN 0 0 0", "DOMAIN_MAX 1 1 1"]
    lines += [f"{r:.6f} {g:.6f} {b:.6f}" for r, g, b in table.reshape(-1, 3)]
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# Built-in looks -----------------------------------------------------------------------------------


def _luma(c: np.ndarray) -> np.ndarray:
    return (c @ LUMA)[..., None]


def _sat(c: np.ndarray, amount: float) -> np.ndarray:
    return _luma(c) + (c - _luma(c)) * amount


def _contrast(c: np.ndarray, amount: float, pivot: float = 0.5) -> np.ndarray:
    return (c - pivot) * amount + pivot


def _soft_s(c: np.ndarray, amount: float) -> np.ndarray:
    """A gentle S-curve: deeper shadows, brighter highlights, no clipping."""
    s = c * c * (3 - 2 * c)
    return c + (s - c) * amount


def look(name: str, size: int = SIZE) -> np.ndarray:
    c = identity(size)
    r, g, b = c[..., 0:1], c[..., 1:2], c[..., 2:3]
    y = _luma(c)
    if name == "canopy":
        greenish = np.clip((g - np.maximum(r, b)) * 3, 0, 1)
        out = c + greenish * np.concatenate([-0.03 * g, 0.06 * g, 0.01 * g], -1)
        out = out + np.concatenate([0.02 * y, 0.01 * y, -0.02 * y], -1)  # warm highlights
        out = _soft_s(_sat(out, 1.12), 0.25)
    elif name == "warm":
        out = np.concatenate([r * 1.06 + 0.015, g * 1.01, b * 0.88], -1)
        out = _soft_s(out, 0.15)
    elif name == "cool":
        out = np.concatenate([r * 0.92, g * 0.99 + 0.005, b * 1.07 + 0.02], -1)
        out = _soft_s(out, 0.1)
    elif name == "film":
        out = _sat(c, 0.82)
        out = 0.06 + out * 0.88  # lifted blacks, softer whites
        out = out + np.concatenate([0.02 + 0 * y, 0.005 + 0 * y, -0.015 + 0 * y], -1)
        out = _contrast(out, 0.95)
    elif name == "teal_orange":
        shadows, highlights = (1 - y) ** 2, y ** 2
        out = c + shadows * np.array([-0.06, 0.02, 0.06], np.float32) + highlights * np.array([0.07, 0.015, -0.07], np.float32)
        out = _soft_s(_sat(out, 1.08), 0.2)
    elif name == "night":
        out = np.concatenate([r * 0.85, g * 0.92, b * 1.05 + 0.03], -1) * 0.9
        out = _contrast(_sat(out, 0.85), 1.08, 0.4)
    elif name == "vivid":
        out = _soft_s(_sat(c, 1.35), 0.3)
    elif name == "mono":
        out = np.repeat(_soft_s(y, 0.3), 3, axis=-1)
    else:
        out = c
    return np.clip(out, 0.0, 1.0).astype(np.float32)


def bake_color_correction(settings: dict, size: int = SIZE) -> np.ndarray:
    """A Color Correction filter's colour change as a table (same maths as the compositor's shader)."""
    c = identity(size)
    g = float(settings.get("gamma", 0.0))
    gamma = 1.0 / (1.0 + g) if g >= 0 else 1.0 - g
    contrast = max(0.0, 1.0 + float(settings.get("contrast", 0.0)))
    brightness = float(settings.get("brightness", 0.0))
    saturation = max(0.0, 1.0 + float(settings.get("saturation", 0.0)))
    hue = math.radians(float(settings.get("hue_shift", 0.0)))
    tint_text = str(settings.get("color", "#ffffff")).lstrip("#")
    try:
        tint = np.array([int(tint_text[i:i + 2], 16) / 255 for i in (0, 2, 4)], np.float32)
    except ValueError:
        tint = np.ones(3, np.float32)
    c = np.power(np.maximum(c, 0.0), gamma)
    c = (c - 0.5) * contrast + 0.5 + brightness
    c = _sat(c, saturation)
    if hue:
        k = np.full(3, 0.57735, np.float32)
        cs, sn = math.cos(hue), math.sin(hue)
        c = c * cs + np.cross(k, c) * sn + k * (c @ k)[..., None] * (1 - cs)
    return np.clip(c * tint, 0.0, 1.0).astype(np.float32)


def apply(table: np.ndarray, rgb: np.ndarray) -> np.ndarray:
    """Look up colours (..., 3 floats 0..1) in a table, with trilinear blending. For previews and tests."""
    n = table.shape[0]
    pos = np.clip(rgb, 0, 1) * (n - 1)
    i0 = np.floor(pos).astype(int).clip(0, n - 2)
    f = pos - i0
    out = np.zeros_like(rgb, dtype=np.float32)
    for dr in (0, 1):
        for dg in (0, 1):
            for db in (0, 1):
                w = ((f[..., 0] if dr else 1 - f[..., 0]) * (f[..., 1] if dg else 1 - f[..., 1])
                     * (f[..., 2] if db else 1 - f[..., 2]))
                out += w[..., None] * table[i0[..., 2] + db, i0[..., 1] + dg, i0[..., 0] + dr]
    return out
