"""The sloth pictures blend on the dark theme: no light halo or leftover background specks on their edges."""

from pathlib import Path

import numpy as np
import pytest

ART = Path(__file__).resolve().parents[1] / "src" / "sprigcast" / "app" / "art"
BRAND = Path(__file__).resolve().parents[1] / "brand" / "art"


@pytest.mark.parametrize("path", sorted(ART.glob("sloth-*.png")) + sorted(BRAND.glob("sloth-*.png")),
                         ids=lambda p: f"{p.parent.name}/{p.name}")
def test_no_white_fringe(path):
    from PIL import Image

    a = np.asarray(Image.open(path).convert("RGBA")).astype(np.int16)
    seen = a[..., 3] > 0
    clear = np.pad(~seen, 2, constant_values=True)
    near = np.zeros_like(seen)
    for dy in range(-2, 3):
        for dx in range(-2, 3):
            near |= clear[2 + dy:2 + dy + seen.shape[0], 2 + dx:2 + dx + seen.shape[1]]
    edge = seen & near  # the outermost 2 px of the picture
    mx, mn = a[..., :3].max(2), a[..., :3].min(2)
    white = edge & (mn > 210) & (mx - mn < 30)
    assert white.sum() <= edge.sum() * 0.01, f"{white.sum()} whitish edge pixels"  # a cup or steam may be white
