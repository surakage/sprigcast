"""Stream details: your title, category, tags, description, privacy and thumbnail, saved straight to the platform.

    Stream details
    [ Twitch ] [ YouTube ] [ Kick ]
    Signed in as slothbloom                    Sign out
    Title        [ Chill games and sloth facts          ]
    Category     [ Just Chatting                        ]
    Tags         [ Cozy, English                        ]
    Saved to Twitch.                     [ Close ] [ Save ]

The card shows at once and fills itself: reading from the platform, looking up a category and saving all run
on a worker that gets plain values and never the card (output/stream_details.py). Nothing runs while it's
closed, and nothing here is in the way of going live: a failed save is a quiet line on the card.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QStringListModel, Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QCompleter,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from sprigcast.app import theme
from sprigcast.app.card import FIELD_STYLE, PRIMARY_PILL, CardDialog
from sprigcast.output import stream_details as sd

log = logging.getLogger(__name__)

LABEL_W = 92
CHIP_W = 104
WAITING_TEXT = "Nothing is scheduled on YouTube. Save sets up your next stream."
LAST_TEXT = "Nothing is scheduled on YouTube, so this is your last stream's. Save sets up your next one."
DUAL_SETUP_URL = "https://www.youtube.com/live_dashboard"  # opens YouTube Studio's Live Control Room  # every platform chip the same width
SEARCH_WAIT_MS = 350  # look a category up once typing pauses
PRIVACY_NAMES = {"public": "Public", "unlisted": "Unlisted", "private": "Private"}
BOX_STYLE = (f"QPlainTextEdit {{ background: {theme.CARD}; border: 1px solid {theme.EDGE_STRONG}; border-radius: 14px;"
             f" padding: 6px 10px; color: {theme.TEXT}; }} QPlainTextEdit:focus {{ border-color: {theme.ACCENT}; }}")


class Jobs:
    """Runs a function on a worker and hands its answer back on the UI thread.

    The worker gets only the function and its plain arguments; the answer waits in a dict until a short UI timer
    picks it up. The timer runs only while something is out, and after stop() no answer is handed back."""

    def __init__(self, owner: QWidget):
        self._out: list[tuple[threading.Thread, dict, Callable]] = []
        self._stopped = False
        self._timer = QTimer(owner)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._check)

    def run(self, fn: Callable, *args, done: Callable[[object, Exception | None], None]) -> None:
        box: dict = {}

        def work() -> None:  # on the worker: no Qt widgets in here
            try:
                box["value"] = fn(*args)
            except Exception as exc:  # noqa: BLE001 (handed back, worded for the person)
                if not isinstance(exc, sd.DetailsError):
                    log.warning("stream details: %s failed", getattr(fn, "__name__", "job"), exc_info=True)
                box["error"] = exc

        thread = threading.Thread(target=work, name="stream-details", daemon=True)
        self._out.append((thread, box, done))
        thread.start()
        self._timer.start()

    def busy(self) -> bool:
        return any(thread.is_alive() for thread, _, _ in self._out)

    def _check(self) -> None:
        finished = [job for job in self._out if not job[0].is_alive()]
        self._out = [job for job in self._out if job[0].is_alive()]
        if not self._out:
            self._timer.stop()
        for _, box, done in finished:
            if not self._stopped:
                done(box.get("value"), box.get("error"))

    def stop(self) -> None:
        self._stopped = True
        self._timer.stop()


def _row(label: str, field: QWidget) -> QHBoxLayout:
    row = QHBoxLayout()
    row.setSpacing(10)
    name = QLabel(label)
    name.setFixedWidth(LABEL_W)
    name.setStyleSheet(f"color: {theme.MUTED}; background: transparent;")
    row.addWidget(name, 0, Qt.AlignmentFlag.AlignTop if isinstance(field, QPlainTextEdit) else Qt.AlignmentFlag.AlignVCenter)
    row.addWidget(field, 1)
    return row


def _link(text: str) -> QPushButton:
    button = QPushButton(text)
    button.setStyleSheet(theme.LINK_STYLE)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    return button


def _quiet(text: str = "") -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet(f"color: {theme.MUTED}; background: transparent;")
    return label


class Pane(QWidget):
    """One platform's fields, with its sign-in state."""

    def __init__(self, service: str, dialog: StreamDetailsDialog, key_ref: str | None = None,
                 vertical_key_ref: str | None = None):
        super().__init__()
        self.service, self.dialog = service, dialog
        self.vertical_key_ref = vertical_key_ref  # YouTube Dual stream: the vertical destination's key
        self.key_ref = key_ref  # YouTube: this destination's stream key, so it edits the stream that key goes to
        self.details = sd.Details()
        self.loaded = False
        self.found: dict[str, str] = {}  # category name -> id, from the last look-up
        self.thumb_path = ""
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(12)

        # Signed in as ... / Sign out
        who = QHBoxLayout()
        self.who = _quiet()
        who.addWidget(self.who, 1)
        self.sign_out = _link("Sign out")
        self.sign_out.clicked.connect(self._sign_out)
        self.sign_out.setVisible(False)
        who.addWidget(self.sign_out)
        box.addLayout(who)

        # Not signed in: a line and a button
        self.signed_out = QWidget()
        out = QHBoxLayout(self.signed_out)
        out.setContentsMargins(0, 0, 0, 0)
        self.why = _quiet()
        out.addWidget(self.why, 1)
        self.sign_in = QPushButton("Sign in")
        self.sign_in.setStyleSheet(PRIMARY_PILL)
        self.sign_in.setCursor(Qt.CursorShape.PointingHandCursor)
        self.sign_in.clicked.connect(self._sign_in)
        out.addWidget(self.sign_in, 0, Qt.AlignmentFlag.AlignTop)
        self.signed_out.setVisible(False)
        box.addWidget(self.signed_out)

        self.form = QWidget()
        form = QVBoxLayout(self.form)
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(10)
        self.title = self._edit("Title")
        self.title.setMaxLength(sd.TITLE_MAX[service])
        form.addLayout(_row("Title", self.title))
        self.category = self.tags = self.description = self.privacy = None
        self.waiting_note = None
        if service in ("twitch", "kick"):
            self.category = self._edit("Search")
            self._model = QStringListModel(self)
            completer = QCompleter(self._model, self)
            completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
            completer.setCompletionMode(QCompleter.CompletionMode.UnfilteredPopupCompletion)
            completer.activated.connect(self._picked)
            self.category.setCompleter(completer)
            self.category.textEdited.connect(self._typed)
            self._search_timer = QTimer(self)
            self._search_timer.setSingleShot(True)
            self._search_timer.setInterval(SEARCH_WAIT_MS)
            self._search_timer.timeout.connect(self._search)
            form.addLayout(_row("Category", self.category))
        if service != "youtube":  # YouTube's go under its description
            self.tags = self._edit("Cozy, English")
            form.addLayout(_row("Tags", self.tags))
        if service == "youtube":
            self.description = QPlainTextEdit()
            self.description.setStyleSheet(BOX_STYLE)
            self.description.setFixedHeight(96)
            form.addLayout(_row("Description", self.description))
            self.tags = self._edit("Cozy, Sloth facts")
            form.addLayout(_row("Tags", self.tags))
            self.privacy = QComboBox()
            for key, name in PRIVACY_NAMES.items():
                self.privacy.addItem(name, key)
            try:
                from sprigcast.app.starter import combo_style

                self.privacy.setStyleSheet(combo_style())
            except Exception:  # noqa: BLE001
                pass
            form.addLayout(_row("Privacy", self.privacy))
            self.video_category = QComboBox()
            for key, name in sorted(sd.YOUTUBE_CATEGORIES.items(), key=lambda kv: kv[1]):
                self.video_category.addItem(name, key)
            self.video_category.setStyleSheet(self.privacy.styleSheet())
            form.addLayout(_row("Category", self.video_category))
            thumb = QWidget()
            tl = QHBoxLayout(thumb)
            tl.setContentsMargins(0, 0, 0, 0)
            self.thumb_name = _quiet("Keep the current one")
            tl.addWidget(self.thumb_name, 1)
            self.thumb_clear = _link("Keep current")
            self.thumb_clear.clicked.connect(lambda: self._set_thumb(""))
            self.thumb_clear.setVisible(False)
            tl.addWidget(self.thumb_clear)
            choose = _link("Choose picture…")
            choose.clicked.connect(self._choose_thumb)
            tl.addWidget(choose)
            form.addLayout(_row("Thumbnail", thumb))
            self.waiting_note = _quiet(WAITING_TEXT)
            self.waiting_note.setVisible(False)
            form.addWidget(self.waiting_note)
            # Dual stream: YouTube's API can't join the vertical key to the stream, so say where it's done once.
            self.dual_note = QLabel("")
            self.dual_note.setWordWrap(True)
            self.dual_note.setOpenExternalLinks(True)
            self.dual_note.setStyleSheet(f"color: {theme.MUTED}; background: transparent;")
            self.dual_note.setVisible(False)
            form.addWidget(self.dual_note)
        self.form.setVisible(False)
        box.addWidget(self.form)

    def _edit(self, placeholder: str) -> QLineEdit:
        edit = QLineEdit()
        edit.setPlaceholderText(placeholder)
        edit.setStyleSheet(FIELD_STYLE)
        edit.setMinimumHeight(34)
        return edit

    # States ---------------------------------------------------------------------------------------------

    def load(self) -> None:
        """Read the platform's details, unless nobody is signed in (that's known without the credential store)."""
        label = sd.LABELS[self.service]
        name = sd.account(self.service)
        if not name:
            self.show_signed_out("Connect Twitch to change your stream details." if self.service == "twitch"
                                 else f"Sign in to {label} to change your stream details.")
            return
        self.who.setText(f"Signed in as {name}")
        self.who.setVisible(True)
        self.sign_out.setVisible(self.service != "twitch")
        self.signed_out.setVisible(False)
        self.form.setVisible(True)
        self.form.setEnabled(False)
        self.dialog.say(f"Reading from {label}…")
        self.dialog.jobs.run(sd.load, self.service, self.key_ref, self.vertical_key_ref, done=self._loaded)

    def show_signed_out(self, why: str) -> None:
        self.loaded = False
        self.who.setText("")
        self.who.setVisible(False)
        self.sign_out.setVisible(False)
        self.form.setVisible(False)
        self.why.setText(why)
        reconnect = self.service == "twitch" and why.startswith("Reconnect")
        self.sign_in.setText("Reconnect Twitch" if reconnect else "Connect Twitch" if self.service == "twitch"
                             else "Sign in")
        self.signed_out.setVisible(True)
        self.dialog.say("")
        self.dialog.refresh_save()

    def _loaded(self, details, error) -> None:
        if isinstance(error, sd.NeedsSignIn):
            self.show_signed_out(str(error))
            return
        if error is not None:
            self.dialog.say(str(error) if isinstance(error, sd.DetailsError)
                            else f"Couldn't read from {sd.LABELS[self.service]}.", warn=True)
            return
        self.details = details
        self.loaded = True
        self.form.setEnabled(True)
        for field, text in ((self.title, details.title), (self.category, details.category),
                            (self.tags, ", ".join(details.tags))):
            if field is not None:
                field.setText(text)
                field.setCursorPosition(0)  # a long title opens at its start, not its end
        if self.category is not None and details.category:
            self.found = {details.category: details.category_id}
        if self.description is not None:
            self.description.setPlainText(details.description)
            self.privacy.setCurrentIndex(max(0, self.privacy.findData(details.privacy)))
            found = self.video_category.findData(details.video_category or "20")  # a new stream: Gaming
            self.video_category.setCurrentIndex(max(0, found))
            self.waiting_note.setText(LAST_TEXT if details.from_last else WAITING_TEXT)
            self.waiting_note.setVisible(not details.broadcast_id)
            self.show_dual_note(details.vertical_key_name)
        self.dialog.say("")
        self.dialog.refresh_save()

    def show_dual_note(self, key_name: str) -> None:
        if not key_name:
            self.dual_note.setVisible(False)
            return
        import html

        self.dual_note.setText(
            f'Vertical joins this stream: in <a href="{DUAL_SETUP_URL}" style="color: {theme.ACCENT};">'
            f"Live Control Room</a>, turn on Dual stream, set Vertical to Encoder and pick "
            f"<b>{html.escape(key_name)}</b>.")
        self.dual_note.setVisible(True)

    # Category look-up -----------------------------------------------------------------------------------

    def _typed(self, _text: str) -> None:
        self._search_timer.start()

    def _search(self) -> None:
        text = self.category.text().strip()
        if len(text) < 2:
            return
        self.dialog.jobs.run(sd.search_categories, self.service, text, done=lambda found, error, t=text:
                             self._found(t, found, error))

    def _found(self, text: str, found, error) -> None:
        if error is not None or self.category.text().strip() != text:
            return  # a failed look-up says nothing; typing on looks again
        for cid, name in found or []:
            self.found[name] = cid
        self._model.setStringList([name for _, name in found or []])
        if found and self.category.hasFocus():
            self.category.completer().complete()

    def _picked(self, name: str) -> None:
        self.category.setText(name)

    # Thumbnail ------------------------------------------------------------------------------------------

    def _choose_thumb(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Thumbnail", "", "Pictures (*.png *.jpg *.jpeg *.bmp *.webp)")
        if path:
            self._set_thumb(path)

    def _set_thumb(self, path: str) -> None:
        self.thumb_path = path
        self.thumb_name.setText(os.path.basename(path) if path else "Keep the current one")
        self.thumb_clear.setVisible(bool(path))

    # Sign in and out ------------------------------------------------------------------------------------

    def _sign_in(self) -> None:
        if self.service == "twitch":
            self.dialog.connect_twitch(self.dialog)
            self.load()
            return
        from sprigcast.app.modal import modal
        from sprigcast.app.signin_dialog import SignInDialog
        from sprigcast.output import service_signin

        if not service_signin.available(self.service):
            self.dialog.say(f"{sd.LABELS[self.service]} sign-in isn't set up in this copy of SprigCast.", warn=True)
            return
        dialog = SignInDialog(self.service, self.dialog, begin=lambda s: service_signin.begin(s, keep=True), hint="")
        if modal(dialog) and dialog.fetched is not None:
            self.dialog.reload(self.service)  # horizontal and vertical YouTube share the sign-in
        elif dialog.error:
            self.dialog.say(dialog.error, warn=True)

    def _sign_out(self) -> None:
        self.dialog.jobs.run(sd.sign_out, self.service, done=lambda _v, _e: None)
        for pane in self.dialog.panes.values():
            if pane.service == self.service:
                pane.show_signed_out(f"Sign in to {sd.LABELS[self.service]} to change your stream details.")

    # Save -----------------------------------------------------------------------------------------------

    def gather(self) -> sd.Details | str:
        """The details to send, or why they can't go yet."""
        details = sd.Details(**{**self.details.__dict__, "tags": list(self.details.tags), "note": ""})
        details.title = self.title.text().strip()
        if not details.title:
            return "Add a title."
        if self.category is not None:
            text = self.category.text().strip()
            if not text:
                details.category, details.category_id = "", ""
            else:
                match = next((n for n in self.found if n.lower() == text.lower()), None)
                if match is None:
                    return "Pick a category from the list."
                details.category, details.category_id = match, self.found[match]
        if self.tags is not None:
            if self.service == "twitch":
                details.tags = sd.clean_tags(self.tags.text())
                problem = sd.tag_problem(details.tags)
            else:
                details.tags = sd.split_tags(self.tags.text())
                problem = sd.youtube_tag_problem(details.tags) if self.service == "youtube" else None
            if problem:
                return problem
        if self.description is not None:
            details.description = self.description.toPlainText()
            details.privacy = self.privacy.currentData()
            details.video_category = self.video_category.currentData()
        return details

    def saved(self, details: sd.Details) -> None:
        self.details = details
        if self.tags is not None:
            self.tags.setText(", ".join(details.tags))
        if self.waiting_note is not None:
            self.waiting_note.setVisible(not details.broadcast_id)
            self._set_thumb("")


def places_for(outputs) -> list[Place]:
    """The chips for your Destinations: one per platform. Horizontal and vertical YouTube are one live stream
    (YouTube's Dual stream: one watch page, one chat, one set of details), so they share one chip, which edits
    the stream on the horizontal key and never makes a separate one for the vertical key."""
    places: list[Place] = []
    youtube = [o for o in outputs if o.service == "youtube"]
    main = next((o for o in youtube if o.canvas == "horizontal" and o.key_ref), None) or \
        next((o for o in youtube if o.key_ref), None)
    vertical = next((o for o in youtube if o.canvas == "vertical" and o.key_ref and o is not main), None)
    for output in outputs:
        if output.service in sd.SERVICES and all(p.service != output.service for p in places):
            if output.service == "youtube":
                places.append(Place("youtube", "youtube", "YouTube", main.key_ref if main else None,
                                    vertical.key_ref if vertical else None))
            else:
                places.append(Place(output.service, output.service, sd.LABELS[output.service]))
    return places


@dataclass
class Place:
    """One chip: a platform, or one YouTube destination."""

    id: str
    service: str
    label: str
    key_ref: str | None = None
    vertical_key_ref: str | None = None  # YouTube: the vertical destination that joins through Dual stream


class StreamDetailsDialog(CardDialog):
    """modal(StreamDetailsDialog(...)). places: the chips (places_for), first one shown first; plain service names
    work too. Only your platforms get a chip; with none, the card says to add one."""

    def __init__(self, parent: QWidget | None = None, places: list | None = None,
                 youtube_key_ref: str | None = None, connect_twitch: Callable[[QWidget], object] | None = None):
        long = sum(1 for p in places or [] if not isinstance(p, str) and len(p.label) > 8)  # "YouTube Vertical"
        super().__init__("Stream details", parent, width=740 if len(places or []) > 3 and long else 560)
        self.jobs = Jobs(self)
        self.connect_twitch = connect_twitch or (lambda _parent: None)
        self.places: dict[str, Place] = {}
        for place in places or []:
            if isinstance(place, str):
                place = Place(place, place, sd.LABELS.get(place, place), youtube_key_ref if place == "youtube" else None)
            if place.service in sd.SERVICES and place.id not in self.places:
                self.places[place.id] = place
        order = list(self.places)

        chips = QHBoxLayout()
        chips.setSpacing(8)
        self.chips: dict[str, QPushButton] = {}
        for pid in order:
            chip = QPushButton(self.places[pid].label)
            chip.setStyleSheet(theme.PILL_STYLE)
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.clicked.connect(lambda _=False, p=pid: self.show_service(p))
            chips.addWidget(chip)
            self.chips[pid] = chip
        width = max([CHIP_W, *(c.sizeHint().width() + 8 for c in self.chips.values())])
        for chip in self.chips.values():
            chip.setFixedWidth(width)  # all the same size
        chips.addStretch(1)
        self.body.addLayout(chips)

        self.panes: dict[str, Pane] = {}
        self.pane_box = QVBoxLayout()
        self.body.addLayout(self.pane_box)

        bottom = QHBoxLayout()
        bottom.setSpacing(8)
        self.note = _quiet()
        bottom.addWidget(self.note, 1)
        close = QPushButton("Close")
        close.setStyleSheet(theme.button_style(17, "9px 18px"))
        close.clicked.connect(self.reject)
        self.save_button = QPushButton("Save")
        self.save_button.setStyleSheet(PRIMARY_PILL)
        self.save_button.clicked.connect(self._save)
        for button in (close, self.save_button):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            bottom.addWidget(button, 0, Qt.AlignmentFlag.AlignBottom)
        self.body.addSpacing(4)
        self.body.addLayout(bottom)
        self.current = ""
        self._saving = False
        if order:
            self.show_service(order[0])
        else:
            self.pane_box.addWidget(_quiet("Add Twitch, YouTube or Kick under Destinations first."))
            self.refresh_save()

    def show_service(self, pid: str) -> None:
        """Show one chip's pane; it reads its details the first time it's shown."""
        for key, chip in self.chips.items():
            chip.setStyleSheet(theme.PILL_SELECTED_STYLE if key == pid else theme.PILL_STYLE)
        for pane in self.panes.values():
            pane.setVisible(False)
        self.current = pid
        pane = self.panes.get(pid)
        self.say("")
        if pane is None:
            place = self.places[pid]
            pane = self.panes[pid] = Pane(place.service, self, place.key_ref, place.vertical_key_ref)
            self.pane_box.addWidget(pane)
            pane.load()
        pane.setVisible(True)
        self.refresh_save()

    def reload(self, service: str) -> None:
        """After signing in: every pane of that platform reads again."""
        for pane in self.panes.values():
            if pane.service == service:
                pane.load()

    def say(self, text: str, warn: bool = False, good: bool = False) -> None:
        colour = theme.WARN if warn else theme.ACCENT if good else theme.MUTED
        self.note.setStyleSheet(f"color: {colour}; background: transparent;")
        self.note.setText(text)

    def refresh_save(self) -> None:
        pane = self.panes.get(self.current)
        self.save_button.setEnabled(bool(pane and pane.loaded) and not self._saving)

    def _save(self) -> None:
        pane = self.panes.get(self.current)
        if pane is None or not pane.loaded or self._saving:
            return
        details = pane.gather()
        if isinstance(details, str):
            self.say(details, warn=True)
            return
        service = pane.service
        self._saving = True
        self.refresh_save()
        self.say(f"Saving to {sd.LABELS[service]}…")
        self.jobs.run(sd.save, service, details, pane.thumb_path, pane.key_ref,
                      done=lambda saved, error, p=pane: self._saved(p, saved, error))

    def _saved(self, pane: Pane, saved, error) -> None:
        self._saving = False
        label = sd.LABELS[pane.service]
        if error is None:
            pane.saved(saved)
            if self.panes.get(self.current) is pane:
                if saved.note:
                    self.say(saved.note, warn=True)
                else:
                    self.say(f"Saved to {label}.", good=True)
        elif isinstance(error, sd.NeedsSignIn):
            pane.show_signed_out(str(error))
        else:
            self.say(str(error) if isinstance(error, sd.DetailsError) else f"Couldn't save to {label}.", warn=True)
        self.refresh_save()

    def done(self, result: int) -> None:  # noqa: D401 (Qt override)
        """Closing stops handing answers back; a worker still on a web call ends by itself and touches nothing."""
        self.jobs.stop()
        super().done(result)
