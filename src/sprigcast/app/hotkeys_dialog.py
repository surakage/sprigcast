"""Hotkeys page: a key for going live, clips, blooms, sounds, sprig looks and videos, in soft cards."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QKeySequenceEdit,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from sprigcast.app import message as msgbox
from sprigcast.app import theme
from sprigcast.app.hotkeys import sections, taken_by, to_pynput
from sprigcast.app.live_stage import heading
from sprigcast.core.scene import SceneCollection

KEY_W = 190  # every key box the same width, so the column reads straight down


def _key_style() -> str:
    return (f"QKeySequenceEdit QLineEdit {{ background: {theme.PANEL}; border: none; border-radius: 12px;"
            f" padding: 7px 14px; min-height: 20px; qproperty-alignment: AlignCenter; }}"
            f"QKeySequenceEdit QLineEdit:focus {{ background: {theme.RAISED}; }}")


class HotkeyRow(QWidget):
    """One action: its name, the key box, and a small × to clear it."""

    def __init__(self, action: str, label: str, combo: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.action, self.label = action, label
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 2, 0, 2)
        row.setSpacing(10)
        from sprigcast.app.live_stage import Elided

        # One line, ending in "…" when it hasn't room (all of it on hover): a word-wrapped label is measured
        # again for its height on every layout, and each step of rows laid the whole page out again (PC
        # 2026-11-18, about 280 ms of the open while live).
        name = Elided(label)
        row.addWidget(name, 1)
        self.edit = QKeySequenceEdit(QKeySequence(combo))
        self.edit.setMaximumSequenceLength(1)
        self.edit.setFixedWidth(KEY_W)  # styled by the page (one style sheet for every row, see HotkeysPage)
        line = self.edit.findChild(QLineEdit)
        if line is not None:
            line.setPlaceholderText("Press keys…")
            line.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row.addWidget(self.edit)
        self.clear = QPushButton("×")
        self.clear.setObjectName("hotkeyClear")
        self.clear.setToolTip("Clear this hotkey")
        self.clear.setFixedSize(34, 34)
        self.clear.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clear.clicked.connect(self.edit.clear)
        row.addWidget(self.clear)
        self.edit.keySequenceChanged.connect(self._mark)
        self._mark()

    def _mark(self, *_):
        self.clear.setVisible(not self.edit.keySequence().isEmpty())


class HotkeysPage(QWidget):
    """Every hotkey, a card per section. cards_later: the cards are made one per tick (cards_made fires once
    they all are), so the window keeps drawing; made all at once, with a style sheet on each row's key box
    and × button, they held the window 427 ms on the PC (2026-11-18 pop-up sweep, live)."""

    CARD_GAP_MS = 15
    ROWS_PER_STEP = 4

    def __init__(self, collection: SceneCollection, parent: QWidget | None = None, cards_later: bool = False):
        super().__init__(parent)
        self.setStyleSheet(_key_style() + theme.button_style(16, "0", selector="QPushButton#hotkeyClear",
                                                             extra="font-size: 16px;")
                           + f"#hotkeyCard {{ background: {theme.CARD}; border-radius: 20px; }}")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)
        top = QHBoxLayout()
        top.setSpacing(12)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Find an action…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter)
        top.addWidget(self.search, 1)
        self.count = QLabel()
        self.count.setStyleSheet(f"color: {theme.MUTED}")
        top.addWidget(self.count)
        layout.addLayout(top)
        intro = QLabel("Click a box, then press keys (like Ctrl+Alt+1). They work in games too, "
                       "so pick combos your games don't use.")
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color: {theme.MUTED}")
        layout.addWidget(intro)

        self.edits: dict[str, QKeySequenceEdit] = {}
        self.rows: list[HotkeyRow] = []
        self.cards: list[tuple[QFrame, list[HotkeyRow]]] = []
        self._layout, self._hotkeys = layout, dict(collection.hotkeys)
        # Made a few rows at a time (ROWS_PER_STEP): a whole card at once was still 60-80 ms on the PC while live.
        found = sections(collection)
        self._waiting = [(title, items[i:i + self.ROWS_PER_STEP], i == 0)
                         for title, items in found
                         for i in range(0, max(1, len(items)), self.ROWS_PER_STEP)]
        layout.addStretch(1)
        self.on_done = None  # called once every card is made
        made = {action for _t, items in found for action, _label in items}
        self._say_count(sum(1 for a, combo in self._hotkeys.items() if combo and a in made))
        if cards_later:
            self._next_card()
        else:
            self.finish()

    def _add_card(self, title: str, items, new: bool = True) -> None:
        """A section's card (new) with its first rows, or more rows for the last card."""
        if new or not self.cards:
            card = QFrame()
            card.setObjectName("hotkeyCard")
            box = self._box = QVBoxLayout(card)
            box.setContentsMargins(24, 18, 16, 18)
            box.setSpacing(6)
            box.addWidget(heading(title, 14))
            self.cards.append((card, []))
            self._layout.insertWidget(self._layout.count() - 1, card)  # above the stretch
        card, rows = self.cards[-1]
        box = self._box
        for action, label in items:
            row = HotkeyRow(action, label, self._hotkeys.get(action, ""))
            row.edit.keySequenceChanged.connect(self._update_count)
            box.addWidget(row)
            rows.append(row)
            self.rows.append(row)
            self.edits[action] = row.edit
        if self.search.text():
            self._filter(self.search.text())

    def _next_card(self) -> None:
        if not self._waiting:
            return
        self._add_card(*self._waiting.pop(0))
        if self._waiting:
            from PySide6.QtCore import QTimer

            QTimer.singleShot(self.CARD_GAP_MS, self, self._next_card)  # not after the page is gone
        else:
            self._update_count()  # once, not each step (it reads every key box made so far)
            if self.on_done is not None:
                self.on_done()

    def finish(self) -> None:
        """Make every card still waiting, now (before reading or saving the keys)."""
        while self._waiting:
            self._add_card(*self._waiting.pop(0))
        self._update_count()

    def _filter(self, text: str) -> None:
        words = text.lower().split()
        for card, rows in self.cards:
            shown = 0
            for row in rows:
                hit = all(w in row.label.lower() for w in words)
                row.setVisible(hit)
                shown += hit
            card.setVisible(shown > 0)

    def _update_count(self, *_):
        self._say_count(sum(1 for e in self.edits.values() if not e.keySequence().isEmpty()))

    def _say_count(self, n: int) -> None:
        self.count.setText("No keys set" if n == 0 else "1 key set" if n == 1 else f"{n} keys set")

    def result_hotkeys(self) -> dict[str, str]:
        self.finish()
        out = {}
        for action, edit in self.edits.items():
            combo = edit.keySequence().toString(QKeySequence.SequenceFormat.PortableText)
            if combo:
                out[action] = combo
        return out

    def problem(self) -> str | None:
        """Why the chosen hotkeys can't be saved, or None when they're fine."""
        seen: dict[str, str] = {}
        for action, combo in self.result_hotkeys().items():
            if to_pynput(combo) is None:
                return f"{combo} can't be used as a hotkey. Add a letter, number or F-key."
            if combo in seen:
                return f"{combo} is used twice."
            why = taken_by(combo)
            if why:
                return f"{combo} can't be a hotkey: {why}."
            seen[combo] = action
        return None


