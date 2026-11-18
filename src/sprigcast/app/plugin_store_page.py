"""Tools → Plugin store: the plugin store, and the plugins you added yourself.

Each store plugin is a soft card: its name, what it adds, one line about it and one pill (Add,
Remove or Update). Adding loads it at once, so it shows up under + → Plugins without a restart.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSettings, Qt, Signal
from PySide6.QtWidgets import QCheckBox, QDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from sprigcast import plugin_store
from sprigcast.app import theme
from sprigcast.app.live_stage import heading
from sprigcast.plugins import PluginReport

HOLD_ON_AIR = "plugins/hold_on_air"


def hold_on_air() -> bool:
    """Tools → Plugin store: an Update pressed while live or recording waits until both stop (on unless switched off)."""
    return QSettings().value(HOLD_ON_AIR, True, type=bool)


def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet(theme.MUTED_STYLE)
    return label


def _card(title: str | None = None) -> tuple[QFrame, QVBoxLayout]:
    card = QFrame()
    card.setObjectName("storeCard")
    card.setStyleSheet(f"#storeCard {{ background: {theme.CARD}; border-radius: 20px; }}")
    box = QVBoxLayout(card)
    box.setContentsMargins(28, 22, 28, 24)
    box.setSpacing(14)
    if title:
        box.addWidget(heading(title, 15))
    return card, box


ADD_STYLE = theme.PILL_STYLE + (f"QPushButton:enabled {{ background: {theme.ACCENT}; color: {theme.ACCENT_TEXT};"
                                 f" font-weight: 600; }}"
                                 f"QPushButton:enabled:hover {{ background: {theme.ACCENT_HOVER}; }}")


REMOVE_STYLE = theme.PILL_STYLE


def _pill(text: str, primary: bool = False) -> QPushButton:
    button = QPushButton(text)
    button.setStyleSheet(ADD_STYLE if primary else theme.PILL_STYLE)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setMinimumWidth(96)
    return button


class StoreRow(QFrame):
    """One plugin in the store."""

    def __init__(self, page: PluginStorePage, plugin: plugin_store.StorePlugin):
        super().__init__()
        self.page, self.plugin = page, plugin
        self.setObjectName("storeRow")
        self.setStyleSheet(f"#storeRow {{ background: {theme.PANEL}; border-radius: 16px; }}")
        row = QHBoxLayout(self)
        row.setContentsMargins(20, 16, 16, 16)
        row.setSpacing(16)
        text = QVBoxLayout()
        text.setSpacing(4)
        top = QHBoxLayout()
        top.setSpacing(10)
        self.name = QLabel(plugin.name)
        self.name.setStyleSheet(f"color: {theme.TEXT_BRIGHT}; font-weight: 600; font-size: 14px;")
        top.addWidget(self.name)
        if plugin.adds:
            tag = QLabel(plugin.adds)
            tag.setStyleSheet(f"background: {theme.SELECT_BG}; color: {theme.SELECT_TEXT}; border-radius: 9px;"
                              " padding: 2px 10px; font-size: 11px;")
            top.addWidget(tag)
        top.addStretch(1)
        text.addLayout(top)
        text.addWidget(_hint(plugin.about))
        self.note = QLabel()
        self.note.setStyleSheet(f"color: {theme.ACCENT}; font-size: 12px;")
        self.note.hide()
        text.addWidget(self.note)
        row.addLayout(text, 1)
        self.update_button = _pill("Update", primary=True)
        self.update_button.clicked.connect(lambda: page.update(plugin))
        self.button = _pill("Add")
        self.button.clicked.connect(self._clicked)
        row.addWidget(self.update_button, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.button, 0, Qt.AlignmentFlag.AlignVCenter)
        self.refresh()

    @property
    def installed(self) -> int | None:
        return plugin_store.installed_version(self.page.plugins_dir, self.plugin.id)

    def refresh(self) -> None:
        version = self.installed
        self.button.setText("Remove" if version is not None else "Add")
        self.button.setStyleSheet(REMOVE_STYLE if version is not None else ADD_STYLE)
        self.button.setEnabled(self.page.plugins_dir is not None)
        self.update_button.setVisible(version is not None and version < self.plugin.version
                                      and self.plugin.id not in plugin_store.waiting)
        error = self.page.report.failed.get(f"{self.plugin.id}.py") if self.page.report else None
        if error:
            self.note.setText(f"Couldn't load: {error}")
            self.note.setStyleSheet(f"color: {theme.BAD}; font-size: 12px;")
            self.note.show()

    def say(self, text: str) -> None:
        self.note.setText(text)
        self.note.setStyleSheet(f"color: {theme.ACCENT}; font-size: 12px;")
        self.note.setVisible(bool(text))

    def _clicked(self) -> None:
        if self.installed is None:
            self.page.install(self.plugin)
        else:
            self.page.remove(self.plugin)


class PluginStorePage(QWidget):
    changed = Signal()  # a plugin was added or removed: menus that list plugin sources should refresh

    def __init__(self, plugins_dir: Path | None, report: PluginReport | None, open_plugins_folder=None,
                 collection=None, parent: QWidget | None = None, on_air: bool = False):
        super().__init__(parent)
        self.plugins_dir = plugins_dir
        self.on_air = on_air  # live or recording when Settings opened
        self.collection = collection  # to warn before removing a plugin something still uses
        self.report = report if report is not None else PluginReport()
        self.rows: dict[str, StoreRow] = {}

        store, sbox = _card("Plugin store")
        sbox.addWidget(_hint("Extras you can add when you want them. Each one adds something new to SprigCast; "
                             "nothing here runs until you add it."))
        sbox.addSpacing(4)
        plugins = plugin_store.catalog()
        for plugin in plugins:
            row = StoreRow(self, plugin)
            self.rows[plugin.id] = row
            sbox.addWidget(row)
        if not plugins:
            sbox.addWidget(_hint("Nothing in the store yet."))
        self.hold = QCheckBox("Hold plugin updates while live or recording")
        self.hold.setChecked(hold_on_air())
        self.hold.toggled.connect(lambda on: QSettings().setValue(HOLD_ON_AIR, on))
        sbox.addSpacing(4)
        sbox.addWidget(self.hold)

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

    def _store_files(self) -> set[str]:
        return {f"{pid}.py" for pid in self.rows}

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
            answer = QMessageBox.question(self, "Remove plugin",
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


class PluginStoreWindow(QDialog):
    """Tools → Plugin store: the store in a window of its own (it used to be a Settings page).

    fill_after_show: the window shows at once and the store is made just after its first paint (made first, it
    held the window 94 ms on the PC while live, 2026-11-18). `changed` passes on the store's own."""

    changed = Signal()

    def __init__(self, plugins_dir: Path | None, report: PluginReport | None, open_plugins_folder=None,
                 collection=None, on_air: bool = False, parent: QWidget | None = None,
                 fill_after_show: bool = False):
        from sprigcast.app.settings_window import window_style

        super().__init__(parent)
        self.setWindowTitle("Plugin store")
        self.setObjectName("settingsWindow")
        self.setStyleSheet(window_style())
        self.resize(820, 680)
        self._args = (plugins_dir, report, open_plugins_folder, collection, on_air)
        root = QVBoxLayout(self)
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

    def _fill(self) -> None:
        if "store" in self.__dict__:
            return
        from sprigcast.app.output_page import _scrolled

        plugins_dir, report, open_plugins_folder, collection, on_air = self._args
        self._waiting = False
        self.store = PluginStorePage(plugins_dir, report, open_plugins_folder, collection, on_air=on_air)
        self.store.changed.connect(self.changed)
        self._slot.addWidget(_scrolled(self.store), 1)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if self._waiting:
            from PySide6.QtCore import QTimer

            QTimer.singleShot(30, self, self._fill)  # after the window's first paint
