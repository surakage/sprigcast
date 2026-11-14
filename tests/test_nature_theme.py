import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtCore import QRectF  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from sprigcast.app import theme  # noqa: E402


def test_leaf_fills_its_box_tip_up_right():
    rect = QRectF(10, 20, 40, 40)
    box = theme.leaf_path(rect).boundingRect()
    assert rect.adjusted(-1, -1, 1, 1).contains(box) and box.width() > 30 and box.height() > 30
    path = theme.leaf_path(rect)
    assert path.contains(rect.center()) and not path.contains(rect.topLeft() + QRectF(0, 0, 4, 4).center())


def test_paint_leaf_draws_green(tmp_path):
    QApplication.instance() or QApplication([])
    image = QImage(40, 40, QImage.Format.Format_ARGB32)
    image.fill(0)
    p = QPainter(image)
    theme.paint_leaf(p, QRectF(4, 4, 32, 32), True)
    p.end()
    accent = QColor(theme.ACCENT)
    green = sum(image.pixelColor(x, y) == accent for x in range(40) for y in range(40))
    assert green > 300  # a filled leaf, not just an outline


def test_style_sheet_uses_leaf_and_seed_pictures(tmp_path):
    rules = theme.nature_rules(str(tmp_path))
    pictures = theme.nature_pictures(str(tmp_path))
    assert set(pictures) == set(theme.NATURE_PICTURES)
    for path in pictures.values():
        assert path.endswith(".svg") and "<svg" in open(path, encoding="utf-8").read()
        assert "\\" not in path
    assert "QCheckBox::indicator:checked {" in rules and pictures["leaf_on"] in rules
    assert pictures["seed_on"] in rules and pictures["seed_handle"] in rules


def test_vine_progress_and_napping_sloth(tmp_path):
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    from sprigcast.app import theme
    from sprigcast.app.empty import EmptyState, NappingSloth

    pics = theme.nature_pictures(str(tmp_path))
    assert "vine" in pics and "<path" in open(pics["vine"], encoding="utf-8").read()
    rules = theme.nature_rules(str(tmp_path))
    assert "QProgressBar::chunk" in rules and "repeat-x" in rules
    clicked = []
    empty = EmptyState("Nothing here yet", "+ Add", lambda: clicked.append(1))
    empty.button.click()
    assert clicked == [1] and empty.label.text() == "Nothing here yet"
    sloth = NappingSloth(0.5)
    assert (sloth.width(), sloth.height()) == (66, 39)
    image = sloth.grab().toImage()
    painted = sum(1 for x in range(0, image.width(), 3) for y in range(0, image.height(), 3)
                  if image.pixelColor(x, y).alpha() > 0)
    assert painted > 20
    app.processEvents()
