"""Stream settings page: one entry per place you go live (Twitch, YouTube, TikTok, ...)."""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from sprigcast.app import message as msgbox
from sprigcast.app.modal import modal
from sprigcast.app import theme
from sprigcast.app.live_stage import Switch
from sprigcast.core.scene import Output, SceneCollection
from sprigcast.output import service_signin, twitch_auth
from sprigcast.output.keys import get_key, set_key
from sprigcast.output.stream import SERVICES, SHOW_ALL, TOP_SERVICES, preset_size, sizes_for


def output_label(output: Output) -> str:
    return _output_label(output) + ("" if output.enabled else "  (off)")


def _output_label(output: Output) -> str:
    service = SERVICES.get(output.service or "custom", SERVICES["custom"])
    label = f"{service.label}  ·  {output.canvas}"
    if output.enhanced and output.service == "twitch":
        from sprigcast.output.enhanced import sends_vertical

        return label + ("  ·  Enhanced + vertical" if sends_vertical(output) else "  ·  Enhanced")
    if output.size:
        label += f" {output.size[0]}×{output.size[1]}"
    if output.fps:
        label += f" {output.fps} fps"
    if output.bandwidth_test and service.bandwidth_test:
        label += "  (test)"
    return label


PLACE_CARDS_STYLE = (
    f"#placeCards {{ background: transparent; border: none; outline: none; padding: 0px; }}"
    f"#placeCards::item {{ background: {theme.SECTION}; color: {theme.TEXT}; border: 1px solid {theme.SECTION_EDGE};"
    f" border-radius: 14px; padding: 10px 14px; margin-bottom: 8px; }}"
    f"#placeCards::item:hover {{ background: {theme.RAISED_HOVER}; }}"
    f"#placeCards::item:selected {{ background: {theme.SELECT_BG}; color: {theme.SELECT_TEXT};"
    f" border-color: {theme.ACCENT}; }}"
    f"QPushButton#streamAdd {{ background: transparent; color: {theme.LABEL}; border: 1px dashed {theme.FIELD_EDGE};"
    f" border-radius: 14px; padding: 10px 14px; font-weight: 600; }}"
    f"QPushButton#streamAdd:hover {{ color: {theme.TEXT_BRIGHT}; border-color: {theme.ACCENT}; }}"
    f"#streamListTitle {{ color: {theme.MUTED}; font-size: 12px; font-weight: 600; letter-spacing: 1px; }}"
)


def _heading(text: str, size: int) -> QLabel:
    label = QLabel(text)
    font = label.font()
    font.setPixelSize(size)
    font.setWeight(QFont.Weight.DemiBold)
    label.setFont(font)
    return label


LABEL_W = 104  # every card's labels share one column, so the fields line up down the page


def _section(title: str) -> tuple[QWidget, QFormLayout]:
    """A titled section card, like every Settings section: a leaf before the title."""
    card = QFrame()
    card.setObjectName("streamCard")  # styled by the page (PAGE_STYLE)
    outer = QVBoxLayout(card)
    outer.setContentsMargins(0, 0, 0, 22)
    outer.setSpacing(18)
    head = QHBoxLayout()
    head.setContentsMargins(24, 16, 24, 0)
    head.setSpacing(10)
    leaf = QLabel()
    leaf.setObjectName("streamLeaf")
    leaf.setFixedSize(14, 14)
    head.addWidget(leaf)
    head.addWidget(_heading(title, theme.SECTION_TITLE_PX), 1)
    outer.addLayout(head)
    form = QFormLayout()
    form.setContentsMargins(26, 0, 26, 0)
    form.setHorizontalSpacing(18)
    form.setVerticalSpacing(12)
    form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
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
                item.widget().setObjectName("streamLabel")  # styled by the page (PAGE_STYLE)
                if isinstance(item.widget(), QLabel):
                    item.widget().setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)


# The page's look in one style sheet, picked by object names: a style sheet on each card, label and note was
# 30-odd of them, and the page was 245 ms of Settings' warm-up slices on the PC (2026-11-18, just after start).
def _leaf_rule() -> str:
    try:
        pic = theme.nature_pictures()
    except OSError:  # can't write the picture: titles without the leaf
        return ""
    return f"#streamLeaf {{ image: url({pic['section_leaf']}); background: transparent; }}"


