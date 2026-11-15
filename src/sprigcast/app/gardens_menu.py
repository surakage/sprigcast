"""The Gardens and Paths menus, and switching between them (see core/gardens.py for what they are).

    Gardens                       Paths
      New…                          New…
      Duplicate…                    Duplicate…
      Rename…                       Rename…
      Remove                        Remove
      Import…                       Import…
      Export…                       Export…
      ─────────                     ─────────
      Missing files…                ✓ Main
      ─────────                       Recording only
      Gaming                          Twitch only
      ✓ Main
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from sprigcast.app.terms import COLLECTION, COLLECTIONS, PROFILE, PROFILES, cap
from sprigcast.core.gardens import Shelf, ShelfError, missing_files, replace_file

if TYPE_CHECKING:
    from sprigcast.app.main_window import MainWindow

log = logging.getLogger(__name__)

WORDS = {"garden": (COLLECTION, COLLECTIONS), "path": (PROFILE, PROFILES)}


class GardenControl:
    """Owns the Shelf for a window and does every garden and path change (menus and the API)."""

    def __init__(self, window: MainWindow, shelf: Shelf):
        self.w = window
        self.shelf = shelf

    # What the API sees --------------------------------------------------------------------------

    def lists(self) -> dict:
        return {"garden": self.shelf.garden().name, "gardens": [g.name for g in self.shelf.gardens()],
                "path": self.shelf.path().name, "paths": [p.name for p in self.shelf.paths()]}

    def do(self, kind: str, op: str, name: str = "", new_name: str = "") -> str | None:
        """op: switch, new, duplicate, rename or remove. Returns a problem in plain words, or None."""
        try:
            if op == "switch":
                return self.switch(kind, name)
            if op in ("new", "duplicate"):
                copy = self.shelf.find(kind, name) if op == "duplicate" else None
                self._new(kind, new_name if op == "duplicate" else name, copy)
            elif op == "rename":
                self.shelf.rename(kind, name, new_name)
            elif op == "remove":
                self.shelf.remove(kind, name)
            else:
                return f"Can't {op} a {kind}."
        except ShelfError as exc:
            return str(exc)
        self._changed()
        return None

    # Switching ----------------------------------------------------------------------------------

    def busy(self) -> bool:
        return self.w.streaming is not None or self.w.recording is not None

    def switch(self, kind: str, wanted: str) -> str | None:
        try:
            entry = self.shelf.find(kind, wanted)
        except ShelfError as exc:
            return str(exc)
        current = self.shelf.garden() if kind == "garden" else self.shelf.path()
        if entry.id == current.id:
            return None
        return self._switch_garden(entry.id) if kind == "garden" else self._switch_path(entry.id)

    def _forget_undo(self) -> None:
        self.w._undo.clear()
        self.w._redo.clear()
        self.w._undo_current = self.w.collection.model_dump_json()

    def _switch_path(self, entry_id: str) -> str | None:
        w = self.w
        if self.busy():
            return f"Stop streaming and recording to change {PROFILE}."
        clips = w._clip_key()
        self.shelf.use_path(entry_id, w.collection)
        w._changed()
        self._forget_undo()  # undo never crosses a switch: the path list would disagree
        w._refresh_stream_boxes()
        w._restart_clips_if_changed(clips)
        self._changed()
        return None

    def _switch_garden(self, entry_id: str) -> str | None:
        w = self.w
        w.collection.save(w.scenes_path)
        self.shelf.sync_path(w.collection)
        w._save_thumbs()
        try:
            entry, incoming = self.shelf.use_garden(entry_id, w.collection)
        except (ShelfError, OSError, ValueError) as exc:
            return str(exc) if isinstance(exc, ShelfError) else "That garden's file can't be read."
        w.scenes_path = self.shelf.garden_file(entry)
        w.selected_folder_id = None
        w.live_panel.thumbs.clear()
        w._restore_state(incoming.model_dump_json())  # saves, hands it to the renderer, redraws
        self._forget_undo()
        w._load_thumbs()
        w.live_panel.show_scenes(w.collection)
        w.hotkeys.apply(w.collection.hotkeys)
        w._chat_sources_changed()
        self._changed()
        problems = missing_files(w.collection)
        if problems:
            w.statusBar().showMessage(f"{len(problems)} missing file(s) in {entry.name}: {cap(COLLECTIONS)} → "
                                      "Missing files…", 15000)
        return None

    def _new(self, kind: str, name: str, copy=None):
        if kind == "garden":
            if copy is not None and copy.id == self.shelf.garden().id:
                self.w.collection.save(self.w.scenes_path)
            return self.shelf.new_garden(name, self.w.collection, copy)
        return self.shelf.new_path(name, self.w.collection, copy)

    def _changed(self) -> None:
        self.w.update_title()
        control = getattr(self.w, "control", None)
        if control is not None:
            control.soon()

    def taken_output_ids(self) -> set[str]:
        return self.shelf.taken_output_ids()

    def close(self) -> None:
        try:
            self.shelf.sync_path(self.w.collection)
        except OSError as exc:
            log.warning("couldn't save the %s: %s", PROFILE, exc)

    # Menus --------------------------------------------------------------------------------------

    def build_menus(self, bar) -> None:
        for kind in ("garden", "path"):
            menu = bar.addMenu(cap(WORDS[kind][1]))
            menu.aboutToShow.connect(lambda m=menu, k=kind: self.fill(m, k))
            self.fill(menu, kind)

    def fill(self, menu: QMenu, kind: str) -> None:
        menu.clear()
        entries = self.shelf.gardens() if kind == "garden" else self.shelf.paths()
        current = (self.shelf.garden() if kind == "garden" else self.shelf.path()).id
        menu.addAction("New…").triggered.connect(lambda: self._ask_new(kind, copy=False))
        menu.addAction("Duplicate…").triggered.connect(lambda: self._ask_new(kind, copy=True))
        menu.addAction("Rename…").triggered.connect(lambda: self._ask_rename(kind))
        remove = menu.addAction("Remove")
        remove.setEnabled(len(entries) > 1)
        remove.triggered.connect(lambda: self._ask_remove(kind))
        menu.addAction("Import…").triggered.connect(lambda: self._import(kind))
        menu.addAction("Export…").triggered.connect(lambda: self._export(kind))
        menu.addSeparator()
        if kind == "garden":
            menu.addAction("Missing files…").triggered.connect(lambda: self.show_missing())
            menu.addSeparator()
        locked = kind == "path" and self.busy()
        for entry in entries:
            action = menu.addAction(entry.name.replace("&", "&&"))
            action.setCheckable(True)
            action.setChecked(entry.id == current)
            action.setEnabled(not locked or entry.id == current)
            action.triggered.connect(lambda _=False, e=entry.id: self._pick(kind, e))

    def _report(self, problem: str | None) -> None:
        if problem:
            QMessageBox.information(self.w, "SprigCast", problem)

    def _pick(self, kind: str, entry_id: str) -> None:
        self._report(self.switch(kind, entry_id))

    def _ask_name(self, title: str, text: str = "") -> str | None:
        name, ok = QInputDialog.getText(self.w, title, "Name:", text=text)
        return name if ok and name.strip() else None

    def _ask_new(self, kind: str, copy: bool) -> None:
        word = WORDS[kind][0]
        current = self.shelf.garden() if kind == "garden" else self.shelf.path()
        title = f"Duplicate {current.name}" if copy else f"New {word}"
        name = self._ask_name(title, f"{current.name} copy" if copy else "")
        if name is None:
            return
        try:
            entry = self._new(kind, name, current if copy else None)
        except ShelfError as exc:
            return self._report(str(exc))
        self._changed()
        self._report(self.switch(kind, entry.id))

    def _ask_rename(self, kind: str) -> None:
        current = self.shelf.garden() if kind == "garden" else self.shelf.path()
        name = self._ask_name(f"Rename {WORDS[kind][0]}", current.name)
        if name is not None:
            self._report(self.do(kind, "rename", current.id, name))

    def _ask_remove(self, kind: str) -> None:
        """Pick one that isn't in use (the one in use can't be removed)."""
        word = WORDS[kind][0]
        current = (self.shelf.garden() if kind == "garden" else self.shelf.path()).id
        others = [e for e in (self.shelf.gardens() if kind == "garden" else self.shelf.paths()) if e.id != current]
        if not others:
            return
        names = [e.name for e in others]
        name, ok = QInputDialog.getItem(self.w, f"Remove {word}", f"{cap(word)}:", names, 0, False)
        if not ok:
            return
        entry = others[names.index(name)]
        answer = QMessageBox.question(self.w, "SprigCast", f"Remove {entry.name}?")
        if answer == QMessageBox.StandardButton.Yes:
            self._report(self.do(kind, "remove", entry.id))

    def _import(self, kind: str) -> None:
        path, _ = QFileDialog.getOpenFileName(self.w, f"Import {WORDS[kind][0]}", str(Path.home()),
                                              "SprigCast (*.json)")
        if not path:
            return
        try:
            entry = self.shelf.import_file(kind, Path(path))
        except ShelfError as exc:
            return self._report(str(exc))
        self._changed()
        self.w.statusBar().showMessage(f"Added {entry.name} to {WORDS[kind][1]}.", 6000)

    def _export(self, kind: str) -> None:
        current = self.shelf.garden() if kind == "garden" else self.shelf.path()
        path, _ = QFileDialog.getSaveFileName(self.w, f"Export {current.name}",
                                              str(Path.home() / f"{current.name}.json"), "SprigCast (*.json)")
        if not path:
            return
        if kind == "garden":
            self.w.collection.save(self.w.scenes_path)
        try:
            self.shelf.export(kind, current.id, Path(path), self.w.collection)
        except OSError as exc:
            return self._report(f"Couldn't save it: {exc}")
        self.w.statusBar().showMessage(f"Exported {current.name}.", 6000)

    # Missing files ------------------------------------------------------------------------------

    def show_missing(self) -> None:
        MissingFiles(self.w).exec()


class MissingFiles(QDialog):
    """Files the garden can't find, each with Find… to point it at where the file is now."""

    def __init__(self, window: MainWindow):
        super().__init__(window)
        self.w = window
        self.setWindowTitle("Missing files")
        self.setMinimumWidth(520)
        self.box = QVBoxLayout(self)
        self.box.setContentsMargins(20, 20, 20, 20)
        self.box.setSpacing(12)
        self.rows = QWidget()
        self.rows_box = QVBoxLayout(self.rows)
        self.rows_box.setContentsMargins(0, 0, 0, 0)
        self.rows_box.setSpacing(8)
        self.box.addWidget(self.rows)
        close = QPushButton("Done")
        close.clicked.connect(self.accept)
        end = QHBoxLayout()
        end.addStretch(1)
        end.addWidget(close)
        self.box.addLayout(end)
        self.fill()

    def fill(self) -> None:
        while self.rows_box.count():
            item = self.rows_box.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        missing = missing_files(self.w.collection)
        if not missing:
            self.rows_box.addWidget(QLabel("Every file is there."))
            return
        for value in missing:
            row = QHBoxLayout()
            label = QLabel(Path(value).name or value)
            label.setToolTip(value)
            row.addWidget(label, 1)
            find = QPushButton("Find…")
            find.clicked.connect(lambda _=False, v=value: self.locate(v))
            row.addWidget(find)
            holder = QWidget()
            holder.setLayout(row)
            row.setContentsMargins(0, 0, 0, 0)
            self.rows_box.addWidget(holder)

    def locate(self, old: str) -> None:
        start = Path(old).parent if Path(old).parent.exists() else Path.home()
        new, _ = QFileDialog.getOpenFileName(self, f"Find {Path(old).name}", str(start))
        if not new:
            return
        if replace_file(self.w.collection, old, new):
            self.w._changed()
            self.w._refresh_sources()
        self.fill()
