import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QPushButton  # noqa: E402

from sprigcast.app.fit import AdaptiveRow, FitButton  # noqa: E402

app = QApplication.instance() or QApplication([])


def test_button_shortens_when_narrow_and_comes_back():
    b = FitButton("Start recording somewhere long")
    b.resize(400, 30)
    b.show()
    app.processEvents()
    assert b.text() == "Start recording somewhere long" and b.toolTip() == ""
    b.resize(70, 30)
    app.processEvents()
    assert b.text().endswith("…") and b.full_text() == "Start recording somewhere long"
    assert b.toolTip() == "Start recording somewhere long"
    assert b.minimumSizeHint().width() < QPushButton("Start recording somewhere long").minimumSizeHint().width()
    b.setText("Stop")  # the app relabels buttons: the new text is what counts
    b.resize(400, 30)
    app.processEvents()
    assert b.text() == "Stop" and b.full_text() == "Stop"
    b.close()


def test_hidden_button_keeps_full_text():
    b = FitButton("Replay buffer")
    b.resize(10, 10)
    assert b.text() == "Replay buffer"


def test_row_stacks_when_squeezed():
    row = AdaptiveRow(FitButton("Replay buffer"), FitButton("Save replay"))
    row.resize(600, 40)
    row.show()
    app.processEvents()
    assert not row.is_stacked()
    row.resize(120, 80)
    app.processEvents()
    assert row.is_stacked()
    row.close()


def test_logo_loads_as_the_app_icon():
    from sprigcast.app.main import app_icon

    icon = app_icon()
    assert not icon.isNull() and not icon.pixmap(32, 32).isNull()
