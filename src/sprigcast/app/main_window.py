"""Main window: two canvas previews plus Scenes, Sources and Properties docks."""

from __future__ import annotations

import queue
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QColor, QFont, QKeySequence
from PySide6.QtWidgets import (
    QColorDialog,
    QDockWidget,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from sprigcast.app.preview import Preview
from sprigcast.core.events import EngineError, ReplaceCollection, SourceInfo, Stats
from sprigcast.core.scene import Scene, SceneCollection, SceneItem, SourceConfig
from sprigcast.render.engine import Engine
from sprigcast.sources.win32_enum import list_monitors, list_windows

ROLE_ID = Qt.ItemDataRole.UserRole


def _button(text: str, slot) -> QPushButton:
    b = QPushButton(text)
    b.clicked.connect(slot)
    return b


class MainWindow(QMainWindow):
    def __init__(self, collection: SceneCollection, scenes_path: Path):
        super().__init__()
        self.collection = collection
        self.scenes_path = scenes_path
        self.selected_scene_id: str | None = collection.active_scenes.get(collection.canvases[0].id)
        self._updating = False  # True while we fill widgets, so their signals do not echo back
        self.source_sizes: dict[str, tuple[int, int]] = {}
        self.source_statuses: dict[str, str] = {}

        self.setWindowTitle("SprigCast")
        self.resize(1440, 860)

        # Previews ------------------------------------------------------------
        self.previews: dict[str, Preview] = {}
        splitter = QSplitter(Qt.Orientation.Horizontal)
        for canvas in collection.canvases:
            preview = Preview(canvas.id.capitalize(), (canvas.width, canvas.height))
            self.previews[canvas.id] = preview
            splitter.addWidget(preview)
            splitter.setStretchFactor(splitter.count() - 1, 3 if canvas.width >= canvas.height else 1)
        self.setCentralWidget(splitter)

        self._build_scenes_dock()
        self._build_sources_dock()
        self._build_properties_dock()
        self._build_menu()
        self.status = QLabel("Starting renderer…")
        self.statusBar().addPermanentWidget(self.status)

        # Engine --------------------------------------------------------------
        self.engine = Engine(collection)
        self.engine.start()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll_engine)
        self.timer.start(33)  # about 30 preview updates per second

        self._refresh_all()

    # Building the UI ------------------------------------------------------

    def _build_scenes_dock(self) -> None:
        self.scene_tree = QTreeWidget()
        self.scene_tree.setHeaderHidden(True)
        self.scene_tree.itemSelectionChanged.connect(self._on_scene_selected)
        self.scene_tree.itemDoubleClicked.connect(lambda *_: self._make_active())
        buttons = QHBoxLayout()
        buttons.addWidget(_button("Show", self._make_active))
        buttons.addWidget(_button("Add", self._add_scene))
        buttons.addWidget(_button("Rename", self._rename_scene))
        buttons.addWidget(_button("Remove", self._remove_scene))
        self._dock("Scenes", self.scene_tree, buttons, Qt.DockWidgetArea.BottomDockWidgetArea)

    def _build_sources_dock(self) -> None:
        self.source_list = QListWidget()
        self.source_list.itemSelectionChanged.connect(self._refresh_properties)
        self.source_list.itemChanged.connect(self._on_source_toggled)
        buttons = QHBoxLayout()
        buttons.addWidget(_button("+ Display", self._add_display))
        buttons.addWidget(_button("+ Window", self._add_window))
        buttons.addWidget(_button("+ Color", self._add_color))
        buttons.addWidget(_button("+ Image", self._add_image))
        buttons.addWidget(_button("Remove", self._remove_item))
        buttons.addWidget(_button("Up", lambda: self._move_item(+1)))
        buttons.addWidget(_button("Down", lambda: self._move_item(-1)))
        self._dock("Sources", self.source_list, buttons, Qt.DockWidgetArea.BottomDockWidgetArea)

    def _build_properties_dock(self) -> None:
        panel = QWidget()
        form = QFormLayout(panel)
        self.prop_name = QLineEdit()
        self.prop_name.editingFinished.connect(self._on_name_edited)
        form.addRow("Name", self.prop_name)
        self.spin: dict[str, QDoubleSpinBox] = {}
        for key, lo, hi, step, decimals in (
            ("x", -10000, 10000, 1, 0),
            ("y", -10000, 10000, 1, 0),
            ("scale", 0.01, 20, 0.05, 2),
            ("rotation", -360, 360, 1, 1),
        ):
            box = QDoubleSpinBox()
            box.setRange(lo, hi)
            box.setSingleStep(step)
            box.setDecimals(decimals)
            box.valueChanged.connect(lambda value, k=key: self._on_transform_edited(k, value))
            self.spin[key] = box
            form.addRow(key.capitalize(), box)
        self.prop_extra = _button("", self._edit_source_settings)
        form.addRow("Source", self.prop_extra)
        self.prop_fit = _button("Fit to canvas", self._fit_item)
        form.addRow("", self.prop_fit)
        self.prop_status = QLabel("")
        self.prop_status.setWordWrap(True)
        form.addRow("Status", self.prop_status)
        self._dock("Properties", panel, None, Qt.DockWidgetArea.BottomDockWidgetArea)

    def _dock(self, title: str, body: QWidget, buttons: QHBoxLayout | None, area) -> None:
        wrapper = QWidget()
        layout = QVBoxLayout(wrapper)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(body)
        if buttons is not None:
            layout.addLayout(buttons)
        dock = QDockWidget(title, self)
        dock.setObjectName(title)
        dock.setWidget(wrapper)
        dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetMovable | QDockWidget.DockWidgetFeature.DockWidgetFloatable)
        self.addDockWidget(area, dock)

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        open_folder = QAction("Show scenes file location", self)
        open_folder.triggered.connect(
            lambda: QMessageBox.information(self, "SprigCast", f"Scenes are saved to:\n{self.scenes_path}")
        )
        file_menu.addAction(open_folder)
        quit_action = QAction("Quit", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

    # Engine link ------------------------------------------------------------

    def _poll_engine(self) -> None:
        for canvas_id, mailbox in self.engine.frames.items():
            frame = mailbox.take()
            if frame is not None and canvas_id in self.previews:
                self.previews[canvas_id].set_frame(frame.data, frame.width, frame.height)
        while True:
            try:
                event = self.engine.events.get_nowait()
            except queue.Empty:
                break
            if isinstance(event, Stats):
                self.status.setText(
                    f"{event.fps:.0f} fps  ·  render {event.render_ms:.1f} ms  ·  late ticks {event.dropped_ticks}"
                )
            elif isinstance(event, SourceInfo):
                self.source_sizes, self.source_statuses = event.sizes, event.statuses
                self._refresh_source_labels()
            elif isinstance(event, EngineError):
                self.statusBar().showMessage(event.message, 10000)
                if "compositor" in event.message:
                    QMessageBox.critical(self, "SprigCast", event.message)

    def _changed(self) -> None:
        """Save and hand a copy of the collection to the engine after every edit."""
        self.collection.save(self.scenes_path)
        self.engine.send(ReplaceCollection(self.collection.model_copy(deep=True)))

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        self.timer.stop()
        self.engine.stop()
        super().closeEvent(event)

    # Selection helpers -----------------------------------------------------

    def _scene(self) -> Scene | None:
        if self.selected_scene_id is None:
            return None
        try:
            return self.collection.scene(self.selected_scene_id)
        except StopIteration:
            return None

    def _item_index(self) -> int | None:
        """Index into scene.items of the selected row. The list shows the top-most item first."""
        scene, row = self._scene(), self.source_list.currentRow()
        if scene is None or row < 0:
            return None
        return len(scene.items) - 1 - row

    def _item(self) -> SceneItem | None:
        scene, index = self._scene(), self._item_index()
        return None if scene is None or index is None else scene.items[index]

    # Refreshing widgets ------------------------------------------------------

    def _refresh_all(self) -> None:
        self._refresh_scenes()
        self._refresh_sources()
        self._refresh_previews()

    def _refresh_previews(self) -> None:
        for canvas_id, preview in self.previews.items():
            scene = self.collection.active_scene(canvas_id)
            preview.subtitle = scene.name if scene else "no scene"
            preview.update()

    def _refresh_scenes(self) -> None:
        self._updating = True
        self.scene_tree.clear()
        bold = QFont()
        bold.setBold(True)
        for canvas in self.collection.canvases:
            parent = QTreeWidgetItem([f"{canvas.id.capitalize()}  ({canvas.width}x{canvas.height})"])
            parent.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.scene_tree.addTopLevelItem(parent)
            active = self.collection.active_scenes.get(canvas.id)
            for scene in self.collection.scenes_for(canvas.id):
                label = scene.name + ("   (linked)" if scene.linked_to else "")
                child = QTreeWidgetItem([label])
                child.setData(0, ROLE_ID, scene.id)
                if scene.id == active:
                    child.setFont(0, bold)
                    child.setText(0, "▶ " + label)
                parent.addChild(child)
                if scene.id == self.selected_scene_id:
                    child.setSelected(True)
            parent.setExpanded(True)
        self._updating = False

    def _refresh_sources(self, keep_row: int | None = None) -> None:
        self._updating = True
        self.source_list.clear()
        scene = self._scene()
        if scene is not None:
            for item in reversed(scene.items):  # top-most first, like OBS
                row = QListWidgetItem(self._source_label(item.source))
                row.setFlags(row.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                row.setCheckState(Qt.CheckState.Checked if item.visible else Qt.CheckState.Unchecked)
                self.source_list.addItem(row)
        if keep_row is not None and 0 <= keep_row < self.source_list.count():
            self.source_list.setCurrentRow(keep_row)
        self._updating = False
        self._refresh_properties()

    def _source_label(self, source_id: str) -> str:
        config = self.collection.source(source_id)
        label = f"{config.name}   [{config.kind}]"
        status = self.source_statuses.get(source_id, "ok")
        return label if status in ("ok", "capturing") else f"{label}   ({status})"

    def _refresh_source_labels(self) -> None:
        scene = self._scene()
        if scene is None or self.source_list.count() != len(scene.items):
            return
        self._updating = True
        for row, item in enumerate(reversed(scene.items)):
            text = self._source_label(item.source)
            if self.source_list.item(row).text() != text:
                self.source_list.item(row).setText(text)
        self._updating = False
        item = self._item()
        if item is not None:
            self.prop_status.setText(self._status_text(item.source))

    def _status_text(self, source_id: str) -> str:
        size = self.source_sizes.get(source_id)
        status = self.source_statuses.get(source_id, "")
        return f"{status}" + (f"  ·  {size[0]}x{size[1]}" if size and size[0] else "")

    def _refresh_properties(self) -> None:
        item = self._item()
        enabled = item is not None
        self._updating = True
        for box in self.spin.values():
            box.setEnabled(enabled)
        self.prop_name.setEnabled(enabled)
        self.prop_extra.setEnabled(enabled)
        self.prop_fit.setEnabled(enabled)
        if item is not None:
            config = self.collection.source(item.source)
            self.prop_name.setText(config.name)
            for key, box in self.spin.items():
                box.setValue(float(getattr(item, key)))
            self.prop_extra.setText(
                {
                    "color": "Pick color…",
                    "image": "Choose image…",
                    "display_capture": "Choose monitor…",
                    "window_capture": "Choose window…",
                }.get(config.kind, "Settings…")
            )
            self.prop_status.setText(self._status_text(item.source))
        else:
            self.prop_name.clear()
            self.prop_extra.setText("")
            self.prop_status.setText("")
        self._updating = False

    # Scene actions ---------------------------------------------------------------

    def _on_scene_selected(self) -> None:
        if self._updating:
            return
        selected = self.scene_tree.selectedItems()
        scene_id = selected[0].data(0, ROLE_ID) if selected else None
        if scene_id:
            self.selected_scene_id = scene_id
            self._refresh_sources()

    def _make_active(self) -> None:
        scene = self._scene()
        if scene is None:
            return
        self.collection.set_active(scene.id)
        self._changed()
        self._refresh_scenes()
        self._refresh_previews()

    def _add_scene(self) -> None:
        canvases = [c.id for c in self.collection.canvases]
        canvas_id, ok = QInputDialog.getItem(self, "Add scene", "Canvas:", canvases, 0, False)
        if not ok:
            return
        name, ok = QInputDialog.getText(self, "Add scene", "Scene name:")
        if not ok or not name.strip():
            return
        link_options = ["(not linked)"] + [s.name for s in self.collection.scenes if s.canvas != canvas_id]
        link, ok = QInputDialog.getItem(self, "Add scene", "Switch together with:", link_options, 0, False)
        if not ok:
            return
        linked = next((s.id for s in self.collection.scenes if s.name == link and s.canvas != canvas_id), None)
        scene = Scene(id=self.collection.new_id("scene"), name=name.strip(), canvas=canvas_id, linked_to=linked)
        self.collection.scenes.append(scene)
        self.collection.active_scenes.setdefault(canvas_id, scene.id)
        self.selected_scene_id = scene.id
        self._changed()
        self._refresh_all()

    def _rename_scene(self) -> None:
        scene = self._scene()
        if scene is None:
            return
        name, ok = QInputDialog.getText(self, "Rename scene", "Scene name:", text=scene.name)
        if ok and name.strip():
            scene.name = name.strip()
            self._changed()
            self._refresh_all()

    def _remove_scene(self) -> None:
        scene = self._scene()
        if scene is None:
            return
        if len(self.collection.scenes_for(scene.canvas)) == 1:
            QMessageBox.information(self, "SprigCast", "Each canvas needs at least one scene.")
            return
        if QMessageBox.question(self, "Remove scene", f"Remove {scene.name!r}?") != QMessageBox.StandardButton.Yes:
            return
        self.collection.scenes = [s for s in self.collection.scenes if s.id != scene.id]
        for other in self.collection.scenes:
            if other.linked_to == scene.id:
                other.linked_to = None
        if self.collection.active_scenes.get(scene.canvas) == scene.id:
            self.collection.active_scenes[scene.canvas] = self.collection.scenes_for(scene.canvas)[0].id
        for item in scene.items:
            self.collection.remove_source_if_unused(item.source)
        self.selected_scene_id = self.collection.active_scenes.get(scene.canvas)
        self._changed()
        self._refresh_all()

    # Source actions --------------------------------------------------------------

    def _add_item(self, config: SourceConfig) -> None:
        scene = self._scene()
        if scene is None:
            QMessageBox.information(self, "SprigCast", "Select a scene first.")
            return
        self.collection.sources.append(config)
        scene.items.append(SceneItem(source=config.id))
        self._changed()
        self._refresh_sources(keep_row=0)

    def _add_color(self) -> None:
        scene = self._scene()
        if scene is None:
            return
        color = QColorDialog.getColor(QColor("#9cc89a"), self, "Color", QColorDialog.ColorDialogOption.ShowAlphaChannel)
        if not color.isValid():
            return
        canvas = self.collection.canvas(scene.canvas)
        hex_color = "#{:02x}{:02x}{:02x}{:02x}".format(color.red(), color.green(), color.blue(), color.alpha())
        self._add_item(
            SourceConfig(
                id=self.collection.new_id("color"),
                kind="color",
                name=f"Color {hex_color}",
                settings={"color": hex_color, "width": canvas.width, "height": canvas.height},
            )
        )

    def _add_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose image", "", "Images (*.png *.jpg *.jpeg *.webp *.bmp *.gif)")
        if not path:
            return
        self._add_item(
            SourceConfig(id=self.collection.new_id("image"), kind="image", name=Path(path).stem, settings={"path": path})
        )

    def _pick_monitor(self, current: int | None = None):
        monitors = list_monitors()
        if not monitors:
            QMessageBox.information(self, "SprigCast", "Display capture is only available on Windows.")
            return None
        labels = [m.label for m in monitors]
        start = next((i for i, m in enumerate(monitors) if m.index == current), 0)
        label, ok = QInputDialog.getItem(self, "Display capture", "Monitor:", labels, start, False)
        return monitors[labels.index(label)] if ok else None

    def _pick_window(self):
        windows = list_windows()
        if not windows:
            QMessageBox.information(self, "SprigCast", "Window capture is only available on Windows.")
            return None
        labels = [w.label for w in windows]
        label, ok = QInputDialog.getItem(self, "Window capture", "Window:", labels, 0, False)
        return windows[labels.index(label)] if ok else None

    def _add_display(self) -> None:
        scene = self._scene()
        if scene is None:
            QMessageBox.information(self, "SprigCast", "Select a scene first.")
            return
        monitor = self._pick_monitor()
        if monitor is None:
            return
        config = SourceConfig(
            id=self.collection.new_id("display"),
            kind="display_capture",
            name=f"Monitor {monitor.index}",
            settings={"monitor": monitor.index, "cursor": True},
        )
        self._add_item(config)
        self._fit_item(size=(monitor.width, monitor.height))

    def _add_window(self) -> None:
        if self._scene() is None:
            QMessageBox.information(self, "SprigCast", "Select a scene first.")
            return
        window = self._pick_window()
        if window is None:
            return
        name = window.title if len(window.title) <= 40 else window.title[:37] + "…"
        self._add_item(
            SourceConfig(
                id=self.collection.new_id("window"),
                kind="window_capture",
                name=name,
                settings={"title": window.title, "process": window.process, "cursor": True},
            )
        )

    def _fit_item(self, checked: bool = False, size: tuple[int, int] | None = None) -> None:
        """Scale the selected item to fit its canvas and centre it."""
        scene, item = self._scene(), self._item()
        if scene is None or item is None:
            return
        size = size or self.source_sizes.get(item.source)
        if not size or not size[0] or not size[1]:
            self.statusBar().showMessage("That source has no picture yet. Try again in a second.", 4000)
            return
        canvas = self.collection.canvas(scene.canvas)
        w, h = item.cropped_size(size)
        if not w or not h:
            return
        item.scale = min(canvas.width / w, canvas.height / h)
        item.rotation = 0.0
        item.x = (canvas.width - w * item.scale) / 2
        item.y = (canvas.height - h * item.scale) / 2
        self._changed()
        self._refresh_properties()

    def _remove_item(self) -> None:
        scene, index = self._scene(), self._item_index()
        if scene is None or index is None:
            return
        removed = scene.items.pop(index)
        self.collection.remove_source_if_unused(removed.source)
        self._changed()
        self._refresh_sources()

    def _move_item(self, direction: int) -> None:
        """direction +1 moves the item up the list (drawn later, so on top)."""
        scene, index = self._scene(), self._item_index()
        if scene is None or index is None:
            return
        target = index + direction
        if not 0 <= target < len(scene.items):
            return
        scene.items[index], scene.items[target] = scene.items[target], scene.items[index]
        self._changed()
        self._refresh_sources(keep_row=len(scene.items) - 1 - target)

    def _on_source_toggled(self, row: QListWidgetItem) -> None:
        if self._updating:
            return
        scene = self._scene()
        if scene is None:
            return
        index = len(scene.items) - 1 - self.source_list.row(row)
        scene.items[index].visible = row.checkState() == Qt.CheckState.Checked
        self._changed()

    def _on_transform_edited(self, key: str, value: float) -> None:
        if self._updating:
            return
        item = self._item()
        if item is None:
            return
        setattr(item, key, value)
        self._changed()

    def _on_name_edited(self) -> None:
        item = self._item()
        if self._updating or item is None or not self.prop_name.text().strip():
            return
        self.collection.source(item.source).name = self.prop_name.text().strip()
        self._changed()
        self._refresh_sources(keep_row=self.source_list.currentRow())

    def _edit_source_settings(self) -> None:
        item = self._item()
        if item is None:
            return
        config = self.collection.source(item.source)
        if config.kind == "color":
            current = QColor(str(config.settings.get("color", "#000000"))[:7])
            color = QColorDialog.getColor(current, self, "Color", QColorDialog.ColorDialogOption.ShowAlphaChannel)
            if not color.isValid():
                return
            config.settings = {
                **config.settings,
                "color": "#{:02x}{:02x}{:02x}{:02x}".format(color.red(), color.green(), color.blue(), color.alpha()),
            }
        elif config.kind == "image":
            path, _ = QFileDialog.getOpenFileName(self, "Choose image", "", "Images (*.png *.jpg *.jpeg *.webp *.bmp *.gif)")
            if not path:
                return
            config.settings = {**config.settings, "path": path}
        elif config.kind == "display_capture":
            monitor = self._pick_monitor(current=int(config.settings.get("monitor", 1)))
            if monitor is None:
                return
            config.settings = {**config.settings, "monitor": monitor.index}
            config.name = f"Monitor {monitor.index}"
        elif config.kind == "window_capture":
            window = self._pick_window()
            if window is None:
                return
            config.settings = {**config.settings, "title": window.title, "process": window.process}
            config.name = window.title if len(window.title) <= 40 else window.title[:37] + "…"
        self._changed()
        self._refresh_sources(keep_row=self.source_list.currentRow())