PAGE_STYLE = (
    f"#streamCard {{ background: {theme.SECTION}; border: 1px solid {theme.SECTION_EDGE}; border-radius: 20px; }}"
    f"#streamCard QLabel {{ color: {theme.LABEL}; }}"
    f"#streamLabel {{ color: {theme.LABEL}; }}"
    f"#streamMuted {{ color: {theme.MUTED}; }}"
    "#streamScroll, #streamForm { background: transparent; }"
    f"#streamWarn {{ color: {theme.WARN}; }}"
    f"#streamDivider {{ background: {theme.EDGE}; }}"
    + PLACE_CARDS_STYLE
    + theme.button_style(13, "6px 13px", "QPushButton#streamSmall, QPushButton#streamRemove")
    + f"QPushButton#streamRemove {{ color: {theme.BAD}; }}"
    + f"#streamCard QLineEdit, #streamCard QComboBox, #streamCard QSpinBox {{ background: {theme.FIELD};"
    f" border: 1px solid {theme.FIELD_EDGE}; border-radius: 10px; padding: 6px 13px; min-height: 20px; }}"
    + f"#streamCard QLineEdit:focus, #streamCard QComboBox:focus, #streamCard QSpinBox:focus"
    f" {{ border-color: {theme.ACCENT}; }}"
    + "#streamCard QComboBox::drop-down { border: none; width: 34px; background: transparent; }"
    + theme.combo_arrow_rule("#streamCard QComboBox")
    + _leaf_rule()
)


log = logging.getLogger(__name__)

NEW_PLACE = "New place"
PICK_SERVICE = "Pick a service"
ALL_PLACES = "__all__"  # the Service box's last line: opens the list of every place


def place_card_text(output: Output) -> str:
    """Two lines for a place card: the service, then shape and quality (and off)."""
    service = SERVICES.get(output.service or "custom", SERVICES["custom"])
    detail = output.canvas.capitalize()
    if output.enhanced and output.service == "twitch":
        detail += " · Enhanced"
    elif output.size:
        detail += f" · {output.size[1]}p"
    if output.bandwidth_test and service.bandwidth_test:
        detail += " · Test"
    if not output.enabled:
        detail += " · Off"
    return f"{service.label}\n{detail}"


