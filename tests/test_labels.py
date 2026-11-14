import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def test_names_with_an_ampersand_show_as_typed():
    from PySide6.QtWidgets import QApplication, QCheckBox

    from sprigcast.app.labels import literal

    app = QApplication.instance() or QApplication([])
    assert literal("SFX & alerts") == "SFX && alerts"
    box = QCheckBox(literal("SFX & alerts"))
    # Qt shows "&&" as one "&" (and a single "&" would underline the next letter instead)
    assert box.text().replace("&&", "&") == "SFX & alerts"
    assert app is not None
