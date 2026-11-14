"""The Optimize for this PC wizard: what you use SprigCast for, a quick check of the PC, then suggestions.

Offered once at first start and any time from Settings → Optimize for this PC (or the command palette).
The checks and suggestions themselves live in app/optimize.py; this is only the window around them.
"""

from __future__ import annotations

import logging
import queue
import threading

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QDialog,
    QDoubleSpinBox,
    QFrame,
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

log = logging.getLogger(__name__)
from sprigcast.core.scene import SceneCollection

TITLE_STYLE = f"font-size: 15pt; font-weight: bold; color: {theme.TEXT_BRIGHT};"


def _label(text: str, style: str = "") -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    if style:
        label.setStyleSheet(style)
    return label


def _room_colour(pct: int | None) -> str:
    if pct is None:
        return theme.MUTED
    return theme.ACCENT if pct <= 60 else theme.WARN if pct <= 85 else theme.BAD


class LeafChoice(QPushButton):
    """A yes/no choice drawn as a card with a leaf: an outline when off, a filled green leaf when on.

    Checkable like a QCheckBox (isChecked, setChecked, toggled), with a title and a line explaining it.
    """

    HEIGHT = 64

    def __init__(self, title: str, detail: str):
        super().__init__()
        self.title, self.detail = title, detail
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName(title)
        self.setAccessibleDescription(detail)
        self.setToolTip(detail)
        # Sized in the style sheet too: the app's QPushButton rule would otherwise shrink it to a text button
        self.setStyleSheet(f"QPushButton {{ background: transparent; border: none; padding: 0;"
                           f" min-height: {self.HEIGHT}px; max-height: {self.HEIGHT}px; }}")

    def sizeHint(self) -> QSize:
        return QSize(420, self.HEIGHT)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        on, enabled = self.isChecked(), self.isEnabled()
        p.setPen(QPen(QColor(theme.ACCENT if on and enabled else theme.EDGE_STRONG), 1.2))
        p.setBrush(QColor(theme.SELECT_BG if on and enabled else theme.RAISED_HOVER if self.underMouse()
                          else theme.CARD))
        p.drawRoundedRect(r, 14, 14)
        # The leaf: tip up and to the right, a vein down the middle
        cx, cy, size = r.left() + 32, r.center().y(), 22
        leaf = QPainterPath(QPointF(cx - size * 0.5, cy + size * 0.5))
        leaf.cubicTo(QPointF(cx - size * 0.6, cy - size * 0.2), QPointF(cx + size * 0.1, cy - size * 0.6),
                     QPointF(cx + size * 0.55, cy - size * 0.55))
        leaf.cubicTo(QPointF(cx + size * 0.6, cy + size * 0.05), QPointF(cx + size * 0.1, cy + size * 0.6),
                     QPointF(cx - size * 0.5, cy + size * 0.5))
        green = QColor(theme.ACCENT if enabled else theme.MUTED)
        if on:
            p.setPen(QPen(green, 1.6))
            p.setBrush(green)
        else:
            p.setPen(QPen(QColor("#5d6b64"), 1.6))
            p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(leaf)
        vein = QColor(theme.ACCENT_TEXT if on else "#5d6b64")
        p.setPen(QPen(vein, 1.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(QPointF(cx - size * 0.5, cy + size * 0.5), QPointF(cx + size * 0.3, cy - size * 0.3))
        # Title and explanation
        text_left = r.left() + 62
        font = p.font()
        font.setBold(True)
        p.setFont(font)
        p.setPen(QColor(theme.TEXT_BRIGHT if enabled else theme.MUTED))
        p.drawText(QRectF(text_left, r.top() + 10, r.width() - 76, 20),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self.title)
        font.setBold(False)
        font.setPointSizeF(font.pointSizeF() * 0.9)
        p.setFont(font)
        p.setPen(QColor(theme.MUTED))
        p.drawText(QRectF(text_left, r.top() + 30, r.width() - 76, r.height() - 34),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap, self.detail)


class StageButton(QPushButton):
    """One Seed-to-Bloom stage: a leaf that grows with the stage, its name and its size and fps."""

    def __init__(self, index: int, name: str, step: tuple[int, int]):
        super().__init__()
        self.index, self.name, self.step = index, name, step
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumWidth(90)
        self.setAccessibleName(f"{name}: {optimize.step_text(step)}")
        self.reached = False  # at or below the chosen stage: drawn in green
        self.suggested = False
        # Sized in the style sheet too: the app's QPushButton rule would otherwise shrink it to a text button
        self.setStyleSheet("QPushButton { background: transparent; border: none; padding: 0;"
                           " min-height: 108px; max-height: 108px; }")

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        on = self.isChecked()
        if on:
            p.setPen(QPen(QColor(theme.ACCENT), 1.2))
            p.setBrush(QColor(theme.SELECT_BG))
            p.drawRoundedRect(r, 12, 12)
        elif self.underMouse():
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(theme.RAISED))
            p.drawRoundedRect(r, 12, 12)
        colour = QColor(theme.SELECT_TEXT if on else theme.ACCENT if self.reached else "#5d6b64")
        size = 18 + self.index * 5  # the leaf grows stage by stage
        cx, base = r.center().x(), r.top() + 14 + 42
        p.setPen(QPen(colour, 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawLine(QPointF(cx, base), QPointF(cx, base - size * 0.45))
        leaf = QPainterPath(QPointF(cx, base - size * 0.45))
        leaf.cubicTo(QPointF(cx, base - size * 1.0), QPointF(cx + size * 0.55, base - size * 1.1),
                     QPointF(cx + size * 0.6, base - size * 1.15))
        leaf.cubicTo(QPointF(cx + size * 0.6, base - size * 0.65), QPointF(cx + size * 0.2, base - size * 0.45),
                     QPointF(cx, base - size * 0.45))
        p.drawPath(leaf)
        if self.index:
            small = QPainterPath(QPointF(cx, base - size * 0.25))
            small.cubicTo(QPointF(cx, base - size * 0.6), QPointF(cx - size * 0.4, base - size * 0.7),
                          QPointF(cx - size * 0.45, base - size * 0.72))
            small.cubicTo(QPointF(cx - size * 0.45, base - size * 0.4), QPointF(cx - size * 0.15, base - size * 0.25),
                          QPointF(cx, base - size * 0.25))
            p.drawPath(small)
        font = p.font()
        font.setBold(True)
        p.setFont(font)
        p.setPen(colour)
        p.drawText(QRectF(r.left(), base + 4, r.width(), 18), Qt.AlignmentFlag.AlignCenter, self.name)
        font.setBold(False)
        font.setPointSizeF(font.pointSizeF() * 0.85)
        p.setFont(font)
        p.setPen(QColor(theme.MUTED))
        short, fps = self.step
        text = f"{short}p · {fps}" + ("  ★" if self.suggested else "")
        p.drawText(QRectF(r.left(), base + 22, r.width(), 16), Qt.AlignmentFlag.AlignCenter, text)


class Gauge(QWidget):
    """A half-ring that fills with how much of something a quality takes: green, yellow when tight, red."""

    def __init__(self, title: str):
        super().__init__()
        self.title, self.pct, self.caption = title, None, ""
        self.setMinimumSize(QSize(200, 136))

    def set(self, pct: int | None, caption: str) -> None:
        self.pct, self.caption = pct, caption
        self.setAccessibleName(f"{self.title}: {caption}")
        self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        radius = min(w / 2 - 14, 68)
        box = QRectF(w / 2 - radius, 10, radius * 2, radius * 2)
        pen = QPen(QColor(theme.RAISED), 12, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawArc(box, 180 * 16, -180 * 16)
        if self.pct is not None:
            pen.setColor(QColor(_room_colour(self.pct)))
            p.setPen(pen)
            p.drawArc(box, 180 * 16, int(-180 * 16 * min(self.pct, 100) / 100))
        font = p.font()
        font.setPointSizeF(font.pointSizeF() * 1.7)
        font.setBold(True)
        p.setFont(font)
        p.setPen(QColor(theme.TEXT_BRIGHT))
        big = "?" if self.pct is None else f"{self.pct}%"
        p.drawText(QRectF(0, 10 + radius - 30, w, 34), Qt.AlignmentFlag.AlignCenter, big)
        font.setPointSizeF(font.pointSizeF() / 1.7)
        font.setBold(False)
        p.setFont(font)
        p.setPen(QColor(theme.MUTED))
        p.drawText(QRectF(0, 10 + radius + 10, w, 18), Qt.AlignmentFlag.AlignCenter, self.title)
        p.drawText(QRectF(0, 10 + radius + 28, w, 18), Qt.AlignmentFlag.AlignCenter, self.caption)


def _chip(text: str, colour: str) -> QLabel:
    chip = QLabel(f"<span style='color:{colour}'>●</span>&nbsp; {text}")
    chip.setStyleSheet(f"background: {theme.PANEL}; border: 1px solid {theme.EDGE}; border-radius: 13px;"
                       " padding: 5px 12px;")
    return chip


class OptimizeWizard(QDialog):
    """Three pages: Start, Checking, Suggestions. ``chosen`` holds the ticked changes after Apply."""

    def __init__(self, collection: SceneCollection, waiting_fps: dict[str, int] | None = None,
                 first_run: bool = False, parent: QWidget | None = None, run_in_thread: bool = True):
        super().__init__(parent)
        self.setWindowTitle("Optimize for this PC")
        self.setMinimumSize(640, 600)
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
        box.addWidget(_label("What do you use SprigCast for? Tap a leaf to turn it on or off.", theme.MUTED_STYLE))
        box.addSpacing(2)
        self.wants_stream = LeafChoice(
            "Stream", "Go live on Twitch, YouTube and the rest. Picks a size, frame rate and bitrate each site "
            "takes and your upload can carry.")
        self.wants_record = LeafChoice(
            "Record and save replays", "Save videos and clips to this PC. They get more quality than a stream, "
            "since nothing is uploaded.")
        self.test_upload = LeafChoice(
            "Test my upload speed", "A hidden 8-second Twitch test stream (viewers never see it), or Cloudflare's "
            "speed test if Twitch isn't set up. Your past clean streams count too.")
        for check in (self.wants_stream, self.wants_record, self.test_upload):
            check.setChecked(True)
            box.addWidget(check)
            box.addSpacing(4)
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
        self.headline = _label("", f"color: {theme.MUTED};")
        upload = QHBoxLayout()
        self.upload_label = QLabel("Upload speed")
        upload.addWidget(self.upload_label)
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

        intro = QVBoxLayout()
        intro.setSpacing(2)
        self.grow_label = _label("", f"color: {theme.MUTED}; font-size: 9pt; letter-spacing: 1px;")
        intro.addWidget(self.grow_label)
        self.stage_title = _label("", TITLE_STYLE.replace("15pt", "20pt"))
        intro.addWidget(self.stage_title)
        intro.addWidget(self.headline)
        box.addLayout(intro)

        card = QFrame()
        card.setObjectName("qualityCard")
        card.setStyleSheet(f"#qualityCard {{ background: {theme.PANEL}; border: 1px solid {theme.EDGE};"
                           " border-radius: 12px; }")
        inner = QVBoxLayout(card)
        inner.setContentsMargins(12, 10, 12, 10)
        inner.setSpacing(4)
        row = QHBoxLayout()
        row.setSpacing(6)
        self.stages: list[StageButton] = []
        for index, (name, step) in enumerate(optimize.STAGES):
            button = StageButton(index, name, step)
            button.clicked.connect(lambda _=False, st=step: self._pick(st))
            row.addWidget(button)
            self.stages.append(button)
        inner.addLayout(row)
        ends = QHBoxLayout()
        ends.addWidget(_label("Lighter", f"color: {theme.MUTED}; font-size: 8pt;"))
        ends.addStretch(1)
        ends.addWidget(_label("Sharper", f"color: {theme.MUTED}; font-size: 8pt;"))
        inner.addLayout(ends)
        box.addWidget(card)

        gauges = QHBoxLayout()
        self.pc_gauge = Gauge("PC room used")
        self.upload_gauge = Gauge("Upload used")
        gauges.addWidget(self.pc_gauge)
        gauges.addWidget(self.upload_gauge)
        box.addLayout(gauges)

        self.chips = QHBoxLayout()
        self.chips.setSpacing(8)
        chips = QWidget()
        chips.setLayout(self.chips)
        self.chips.setContentsMargins(0, 0, 0, 0)
        box.addWidget(chips)
        box.addWidget(self.upload_row)
        self.follows = self.headline  # one line under the title says what fits this PC

        self.details_button = QPushButton("Fine-tune each setting")
        self.details_button.setCheckable(True)
        self.details_button.toggled.connect(self._toggle_details)
        details_row = QHBoxLayout()
        details_row.addWidget(self.details_button)
        details_row.addStretch(1)
        box.addLayout(details_row)
        self.change_list = QListWidget()
        self.change_list.setWordWrap(True)
        self.change_list.setSpacing(4)
        self.change_list.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self.change_list.setVisible(False)
        box.addWidget(self.change_list, 1)
        self.notes = _label("", f"{theme.NOTE_STYLE} padding: 8px 10px;")
        box.addWidget(self.notes)
        self._spacer = QWidget()
        box.addWidget(self._spacer, 1)
        return page

    def _toggle_details(self, shown: bool) -> None:
        self.change_list.setVisible(shown)
        self._spacer.setVisible(not shown)
        self.details_button.setText("Hide the details" if shown else "Fine-tune each setting")

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
        self.profile.extra["collection"] = self.collection  # the upload check streams to Twitch when a key is set
        from PySide6.QtCore import QSettings

        self.profile.extra["proven_kbps"] = optimize.load_proven(QSettings())  # what your last clean stream carried
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
        p = self.profile
        log.info("optimize: %s, %d threads, %.0f GB, %s, x264 %s ms/720p frame, upload %s kbps (%s)",
                 p.cpu_name or "CPU", p.threads, p.memory_gb, p.codec, p.x264_ms_720, p.upload_kbps,
                 p.extra.get("upload_source") or "not tested")
        if p.extra.get("proven_kbps"):
            log.info("optimize: your last clean stream carried %s kbps", p.extra["proven_kbps"])
        self.upload_row.setVisible(self.profile.wants_stream)
        self.upload.blockSignals(True)
        self.upload.setValue((self.profile.upload_kbps or 0) / 1000)
        self.upload.blockSignals(False)
        source = self.profile.extra.get("upload_source")
        self.upload_label.setText(f"Upload speed ({source} test)" if source else "Upload speed")
        self.profile.extra.pop(optimize.QUALITY, None)
        self._fill_plan()
        self.pages.setCurrentIndex(2)
        self._show_buttons()

    def _upload_changed(self, mbps: float) -> None:
        self.profile.upload_kbps = int(mbps * 1000) or None
        self._fill_plan()
        self._show_buttons()

    def _pick(self, step: tuple[int, int]) -> None:
        if self.plan is None:
            return
        if step == self.plan.recommended:
            self.profile.extra.pop(optimize.QUALITY, None)  # back on the suggestion: it follows the upload again
        else:
            self.profile.extra[optimize.QUALITY] = step
        self._fill_plan()
        self._show_buttons()

    def _fill_quality(self) -> None:
        quality = self.plan.quality
        chosen = [n for n, st in optimize.STAGES].index(optimize.STAGE_NAME[quality])
        for button in self.stages:
            button.setChecked(button.step == quality)
            button.reached = button.index <= chosen
            button.suggested = button.step == self.plan.recommended
            button.update()
        name = optimize.STAGE_NAME[quality]
        kbps = optimize.STEP_KBPS[quality]
        self.grow_label.setText("YOUR PC CAN GROW TO" if quality == self.plan.recommended else "YOU PICKED")
        self.stage_title.setText(f"{name}  <span style='font-size:13pt; font-weight:normal; color:{theme.SELECT_TEXT}'>"
                                 f"{optimize.step_text(quality)} · {kbps} kbps</span>")
        pc, upload = optimize.room(self.profile, self.collection, quality)
        self.pc_gauge.set(pc, "Graphics-card encoder" if self.profile.hardware else "CPU encoder")
        self.upload_gauge.setVisible(self.profile.wants_stream)
        usable = optimize.usable_upload(self.profile)
        proven = (usable or 0) > (self.profile.upload_kbps or 0)
        self.upload_gauge.set(upload, (f"of {usable / 1000:.1f} Mbps" + (" (your streams)" if proven else ""))
                              if upload is not None else "Type your speed below")
        while self.chips.count():
            item = self.chips.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        from sprigcast.output.stream import SERVICES

        for output, step, _ in optimize.stream_steps(self.profile, self.collection, quality):
            service = SERVICES.get(output.service or "custom", SERVICES["custom"])
            limit = step != quality
            text = f"{service.label} · {step[0]}p{step[1]}" + (", its limit" if limit else "")
            self.chips.addWidget(_chip(text, theme.WARN if limit else theme.ACCENT))
        if self.profile.wants_record:
            rec = self.plan.recording
            self.chips.addWidget(_chip(f"Recordings · {rec[0]}p{rec[1]}", theme.ACCENT))
        self.chips.addStretch(1)

    def _fill_plan(self) -> None:
        self.plan = optimize.plan(self.profile, self.collection, self.waiting_fps)
        self._fill_quality()
        light = self.profile.weak_reasons
        headline = f"Kept light for this PC: {', '.join(light)}." if light else ""
        if not self.plan.changes:
            headline += (" " if headline else "") + "Your settings already match."
        self.headline.setText(headline)
        self.headline.setVisible(bool(headline))
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
