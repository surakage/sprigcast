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
import os
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QStandardPaths, Qt
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMenu,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from sprigcast.app.terms import COLLECTION, COLLECTIONS, PROFILE, PROFILES, cap
from sprigcast.core import convert, pack
from sprigcast.core.gardens import (
    Shelf,
    ShelfError,
    exports_folder,
    file_name,
    find_in_folder,
    missing_files,
    replace_file,
)

if TYPE_CHECKING:
    from sprigcast.app.main_window import MainWindow

log = logging.getLogger(__name__)

SHELF_HOTKEYS = ("garden:", "path:")  # hotkeys that switch gardens and paths
WORDS = {"garden": (COLLECTION, COLLECTIONS), "path": (PROFILE, PROFILES)}


class GardenControl:
    """Owns the Shelf for a window and does every garden and path change (menus and the API)."""

    def __init__(self, window: MainWindow, shelf: Shelf):
        self.w = window
        self.shelf = shelf
        self.last_notes: list[str] = []  # what the last import couldn't bring over
        self.working = False  # packing or unpacking in the background: no switching meanwhile
        from sprigcast.app import hotkeys

        hotkeys.shelf_actions = self.hotkey_actions

    def hotkey_actions(self) -> list[tuple[str, str]]:
        return [*((f"garden:{g.id}", f"{cap(COLLECTION)}: {g.name}") for g in self.shelf.gardens()),
                *((f"path:{p.id}", f"{cap(PROFILE)}: {p.name}") for p in self.shelf.paths())]

    # What the API sees --------------------------------------------------------------------------

    def lists(self) -> dict:
        return {"garden": self.shelf.garden().name, "gardens": [g.name for g in self.shelf.gardens()],
                "path": self.shelf.path().name, "paths": [p.name for p in self.shelf.paths()]}

    def do(self, kind: str, op: str, name: str = "", new_name: str = "") -> str | None:
        """op: switch, new, duplicate, rename or remove. Returns a problem in plain words, or None."""
        problem = self.blocked()
        if problem:
            return problem
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
        except OSError as exc:
            return f"Couldn't save it: {exc.strerror or exc}"
        self._changed()
        return None

    # Switching ----------------------------------------------------------------------------------

    def busy(self) -> bool:
        return self.w.streaming is not None or self.w.recording is not None

    def blocked(self) -> str | None:
        """Why a hotkey or the API can't change gardens or paths right now (None when it can)."""
        if self.working:
            return "Wait for the pack to finish."
        if QApplication.activeModalWidget() is not None:  # e.g. Settings, which saves into the garden on screen
            return "Close the open window first."
        return None

    def switch(self, kind: str, wanted: str) -> str | None:
        if self.working:
            return "Wait for the pack to finish."
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
        # Hotkeys that switch gardens and paths work from every garden: they come along.
        incoming.hotkeys = {**{k: v for k, v in incoming.hotkeys.items() if not k.startswith(SHELF_HOTKEYS)},
                            **{k: v for k, v in w.collection.hotkeys.items() if k.startswith(SHELF_HOTKEYS)}}
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
        self.refresh_chip()
        control = getattr(self.w, "control", None)
        if control is not None:
            control.soon()

    def taken_output_ids(self) -> set[str]:
        return self.shelf.taken_output_ids()

    def close(self) -> None:
        from sprigcast.app import hotkeys

        if hotkeys.shelf_actions == self.hotkey_actions:  # a closed window's gardens aren't offered any more
            hotkeys.shelf_actions = hotkeys._no_shelf
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
        if kind == "garden":
            for app, files in self._other_apps().items():
                sub = menu.addMenu(f"Import from {convert.APP_NAMES[app]}")
                names = convert.streamlabs_names(files[0].parent) if app == "streamlabs" else {}
                if names:  # files the manifest doesn't list are collections Streamlabs deleted
                    files = sorted((f for f in files if f.stem in names), key=lambda f: names[f.stem].casefold())
                if not files:
                    menu.removeAction(sub.menuAction())
                    continue
                for file in files:
                    label = names.get(file.stem, file.stem.replace("_", " "))
                    sub.addAction(label.replace("&", "&&")).triggered.connect(
                        lambda _=False, f=file: self._report(self.import_garden(f)))
        menu.addAction("Export…").triggered.connect(lambda: self._export(kind))
        if kind == "garden":
            menu.addAction("Export with files…").triggered.connect(lambda: self._export_pack())
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

    def _other_apps(self) -> dict[str, list[Path]]:
        """OBS and Streamlabs scene collections on this PC (Windows keeps them under %APPDATA%)."""
        appdata = os.environ.get("APPDATA")
        return convert.scene_collection_files(Path(appdata)) if appdata else {}

    def _report(self, problem: str | None) -> None:
        if problem:
            QMessageBox.information(self.w, "SprigCast", problem)

    def _pick(self, kind: str, entry_id: str) -> None:
        if kind == "garden" and self.w.streaming is not None:
            name = self.shelf.find("garden", entry_id).name
            answer = QMessageBox.question(self.w, "SprigCast", f"You're live. Switch to {name} now?")
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._report(self.switch(kind, entry_id))

    def hotkey(self, action: str) -> None:
        """A "garden:<id>" or "path:<id>" hotkey."""
        kind, _, entry_id = action.partition(":")
        problem = self.blocked() or self.switch(kind, entry_id)
        if problem:
            self.w.statusBar().showMessage(problem, 6000)

    # The chip in the header: which garden and path are in use, and a quick way to switch ------

    def make_chip(self) -> QToolButton:
        from sprigcast.app import icons, theme

        chip = QToolButton()
        chip.setObjectName("shelfChip")
        chip.setIcon(icons.icon("leaf", theme.ACCENT, 16))
        chip.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        chip.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        chip.setCursor(Qt.CursorShape.PointingHandCursor)
        chip.setStyleSheet(
            f"QToolButton#shelfChip {{ background: {theme.PANEL}; border: none; border-radius: 18px;"
            f" padding: 8px 16px; color: {theme.TEXT}; }}"
            f"QToolButton#shelfChip:hover {{ background: {theme.RAISED}; }}"
            "QToolButton#shelfChip::menu-indicator { image: none; width: 0; }")
        menu = QMenu(chip)
        menu.aboutToShow.connect(lambda: self._fill_chip(menu))
        chip.setMenu(menu)
        self.chip = chip
        self.refresh_chip()
        return chip

    def refresh_chip(self) -> None:
        chip = getattr(self, "chip", None)
        if chip is not None:
            garden, path = self.shelf.garden().name, self.shelf.path().name
            chip.setText(f"{garden}  ·  {path}".replace("&", "&&"))
            chip.setToolTip(f"{cap(COLLECTION)}: {garden}\n{cap(PROFILE)}: {path}")

    def _fill_chip(self, menu: QMenu) -> None:
        menu.clear()
        for kind in ("garden", "path"):
            entries = self.shelf.gardens() if kind == "garden" else self.shelf.paths()
            current = (self.shelf.garden() if kind == "garden" else self.shelf.path()).id
            menu.addSection(cap(WORDS[kind][1]))
            locked = kind == "path" and self.busy()
            for entry in entries:
                action = menu.addAction(entry.name.replace("&", "&&"))
                action.setCheckable(True)
                action.setChecked(entry.id == current)
                action.setEnabled(not locked or entry.id == current)
                action.triggered.connect(lambda _=False, k=kind, e=entry.id: self._pick(k, e))

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

    def _exports(self) -> Path:
        folder = exports_folder(Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)))
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            return Path.home()
        return folder

    def _import(self, kind: str) -> None:
        types = f"SprigCast, OBS, Streamlabs or Meld (*.json *{pack.PACK_SUFFIX} *{convert.OVERLAY_SUFFIX})" if kind == "garden" \
            else "SprigCast (*.json)"
        path, _ = QFileDialog.getOpenFileName(self.w, f"Import {WORDS[kind][0]}", str(self._exports()), types)
        if not path:
            return
        if kind == "garden":
            return self._report(self.import_garden(Path(path)))
        try:
            entry = self.shelf.import_file(kind, Path(path))
        except ShelfError as exc:
            return self._report(str(exc))
        self._changed()
        self.w.statusBar().showMessage(f"Added {entry.name} to {WORDS[kind][1]}.", 6000)

    def _audio_names(self) -> list[str]:
        try:
            from sprigcast.sources.audio import list_audio_devices

            return [d.name for loop in (False, True) for d in list_audio_devices(loopback=loop, rescan=False)]
        except Exception:
            return []

    def import_garden(self, path: Path, show_notes: bool = True) -> str | None:
        """A SprigCast export or pack, or a scene collection from OBS, Streamlabs or Meld: added, then switched to."""
        try:
            path, notes = Path(path), []
            if convert.is_overlay_file(path):
                entry, notes = self._import_overlay(path)
            elif pack.is_pack(path):
                folder = pack.free_folder(self._media(), path.stem)
                garden = self._in_background(f"Unpacking {path.stem}…",
                                             lambda progress: pack.read_pack(path, folder, progress))
                entry = self.shelf.add_garden(path.stem, garden)
            elif (app := convert.detect(data := convert.read_json(path))) == "sprigcast":
                entry = self.shelf.import_file("garden", path)
            elif app:
                garden, notes = convert.convert(data, self.w.collection, audio_devices=self._audio_names())
                entry = self.shelf.add_garden(convert.guess_name(path, data), garden)
            else:
                return f"{path.name} isn't a scene collection from SprigCast, OBS, Streamlabs or Meld."
        except (convert.ConvertError, ShelfError, pack.PackError) as exc:
            return str(exc)
        except Exception:
            log.exception("importing %s", path)
            return f"{path.name} couldn't be turned into a {COLLECTION}."
        self._changed()
        self.last_notes = notes
        if self.w.streaming is not None and QMessageBox.question(
                self.w, "SprigCast", f"Added {entry.name}. You're live: switch to it now?") \
                != QMessageBox.StandardButton.Yes:
            self.w.statusBar().showMessage(f"Added {entry.name} to {COLLECTIONS}.", 8000)
        else:
            problem = self.switch("garden", entry.id)
            if problem:
                return problem
        if notes and show_notes:
            ImportNotes(self.w, entry.name, notes).exec()
        return None

    def _export(self, kind: str) -> None:
        current = self.shelf.garden() if kind == "garden" else self.shelf.path()
        start = self._exports() / f"{file_name(current.name)}.json"
        path, _ = QFileDialog.getSaveFileName(self.w, f"Export {current.name}", str(start), "SprigCast (*.json)")
        if not path:
            return
        target = Path(path)
        target = target.with_name(file_name(target.stem) + ".json")
        if kind == "garden":
            self.w.collection.save(self.w.scenes_path)
        try:
            self.shelf.export(kind, current.id, target, self.w.collection)
        except ShelfError as exc:
            return self._report(str(exc))
        except OSError as exc:
            return self._report(f"Couldn't save it: {exc.strerror or exc}")
        self.w.statusBar().showMessage(f"Exported {current.name} to {target}", 8000)

    def _media(self) -> Path:
        return pack.media_folder(Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)))

    def _in_background(self, label: str, work):
        """Run work(progress) on its own thread with a progress bar, so the window and the stream stay
        smooth (zip and disk work let go of Python's lock); switching waits until it's done."""
        self.working = True
        try:
            return _Progress(self.w, label).run(work)
        finally:
            self.working = False

    def _import_overlay(self, path: Path):
        """A Streamlabs .overlay: its media unpack to Documents/SprigCast/Media/<name>, then it's converted."""
        import shutil

        folder = pack.free_folder(self._media(), path.stem)
        data = self._in_background(f"Unpacking {path.stem}…", lambda _progress: convert.unpack_overlay(path, folder))
        try:
            garden, notes = convert.convert(data, self.w.collection, audio_devices=self._audio_names())
            return self.shelf.add_garden(path.stem, garden), notes
        except Exception:
            shutil.rmtree(folder, ignore_errors=True)
            raise

    def _export_pack(self) -> None:
        current = self.shelf.garden()
        start = self._exports() / f"{file_name(current.name)}{pack.PACK_SUFFIX}"
        path, _ = QFileDialog.getSaveFileName(self.w, f"Export {current.name} with files", str(start),
                                              f"SprigCast with files (*{pack.PACK_SUFFIX})")
        if not path:
            return
        target = Path(path)
        target = target.with_name(file_name(target.stem) + pack.PACK_SUFFIX)
        self.w.collection.save(self.w.scenes_path)
        snapshot = self.w.collection.model_copy(deep=True)  # the garden as it is now, whatever happens meanwhile
        try:
            left_out = self._in_background(f"Packing {current.name}…",
                                           lambda progress: self.shelf.export_pack(current.id, target, snapshot,
                                                                                   progress))
        except ShelfError as exc:
            return self._report(str(exc))
        except OSError as exc:
            return self._report(f"Couldn't save it: {exc.strerror or exc}")
        if left_out:
            ImportNotes(self.w, current.name, [f"Not found, left out: {f}" for f in left_out]).exec()
        self.w.statusBar().showMessage(f"Exported {current.name} to {target}", 8000)

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
        self.look = QPushButton("Look in a folder…")
        self.look.clicked.connect(self.look_in_folder)
        end = QHBoxLayout()
        end.addWidget(self.look)
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
        if hasattr(self, "look"):
            self.look.setVisible(bool(missing))
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

    def look_in_folder(self) -> None:
        """Find every missing file at once in a folder you pick (e.g. the drive they moved to)."""
        folder = QFileDialog.getExistingDirectory(self, "Look in a folder", str(Path.home()))
        if not folder:
            return
        missing = missing_files(self.w.collection)
        self.relink(self.w.gardens._in_background(
            "Looking for the files…", lambda _progress: find_in_folder(missing, Path(folder))))

    def relink(self, found: dict[str, str]) -> None:
        changed = sum(replace_file(self.w.collection, old, new) for old, new in found.items())
        if changed:
            self.w._changed()
            self.w._refresh_sources()
        self.w.statusBar().showMessage(f"Found {len(found)} file(s)." if found else "None of them are in that folder.",
                                       6000)
        self.fill()

    def locate(self, old: str) -> None:
        start = Path(old).parent if Path(old).parent.exists() else Path.home()
        new, _ = QFileDialog.getOpenFileName(self, f"Find {Path(old).name}", str(start))
        if not new:
            return
        if replace_file(self.w.collection, old, new):
            self.w._changed()
            self.w._refresh_sources()
        self.fill()


