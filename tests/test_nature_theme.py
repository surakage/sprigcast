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


def test_tick_box_is_the_leaf_cut_box(tmp_path):
    pictures = theme.nature_pictures(str(tmp_path))
    on = open(pictures["leaf_on"], encoding="utf-8").read()
    assert theme.LEAFCUT_D in on and theme.TICK_D in on  # the box, ticked
    assert theme.TICK_D not in open(pictures["leaf_off"], encoding="utf-8").read()
    image = QImage(48, 48, QImage.Format.Format_ARGB32)
    image.fill(0)
    p = QPainter(image)
    theme.paint_tick_box(p, QRectF(0, 0, 48, 48), True)
    p.end()
    # Round top-left corner, nearly square top-right one: the leaf cut
    assert image.pixelColor(7, 7).alpha() == 0 and image.pixelColor(41, 7).alpha() > 0


def test_style_sheet_uses_leaf_and_seed_pictures(tmp_path):
    rules = theme.nature_rules(str(tmp_path))
    pictures = theme.nature_pictures(str(tmp_path))
    assert set(pictures) == set(theme.NATURE_PICTURES)
    for path in pictures.values():
        assert path.endswith(".svg") and "<svg" in open(path, encoding="utf-8").read()
        assert "\\" not in path
    assert "QCheckBox::indicator:checked {" in rules and pictures["leaf_on"] in rules
    assert pictures["seed_on"] in rules and pictures["seed_handle"] in rules


def test_plain_progress_and_napping_sloth(tmp_path):
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    from sprigcast.app import theme
    from sprigcast.app.empty import EmptyState, NappingSloth

    assert "QProgressBar::chunk" not in theme.nature_rules(str(tmp_path))  # plain progress bars
    clicked = []
    empty = EmptyState("Nothing here yet", "+ Add", lambda: clicked.append(1))
    empty.button.click()
    assert clicked == [1] and empty.label.text() == "Nothing here yet"
    sloth = NappingSloth(0.5)
    assert (sloth.width(), sloth.height()) == (75, 48)
    image = sloth.grab().toImage()
    from sprigcast.app.empty import sloth_art

    assert not sloth_art().isNull()
    painted = sum(1 for x in range(0, image.width(), 3) for y in range(0, image.height(), 3)
                  if image.pixelColor(x, y).alpha() > 0)
    assert painted > 20
    app.processEvents()


def test_welcome_and_live_art():
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QWidget

    app = QApplication.instance() or QApplication([])
    from sprigcast.app.empty import ArtPicture, LiveCheer, art

    for name in ("napping", "welcome", "live"):
        assert not art(name).isNull(), name
    welcome = ArtPicture("welcome", 240)
    shape = art("welcome").height() / art("welcome").width()
    assert welcome.width() == 240 and abs(welcome.height() - 240 * shape) <= 1  # keeps the picture's shape
    stage = QWidget()
    stage.resize(900, 600)
    stage.show()
    cheer = LiveCheer(stage)
    cheer.cheer("Twitch, YouTube")
    app.processEvents()
    assert cheer.isVisible() and cheer.where.text() == "On Twitch, YouTube"
    assert cheer.y() == 24 and abs(cheer.x() + cheer.width() / 2 - 450) <= 1
    cheer.picture._step()
    assert cheer.picture.phase > 0
    stage.close()
