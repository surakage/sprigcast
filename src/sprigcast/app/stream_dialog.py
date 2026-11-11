"""Stream settings: one entry per place you go live (Twitch, YouTube, TikTok, ...)."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from sprigcast.core.scene import Output, SceneCollection
from sprigcast.output.keys import get_key, set_key
from sprigcast.output.stream import SERVICES, preset_size, sizes_for


def output_label(output: Output) -> str:
    service = SERVICES.get(output.service or "custom", SERVICES["custom"])
    label = f"{service.label}  ·  {output.canvas}"
    if output.size:
        label += f" {output.size[0]}×{output.size[1]}"
    if output.fps:
        label += f" {output.fps} fps"
    if output.bandwidth_test and service.bandwidth_test:
        label += "  (test)"
    return label


class StreamDialog(QDialog):
    """Edits copies of the collection's stream outputs; apply_to() writes them back and saves keys."""

    def __init__(self, collection: SceneCollection, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Stream settings")
        self.setMinimumWidth(720)
        self.collection = collection
        self.outputs = [o.model_copy() for o in collection.outputs if o.type == "stream"]
        self.keys = {o.key_ref: get_key(o.key_ref) or "" for o in self.outputs if o.key_ref}
        self._saved_keys = dict(self.keys)  # only keys that change are written back
        self._loading = False

        root = QHBoxLayout(self)
        left = QVBoxLayout()
        self.list = QListWidget()
        self.list.currentRowChanged.connect(self._load)
        left.addWidget(self.list)
        buttons = QHBoxLayout()
        add, remove = QPushButton("Add"), QPushButton("Remove")
        add.clicked.connect(self._add)
        remove.clicked.connect(self._remove)
        buttons.addWidget(add)
        buttons.addWidget(remove)
        left.addLayout(buttons)
        root.addLayout(left, 1)

        right = QVBoxLayout()
        self.form_widget = QWidget()
        form = QFormLayout(self.form_widget)
        self.service = QComboBox()
        for key, service in SERVICES.items():
            self.service.addItem(service.label, key)
        self.service.currentIndexChanged.connect(self._service_changed)
        form.addRow("Service", self.service)
        self.server = QLineEdit()
        self.server.setPlaceholderText("rtmp://… or rtmps://…")
        self.server.textEdited.connect(self._edited)
        form.addRow("Server", self.server)
        self.canvas = QComboBox()
        for canvas in collection.canvases:
            self.canvas.addItem(f"{canvas.id} ({canvas.width}×{canvas.height})", canvas.id)
        self.canvas.currentIndexChanged.connect(self._canvas_changed)
        form.addRow("Canvas", self.canvas)
        self.size = QComboBox()
        self.size.currentIndexChanged.connect(self._edited)
        form.addRow("Resolution", self.size)
        self.fps = QComboBox()
        self.fps.currentIndexChanged.connect(self._edited)
        form.addRow("Frame rate", self.fps)
        self.bitrate = QSpinBox()
        self.bitrate.setRange(500, 50000)
        self.bitrate.setSingleStep(500)
        self.bitrate.setSuffix(" kbps")
        self.bitrate.valueChanged.connect(self._edited)
        form.addRow("Video bitrate", self.bitrate)
        key_row = QHBoxLayout()
        self.key = QLineEdit()
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key.textEdited.connect(self._edited)
        show = QCheckBox("Show")
        show.toggled.connect(
            lambda on: self.key.setEchoMode(QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password)
        )
        key_row.addWidget(self.key, 1)
        key_row.addWidget(show)
        form.addRow("Stream key", key_row)
        self.bandwidth_test = QCheckBox("Bandwidth test: send to Twitch without going live (viewers see nothing)")
        self.bandwidth_test.toggled.connect(self._edited)
        form.addRow("", self.bandwidth_test)
        self.help = QLabel()
        self.help.setWordWrap(True)
        self.help.setStyleSheet("color: gray")
        form.addRow("", self.help)
        note = QLabel("Keys are saved in Windows Credential Manager, never in the scenes file.")
        note.setWordWrap(True)
        note.setStyleSheet("color: gray")
        form.addRow("", note)
        right.addWidget(self.form_widget)
        right.addStretch(1)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        right.addWidget(box)
        root.addLayout(right, 2)

        self._refresh_list(0)

    # List ------------------------------------------------------------------------

    def _refresh_list(self, row: int) -> None:
        self._loading = True
        self.list.clear()
        for output in self.outputs:
            self.list.addItem(output_label(output))
        self._loading = False
        self.form_widget.setEnabled(bool(self.outputs))
        if self.outputs:
            self.list.setCurrentRow(max(0, min(row, len(self.outputs) - 1)))

    def _current(self) -> Output | None:
        row = self.list.currentRow()
        return self.outputs[row] if 0 <= row < len(self.outputs) else None

    def _add(self) -> None:
        taken = {o.id for o in self.outputs} | {o.id for o in self.collection.outputs}
        n = 1
        while f"out-{n}" in taken:
            n += 1
        output_id = f"out-{n}"
        self.outputs.append(
            Output(id=output_id, type="stream", service="twitch", canvas="horizontal",
                   bitrate_kbps=6000, key_ref=f"sprigcast/{output_id}", enabled=True)
        )
        self._refresh_list(len(self.outputs) - 1)

    def _remove(self) -> None:
        output = self._current()
        if output is None:
            return
        self.outputs.remove(output)
        if output.key_ref:
            self.keys[output.key_ref] = ""  # delete the saved key too
        self._refresh_list(self.list.currentRow())

    # Form ------------------------------------------------------------------------

    def _load(self, row: int) -> None:
        output = self._current()
        if output is None or self._loading:
            return
        self._loading = True
        self.service.setCurrentIndex(max(0, self.service.findData(output.service or "custom")))
        self.canvas.setCurrentIndex(max(0, self.canvas.findData(output.canvas)))
        self._fill_size_and_fps(output.size, output.fps)
        self.bitrate.setValue(output.bitrate_kbps)
        self.bandwidth_test.setChecked(output.bandwidth_test)
        self.key.setText(self.keys.get(output.key_ref or "", ""))
        self._show_service(output)
        self._loading = False

    def _canvas(self):
        canvas_id = self.canvas.currentData()
        return next((c for c in self.collection.canvases if c.id == canvas_id), self.collection.canvases[0])

    def _fill_size_and_fps(self, size: tuple[int, int] | None, fps: int | None) -> None:
        """Offer sizes that keep the canvas shape and frame rates up to the canvas's."""
        canvas = self._canvas()
        loading, self._loading = self._loading, True
        self.size.clear()
        for w, h in sizes_for(canvas):
            native = (w, h) == (canvas.width, canvas.height)
            self.size.addItem(f"{w}×{h}" + ("  (canvas)" if native else ""), "" if native else f"{w}x{h}")
        index = self.size.findData(f"{size[0]}x{size[1]}" if size else "")
        self.size.setCurrentIndex(max(0, index))
        self.fps.clear()
        for rate in sorted({canvas.fps, 30} if canvas.fps > 30 else {canvas.fps}, reverse=True):
            self.fps.addItem(f"{rate} fps" + ("  (canvas)" if rate == canvas.fps else ""), 0 if rate == canvas.fps else rate)
        self.fps.setCurrentIndex(max(0, self.fps.findData(fps or 0)))
        self._loading = loading

    def _show_service(self, output: Output) -> None:
        service = SERVICES.get(output.service or "custom", SERVICES["custom"])
        fixed = bool(service.server)
        self.server.setReadOnly(fixed)
        self.server.setText(service.server if fixed else output.server)
        tip = service.help
        if output.bitrate_kbps > service.max_kbps:
            tip += f"  {service.label} accepts up to {service.max_kbps} kbps."
        self.help.setText(tip)
        self.bandwidth_test.setVisible(service.bandwidth_test)

    def _service_changed(self) -> None:
        output = self._current()
        if output is None or self._loading:
            return
        output.service = self.service.currentData()
        service = SERVICES[output.service]
        # Apply the platform's preset: canvas, resolution, frame rate and bitrate.
        self._loading = True
        self.canvas.setCurrentIndex(max(0, self.canvas.findData(service.canvas)))
        canvas = self._canvas()
        size = preset_size(service, canvas)
        self._fill_size_and_fps(None if size == (canvas.width, canvas.height) else size,
                                None if service.fps >= canvas.fps else service.fps)
        self.bitrate.setValue(service.kbps)
        self._loading = False
        self._edited()

    def _canvas_changed(self) -> None:
        if self._loading:
            return
        self._fill_size_and_fps(None, None)
        self._edited()

    def _edited(self) -> None:
        output = self._current()
        if output is None or self._loading:
            return
        service = SERVICES.get(self.service.currentData(), SERVICES["custom"])
        output.service = self.service.currentData()
        output.server = "" if service.server else self.server.text().strip()
        output.canvas = self.canvas.currentData()
        size = self.size.currentData() or ""
        output.size = tuple(int(n) for n in size.split("x")) if size else None
        output.fps = self.fps.currentData() or None
        output.bitrate_kbps = self.bitrate.value()
        output.bandwidth_test = service.bandwidth_test and self.bandwidth_test.isChecked()
        if not output.key_ref:
            output.key_ref = f"sprigcast/{output.id}"
        self.keys[output.key_ref] = self.key.text().strip()
        self._show_service(output)
        self.list.item(self.list.currentRow()).setText(output_label(output))

    # Result ------------------------------------------------------------------------

    def apply_to(self, collection: SceneCollection) -> bool:
        """Write outputs back and save keys. Returns False (and says why) if a key could not be saved."""
        try:
            for key_ref, key in self.keys.items():
                if key != self._saved_keys.get(key_ref, ""):
                    set_key(key_ref, key)
        except Exception as exc:
            QMessageBox.critical(self, "SprigCast", f"Could not save the stream key:\n{exc}")
            return False
        others = [o for o in collection.outputs if o.type != "stream"]
        collection.outputs = others + self.outputs
        return True
