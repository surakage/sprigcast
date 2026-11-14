"""Stream settings page: one entry per place you go live (Twitch, YouTube, TikTok, ...)."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from sprigcast.app import theme
from sprigcast.app.live_stage import Switch
from sprigcast.core.scene import Output, SceneCollection
from sprigcast.output import twitch_auth
from sprigcast.output.keys import get_key, set_key
from sprigcast.output.stream import SERVICES, preset_size, sizes_for


def output_label(output: Output) -> str:
    return _output_label(output) + ("" if output.enabled else "  (off)")


def _output_label(output: Output) -> str:
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


PLACE_CARDS_STYLE = (
    f"#placeCards {{ background: transparent; border: none; outline: none; }}"
    f"#placeCards::item {{ background: {theme.PANEL}; color: {theme.TEXT}; border-radius: 14px;"
    f" padding: 10px 14px; margin-bottom: 8px; }}"
    f"#placeCards::item:hover {{ background: {theme.RAISED_HOVER}; }}"
    f"#placeCards::item:selected {{ background: {theme.SELECT_BG}; color: {theme.SELECT_TEXT}; }}"
)
REMOVE_STYLE = theme.SMALL_PILL_STYLE + f"QPushButton {{ color: {theme.BAD}; }}"


def _heading(text: str, size: int) -> QLabel:
    label = QLabel(text)
    font = label.font()
    font.setPixelSize(size)
    font.setWeight(QFont.Weight.DemiBold)
    label.setFont(font)
    return label


LABEL_W = 104  # every card's labels share one column, so the fields line up down the page


def _section(title: str) -> tuple[QWidget, QFormLayout]:
    """A soft rounded card with its title inside (no OBS-style group box frame)."""
    card = QFrame()
    card.setObjectName("streamCard")
    card.setStyleSheet(f"#streamCard {{ background: {theme.CARD}; border-radius: 18px; }}")
    outer = QVBoxLayout(card)
    outer.setContentsMargins(26, 20, 26, 22)
    outer.setSpacing(14)
    outer.addWidget(_heading(title, 15))
    form = QFormLayout()
    form.setContentsMargins(0, 0, 0, 0)
    form.setHorizontalSpacing(18)
    form.setVerticalSpacing(12)
    form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
    outer.addLayout(form)
    _FORMS.append(form)
    return card, form


_FORMS: list[QFormLayout] = []


def _line_up_labels(forms: list[QFormLayout]) -> None:
    for form in forms:
        for row in range(form.rowCount()):
            item = form.itemAt(row, QFormLayout.ItemRole.LabelRole)
            if item is not None and item.widget() is not None:
                item.widget().setFixedWidth(LABEL_W)
                item.widget().setStyleSheet(theme.MUTED_STYLE)


def place_card_text(output: Output) -> str:
    """Two lines for a place card: the service, then shape and quality (and off)."""
    service = SERVICES.get(output.service or "custom", SERVICES["custom"])
    detail = output.canvas
    if output.enhanced and output.service == "twitch":
        detail += " · Enhanced"
    elif output.size:
        detail += f" · {output.size[1]}p"
    if output.bandwidth_test and service.bandwidth_test:
        detail += " · test"
    if not output.enabled:
        detail += " · off"
    return f"{service.label}\n{detail}"


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
        root.setSpacing(0)
        _FORMS.clear()

        # Left: the places you stream to, as cards. "+ Add" sits on top so it never scrolls away.
        left = QVBoxLayout()
        left.setSpacing(10)
        top = QHBoxLayout()
        top.addWidget(_heading("Places", 17), 1)
        self.add_button = QPushButton("+ Add")
        self.add_button.setStyleSheet(theme.SMALL_PILL_STYLE)
        self.add_button.setToolTip("Add another place to stream")
        self.add_button.clicked.connect(self._add)
        top.addWidget(self.add_button)
        left.addLayout(top)
        self.list = QListWidget()
        self.list.setObjectName("placeCards")
        self.list.setFixedWidth(220)
        self.list.setFrameShape(QListWidget.Shape.NoFrame)
        self.list.setStyleSheet(PLACE_CARDS_STYLE)
        self.list.currentRowChanged.connect(self._load)
        left.addWidget(self.list, 1)
        root.addLayout(left)
        root.addSpacing(22)
        divider = QFrame()
        divider.setFixedWidth(1)
        divider.setStyleSheet(f"background: {theme.EDGE};")
        root.addWidget(divider)
        root.addSpacing(6)

        # Right: the chosen place, scrolling on its own.
        self.form_widget = QWidget()
        groups = QVBoxLayout(self.form_widget)
        groups.setContentsMargins(18, 2, 14, 12)
        groups.setSpacing(16)
        hero = QHBoxLayout()
        hero.setSpacing(12)
        self.hero = _heading("", 22)
        hero.addWidget(self.hero, 1)
        self.enabled = Switch()
        self.enabled.setToolTip("On: Go live sends here. Off: skipped.")
        self.enabled.toggled.connect(self._edited)
        self.enabled_label = QLabel("Go live here")
        self.enabled_label.setStyleSheet(theme.MUTED_STYLE)
        hero.addWidget(self.enabled_label)
        hero.addWidget(self.enabled)
        self.remove_button = QPushButton("Remove")
        self.remove_button.setStyleSheet(REMOVE_STYLE)
        self.remove_button.setToolTip("Remove this place (and its saved stream key)")
        self.remove_button.clicked.connect(self._remove)
        hero.addWidget(self.remove_button)
        groups.addLayout(hero)
        groups.addSpacing(2)

        # Account: which service and how SprigCast signs in.
        destination, form = _section("Account")
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
        self.help.setStyleSheet(f"color: {theme.MUTED}")
        form.addRow("", self.help)
        groups.addWidget(destination)

        # Picture: what this place gets from its canvas.
        video, form = _section("Picture")
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
        form.addRow("Bitrate", self.bitrate)
        self.video_note = QLabel("Encoder settings (rate control, preset, keyframes…) are on the Output page.")
        self.video_note.setWordWrap(True)
        self.video_note.setStyleSheet(f"color: {theme.MUTED}")
        self.auto_bitrate = QCheckBox("Auto-pilot bitrate")
        self.auto_bitrate.setToolTip("When your upload can't keep up, lower the bitrate smoothly instead of "
                                     "dropping frames, then climb back to this bitrate once it recovers")
        self.auto_bitrate.toggled.connect(self._edited)
        form.addRow("", self.auto_bitrate)
        form.addRow("", self.video_note)
        self.video_box = video
        groups.addWidget(video)

        # Twitch Enhanced Broadcasting: Twitch picks several qualities for this PC, all sent at once.
        self.enhanced_box, form = _section("Twitch Enhanced Broadcasting")
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
        self.enhanced_note.setStyleSheet(f"color: {theme.MUTED}")
        form.addRow("", self.enhanced_note)
        groups.addWidget(self.enhanced_box)

        # Advanced options.
        advanced, form = _section("Extras")
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
        note.setStyleSheet(f"color: {theme.MUTED}")
        form.addRow("", note)
        groups.addWidget(advanced)

        groups.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setObjectName("streamScroll")
        self.form_widget.setObjectName("streamForm")
        scroll.setStyleSheet("#streamScroll, #streamForm { background: transparent; }")
        scroll.viewport().setAutoFillBackground(False)
        scroll.setWidget(self.form_widget)
        root.addWidget(scroll, 1)
        _line_up_labels(_FORMS)
        _FORMS.clear()

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
            self.list.addItem(place_card_text(output))
        self._loading = False
        self.form_widget.setEnabled(bool(self.outputs))
        if not self.outputs:
            self.hero.setText("Nothing here yet. Press + Add.")
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
        self.enabled.setChecked(output.enabled)
        self.hero.setText(SERVICES.get(output.service or "custom", SERVICES["custom"]).label)
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
        self.auto_bitrate.setChecked(output.auto_bitrate)
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
        output.enabled = self.enabled.isChecked()
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
        output.auto_bitrate = self.auto_bitrate.isChecked()
        if not output.key_ref:
            output.key_ref = f"sprigcast/{output.id}"
        self.keys[output.key_ref] = self.key.text().strip()
        self._show_service(output)
        self.list.item(self.list.currentRow()).setText(place_card_text(output))
        self.hero.setText(SERVICES.get(output.service or "custom", SERVICES["custom"]).label)

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
