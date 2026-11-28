"""A source whose settings can't give a size never ends the drawing thread (sim PC, test build 16, 2026-11-28:
a color source's width set to "" raised outside the per-frame guard)."""
from sprigcast.render.engine import size_of


class _Broken:
    id, kind = "color-9", "color"

    @property
    def size(self):
        return int(""), 1080


class _Fine:
    size = (640.0, 360)


def test_a_source_that_cannot_say_its_size_counts_as_one_pixel_and_is_logged_once(caplog):
    broken = _Broken()
    with caplog.at_level("WARNING"):
        assert size_of(broken) == (1, 1)
        assert size_of(broken) == (1, 1)
    assert sum("can't say its size" in r.getMessage() for r in caplog.records) == 1
    assert size_of(_Fine()) == (640, 360)
