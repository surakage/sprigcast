"""The Optimize for this PC wizard: what you use SprigCast for, a quick check of the PC, then suggestions.

Offered once at first start and any time from Settings → Optimize for this PC (or the command palette).
The checks and suggestions themselves live in app/optimize.py; this is only the window around them.
"""

from __future__ import annotations

import queue
import threading

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from sprigcast.app import optimize, theme
from sprigcast.core.scene import SceneCollection

TITLE_STYLE = f"font-size: 15pt; font-weight: bold; color: {theme.TEXT_BRIGHT};"


def _label(text: str, style: str = "") -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    if style:
        label.setStyleSheet(style)
    return label


class OptimizeWizard(QDialog):
    """Three pages: Start, Checking, Suggestions. ``chosen`` holds the ticked changes after Apply."""

    def __init__(self, collection: SceneCollection, waiting_fps: dict[str, int] | None = None,
                 first_run: bool = False, parent: QWidget | None = None, run_in_thread: bool = True):
        super().__init__(parent)
        self.setWindowTitle("Optimize for this PC")
        self.setMinimumWidth(560)
        self.collection = collection
        self.waiting_fps = waiting_fps or {}
        self.run_in_thread = run_in_thread
        self.profile = optimize.PcProfile()
        self.plan: optimize.Plan | None = None
        self.chosen: list[optimize.Change] = []
        self._results: queue.Queue = queue.Queue()
        self._rows: dict[str, QListWidgetItem] = {}
        self._pending: list[optimize.Check] = []

        self.pages = QStackedWidget()
        self.pages.addWidget(self._start_page(first_run))
        self.pages.addWidget(self._check_page())
        self.pages.addWidget(self._result_page())

        self.back = QPushButton("Back")
        self.back.clicked.connect(lambda: self.pages.setCurrentIndex(0))
        self.cancel = QPushButton("Not now" if first_run else "Cancel")
        self.cancel.clicked.connect(self.reject)
        self.go = QPushButton("Check my PC")
        self.go.setStyleSheet(theme.PRIMARY_STYLE)
        self.go.setDefault(True)
        self.go.clicked.connect(self._next)
        buttons = QHBoxLayout()
        buttons.addWidget(self.back)
        buttons.addStretch(1)
        buttons.addWidget(self.cancel)
        buttons.addWidget(self.go)
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 18)
        root.setSpacing(14)
        root.addWidget(self.pages, 1)
        root.addLayout(buttons)
        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._poll)
        self._show_buttons()

    # Pages -------------------------------------------------------------------------

    def _start_page(self, first_run: bool) -> QWidget:
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 0, 0, 0)
        box.addWidget(_label("Welcome to SprigCast" if first_run else "Optimize for this PC", TITLE_STYLE))
        box.addWidget(_label("SprigCast checks your PC and suggests stream and recording settings that fit it. "
                             "Takes about 15 seconds. Nothing changes until you press Apply."))
        box.addSpacing(6)
        box.addWidget(_label("I use SprigCast to:", theme.MUTED_STYLE))
        self.wants_stream = QCheckBox("Stream")
        self.wants_record = QCheckBox("Record and save replays")
        self.test_upload = QCheckBox("Test my upload speed (sends about 10 MB to Cloudflare's speed test)")
        for check in (self.wants_stream, self.wants_record, self.test_upload):
            check.setChecked(True)
            box.addWidget(check)
        self.wants_stream.toggled.connect(self.test_upload.setEnabled)
        for check in (self.wants_stream, self.wants_record):
            check.toggled.connect(self._show_buttons)
        box.addStretch(1)
        return page

    def _check_page(self) -> QWidget:
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 0, 0, 0)
        box.addWidget(_label("Checking your PC…", TITLE_STYLE))
        self.check_list = QListWidget()
        self.check_list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.check_list.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        box.addWidget(self.check_list, 1)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setMaximumHeight(6)
        box.addWidget(self.progress)
        return page

    def _result_page(self) -> QWidget:
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 0, 0, 0)
        box.addWidget(_label("Suggested settings", TITLE_STYLE))
        self.headline = _label("")
        box.addWidget(self.headline)
        upload = QHBoxLayout()
        upload.addWidget(QLabel("Upload speed"))
        self.upload = QDoubleSpinBox()
        self.upload.setRange(0, 1000)
        self.upload.setDecimals(1)
        self.upload.setSuffix(" Mbps")
        self.upload.setSpecialValueText("Not tested")
        self.upload.setToolTip("Change it to see what fits: a speed test site such as speedtest.net shows it.")
        self.upload.valueChanged.connect(self._upload_changed)
        upload.addWidget(self.upload)
        upload.addStretch(1)
        self.upload_row = QWidget()
        self.upload_row.setLayout(upload)
        upload.setContentsMargins(0, 0, 0, 0)
        box.addWidget(self.upload_row)
        self.change_list = QListWidget()
        self.change_list.setWordWrap(True)
        self.change_list.setSpacing(4)
        self.change_list.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        box.addWidget(self.change_list, 1)
        self.notes = _label("", f"{theme.NOTE_STYLE} padding: 8px 10px;")
        box.addWidget(self.notes)
        return page

    # Flow ------------------------------------------------------------------------------

    def _show_buttons(self) -> None:
        page = self.pages.currentIndex()
        self.back.setVisible(page == 2)
        self.go.setVisible(page != 1)
        if page == 0:
            self.go.setText("Check my PC")
            self.go.setEnabled(self.wants_stream.isChecked() or self.wants_record.isChecked())
        elif page == 2:
            self.go.setText("Apply")
            self.go.setEnabled(bool(self.plan and self.plan.changes))

    def _next(self) -> None:
        if self.pages.currentIndex() == 0:
            self.start_checks()
        elif self.pages.currentIndex() == 2:
            self._apply()

    def start_checks(self) -> None:
        self.profile = optimize.PcProfile(wants_stream=self.wants_stream.isChecked(),
                                          wants_record=self.wants_record.isChecked())
        self.profile.extra["test_upload"] = self.test_upload.isChecked()
        self.check_list.clear()
        self._rows.clear()
        self._pending = list(optimize.CHECKS)
        self.progress.setRange(0, len(self._pending))
        self.progress.setValue(0)
        self.pages.setCurrentIndex(1)
        self._show_buttons()
        self._run_next()

    def _run_next(self) -> None:
        while self._pending:
            check = self._pending.pop(0)
            if not check.when(self.profile):
                self.progress.setMaximum(self.progress.maximum() - 1)
                continue
            self._rows[check.key] = QListWidgetItem(f"⋯   {check.label}", self.check_list)
            if check.background and self.run_in_thread:
                threading.Thread(target=self._run_check, args=(check,), daemon=True, name=f"optimize-{check.key}").start()
                self._timer.start()
            else:
                self._run_check(check)
                self._poll()
            return
        self._timer.stop()
        self.show_plan()

    def _run_check(self, check: optimize.Check) -> None:
        try:
            result = check.run(self.profile)
        except Exception as exc:  # one failed check never stops the rest
            result = f"Couldn't check ({exc})"
        self._results.put((check, result))

    def _poll(self) -> None:
        try:
            check, result = self._results.get_nowait()
        except queue.Empty:
            return
        self._rows[check.key].setText(f"✓   {check.label}:  {result}")
        self.progress.setValue(self.progress.value() + 1)
        self._run_next()

    def show_plan(self) -> None:
        self.upload_row.setVisible(self.profile.wants_stream)
        self.upload.blockSignals(True)
        self.upload.setValue((self.profile.upload_kbps or 0) / 1000)
        self.upload.blockSignals(False)
        self._fill_plan()
        self.pages.setCurrentIndex(2)
        self._show_buttons()

    def _upload_changed(self, mbps: float) -> None:
        self.profile.upload_kbps = int(mbps * 1000) or None
        self._fill_plan()
        self._show_buttons()

    def _fill_plan(self) -> None:
        self.plan = optimize.plan(self.profile, self.collection, self.waiting_fps)
        self.headline.setText(self.plan.headline)
        self.change_list.clear()
        for change in self.plan.changes:
            item = QListWidgetItem(f"{change.label}\nNow: {change.now}", self.change_list)
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            item.setData(Qt.ItemDataRole.UserRole, change)
        if not self.plan.changes:
            QListWidgetItem("Your settings already fit this PC. Nothing to change.", self.change_list)
        self.notes.setText("\n".join(self.plan.notes))
        self.notes.setVisible(bool(self.plan.notes))

    def _apply(self) -> None:
        self.chosen = [self.change_list.item(i).data(Qt.ItemDataRole.UserRole)
                       for i in range(self.change_list.count())
                       if self.change_list.item(i).checkState() == Qt.CheckState.Checked
                       and self.change_list.item(i).data(Qt.ItemDataRole.UserRole) is not None]
        self.accept()

    def done(self, result: int) -> None:
        self._timer.stop()
        super().done(result)