class _Progress:
    """A progress bar for packing and unpacking; shown only when it takes more than a moment."""

    def __init__(self, window: QWidget, label: str):
        self.window, self.label, self.bar = window, label, None

    def __enter__(self):
        if QApplication.instance() is not None:
            self.bar = QProgressDialog(self.label, "", 0, 1000, self.window)
            self.bar.setWindowTitle("SprigCast")
            self.bar.setCancelButton(None)
            self.bar.setMinimumDuration(400)
            self.bar.setWindowModality(Qt.WindowModality.WindowModal)
        return self

    def __call__(self, done: int, total: int) -> None:  # from the worker thread: just noted
        self.share = done / total if total else 1.0

    def __exit__(self, *exc) -> None:
        if self.bar is not None:
            self.bar.close()
            self.bar.deleteLater()

    def run(self, work):
        """work(self) on a thread; the window keeps running until it's done. Its error is raised here."""
        import threading

        from PySide6.QtCore import QEventLoop

        self.share = 0.0
        result: dict = {}

        def target() -> None:
            try:
                result["value"] = work(self)
            except BaseException as exc:  # noqa: BLE001 (handed back to the window's thread below)
                result["error"] = exc

        with self:
            thread = threading.Thread(target=target, name="sprigcast-pack", daemon=True)
            thread.start()
            while thread.is_alive():
                if self.bar is not None:
                    self.bar.setValue(int(1000 * self.share))
                QApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 30)
                thread.join(0.03)
        if "error" in result:
            raise result["error"]
        return result.get("value")


class ImportNotes(QDialog):
    """What an import couldn't bring over, one line each."""

    def __init__(self, window: MainWindow, name: str, notes: list[str]):
        super().__init__(window)
        self.setWindowTitle(f"{name}: to check")
        self.setMinimumWidth(560)
        box = QVBoxLayout(self)
        box.setContentsMargins(20, 20, 20, 20)
        box.setSpacing(12)
        text = QLabel("\n".join(f"•  {n}" for n in notes[:40]) + (f"\n…and {len(notes) - 40} more" if len(notes) > 40 else ""))
        text.setWordWrap(True)
        text.setTextInteractionFlags(text.textInteractionFlags() | Qt.TextInteractionFlag.TextSelectableByMouse)
        box.addWidget(text)
        done = QPushButton("Done")
        done.clicked.connect(self.accept)
        end = QHBoxLayout()
        end.addStretch(1)
        end.addWidget(done)
        box.addLayout(end)
