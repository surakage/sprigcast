"""Sending a problem report: the card after a crash, and Help → Report a problem.

The tester writes what they were doing; with "Include the log and PC details" ticked the report is the
File → Export report zip (app/tester_report.py: logs, stream reports, crash.log, this PC and settings,
everything private hidden) plus their note. Unticked, it's only their note, the version, Windows' build and
what crashed. Nothing goes anywhere until they press Send report.

It goes to the report inbox on slothbloom.com (integrations/report-inbox: a Cloudflare Worker that keeps
reports 30 days and answers with a number like SC-7F3K2M). If that can't be reached, the zip is saved to the
Desktop instead so it can be sent by hand.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QWidget,
)

from sprigcast.app.modal import modal
from sprigcast.app.open_folder import open_folder
from sprigcast.app import theme, tester_report
from sprigcast.app.card import CardDialog, field_label

log = logging.getLogger(__name__)

REPORT_URL = "https://reports.slothbloom.com/v1/report"
TIMEOUT_S = 30
NOTE_MAX = 4000


def report_url() -> str:
    return os.environ.get("SPRIGCAST_REPORT_URL", "").strip() or REPORT_URL


def crash_line(crash) -> str:
    """'Access violation in nvcuda64.dll · 21:42' for the top of the crash card."""
    when = ""
    try:
        started = datetime.strptime(crash.started, "%Y-%m-%d %H:%M:%S")
        when = f" · started {started:%H:%M}" if started.date() == datetime.now().date() else \
            f" · started {started:%d %b %H:%M}"
    except (TypeError, ValueError):
        pass
    return crash.summary() + when


def _summary(kind: str, crash) -> str:
    from sprigcast.app.logbook import _version

    lines = [f"SprigCast {_version()}", f"Windows: {tester_report._windows()}", f"Report: {kind}"]
    if crash is not None:
        lines.append(f"Crash: {crash.summary()}" + (f" (at {crash.where})" if crash.where else ""))
        if crash.started:
            lines.append(f"That run started {crash.started}")
    return "\n".join(lines) + "\n"


def make(target: Path, note: str, include: bool, crash=None, window=None, scrub=None) -> Path:
    """Write the report zip: the note and what crashed, and with `include` the whole export report."""
    kind = "crash" if crash is not None else "problem"
    if include:
        collection = getattr(window, "collection", None)
        settings = None
        try:
            from PySide6.QtCore import QSettings

            settings = QSettings()
        except Exception:
            pass
        if scrub is None:
            scrub = tester_report.Scrubber.for_this_pc(tester_report.known_secrets(settings, collection))
        tester_report.build(target, collection=collection, settings=settings,
                            plugins_dir=getattr(window, "plugins_dir", None), scrub=scrub)
    else:
        scrub = scrub or tester_report.Scrubber.for_this_pc()
        target.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED):
            pass
    with zipfile.ZipFile(target, "a", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("summary.txt", scrub(_summary(kind, crash)))
        if note.strip():
            z.writestr("note.txt", scrub(note.strip()[:NOTE_MAX]) + "\n")
    return target


def send(path: Path, kind: str, cause: str = "", url: str | None = None, opener=urllib.request.urlopen) -> str:
    """Upload the zip; returns the report number. Raises OSError when it can't be sent."""
    from sprigcast.app.logbook import _version

    data = path.read_bytes()
    request = urllib.request.Request(url or report_url(), data=data, method="POST", headers={
        "Content-Type": "application/zip",
        "User-Agent": f"SprigCast/{_version()}",
        "X-SprigCast-Version": _version(),
        "X-SprigCast-Kind": kind,
        "X-SprigCast-Cause": tester_report.Scrubber.for_this_pc()(cause)
        .encode("ascii", "ignore").decode()[:160],
    })
    try:
        with opener(request, timeout=TIMEOUT_S) as response:
            answer = json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        raise OSError(f"the report inbox said {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise OSError(str(getattr(exc, "reason", exc))) from exc
    report_id = str(answer.get("id", ""))
    if not report_id:
        raise OSError("the report inbox gave no number")
    return report_id


# Cards ---------------------------------------------------------------------------------------------------


def _label(text: str, colour: str = theme.TEXT) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet(f"color: {colour}; background: transparent;")
    return label


def _link(text: str) -> QPushButton:
    button = QPushButton(text)
    button.setStyleSheet(theme.LINK_STYLE)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    return button


class ReportCard(CardDialog):
    """The crash card (with `crash`) or Help → Report a problem: a note, the tick box, Send report."""

    def __init__(self, parent: QWidget | None = None, crash=None):
        super().__init__("SprigCast closed unexpectedly" if crash is not None else "Report a problem", parent,
                         width=480)
        self.crash = crash
        if crash is not None:
            self.body.addWidget(_label(crash_line(crash), theme.MUTED))
        self.body.addWidget(field_label("What were you doing?" if crash is not None else "What happened?"))
        self.note = QPlainTextEdit()
        self.note.setPlaceholderText("Optional" if crash is not None else "")
        self.note.setFixedHeight(96)
        self.body.addWidget(self.note)
        row = QHBoxLayout()
        row.setSpacing(6)
        self.include = QCheckBox("Include the log and PC details")
        self.include.setChecked(True)
        row.addWidget(self.include)
        row.addStretch(1)
        self.see = _link("See what's sent")
        self.see.clicked.connect(self._show_contents)
        row.addWidget(self.see)
        self.body.addLayout(row)
        self.send_button = self.add_buttons("Send report", "Not now" if crash is not None else "Cancel")
        self.note.setFocus()

    def kind(self) -> str:
        return "crash" if self.crash is not None else "problem"

    def build(self, target: Path) -> Path:
        """The report as it would be sent, made on a worker while the window keeps answering."""
        from sprigcast.core import workers

        return workers.wait_answering(make, target, self.note.toPlainText(), self.include.isChecked(),
                                      self.crash, self.parent())

    def _show_contents(self) -> None:
        folder = Path(tempfile.mkdtemp(prefix="sprigcast-report-"))
        try:
            path = self.build(folder / tester_report.default_name())
        except OSError as exc:
            log.warning("report: couldn't build it to show: %s", exc)
            return
        modal(ContentsCard(path, self))


def contents(path: Path) -> list[tuple[str, str]]:
    """(name, what) for each file in the report, in plain words, your note first."""
    rows = []
    with zipfile.ZipFile(path) as z:
        for info in z.infolist():
            name, size = info.filename, f"{max(1, round(info.file_size / 1024))} KB"
            if name == "note.txt":
                rows.insert(0, ("Your note", f"{info.file_size} characters"))
            elif name == "summary.txt":
                rows.append(("summary.txt", "Version, Windows build, what crashed"))
            elif name == "about.txt":
                rows.append(("about.txt", "PC, GPU, settings"))
            elif name == "logs/crash.log":
                rows.append(("crash.log", f"Last runs, {size}"))
            elif name.startswith("logs/sprigcast "):
                rows.append(("Log", f"{name.removeprefix('logs/sprigcast ').removesuffix('.log')}, {size}"))
            elif name.startswith("logs/stream "):
                rows.append(("Stream report", f"{name.removeprefix('logs/stream ').removesuffix('.txt')}, {size}"))
            else:
                rows.append((name, size))
    return rows


class ContentsCard(CardDialog):
    """What's sent: each file, what's hidden, Open the files and Save copy…."""

    def __init__(self, path: Path, parent: QWidget | None = None):
        super().__init__("What's sent", parent, width=480)
        self.path = path
        for name, what in contents(path):
            row = QHBoxLayout()
            left = _label(name)
            left.setFixedWidth(130)
            row.addWidget(left)
            row.addWidget(_label(what, theme.MUTED), 1)
            self.body.addLayout(row)
        hidden = QLabel("Stream keys, passwords, tokens, your name, PC name, email and IP are removed. "
                        "Chat isn't logged.")
        hidden.setWordWrap(True)
        hidden.setStyleSheet(theme.NOTE_STYLE + " padding: 10px 12px;")
        self.body.addWidget(hidden)
        row = QHBoxLayout()
        opener = _link("Open the files")
        opener.clicked.connect(self._open_files)
        row.addWidget(opener)
        row.addStretch(1)
        self.body.addLayout(row)
        back = self.add_buttons("Back", "Save copy…")
        back.setFocus()
        for button in self.findChildren(QPushButton):
            if button.text() == "Save copy…":
                button.clicked.disconnect()
                button.clicked.connect(self._save_copy)

    def _open_files(self) -> None:
        folder = self.path.with_suffix("")
        with zipfile.ZipFile(self.path) as z:
            z.extractall(folder)
        open_folder(folder)

    def _save_copy(self) -> None:
        picked, _ = QFileDialog.getSaveFileName(self, "Save copy", str(tester_report.default_folder() /
                                                                        self.path.name), "Zip files (*.zip)")
        if picked:
            import shutil

            shutil.copyfile(self.path, picked)


def sent_card(parent: QWidget | None, report_id: str) -> CardDialog:
    card = CardDialog("Report sent", parent, width=480)
    card.body.addWidget(_label(f"Thanks. Report {report_id}."))
    card.add_buttons("OK", "Copy number")
    for button in card.findChildren(QPushButton):
        if button.text() == "Copy number":
            button.clicked.disconnect()
            button.clicked.connect(lambda: QGuiApplication.clipboard().setText(report_id))
    return card


def saved_card(parent: QWidget | None, path: Path, reveal) -> CardDialog:
    card = CardDialog("Couldn't send", parent, width=480)
    card.body.addWidget(_label("Saved to your Desktop instead:"))
    card.body.addWidget(_label(path.name, theme.MUTED))
    show = card.add_buttons("Show file", "Close")
    show.clicked.connect(lambda: reveal(path))
    return card


# The flow ------------------------------------------------------------------------------------------------


class _Sent(QObject):
    """Carries the upload's result from its thread back to the window."""

    done = Signal(str, str)  # report number ("" when it failed), why it failed


def _reveal(path: Path) -> None:
    if sys.platform == "win32" and path.exists():
        import subprocess

        subprocess.Popen(["explorer", "/select,", str(path)])
        return
    open_folder(path.parent)


def _pop(card: CardDialog) -> CardDialog:
    """Show a card without holding the window (no .exec()): the canvases, the stream and clicks elsewhere go
    on. It's deleted once closed."""
    card.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    card.show()
    return card


def ask(window, crash=None, *, sender=send, show_result: bool = True) -> ReportCard:
    """Show the card, without holding the window: the crash card after a crash froze it 404 and 660 ms on
    the PC while it was run with .exec() (2026-11-18), and read as a new crash. On Send report the zip is built
    and uploaded on a thread of its own (card.upload, which tests join), then a card says how it went."""
    card = ReportCard(window, crash)
    card.upload = None

    def answered(result: int) -> None:
        if not result:
            log.info("report: %s card closed without sending", card.kind())
            return
        card.upload = send_report(window, card.note.toPlainText(), card.include.isChecked(), crash,
                                  sender=sender, show_result=show_result)

    card.finished.connect(answered)
    return _pop(card)


def send_report(window, note: str, include: bool, crash=None, *, sender=send,
                show_result: bool = True) -> threading.Thread:
    """Build the report and send it, both on a thread of its own; then say how it went."""
    from types import SimpleNamespace

    kind, cause = ("crash" if crash is not None else "problem"), crash.summary() if crash is not None else ""
    # The thread reads a copy: the window may change the garden meanwhile.
    collection = getattr(window, "collection", None)
    source = SimpleNamespace(collection=collection.model_copy(deep=True) if collection is not None else None,
                             plugins_dir=getattr(window, "plugins_dir", None))
    status = getattr(window, "statusBar", None)
    if status is not None:
        status().showMessage("Sending report…")
    carrier = _Sent(window)
    made: dict = {}

    def finished(report_id: str, why: str) -> None:
        carrier.deleteLater()
        if status is not None:
            status().clearMessage()
        if report_id:
            log.info("report: %s report sent as %s", kind, report_id)
            if show_result:
                _pop(sent_card(window, report_id))
            return
        path = made.get("path")
        if path is None:
            return  # couldn't even be made (logged)
        log.warning("report: couldn't send (%s); saving it to the Desktop", why)
        saved = tester_report.default_folder() / path.name
        try:
            saved.parent.mkdir(parents=True, exist_ok=True)
            import shutil

            shutil.copyfile(path, saved)
        except OSError as exc:
            log.warning("report: couldn't save it either: %s", exc)
            saved = path
        if show_result:
            _pop(saved_card(window, saved, _reveal))

    carrier.done.connect(finished, Qt.ConnectionType.QueuedConnection)

    def upload() -> None:
        folder = Path(tempfile.mkdtemp(prefix="sprigcast-report-"))
        try:
            path = made["path"] = make(folder / tester_report.default_name(), note, include, crash, source)
        except OSError as exc:
            log.warning("report: couldn't make it: %s", exc)
            carrier.done.emit("", str(exc))
            return
        try:
            report_id, why = sender(path, kind, cause), ""
        except OSError as exc:
            report_id, why = "", str(exc) or exc.__class__.__name__
        carrier.done.emit(report_id, why)

    thread = threading.Thread(target=upload, name="report-upload", daemon=True)
    thread.start()
    return thread
