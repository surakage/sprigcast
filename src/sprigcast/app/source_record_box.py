"""Settings → Recording: Source record, a second file without the sources you pick."""

from __future__ import annotations

from PySide6.QtWidgets import QCheckBox, QGridLayout, QGroupBox, QLabel, QVBoxLayout

from sprigcast.app.labels import literal
from sprigcast.app import theme
from sprigcast.app.audio_page import has_sound
from sprigcast.core.scene import SceneCollection


def pickable(collection: SceneCollection) -> list:
    """What a source record can leave out: anything placed in a bloom (a whole sprig too) and every sound."""
    placed = {item.source for scene in collection.scenes if scene.kind == "show" for item in scene.items}
    return [s for s in collection.sources
            if s.id in placed or (has_sound(s) and not collection.device_off(s))]


class SourceRecordBox(QGroupBox):
    def __init__(self, collection: SceneCollection):
        super().__init__("Source record")
        settings = collection.recording
        box = QVBoxLayout(self)
        box.setContentsMargins(18, 30, 18, 18)
        box.setSpacing(12)
        self.on = QCheckBox("Also record a copy without the sources ticked below")
        self.on.setToolTip("Another file per shape, next to the recording, e.g. game and webcam with no alerts.\n"
                           "It uses one more encoder while recording.")
        self.on.setChecked(settings.source_record)
        box.addWidget(self.on)
        hint = QLabel("Leave out")
        hint.setStyleSheet(f"color: {theme.MUTED}")
        box.addWidget(hint)
        grid = QGridLayout()
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(8)
        self.ticks: dict[str, QCheckBox] = {}
        hidden = set(settings.source_record_hide)
        for i, source in enumerate(pickable(collection)):
            tick = QCheckBox(literal(source.name))
            tick.setChecked(source.id in hidden)
            self.ticks[source.id] = tick
            grid.addWidget(tick, i // 2, i % 2)
        box.addLayout(grid)
        if not self.ticks:
            box.addWidget(QLabel("Nothing to leave out yet."))
        self.on.toggled.connect(self._toggled)
        self._toggled(self.on.isChecked())

    def _toggled(self, on: bool) -> None:
        for tick in self.ticks.values():
            tick.setEnabled(on)

    def apply_to(self, collection: SceneCollection) -> None:
        settings = collection.recording
        settings.source_record = self.on.isChecked()
        kept_unknown = [i for i in settings.source_record_hide if i not in self.ticks]  # not listed now
        settings.source_record_hide = kept_unknown + [i for i, t in self.ticks.items() if t.isChecked()]
