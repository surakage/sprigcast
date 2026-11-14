"""Stats: how the app, the PC and every output are doing, refreshed once a second."""

from __future__ import annotations

import html
import shutil
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QScrollArea, QVBoxLayout, QWidget

from sprigcast.app import theme

try:
    import psutil
except ImportError:  # optional: the panel just leaves out CPU and memory
    psutil = None


def _row(label: str, value: str, warn: bool = False) -> str:
    # Qt's rich text has no "inherit": an unknown colour draws near-black, unreadable on the dark theme.
    color = theme.BAD if warn else theme.TEXT
    return (f"<tr><td style='color:{theme.MUTED}'>{html.escape(label)}</td>"
            f"<td style='padding-left:12px; color:{color}'>{value}</td></tr>")


class StatsPanel(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.label = QLabel("Waiting for the renderer…")
        self.label.setTextFormat(Qt.TextFormat.RichText)
        self.label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.label)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(scroll)
        self._process = psutil.Process() if psutil else None
        if self._process is not None:
            self._process.cpu_percent()  # first call only starts the measurement

    def refresh(self, engine_stats, outputs: list[tuple[str, object]], recordings_folder: Path) -> None:
        """engine_stats: the last Stats event (or None). outputs: (label, Recorder or Streamer)."""
        rows = []
        if engine_stats is not None:
            rows.append(_row("Render", f"{engine_stats.fps:.0f} fps, {engine_stats.render_ms:.1f} ms per tick"))
            rows.append(_row("Late render ticks", str(engine_stats.dropped_ticks), engine_stats.dropped_ticks > 0))
            rows.append(_row("GPU → CPU copies", f"{getattr(engine_stats, 'readbacks', 0):.0f} per second "
                             "(previews you can see + outputs)"))
        if self._process is not None:
            cpu = self._process.cpu_percent() / max(1, psutil.cpu_count() or 1)
            rows.append(_row("SprigCast CPU", f"{cpu:.0f}% of the whole PC"))
            rows.append(_row("SprigCast memory", f"{self._process.memory_info().rss / 1e6:.0f} MB"))
            rows.append(_row("PC CPU", f"{psutil.cpu_percent():.0f}%"))
        try:
            folder = recordings_folder if recordings_folder.exists() else recordings_folder.anchor or Path.home()
            free = shutil.disk_usage(folder).free / 1e9
            rows.append(_row("Free space for recordings", f"{free:.1f} GB", free < 10))
        except OSError:
            pass
        for label, job in outputs:
            st = job.stats()
            rows.append(f"<tr><td colspan=2 style='padding-top:8px'><b>{html.escape(label)}</b></td></tr>")
            rows.append(_row("Picture", f"{st['size']} at {st['fps']} fps"))
            rows.append(_row("Frames sent / dropped",
                             f"{st['frames']} / {st['dropped']} ({st['dropped_pct']:.1f}%)", st["dropped_pct"] > 1))
            rows.append(_row("Encoding per frame", f"{st['encode_ms']:.1f} ms of {st['budget_ms']:.1f} ms",
                             st["encode_ms"] > 0.6 * st["budget_ms"]))
            rows.append(_row("Waiting on file/network", f"{st['network_pct']:.0f}% of the time, longest {st['longest_stall_s']:.2f} s",
                             st["longest_stall_s"] > 1.0))
            rows.append(_row("Data written", f"{st['megabytes']:.0f} MB"))
            pilot = getattr(job, "autopilot", None)
            if pilot is not None:
                rows.append(_row("Auto-pilot bitrate", f"{pilot.current_kbps} of {pilot.target_kbps} kbps"
                                 + (f", lowered {pilot.steps_down}×" if pilot.steps_down else ""), pilot.lowered))
            if "backlog_s" in st and label.startswith("Stream"):
                rows.append(_row("Send buffer", f"{st['backlog_s']:.1f} s waiting to upload", st["backlog_s"] > 1.0))
        if not outputs:
            rows.append(f"<tr><td colspan=2 style='padding-top:8px; color:{theme.MUTED}'>Not streaming or recording.</td></tr>")
        self.label.setText("<table>" + "".join(rows) + "</table>")


class StatsStrip(QWidget):
    """The slim line along the bottom: render speed and what SprigCast uses. Click it (or Ctrl+Shift+S)
    for every number, in a card that opens above it and closes when you click anywhere else."""

    def __init__(self, render: QLabel, usage: QLabel, panel: StatsPanel, parent: QWidget | None = None):
        super().__init__(parent)
        from PySide6.QtWidgets import QFrame, QHBoxLayout

        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Click for all the numbers (Ctrl+Shift+S)")
        row = QHBoxLayout(self)
        row.setContentsMargins(8, 0, 4, 0)
        row.setSpacing(14)
        for label in (render, usage):
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            row.addWidget(label)
        self.more = QLabel("▴")
        self.more.setStyleSheet(f"color: {theme.MUTED};")
        row.addWidget(self.more)
        self.card = QFrame(self, Qt.WindowType.Popup)
        self.card.setObjectName("statsCard")
        self.card.setStyleSheet(f"#statsCard {{ background: {theme.PANEL}; border: 1px solid {theme.EDGE_STRONG};"
                                " border-radius: 14px; }")
        inner = QVBoxLayout(self.card)
        inner.setContentsMargins(16, 14, 16, 14)
        inner.addWidget(panel)
        scroll = panel.findChild(QScrollArea)
        if scroll is not None:  # the card is the background, no well inside it
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }")
            panel.label.setStyleSheet("background: transparent;")
        self.card.resize(420, 300)
        self.on_open = None  # set by the window: fill the numbers before the card shows

    def is_open(self) -> bool:
        return self.card.isVisible()

    def toggle(self) -> None:
        if self.card.isVisible():
            self.card.hide()
            return
        if self.on_open is not None:
            self.on_open()
        corner = self.mapToGlobal(self.rect().topRight())
        self.card.move(corner.x() - self.card.width(), corner.y() - self.card.height() - 8)
        self.card.show()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        if event.button() == Qt.MouseButton.LeftButton:
            self.toggle()
