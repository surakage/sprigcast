"""The app icon: the bare S on transparency, every .ico size drawn from the vector."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packaging"))

pytest.importorskip("PySide6")
PIL = pytest.importorskip("PIL.Image")


def test_logo_has_no_tile():
    svg = (ROOT / "src" / "sprigcast" / "data" / "logo.svg").read_text()
    assert "<rect" not in svg


def test_ico_has_every_size_transparent_and_drawn_from_vector(tmp_path):
    import make_icon
    from PIL import Image, ImageChops

    out = tmp_path / "sprigcast.ico"
    make_icon.main(str(out))
    ico = Image.open(out)
    assert sorted(s for s, _ in ico.info["sizes"]) == list(make_icon.SIZES)
    for size in make_icon.SIZES:
        ico.size = (size, size)
        frame = ico.copy().convert("RGBA")
        assert frame.size == (size, size)
        for corner in ((0, 0), (size - 1, 0), (0, size - 1), (size - 1, size - 1)):
            assert frame.getpixel(corner)[3] == 0, (size, corner)
        assert ImageChops.difference(frame, make_icon.render(size)).getbbox() is None, size
