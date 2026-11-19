"""Get started: picking a chip never makes the card (and its chips) narrower (PC, 2026-11-19)."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtWidgets import QApplication  # noqa: E402


def test_picking_keeps_the_width():
    QApplication.instance() or QApplication([])
    from sprigcast.app.getting_started import GettingStartedDialog

    dialog = GettingStartedDialog()
    dialog.resize(900, dialog.height())
    dialog.show()
    QApplication.processEvents()
    width = dialog.width()
    chip = next(iter(dialog.content.values()))
    chip_width = chip.width()
    chip.click()
    dialog.live.click()  # shows the places row: taller, not narrower
    QApplication.processEvents()
    assert dialog.width() == width and chip.width() == chip_width
    dialog.close()
