"""Tools → Plugin store: the plugin store, and the plugins you added yourself.

Each store plugin is a soft card: its name, what it adds, one line about it and one pill (Add,
Remove or Update). Adding loads it at once, so it shows up under + → Plugins without a restart.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QObject, QSettings, Qt, Signal
from PySide6.QtWidgets import QCheckBox, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from sprigcast import plugin_store
from sprigcast.app import message as msgbox
from sprigcast.app import theme
from sprigcast.app.card import CardWindow
from sprigcast.app.live_stage import heading
from sprigcast.plugins import PluginReport

log = logging.getLogger(__name__)

HOLD_ON_AIR = "plugins/hold_on_air"


def hold_on_air() -> bool:
    """Tools → Plugin store: an Update pressed while live or recording waits until both stop (on unless switched off)."""
    return QSettings().value(HOLD_ON_AIR, True, type=bool)


def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setObjectName("storeHint")  # styled by the page (PAGE_STYLE)
    return label


def _card(title: str | None = None) -> tuple[QFrame, QVBoxLayout]:
    card = QFrame()
    card.setObjectName("storeCard")
    box = QVBoxLayout(card)
    box.setContentsMargins(28, 22, 28, 24)
    box.setSpacing(14)
    if title:
        box.addWidget(heading(title, 15))
    return card, box


# The whole page's look in one style sheet, picked by object names and properties: each row had six style sheets
# of its own (its card, name, tag, note and two pills, then the pill's again), and the store's cards were 333 ms of
# the PC's open while live (2026-11-18).
PAGE_STYLE = (
    f"#storeCard {{ background: {theme.CARD}; border-radius: 20px; }}"
    f"#storeRow {{ background: {theme.PANEL}; border-radius: 16px; }}"
    f"#storeHint {{ color: {theme.MUTED}; }}"
    f"#storeName {{ color: {theme.TEXT_BRIGHT}; font-weight: 600; font-size: 14px; }}"
    f"#storeTag {{ background: {theme.SELECT_BG}; color: {theme.SELECT_TEXT}; border-radius: 9px;"
    " padding: 2px 10px; font-size: 11px; }"
    f"#storeNote {{ color: {theme.ACCENT}; font-size: 12px; }}"
    f'#storeNote[tone="bad"] {{ color: {theme.BAD}; }}'
    + theme.button_style(19, "10px 14px", selector="QPushButton#storePill")
    + f'QPushButton#storePill[primary="true"]:enabled {{ background: {theme.ACCENT}; color: {theme.ACCENT_TEXT};'
    " font-weight: 600; }"
    f'QPushButton#storePill[primary="true"]:enabled:hover {{ background: {theme.ACCENT_HOVER}; }}'
)


def _restyle(widget: QWidget, name: str, value) -> None:
    """A new value for a property PAGE_STYLE picks by, shown straight away (only when it changes)."""
    if widget.property(name) == value:
        return
    widget.setProperty(name, value)
    if widget.testAttribute(Qt.WidgetAttribute.WA_WState_Polished):  # not yet shown: it's styled when it is
        widget.style().unpolish(widget)
        widget.style().polish(widget)


def _pill(text: str, primary: bool = False) -> QPushButton:
    button = QPushButton(text)
    button.setObjectName("storePill")
    button.setProperty("primary", primary)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setMinimumWidth(96)
    return button


class StoreRow(QFrame):
    """One plugin in the store."""

    def __init__(self, page: PluginStorePage, plugin: plugin_store.StorePlugin, version=None, read: bool = True):
        """version: the installed version already read (read=False), so the row asks the disk nothing."""
        super().__init__()
        self.page, self.plugin = page, plugin
        self.setObjectName("storeRow")
        row = QHBoxLayout(self)
        row.setContentsMargins(20, 16, 16, 16)
        row.setSpacing(16)
        text = QVBoxLayout()
        text.setSpacing(4)
        top = QHBoxLayout()
        top.setSpacing(10)
        self.name = QLabel(plugin.name)
        self.name.setObjectName("storeName")
        top.addWidget(self.name)
        if plugin.adds:
            tag = QLabel(plugin.adds)
            tag.setObjectName("storeTag")
            top.addWidget(tag)
        top.addStretch(1)
        text.addLayout(top)
        text.addWidget(_hint(plugin.about))
        self.note = QLabel()
        self.note.setObjectName("storeNote")
        self.note.hide()
        text.addWidget(self.note)
        row.addLayout(text, 1)
        self.update_button = _pill("Update", primary=True)
        self.update_button.clicked.connect(lambda: page.update(plugin))
        self.button = _pill("Add")
        self.button.clicked.connect(self._clicked)
        row.addWidget(self.update_button, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.button, 0, Qt.AlignmentFlag.AlignVCenter)
        self.refresh(*(() if read else (version,)))

    @property
    def installed(self) -> int | None:
        return plugin_store.installed_version(self.page.plugins_dir, self.plugin.id)

    def refresh(self, *known) -> None:
        """known: (the installed version,) when it has just been read elsewhere (off the window's thread)."""
        version = known[0] if known else self.installed
        self.button.setText("Remove" if version is not None else "Add")
        _restyle(self.button, "primary", version is None)  # Add stands out, Remove doesn't
        self.button.setEnabled(self.page.plugins_dir is not None)
        self.update_button.setVisible(version is not None and version < self.plugin.version
                                      and self.plugin.id not in plugin_store.waiting)
        error = self.page.report.failed.get(f"{self.plugin.id}.py") if self.page.report else None
        if error:
            self.note.setText(f"Couldn't load: {error}")
            _restyle(self.note, "tone", "bad")
            self.note.show()

    def say(self, text: str) -> None:
        self.note.setText(text)
        _restyle(self.note, "tone", "ok")
        self.note.setVisible(bool(text))

    def _clicked(self) -> None:
        if self.installed is None:
            self.page.install(self.plugin)
        else:
            self.page.remove(self.plugin)


class PluginStorePage(QWidget):
    changed = Signal()  # a plugin was added or removed: menus that list plugin sources should refresh

    def __init__(self, plugins_dir: Path | None, report: PluginReport | None, open_plugins_folder=None,
                 collection=None, parent: QWidget | None = None, on_air: bool = False, known=None):
        """known: (the store's plugins, {plugin id: installed version}) read off the window's thread (read_store);
        None: read here."""
        super().__init__(parent)
        self.plugins_dir = plugins_dir
        self.on_air = on_air  # live or recording when Settings opened
        self.collection = collection  # to warn before removing a plugin something still uses
        self.report = report if report is not None else PluginReport()
        self.rows: dict[str, StoreRow] = {}
        self.setStyleSheet(PAGE_STYLE)

        store, sbox = _card("Plugin store")
        sbox.addWidget(_hint("Extras you can add when you want them. Each one adds something new to SprigCast; "
                             "nothing here runs until you add it."))
        sbox.addSpacing(4)
        plugins, versions = known if known is not None else (plugin_store.catalog(), None)
        self._sbox, self._versions = sbox, versions
        self._ids = [p.id for p in plugins]  # every store plugin, cards made or not yet
        # Read off the window's thread (known): the first few cards now, the rest a few a tick (all at once they
        # were 90 ms of the open on the PC while live, 2026-11-18). Read here (tests, older callers): all now.
        now = plugins if known is None else plugins[:self.ROWS_FIRST]
        self._rows_waiting = list(plugins[len(now):])
        for plugin in now:
            self._add_row(plugin)
        if not plugins:
            sbox.addWidget(_hint("Nothing in the store yet."))
        self.hold = QCheckBox("Hold plugin updates while live or recording")
        self.hold.setChecked(hold_on_air())
        self.hold.toggled.connect(lambda on: QSettings().setValue(HOLD_ON_AIR, on))
        sbox.addSpacing(4)
        sbox.addWidget(self.hold)
        if self._rows_waiting:
            from PySide6.QtCore import QTimer

            QTimer.singleShot(self.ROW_GAP_MS, self, self._more_rows)

        mine, mbox = _card("Your plugins")
        self.mine = QLabel()
        self.mine.setWordWrap(True)
        self.mine.setTextFormat(Qt.TextFormat.RichText)
        mbox.addWidget(self.mine)
        mbox.addWidget(_hint("Made your own? Put its .py file in the plugins folder and restart SprigCast. "
                             "Plugins can do anything SprigCast can, so only add ones you trust."))
        if open_plugins_folder is not None:
            folder = _pill("Open plugins folder")
            folder.clicked.connect(open_plugins_folder)
            line = QHBoxLayout()
            line.addWidget(folder)
            line.addStretch(1)
            mbox.addLayout(line)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(20)
        layout.addWidget(store)
        layout.addWidget(mine)
        layout.addStretch(1)
        self._refresh_mine()

    ROWS_FIRST = 1  # cards made with the page itself (it's put in its scroll in the same go)
    ROWS_PER_STEP = 2  # then this many a tick: 4 a tick were 147 ms over the fill on the PC while live (2026-11-18)
    ROW_GAP_MS = 15

    def _add_row(self, plugin) -> None:
        versions = self._versions
        row = (StoreRow(self, plugin) if versions is None
               else StoreRow(self, plugin, versions.get(plugin.id), read=False))
        self.rows[plugin.id] = row
        hold = getattr(self, "hold", None)
        if hold is None:
            self._sbox.addWidget(row)
        else:  # above the spacing and the Hold tick
            self._sbox.insertWidget(self._sbox.indexOf(hold) - 1, row)

    def _more_rows(self) -> None:
        theme.ui_burst()  # Python's lock handed back quickly while they're made (the open's own burst has ended)
        for plugin in self._rows_waiting[:self.ROWS_PER_STEP]:
            self._add_row(plugin)
        del self._rows_waiting[:self.ROWS_PER_STEP]
        if self._rows_waiting:
            from PySide6.QtCore import QTimer

            QTimer.singleShot(self.ROW_GAP_MS, self, self._more_rows)

    def finish(self) -> None:
        """Make every card still waiting."""
        while self._rows_waiting:
            self._add_row(self._rows_waiting.pop(0))

    def _store_files(self) -> set[str]:
        return {f"{pid}.py" for pid in self._ids}

    def _refresh_mine(self) -> None:
        store = self._store_files()
        lines = [f"<span style='color:{theme.ACCENT}'>✓</span>&nbsp; {name}"
                 for name in self.report.loaded if name not in store]
        lines += [f"<span style='color:{theme.BAD}'>✗</span>&nbsp; {name}: {error}"
                  for name, error in self.report.failed.items() if name not in store]
        self.mine.setText("<br>".join(lines) if lines else f"<span style='color:{theme.MUTED}'>None yet.</span>")

    def used_by(self, plugin_id: str) -> list[str]:
        """Names of sources made with this plugin."""
        if self.collection is None:
            return []
        from sprigcast.core.source import known_kinds, source_class

        module = f"sprigcast_plugin_{plugin_id}"
        kinds = {k for k in known_kinds() if getattr(source_class(k), "__module__", "") == module}
        return [s.name for s in self.collection.sources if s.kind in kinds]

    def install(self, plugin: plugin_store.StorePlugin) -> None:
        if self.plugins_dir is None:
            return
        try:
            error = plugin_store.install(self.plugins_dir, plugin, self.report)
        except OSError as exc:
            error = str(exc)
        row = self.rows[plugin.id]
        row.refresh()
        if error is None:
            row.say(f"Added. Find it under + → Plugins → {plugin.name}." if plugin.adds == "Source"
                    else "Added.")
        self.changed.emit()

    def update(self, plugin: plugin_store.StorePlugin) -> None:
        """Update now, or once the stream and recording stop (Hold plugin updates while live or recording)."""
        if self.on_air and hold_on_air():
            plugin_store.waiting[plugin.id] = plugin
            row = self.rows[plugin.id]
            row.refresh()
            row.say("Updates once you stop.")
            return
        self.install(plugin)
        if plugin.new:
            self.rows[plugin.id].say(f"Updated. {plugin.new}")

    def remove(self, plugin: plugin_store.StorePlugin) -> None:
        if self.plugins_dir is None:
            return
        used = self.used_by(plugin.id)
        if used:
            from PySide6.QtWidgets import QMessageBox

            names = ", ".join(used[:4]) + ("…" if len(used) > 4 else "")
            answer = msgbox.question(self, "Remove plugin",
                                          f"{names} use {plugin.name}. They'll show nothing once it's gone. Remove it?")
            if answer != QMessageBox.StandardButton.Yes:
                return
        try:
            plugin_store.remove(self.plugins_dir, plugin.id, self.report)
        except OSError as exc:
            self.rows[plugin.id].say(f"Couldn't remove it: {exc}")
            return
        row = self.rows[plugin.id]
        row.refresh()
        row.say("Removed. It's gone for good the next time SprigCast starts.")
        self.changed.emit()


class PluginStoreWindow(CardWindow):
    """Tools → Plugin store: the store in a window of its own (it used to be a Settings page).

    fill_after_show: the window shows at once and the store is made just after its first paint (made first, it
    held the window 94 ms on the PC while live, 2026-11-18). `changed` passes on the store's own."""

    changed = Signal()

    def __init__(self, plugins_dir: Path | None, report: PluginReport | None, open_plugins_folder=None,
                 collection=None, on_air: bool = False, parent: QWidget | None = None,
                 fill_after_show: bool = False):
        from sprigcast.app.settings_window import window_style

        super().__init__("Plugin store", parent, (820, 680), name="settingsWindow")
        self.setStyleSheet(window_style())
        self._args = (plugins_dir, report, open_plugins_folder, collection, on_air)
        root = QVBoxLayout(self.card)
        root.setContentsMargins(24, 22, 24, 20)
        root.setSpacing(16)
        root.addWidget(heading("Plugin store", 20))
        self._slot = QVBoxLayout()
        root.addLayout(self._slot, 1)
        self._waiting = fill_after_show
        if not fill_after_show:
            self._fill()
        row = QHBoxLayout()
        row.addStretch(1)
        close = QPushButton("Close")
        close.setStyleSheet(theme.PILL_STYLE)
        close.setMinimumWidth(96)
        close.setAutoDefault(False)
        close.clicked.connect(self.accept)
        row.addWidget(close)
        root.addLayout(row)

    def _fill(self, known=None) -> None:
        if "store" in self.__dict__:
            return
        from sprigcast.app.output_page import _scrolled

        plugins_dir, report, open_plugins_folder, collection, on_air = self._args
        self._waiting = False
        self.store = PluginStorePage(plugins_dir, report, open_plugins_folder, collection, on_air=on_air,
                                     known=known)
        self.store.changed.connect(self.changed)
        self._slot.addWidget(_scrolled(self.store), 1)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if self._waiting:
            self._waiting = False
            # The store's list and what's installed are read on a worker (a file read per plugin on the window's
            # thread was 113-167 ms of the open on the PC while live, 2026-11-18); the cards are made once it's back.
            from sprigcast.core import workers

            read = _StoreRead()  # no parent: the worker may finish after this window closed
            read.done.connect(self._read_back, Qt.ConnectionType.QueuedConnection)
            plugins_dir = self._args[0]
            workers.run(lambda: read.done.emit(read_store(plugins_dir)), name="plugin store")

    def _read_back(self, known) -> None:
        import shiboken6

        if shiboken6.isValid(self):
            theme.ui_burst()  # (read back a moment after the open: its own burst has ended)
            self._fill(known)


class _StoreRead(QObject):
    done = Signal(object)


def read_store(plugins_dir: Path | None) -> tuple[list, dict] | None:
    """The store's plugins and the installed version of each (None: not added). Reads files: not on the UI thread."""
    try:
        plugins = plugin_store.catalog()
        return plugins, {p.id: plugin_store.installed_version(plugins_dir, p.id) for p in plugins}
    except Exception:  # noqa: BLE001 (the window then reads them itself, as it used to)
        log.exception("plugin store: couldn't read the store")
        return None