class HotkeysWindow(QDialog):
    """Tools → Hotkeys: every key in a window of its own (it used to make Settings very tall)."""

    def __init__(self, collection: SceneCollection, parent: QWidget | None = None, fill_after_show: bool = False):
        """fill_after_show: the window shows at once and its list of keys is made a moment later (made first, it
        held the window 380 ms on the PC, 2026-11-18, the canvases waiting meanwhile)."""
        from sprigcast.app.settings_window import SAVE_STYLE, window_style

        super().__init__(parent)
        self.setWindowTitle("Hotkeys")
        self.setObjectName("settingsWindow")
        self.setStyleSheet(window_style())
        self.resize(760, 720)
        self.collection = collection
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 20)
        root.setSpacing(16)
        root.addWidget(heading("Hotkeys", 20))
        self._slot = QVBoxLayout()
        root.addLayout(self._slot, 1)
        self._waiting = fill_after_show
        if not fill_after_show:
            self._fill()
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addStretch(1)
        self.cancel_button = QPushButton("Cancel")
        self.save_button = QPushButton("Save")
        for button, style, slot in ((self.cancel_button, theme.PILL_STYLE, self.reject),
                                    (self.save_button, SAVE_STYLE, self._save)):
            button.setStyleSheet(style)
            button.setMinimumWidth(96)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setAutoDefault(False)
            button.clicked.connect(slot)
            row.addWidget(button)
        root.addLayout(row)

    def _fill(self) -> None:
        if "page" in self.__dict__:
            return
        from sprigcast.app.output_page import _scrolled

        self.page = HotkeysPage(self.collection, cards_later=self._waiting)
        self._waiting = False
        self._slot.addWidget(_scrolled(self.page), 1)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if self._waiting:
            from PySide6.QtCore import QTimer

            QTimer.singleShot(30, self._fill)  # after the window's first paint

    def _save(self) -> None:
        self._fill()

        problem = self.page.problem()
        if problem:
            msgbox.warning(self, "Hotkeys", problem)
            return
        self.collection.hotkeys = self.page.result_hotkeys()
        self.accept()