class StreamPage(QWidget):
    """Edits copies of the collection's stream outputs; apply_to() writes them back and saves keys."""

    def __init__(self, collection: SceneCollection, parent: QWidget | None = None, later: bool = False):
        """later: only the list of places is made now; the caller makes the rest with take_part(), PARTS times,
        a tick apart (Settings: the whole page at once held the window 399 ms on the PC while live,
        2026-11-21). Anything that needs the form makes what's left first (finish)."""
        super().__init__(parent)
        self.setStyleSheet(PAGE_STYLE)
        self.collection = collection
        self.outputs = [o.model_copy() for o in collection.outputs if o.type == "stream"]
        self.other_paths_ids: set[str] = set()  # places in other paths (their ids name their keys)
        # Keys are read from the password store when a place is first shown, not all up front: each read can
        # take a while on Windows, and opening a place from Live waited for every one (2026-11-17).
        self.keys: dict[str, str] = {}
        self._saved_keys: dict[str, str | None] = {}  # what the store held (None: never read); only changes are written
        self._loading = False
        self._unpicked: set[str] = set()  # places just added: no service chosen yet, nothing filled in
        self._error_for = ""  # the place the last sign-in error belongs to
        self.live_ids: set[str] = set()  # the places sending right now (set_live)
        self._expert = False
        self._in_part = self._built = False

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        _FORMS.clear()

        # Left: the places you stream to, as cards, with "+ Add place" right under the last one (the list is as tall
        # as its cards, so the button never scrolls away; with many places the list scrolls above it).
        left = QVBoxLayout()
        left.setSpacing(10)
        title = QLabel("PLACES")
        title.setObjectName("streamListTitle")
        left.addWidget(title)
        self.list = QListWidget()
        self.list.setObjectName("placeCards")
        self.list.setFixedWidth(220)
        self.list.setFrameShape(QListWidget.Shape.NoFrame)
        self.list.setSizeAdjustPolicy(QListWidget.SizeAdjustPolicy.AdjustToContents)
        self.list.currentRowChanged.connect(self._load)
        self.list.model().rowsInserted.connect(lambda *_: self._fit_list())
        self.list.model().rowsRemoved.connect(lambda *_: self._fit_list())
        # Drag a place to a new spot in the list (the order Live's Destinations shows them in too); Alt+Up and
        # Alt+Down do the same from the keyboard.
        self.list.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        self.list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.list.setDragDropOverwriteMode(False)
        self.list.setToolTip("Drag to reorder")
        self.list.model().rowsMoved.connect(lambda *_: self._order_changed())
        self.list.model().rowsInserted.connect(lambda *_: self._order_changed())  # (a drop done as take and put)
        for keys, step in (("Alt+Up", -1), ("Alt+Down", 1)):
            shortcut = QShortcut(QKeySequence(keys), self.list)
            shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
            shortcut.activated.connect(lambda step=step: self.move_place(step))
        left.addWidget(self.list)
        self.add_button = QPushButton("+ Add place")
        self.add_button.setObjectName("streamAdd")
        self.add_button.setFixedWidth(220)
        self.add_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_button.setToolTip("Add another place to stream")
        self.add_button.clicked.connect(self._add)
        left.addWidget(self.add_button)
        left.addStretch(1)
        root.addLayout(left)
        root.addSpacing(22)
        divider = QFrame()
        divider.setFixedWidth(1)
        divider.setObjectName("streamDivider")
        root.addWidget(divider)
        root.addSpacing(6)

        self._building = self._build(root)
        if later:
            self._loading = True  # the cards now, so the list is there at once; the first is picked at the end
            for output in self.outputs:
                item = QListWidgetItem(self._card_text(output))
                item.setData(Qt.ItemDataRole.UserRole, output.id)
                self.list.addItem(item)
            self._loading = False
        else:
            self.finish()

    PARTS = 5  # take_part() this many times makes the whole form

    def take_part(self) -> None:
        """Make the next part of the form (nothing once it's all made)."""
        if self._in_part:
            return
        self._in_part = True
        try:
            next(self._building, None)
        finally:
            self._in_part = False

    def finish(self) -> None:
        """Make whatever of the form is still to come, now."""
        if self._in_part:
            return
        self._in_part = True
        try:
            for _ in self._building:
                pass
        finally:
            self._in_part = False

    def _build(self, root):
        """The form on the right, a part at each step: the heading and Account; Server and key and Picture;
        Twitch Enhanced Broadcasting and Extras; all of it put on the page; then the first place shown."""
        collection = self.collection
        # Right: the chosen place, scrolling on its own.
        self.form_widget = QWidget()
        groups = QVBoxLayout(self.form_widget)
        groups.setContentsMargins(18, 2, 14, 12)
        groups.setSpacing(16)
        hero = QHBoxLayout()
        hero.setSpacing(12)
        self.hero = _heading("", 22)
        hero.addWidget(self.hero, 1)
        # Twitch's bandwidth test, up top beside Go live here so it's found at once (it sat in Extras).
        self.bandwidth_test = Switch()
        self.bandwidth_test.setToolTip("On: the stream reaches Twitch so you can check it, but viewers don't see it")
        self.bandwidth_test.toggled.connect(self._edited)
        self.bandwidth_label = QLabel("Test only")
        self.bandwidth_label.setObjectName("streamMuted")
        self.bandwidth_label.setToolTip(self.bandwidth_test.toolTip())
        hero.addWidget(self.bandwidth_label)
        hero.addWidget(self.bandwidth_test)
        hero.addSpacing(6)
        self.enabled = Switch()
        self.enabled.setToolTip("On: Go live sends here. Off: skipped.")
        self.enabled.toggled.connect(self._edited)
        self.enabled_label = QLabel("Go live here")
        self.enabled_label.setObjectName("streamMuted")
        hero.addWidget(self.enabled_label)
        hero.addWidget(self.enabled)
        self.remove_button = QPushButton("Remove")
        self.remove_button.setObjectName("streamRemove")
        self.remove_button.setToolTip("Remove this place (and its saved stream key)")
        self.remove_button.clicked.connect(self._remove)
        hero.addWidget(self.remove_button)
        groups.addLayout(hero)
        # A place that is sending right now can't be changed: its switch on the Live screen ends just that one.
        self.live_note = QLabel("Live now. Switch it off in Destinations to change it.")
        self.live_note.setObjectName("streamWarn")
        self.live_note.hide()
        groups.addWidget(self.live_note)
        groups.addSpacing(2)

        # Account: which service and how SprigCast signs in.
        destination, form = _section("Account")
        self.service = QComboBox()
        self.service.addItem(PICK_SERVICE, "")  # a new place starts here: you choose, nothing is assumed
        # The five everyday places and Custom RTMP; the rest are behind "Show all…" (a searchable A to Z list).
        for key in (*TOP_SERVICES, "custom"):
            self.service.addItem(SERVICES[key].label, key)
        self.service.addItem(SHOW_ALL, ALL_PLACES)
        self._service_was = 0
        self.service.currentIndexChanged.connect(self._service_picked)
        form.addRow("Service", self.service)
        self._account_form = form
        self.server = QLineEdit()
        self.server.setPlaceholderText("rtmp://… or rtmps://…")
        self.server.textEdited.connect(self._edited)
        # Twitch: connect the account instead of pasting the key.
        self.twitch_row = QWidget()
        twitch = QHBoxLayout(self.twitch_row)
        twitch.setContentsMargins(0, 0, 0, 0)
        self.twitch_label = QLabel()
        self.twitch_button = QPushButton()
        self.twitch_button.clicked.connect(self._twitch_clicked)
        self.twitch_sign_out = QPushButton("Sign out")
        self.twitch_sign_out.clicked.connect(lambda: self._sign_out("twitch"))
        twitch.setSpacing(12)
        twitch.addWidget(self.twitch_button)
        twitch.addWidget(self.twitch_label)
        twitch.addWidget(self.twitch_sign_out)
        twitch.addStretch(1)
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
        # YouTube and Kick: sign in to fetch the key; the place then says who it's signed in as.
        self.signin_row = QWidget()
        signin = QHBoxLayout(self.signin_row)
        signin.setContentsMargins(0, 0, 0, 0)
        self.sign_in_button = QPushButton("Sign in")
        self.sign_in_button.setToolTip("Sign in to fill in the stream key")
        self.sign_in_button.clicked.connect(self._sign_in_clicked)
        self.signin_state = QLabel()
        self.sign_out_button = QPushButton("Sign out")
        self.sign_out_button.clicked.connect(lambda: self._sign_out(self._current_service()))
        signin.setSpacing(12)
        signin.addWidget(self.sign_in_button)
        signin.addWidget(self.signin_state)
        signin.addWidget(self.sign_out_button)
        signin.addStretch(1)
        self.signin_label = QLabel("Account")
        form.addRow(self.signin_label, self.signin_row)
        self.signin_error = QLabel()
        self.signin_error.setWordWrap(True)
        self.signin_error.setObjectName("streamWarn")
        self.signin_error.hide()
        form.addRow("", self.signin_error)
        groups.addWidget(destination)

        yield
        # Server and key: where the stream goes.
        self._key_section, form = _section("Server and key")
        self._key_form = form
        form.addRow("Server", self.server)
        self.key_label = QLabel("Stream key")
        form.addRow(self.key_label, self.key_row)
        self.help = QLabel()
        self.help.setWordWrap(True)
        self.help.setObjectName("streamMuted")
        form.addRow("", self.help)
        groups.addWidget(self._key_section)

        # Picture: what this place gets from its canvas.
        video, form = _section("Picture")
        self.canvas = QComboBox()
        for canvas in collection.canvases:
            self.canvas.addItem(f"{canvas.id.capitalize()} ({canvas.width}×{canvas.height})", canvas.id)
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
        self.video_note = QLabel("Encoder and quality are on the Quality tab.")
        self.video_note.setWordWrap(True)
        self.video_note.setObjectName("streamMuted")
        self.auto_bitrate = QCheckBox("Auto-pilot bitrate")
        self.auto_bitrate.setToolTip("When your upload can't keep up, lower the bitrate smoothly instead of "
                                     "dropping frames, then climb back to this bitrate once it recovers")
        self.auto_bitrate.toggled.connect(self._edited)
        form.addRow("", self.auto_bitrate)
        self.limits = QLabel()  # what the service takes at most (shown with or without Expert settings)
        form.addRow("", self.limits)
        form.addRow("", self.video_note)
        self.video_box = video
        groups.addWidget(video)
        self._picture_section = video

        yield
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
        # Twitch's vertical (dual format) doesn't work yet, so the row only shows when it's switched on to try.
        from sprigcast.output.enhanced import vertical_allowed

        form.setRowVisible(self.extra_canvas, vertical_allowed())
        self.enhanced_note = QLabel(
            "Twitch sets sizes and bitrates; the Video settings above are the fallback. Needs an NVIDIA or Intel Arc GPU."
        )
        self.enhanced_note.setWordWrap(True)
        self.enhanced_note.setObjectName("streamMuted")
        form.addRow("", self.enhanced_note)
        groups.addWidget(self.enhanced_box)

        # Advanced options.
        advanced, form = _section("Extras")
        self.ignore_recommendations = QCheckBox("Ignore streaming service setting recommendations")
        self.ignore_recommendations.toggled.connect(self._edited)
        form.addRow("", self.ignore_recommendations)
        note = QLabel("Keys are saved in Windows Credential Manager, never in the scenes file.")
        note.setWordWrap(True)
        note.setObjectName("streamMuted")
        form.addRow("", note)
        groups.addWidget(advanced)
        self._extras_section = advanced

        yield
        groups.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setObjectName("streamScroll")
        self.form_widget.setObjectName("streamForm")
        scroll.viewport().setAutoFillBackground(False)
        scroll.setWidget(self.form_widget)
        root.addWidget(scroll, 1)
        _line_up_labels(_FORMS)
        _FORMS.clear()

        self._built = True
        yield
        self._refresh_list(max(0, self.list.currentRow()))
        if self.__dict__.pop("_add_when_built", False):
            self._add()
        if self._expert:  # asked for while the form was still being made
            self.set_expert(True)

    def set_expert(self, on: bool) -> None:
        """Extras (bandwidth test, ignoring the service's recommendations) are expert rows."""
        self._expert = on
        if not self._built:
            return  # shown as asked once the form is made
        output = self._current()
        unpicked = output is not None and output.id in self._unpicked
        self._show_unpicked(unpicked)
        if output is not None and not unpicked:
            # Showing the rows again brought back every account row at once: Twitch's "Connected as" and, under
            # it, YouTube and Kick's Sign in / Sign out (2026-11-19, with Expert settings on). Only this
            # service's row stays.
            self._show_service(output)

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
            item = QListWidgetItem(self._card_text(output))
            item.setData(Qt.ItemDataRole.UserRole, output.id)
            self.list.addItem(item)
        self._loading = False
        self.form_widget.setEnabled(bool(self.outputs))
        if not self.outputs:
            self.hero.setText("Nothing here yet. Press + Add.")
        if self.outputs:
            self.list.setCurrentRow(max(0, min(row, len(self.outputs) - 1)))

    def _order_changed(self) -> None:
        """A place was dragged to a new spot: the places follow the list's order, and the one dragged stays picked."""
        by_id = {o.id: o for o in self.outputs}
        ids = [self.list.item(row).data(Qt.ItemDataRole.UserRole) for row in range(self.list.count())]
        if self._loading or sorted(ids) != sorted(by_id) or ids == [o.id for o in self.outputs]:
            return  # the list being filled, or nothing moved
        self.outputs[:] = [by_id[i] for i in ids]
        self._load(self.list.currentRow())

    def move_place(self, step: int) -> None:
        """The picked place one spot up (-1) or down (1)."""
        self.finish()
        row = self.list.currentRow()
        to = row + step
        if not (0 <= row < len(self.outputs) and 0 <= to < len(self.outputs)):
            return
        self.outputs.insert(to, self.outputs.pop(row))
        self._refresh_list(to)

    def _current(self) -> Output | None:
        row = self.list.currentRow()
        return self.outputs[row] if 0 <= row < len(self.outputs) else None

    def select_output(self, output_id: str) -> None:
        row = next((i for i, o in enumerate(self.outputs) if o.id == output_id), None)
        if row is None:
            return
        if not self._built:
            # Its card is picked now and its form filled in when the form is made: making the rest at once
            # here was the whole page in one go again, each time a place was opened from Live (2026-11-21).
            self.list.blockSignals(True)
            self.list.setCurrentRow(row)
            self.list.blockSignals(False)
            return
        self.list.setCurrentRow(row)

    def add_output(self) -> None:
        if not self._built:
            self._add_when_built = True  # (a new place needs the form: added as soon as it's made)
            return
        self._add()

    def _add(self) -> None:
        self.finish()
        taken = {o.id for o in self.outputs} | {o.id for o in self.collection.outputs} | self.other_paths_ids
        n = 1
        while f"out-{n}" in taken:
            n += 1
        output_id = f"out-{n}"
        # Blank: no service until you pick one (it then fills in that service's usual settings).
        self.outputs.append(Output(id=output_id, type="stream", service=None, canvas="horizontal",
                                   key_ref=f"sprigcast/{output_id}", enabled=False))
        self._unpicked.add(output_id)
        self._refresh_list(len(self.outputs) - 1)

    def _remove(self) -> None:
        output = self._current()
        if output is None or output.id in self.live_ids:
            return
        self.outputs.remove(output)
        if output.key_ref:
            self._saved_keys.setdefault(output.key_ref, None)  # unread: still delete it
            self.keys[output.key_ref] = ""  # delete the saved key too
        self._refresh_list(self.list.currentRow())

    # Form ------------------------------------------------------------------------

    def set_live(self, ids) -> None:
        """The places sending right now (while live): shown, but locked until they're ended."""
        self.live_ids = set(ids)
        for row, output in enumerate(self.outputs):
            if self.list.item(row) is not None:
                self.list.item(row).setText(self._card_text(output))
        if self._built:  # (else the place shown is locked as it's loaded)
            self._lock_live()

    def _lock_live(self) -> None:
        output = self._current()
        live = output is not None and output.id in self.live_ids
        self.live_note.setVisible(live)
        for card in self.form_widget.findChildren(QFrame, "streamCard"):
            card.setEnabled(not live)
        for widget in (self.enabled, self.enabled_label, self.bandwidth_test, self.bandwidth_label,
                       self.remove_button):
            widget.setEnabled(not live)

    def _load(self, row: int) -> None:
        if self._loading:
            return
        self.finish()  # a card clicked while the form was still being made
        output = self._current()
        if output is None:
            return
        self._lock_live()
        self._loading = True
        self.enabled.setChecked(output.enabled)
        if output.id in self._unpicked:
            self.hero.setText(NEW_PLACE)
            self.service.setCurrentIndex(0)
            self.key.setText("")
            self._show_unpicked(True)
            self._loading = False
            return
        self._show_unpicked(False)
        self.hero.setText(SERVICES.get(output.service or "custom", SERVICES["custom"]).label)
        self.service.setCurrentIndex(self._service_row(output.service or "custom"))
        self._service_was = self.service.currentIndex()
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
        self.key.setText(self._key_text(output))
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
        self.bandwidth_label.setVisible(service.bandwidth_test)
        is_twitch = (output.service or "") == "twitch"
        self.twitch_row.setVisible(is_twitch)
        self.account_label.setVisible(is_twitch)
        self.enhanced_box.setVisible(is_twitch)
        for widget in (self.enhanced_kbps_auto, self.enhanced_tracks_auto, self.extra_canvas, self.enhanced_note):
            widget.setEnabled(output.enhanced)
        self.enhanced_kbps.setEnabled(output.enhanced and bool(output.enhanced_max_kbps))
        self.enhanced_tracks.setEnabled(output.enhanced and bool(output.enhanced_max_tracks))
        account = self._twitch_account() if is_twitch else None
        self.use_account.setVisible(is_twitch and account is not None)
        # With the key fetched from the account there is nothing to paste, as in OBS.
        hide_key = is_twitch and account is not None and output.twitch_account
        self.key_row.setVisible(not hide_key)
        self.key_label.setVisible(not hide_key)
        can_sign_in = service_signin.available(output.service or "")
        self.signin_row.setVisible(can_sign_in)
        self.signin_label.setVisible(can_sign_in)
        if can_sign_in:  # one sign-in per platform: signed in, the key comes without a new sign-in
            from sprigcast.output import stream_details

            central = stream_details.account(output.service or "")
            gives_key = (output.service or "") in service_signin.GIVES_KEY
            # A key's account outlives a sign-out (the key still works); TikTok's sign-in is the whole story.
            name = central or (output.account if gives_key else "")
            self.signin_state.setText(f"Signed in as {name}" if name else "Not signed in")
            self.sign_out_button.setVisible(bool(central))
            # TikTok shares no key with apps: its sign-in only shows which account is connected.
            self.sign_in_button.setVisible(gives_key or not central)
            self.sign_in_button.setText("Get key" if central else "Sign in")
            self.sign_in_button.setToolTip("Fill in the stream key from your account" if central
                                           else "Sign in to fill in the stream key" if gives_key
                                           else "Sign in to show which account is connected")
        if not can_sign_in or output.id != self._error_for:
            self.signin_error.hide()
        if is_twitch:
            self.twitch_label.setText(f"Connected as {account}" if account else "Not connected")
            self.twitch_button.setText("Connect Account (recommended)")
            self.twitch_button.setVisible(not account)
            self.twitch_sign_out.setVisible(bool(account))

    def _fit_list(self) -> None:
        """The place list as tall as its cards (so + Add place sits under the last), scrolling once it can't be."""
        rows = sum(self.list.sizeHintForRow(i) for i in range(self.list.count()))
        self.list.setMaximumHeight(max(4, rows + 4))

    def _card_text(self, output: Output) -> str:
        if output.id in self._unpicked:
            return f"{NEW_PLACE}\n{PICK_SERVICE}"
        text = place_card_text(output)
        if output.id in self.live_ids:  # sending right now, whatever its switch says for next time
            text = text.removesuffix(" · Off") + " · Live"
        return text

    def _show_unpicked(self, unpicked: bool) -> None:
        """A new place shows only the service to pick; the rest appears once one is picked."""
        for widget in (self.twitch_row, self.use_account, self.signin_row):
            self._account_form.setRowVisible(widget, not unpicked)
        for widget in (self._picture_section, self._key_section, self.enabled, self.enabled_label):
            widget.setVisible(not unpicked)
        extras_on = self.ignore_recommendations.isChecked()
        self._extras_section.setVisible(not unpicked and (self._expert or extras_on))  # never hidden while on
        if unpicked:
            self.enhanced_box.setVisible(False)
            self.bandwidth_test.setVisible(False)
            self.bandwidth_label.setVisible(False)

    def _service_row(self, key: str) -> int:
        """The Service box's row for a place; one from "Show all…" gets a row of its own, above Custom RTMP."""
        row = self.service.findData(key)
        if row < 0 and key in SERVICES:
            row = self.service.findData("custom")
            self.service.insertItem(row, SERVICES[key].label, key)
        return row if row >= 0 else self.service.findData("custom")

    def _service_picked(self) -> None:
        """The Service box changed: "Show all…" opens the list first (closed without a pick, it goes back)."""
        if self.service.currentData() != ALL_PLACES:
            self._service_was = self.service.currentIndex()
            self._service_changed()
            return
        if self._loading:
            return
        from sprigcast.app.place_picker import pick_place

        was_key = self.service.itemData(self._service_was)
        key = pick_place(self.window())
        self.service.blockSignals(True)
        row = self._service_row(key) if key else self.service.findData(was_key)
        self.service.setCurrentIndex(max(0, row))
        self.service.blockSignals(False)
        self._service_was = self.service.currentIndex()
        if key:
            self._service_changed()

    def _service_changed(self) -> None:
        output = self._current()
        if output is None or self._loading:
            return
        if not self.service.currentData():  # back to "Pick a service": nothing to fill in
            return
        if output.id in self._unpicked:  # first pick: the place comes alive with that service's settings
            self._unpicked.discard(output.id)
            output.enabled = True
            self._loading = True
            self.enabled.setChecked(True)
            self._loading = False
            self._show_unpicked(False)
        changed = output.service != self.service.currentData()
        output.service = self.service.currentData()
        service = SERVICES[output.service]
        # Apply the platform's preset: canvas, resolution, frame rate and bitrate.
        self._loading = True
        if changed:  # the old service's server means nothing here: its own (fixed) one, or blank to fill in
            output.server = ""
            output.account = ""  # signed in to the old service, not this one
            if self._error_for == output.id:
                self._error_for = ""
            self.server.setText(service.server or "")
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
        if output is None or self._loading or output.id in self._unpicked:
            return
        service = SERVICES.get(self.service.currentData(), SERVICES["custom"])
        output.enabled = self.enabled.isChecked()
        output.service = self.service.currentData() or output.service  # "Pick a service" changes nothing
        output.server = "" if service.server else self.server.text().strip()
        output.canvas = self.canvas.currentData()
        size = self.size.currentData() or ""
        output.size = tuple(int(n) for n in size.split("x")) if size else None
        output.fps = self.fps.currentData() or None
        output.bitrate_kbps = self.bitrate.value()
        output.bandwidth_test = service.bandwidth_test and self.bandwidth_test.isChecked()
        output.twitch_account = output.service == "twitch" and self.use_account.isChecked()
        output.enhanced = output.service == "twitch" and self.enhanced.isChecked()
        from sprigcast.output.enhanced import vertical_allowed

        if vertical_allowed():  # while the row is hidden, a saved choice stays as it was
            output.enhanced_vertical = output.enhanced and bool(self.extra_canvas.currentData())
        output.enhanced_max_tracks = 0 if self.enhanced_tracks_auto.isChecked() else self.enhanced_tracks.value()
        output.enhanced_max_kbps = 0 if self.enhanced_kbps_auto.isChecked() else self.enhanced_kbps.value()
        output.ignore_recommendations = self.ignore_recommendations.isChecked()
        output.auto_bitrate = self.auto_bitrate.isChecked()
        if not output.key_ref:
            output.key_ref = f"sprigcast/{output.id}"
        self.keys[output.key_ref] = self.key.text().strip()
        self._show_service(output)
        self.list.item(self.list.currentRow()).setText(self._card_text(output))
        self.hero.setText(SERVICES.get(output.service or "custom", SERVICES["custom"]).label)

    def _twitch_clicked(self) -> None:
        output = self._current()
        if output is None:
            return
        if twitch_auth.connected_account():  # connected already: Sign out is the button beside it
            self._show_service(output)
            return
        from sprigcast.app.twitch_connect import TwitchConnectDialog

        dialog = TwitchConnectDialog(self)
        if not modal(dialog):
            return
        if dialog.stream_key:
            self.key.setText(dialog.stream_key)  # saved too, so going live still works if Twitch is unreachable
        self._show_service(output)
        self.use_account.setChecked(True)
        self._edited()

    def _twitch_account(self) -> str | None:
        """The connected Twitch login, from settings (instant). Never recorded (older installs, a first run): it's
        looked up in the password store on a worker and the place filled in when that's back. Opening the store
        on the window's thread held the Stream page 16.6 s on a cold build machine (#672's smoke run, 2026-11-19)."""
        login = twitch_auth.remembered_login()
        if login is None:
            self._find_twitch_account()
            return None
        return login or None

    def _find_twitch_account(self) -> None:
        if getattr(self, "_twitch_lookup", None) is not None:
            return  # already looking (or looked: what it found is in settings now)
        from PySide6.QtCore import QTimer

        from sprigcast.core import workers

        self._twitch_lookup = workers.run(lambda: twitch_auth.connected_account() or "", name="twitch-account")
        self._twitch_poll = QTimer(self, interval=50)
        self._twitch_poll.timeout.connect(self._twitch_found)
        self._twitch_poll.start()

    def _twitch_found(self) -> None:
        lookup = self._twitch_lookup
        if not lookup.done():
            return
        self._twitch_poll.stop()
        try:
            login = lookup.result() or ""
        except Exception:  # noqa: BLE001  the store couldn't be read: as if nobody's connected, asked again next time
            return
        twitch_auth._announce(login)  # recorded (here, on the window's thread): instant from now on
        output = self._current()
        if login and output is not None and (output.service or "") == "twitch":
            self._show_service(output)

    def _key_text(self, output: Output) -> str:
        """This place's stream key, read from the password store the first time it's shown."""
        ref = output.key_ref
        if not ref:
            return ""
        if ref not in self.keys:
            value = get_key(ref) or ""
            self.keys[ref] = value
            self._saved_keys.setdefault(ref, value)
        return self.keys[ref]

    def _sign_in_clicked(self) -> None:
        """YouTube or Kick: fill in the key (and Kick's server). Signed in to the platform already, straight from
        the account; otherwise sign in once, which signs in Stream details and chat too."""
        output = self._current()
        if output is None:
            return
        from sprigcast.app.signin_dialog import SignInDialog
        from sprigcast.output import service_signin, stream_details

        vertical = output.service == "youtube" and output.canvas == "vertical"  # its own key for Dual stream
        if stream_details.account(output.service or "") and output.service in service_signin.GIVES_KEY:
            self._key_from_account(output, vertical)
            return
        extra = {"begin": lambda s: service_signin.begin(s, vertical=True)} if vertical else {}
        dialog = SignInDialog(output.service or "", self, **extra)
        try:
            ok = bool(dialog.exec())
            fetched, error = dialog.fetched if ok else None, dialog.error
        finally:
            dialog.deleteLater()
        if fetched is None:
            if error:  # stays on the place, so it's still there once the window has closed
                self._error_for = output.id
                self.signin_error.setText(error)
                self.signin_error.show()
            return
        self._use_fetched(output, fetched)

    def _current_service(self) -> str:
        output = self._current()
        return (output.service or "") if output is not None else ""

    def _sign_out(self, service: str) -> None:
        """Sign out of the platform everywhere: every place on it, Stream details and chat. Keys already filled in
        keep working. Never done after a place's own sign-in: Google's revoke ends the whole grant (#596/#603)."""
        if not service:
            return
        from sprigcast.app.accounts_page import sign_out

        error = sign_out(service)
        output = self._current()
        if error and output is not None:
            self._error_for = output.id
            self.signin_error.setText(error)
            self.signin_error.show()
        if output is not None:
            self._show_service(output)

    def _use_fetched(self, output: Output, fetched) -> None:
        self._error_for = ""
        self.signin_error.hide()
        output.account = fetched.account
        if fetched.key:  # a TikTok sign-in brings no key: keep the pasted one
            self.key.setText(fetched.key)
        if fetched.server and not self.server.isReadOnly():
            self.server.setText(fetched.server)
        self._edited()

    def _key_from_account(self, output: Output, vertical: bool) -> None:
        """On a worker: the key through the signed-in account. The answer is picked up on the UI thread."""
        import threading

        from sprigcast.output import service_signin

        box: dict = {}

        def work() -> None:  # no Qt in here
            try:
                box["fetched"] = service_signin.key_from_account(output.service or "", vertical)
            except Exception as exc:  # noqa: BLE001 (worded for the person, or a plain note)
                box["error"] = str(exc) if isinstance(exc, service_signin.SignInError) else "Couldn't get the key."
                if not isinstance(exc, service_signin.SignInError):
                    log.warning("sign-in: %s key from the account failed", output.service, exc_info=True)

        self.sign_in_button.setEnabled(False)
        thread = threading.Thread(target=work, name="destination-key", daemon=True)
        thread.start()
        timer = QTimer(self)
        timer.setInterval(100)

        def check() -> None:
            if thread.is_alive():
                return
            timer.stop()
            timer.deleteLater()
            self.sign_in_button.setEnabled(True)
            if self._current() is not output:
                return  # another place is showing now
            if "fetched" in box:
                self._use_fetched(output, box["fetched"])
            else:
                self._error_for = output.id
                self.signin_error.setText(box.get("error", "Couldn't get the key."))
                self.signin_error.show()

        timer.timeout.connect(check)
        timer.start()

    # Result ------------------------------------------------------------------------

    def apply_to(self, collection: SceneCollection) -> bool:
        """Write outputs back and save keys. Returns False (and says why) if a key could not be saved."""
        self.finish()
        try:
            for key_ref, key in self.keys.items():
                if key != self._saved_keys.get(key_ref, ""):
                    set_key(key_ref, key)
        except Exception as exc:
            msgbox.critical(self, "SprigCast", f"Could not save the stream key:\n{exc}")
            return False
        others = [o for o in collection.outputs if o.type != "stream"]
        picked = [o for o in self.outputs if o.id not in self._unpicked]  # a place left blank isn't kept
        before = {o.id: o.service for o in collection.outputs if o.type == "stream"}
        after = {o.id: o.service for o in picked}
        for output_id in before.keys() - after.keys():  # one line each, so a log shows where a place went
            log.info("places: %s (%s) removed in Settings", output_id, before[output_id] or "custom")
        for output_id in after.keys() - before.keys():
            log.info("places: %s (%s) added in Settings", output_id, after[output_id] or "custom")
        sending = {o.id: o for o in collection.outputs if o.type == "stream" and o.id in self.live_ids}
        # copies: later edits wait for Apply. A place sending right now stays exactly as it went live.
        collection.outputs = others + [sending.get(o.id, o).model_copy() for o in picked]
        for output in sending.values():  # (and can't be removed from under its stream)
            if all(o.id != output.id for o in collection.outputs):
                collection.outputs.append(output)
        return True
