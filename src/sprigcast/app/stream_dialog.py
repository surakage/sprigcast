"""Stream settings page: one entry per place you go live (Twitch, YouTube, TikTok, ...)."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
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
from sprigcast.output import twitch_auth
from sprigcast.output.keys import get_key, set_key
from sprigcast.output.stream import SERVICES, preset_size, sizes_for


def output_label(output: Output) -> str:
    service = SERVICES.get(output.service or "custom", SERVICES["custom"])
    label = f"{service.label}  ·  {output.canvas}"
    if output.enhanced and output.service == "twitch":
        return label + ("  ·  Enhanced + vertical" if output.enhanced_vertical else "  ·  Enhanced")
    if output.size:
        label += f" {output.size[0]}×{output.size[1]}"
    if output.fps:
        label += f" {output.fps} fps"
    if output.bandwidth_test and service.bandwidth_test:
        label += "  (test)"
    return label


class StreamPage(QWidget):
    """Edits copies of the collection's stream outputs; apply_to() writes them back and saves keys."""

    def __init__(self, collection: SceneCollection, parent: QWidget | None = None):
        super().__init__(parent)
        self.collection = collection
        self.outputs = [o.model_copy() for o in collection.outputs if o.type == "stream"]
        self.keys = {o.key_ref: get_key(o.key_ref) or "" for o in self.outputs if o.key_ref}
        self._saved_keys = dict(self.keys)  # only keys that change are written back
        self._loading = False

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
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
        groups = QVBoxLayout(self.form_widget)
        groups.setContentsMargins(0, 0, 0, 0)

        # Destination: where the stream goes and how SprigCast signs in.
        destination = QGroupBox("Destination")
        form = QFormLayout(destination)
        self.service = QComboBox()
        for key, service in SERVICES.items():
            self.service.addItem(service.label, key)
        self.service.currentIndexChanged.connect(self._service_changed)
        form.addRow("Service", self.service)
        self.server = QLineEdit()
        self.server.setPlaceholderText("rtmp://… or rtmps://…")
        self.server.textEdited.connect(self._edited)
        form.addRow("Server", self.server)
        # Twitch: connect the account instead of pasting the key.
        self.twitch_row = QWidget()
        twitch = QHBoxLayout(self.twitch_row)
        twitch.setContentsMargins(0, 0, 0, 0)
        self.twitch_label = QLabel()
        self.twitch_button = QPushButton()
        self.twitch_button.clicked.connect(self._twitch_clicked)
        twitch.addWidget(self.twitch_button)
        twitch.addWidget(self.twitch_label, 1)
        self.account_label = QLabel("Account")
        form.addRow(self.account_label, self.twitch_row)
        self.use_account = QCheckBox("Get the stream key from my Twitch account when I go live")
        self.use_account.toggled.connect(self._edited)
        form.addRow("", self.use_account)
        self.key_row = QWidget()
        key_row = QHBoxLayout(self.key_row)
        key_row.setContentsMargins(0, 0, 0, 0)
        self.key = QLineEdit()
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key.textEdited.connect(self._edited)
        show = QCheckBox("Show")
        show.toggled.connect(
            lambda on: self.key.setEchoMode(QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password)
        )
        key_row.addWidget(self.key, 1)
        key_row.addWidget(show)
        self.key_label = QLabel("Stream key")
        form.addRow(self.key_label, self.key_row)
        self.help = QLabel()
        self.help.setWordWrap(True)
        self.help.setStyleSheet("color: gray")
        form.addRow("", self.help)
        groups.addWidget(destination)

        # Video: what this destination gets from its canvas.
        video = QGroupBox("Video")
        form = QFormLayout(video)
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
        self.video_note = QLabel("Encoder settings (rate control, preset, keyframes…) are in Settings → Output.")
        self.video_note.setWordWrap(True)
        self.video_note.setStyleSheet("color: gray")
        form.addRow("", self.video_note)
        self.video_box = video
        groups.addWidget(video)

        # Twitch Enhanced Broadcasting: Twitch picks several qualities for this PC, all sent at once.
        self.enhanced_box = QGroupBox("Multitrack Video")
        form = QFormLayout(self.enhanced_box)
        intro = QLabel(
            "Twitch picks your stream qualities, and can take a vertical version too. "
            "Turning this on sends Twitch details about your PC and software. "
            '<a href="https://help.twitch.tv/s/article/multiple-encodes">Learn More</a>'
        )
        intro.setWordWrap(True)
        intro.setOpenExternalLinks(True)
        form.addRow(intro)
        self.enhanced = QCheckBox("Enable Enhanced Broadcasting")
        self.enhanced.toggled.connect(self._edited)
        form.addRow("", self.enhanced)
        self.enhanced_kbps_auto, self.enhanced_kbps = QCheckBox("Auto"), QSpinBox()
        self.enhanced_kbps.setRange(0, 100000)
        self.enhanced_kbps.setSingleStep(500)
        self.enhanced_kbps.setSuffix(" Kbps")
        form.addRow("Maximum streaming bandwidth", self._auto_row(self.enhanced_kbps_auto, self.enhanced_kbps, 8000))
        self.enhanced_tracks_auto, self.enhanced_tracks = QCheckBox("Auto"), QSpinBox()
        self.enhanced_tracks.setRange(0, 5)
        form.addRow("Maximum video tracks", self._auto_row(self.enhanced_tracks_auto, self.enhanced_tracks, 3))
        self.extra_canvas = QComboBox()
        self.extra_canvas.addItem("None", "")
        for canvas in collection.canvases:
            if canvas.height > canvas.width:
                self.extra_canvas.addItem(f"{canvas.id.capitalize()} ({canvas.width}×{canvas.height})", canvas.id)
        self.extra_canvas.currentIndexChanged.connect(self._edited)
        form.addRow("Additional canvas", self.extra_canvas)
        self.enhanced_note = QLabel(
            "Twitch sets sizes and bitrates; the Video settings above are the fallback. Needs an NVIDIA or Intel Arc GPU."
        )
        self.enhanced_note.setWordWrap(True)
        self.enhanced_note.setStyleSheet("color: gray")
        form.addRow("", self.enhanced_note)
        groups.addWidget(self.enhanced_box)

        # Advanced options.
        advanced = QGroupBox("Advanced options")
        form = QFormLayout(advanced)
        self.bandwidth_test = QCheckBox("Enable bandwidth test mode (send to Twitch without going live)")
        self.bandwidth_test.toggled.connect(self._edited)
        form.addRow("", self.bandwidth_test)
        self.ignore_recommendations = QCheckBox("Ignore streaming service setting recommendations")
        self.ignore_recommendations.toggled.connect(self._edited)
        form.addRow("", self.ignore_recommendations)
        self.limits = QLabel()
        form.addRow("", self.limits)
        note = QLabel("Keys are saved in Windows Credential Manager, never in the scenes file.")
        note.setWordWrap(True)
        note.setStyleSheet("color: gray")
        form.addRow("", note)
        groups.addWidget(advanced)

        right.addWidget(self.form_widget)
        right.addStretch(1)
        root.addLayout(right, 3)

        self._refresh_list(0)

    def _auto_row(self, auto: QCheckBox, spin: QSpinBox, start: int) -> QWidget:
        """An "Auto" tick beside a number box, like OBS: ticked means 0 (let Twitch decide)."""
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(auto)
        layout.addWidget(spin, 1)
        def toggled(on: bool) -> None:
            spin.setEnabled(not on)
            if not on and not spin.value() and not self._loading:
                spin.setValue(start)  # a sensible first value instead of 0 (which means Auto)

        auto.toggled.connect(toggled)
        auto.toggled.connect(self._edited)
        spin.valueChanged.connect(self._edited)
        return row

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

    def select_output(self, output_id: str) -> None:
        row = next((i for i, o in enumerate(self.outputs) if o.id == output_id), None)
        if row is not None:
            self.list.setCurrentRow(row)

    def add_output(self) -> None:
        self._add()

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
        self.use_account.setChecked(output.twitch_account)
        self.enhanced.setChecked(output.enhanced)
        self.extra_canvas.setCurrentIndex(1 if output.enhanced_vertical and self.extra_canvas.count() > 1 else 0)
        for auto, spin, value in ((self.enhanced_kbps_auto, self.enhanced_kbps, output.enhanced_max_kbps),
                                  (self.enhanced_tracks_auto, self.enhanced_tracks, output.enhanced_max_tracks)):
            auto.setChecked(not value)
            spin.setValue(value)
            spin.setEnabled(bool(value))
        self.ignore_recommendations.setChecked(output.ignore_recommendations)
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
            tip += (f"  {service.label} accepts up to {service.max_kbps} kbps"
                    + (", so SprigCast sends that much." if not output.ignore_recommendations else "."))
        self.help.setText(tip)
        self.limits.setText(f"Maximum video bitrate: {service.max_kbps} kbps\nMaximum audio bitrate: 320 kbps")
        self.limits.setVisible(not output.ignore_recommendations)
        self.bandwidth_test.setVisible(service.bandwidth_test)
        is_twitch = (output.service or "") == "twitch"
        self.twitch_row.setVisible(is_twitch)
        self.account_label.setVisible(is_twitch)
        self.enhanced_box.setVisible(is_twitch)
        for widget in (self.enhanced_kbps_auto, self.enhanced_tracks_auto, self.extra_canvas, self.enhanced_note):
            widget.setEnabled(output.enhanced)
        self.enhanced_kbps.setEnabled(output.enhanced and bool(output.enhanced_max_kbps))
        self.enhanced_tracks.setEnabled(output.enhanced and bool(output.enhanced_max_tracks))
        account = twitch_auth.connected_account() if is_twitch else None
        self.use_account.setVisible(is_twitch and account is not None)
        # With the key fetched from the account there is nothing to paste, as in OBS.
        hide_key = is_twitch and account is not None and output.twitch_account
        self.key_row.setVisible(not hide_key)
        self.key_label.setVisible(not hide_key)
        if is_twitch:
            self.twitch_label.setText(f"Connected as {account}" if account else "Not connected")
            self.twitch_button.setText("Disconnect Account" if account else "Connect Account (recommended)")

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
        output.twitch_account = output.service == "twitch" and self.use_account.isChecked()
        output.enhanced = output.service == "twitch" and self.enhanced.isChecked()
        output.enhanced_vertical = output.enhanced and bool(self.extra_canvas.currentData())
        output.enhanced_max_tracks = 0 if self.enhanced_tracks_auto.isChecked() else self.enhanced_tracks.value()
        output.enhanced_max_kbps = 0 if self.enhanced_kbps_auto.isChecked() else self.enhanced_kbps.value()
        output.ignore_recommendations = self.ignore_recommendations.isChecked()
        if not output.key_ref:
            output.key_ref = f"sprigcast/{output.id}"
        self.keys[output.key_ref] = self.key.text().strip()
        self._show_service(output)
        self.list.item(self.list.currentRow()).setText(output_label(output))

    def _twitch_clicked(self) -> None:
        output = self._current()
        if output is None:
            return
        if twitch_auth.connected_account():
            try:
                twitch_auth.disconnect()
            except Exception as exc:
                QMessageBox.warning(self, "SprigCast", f"Could not remove the Twitch sign-in:\n{exc}")
            self.use_account.setChecked(False)
            self._show_service(output)
            return
        from sprigcast.app.twitch_connect import TwitchConnectDialog

        dialog = TwitchConnectDialog(self)
        if not dialog.exec():
            return
        if dialog.stream_key:
            self.key.setText(dialog.stream_key)  # saved too, so going live still works if Twitch is unreachable
        self._show_service(output)
        self.use_account.setChecked(True)
        self._edited()

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
        collection.outputs = others + [o.model_copy() for o in self.outputs]  # copies: later edits wait for Apply
        return True
