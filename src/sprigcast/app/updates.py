"""Check for updates: the window side of sprigcast/update.py.

- At start (after a short wait) and every 12 hours (every 3 hours on a test build), if Settings → General →
  Updates allows it.
- A test build also hears about newer test builds ("Beta 0.5 test build 3 is ready"), so testers update in place.
  The channel (stable, test builds, or both) is kept in updates/channel; the choice shows in Settings from
  Version 1 on, until then a test build uses both and a release copy stable.
- Tools → Check for updates… and the Check now button in Settings ask right away.
- A rounded pop-up shows the new version and what's new. Update now downloads and checks the installer,
  closes SprigCast and lets Setup put the new version in and start it again.
- Never while live or recording: an update found then waits until both are off, and Update now becomes
  "When I close", which installs as SprigCast quits.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QObject, QSettings, QStandardPaths, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from sprigcast import __version__, short_version, test_build, update
from sprigcast.app.modal import modal
from sprigcast.app import theme

log = logging.getLogger(__name__)
PROGRESS_EVERY_S = 0.25  # download progress shown at most this often

AUTO_CHECK = "updates/auto_check"
SKIPPED = "updates/skipped"
LAST_CHECK = "updates/last_check"
CHANNEL = "updates/channel"
CHECK_EVERY_S = 12 * 3600
TEST_CHECK_EVERY_S = 3 * 3600  # test builds come often: testers hear within a few hours
FIRST_CHECK_MS = 20_000  # after start, once the window and sources have settled
BUSY_RETRY_MS = 60_000


def auto_check_enabled() -> bool:
    return QSettings().value(AUTO_CHECK, True, type=bool)


def channel() -> str:
    """The saved update channel when it's one we know, else the build's own default."""
    saved = QSettings().value(CHANNEL, "", type=str)
    return saved if saved in update.CHANNELS else update.default_channel()


CHANNEL_NAMES = (("Stable versions", update.STABLE), ("Test builds", update.TESTS),
                 ("Stable versions and test builds", update.BOTH))


def channel_choice_shown() -> bool:
    """The channel choice joins Settings → Updates with Version 1 (user, 2026-11-27)."""
    parsed = update.parse_version(__version__)
    return parsed is not None and parsed[0] >= 1


def check_every_s() -> int:
    return TEST_CHECK_EVERY_S if update.is_test_copy() else CHECK_EVERY_S


def this_build() -> str:
    """"Beta 0.5 test build 2" or "Beta 0.5"."""
    return short_version(__version__, test_build())


def downloads_dir() -> Path:
    base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation)
    return Path(base or Path.home()) / "Updates"


def _mb(n: int) -> str:
    return f"{n / 1_000_000:.0f} MB"


NOTES_CSS = (  # the changelog's headings in the app's mint, lists with room to breathe
    f"h1, h2 {{ color: {theme.TEXT_BRIGHT}; font-size: 18px; font-weight: 700; margin: 10px 0 6px 0; }}"
    f"h3, h4 {{ color: {theme.ACCENT}; font-size: 15px; font-weight: 700; margin: 12px 0 4px 0; }}"
    f"p, li {{ color: {theme.TEXT}; margin: 2px 0; }}"
    f"a {{ color: {theme.ACCENT}; }}"
)


def notes_html(markdown: str) -> str:
    """The release notes (Markdown from the changelog) as HTML that NOTES_CSS can colour."""
    from PySide6.QtGui import QTextDocument

    doc = QTextDocument()
    doc.setMarkdown(markdown)
    return doc.toHtml()


class UpdatePrompt(QWidget):
    """What the pop-up shows: the sloth, the new version, what's new (scrolls), progress and the buttons.

    Sized for what it holds: the first version sat in the pop-up's smallest size, squashed so the notes and the
    buttons overlapped and the sloth didn't show (2026-11-22)."""

    WIDTH, NOTES_HEIGHT = 600, 320

    def __init__(self, release: update.Release, busy: bool, installed: bool, parent: QWidget | None = None):
        super().__init__(parent)
        from sprigcast.app.empty import ArtPicture

        self.release = release
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 4)
        layout.setSpacing(16)

        top = QHBoxLayout()
        top.setSpacing(18)
        top.addWidget(ArtPicture("happy", 96), 0, Qt.AlignmentFlag.AlignVCenter)
        words = QVBoxLayout()
        words.setSpacing(4)
        words.addStretch(1)
        title = QLabel(f"SprigCast {release.label} is ready")
        title.setObjectName("updateTitle")
        title.setWordWrap(True)
        title.setStyleSheet(f"color: {theme.TEXT_BRIGHT}; font-size: 20px; font-weight: 700;")
        words.addWidget(title)
        size = f" · {_mb(release.installer_size)} download" if release.installer_size else ""
        sub = QLabel(f"You have {this_build()}{size}")
        sub.setStyleSheet(theme.MUTED_STYLE)
        words.addWidget(sub)
        words.addStretch(1)
        top.addLayout(words, 1)
        layout.addLayout(top)

        self.notes = QTextBrowser()
        self.notes.setObjectName("updateNotes")
        self.notes.setOpenExternalLinks(True)
        self.notes.document().setDefaultStyleSheet(NOTES_CSS)
        self.notes.document().setDocumentMargin(14)
        self.notes.document().setIndentWidth(18)  # lists sit close to their heading
        self.notes.setHtml(notes_html(release.notes or "See the release page for what's new."))
        self.notes.setStyleSheet(f"QTextBrowser {{ background: {theme.SECTION}; color: {theme.TEXT};"
                                 f" border: 1px solid {theme.SECTION_EDGE}; border-radius: 16px; padding: 4px; }}")
        self.notes.setFixedHeight(self.NOTES_HEIGHT)
        layout.addWidget(self.notes)

        # Download progress gets its own row between the notes and the buttons, its room kept from the start: shown
        # later into a pop-up already sized, it was drawn over the notes' bottom edge (PC, builds 2 to 7, 2026-11-27).
        progress = QVBoxLayout()
        progress.setContentsMargins(6, 0, 6, 0)
        progress.setSpacing(8)
        self.status = QLabel()
        self.status.setObjectName("updateStatus")
        self.status.setWordWrap(True)
        self.status.setStyleSheet(theme.MUTED_STYLE)
        progress.addWidget(self.status)
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(8)
        self.bar.setStyleSheet(f"QProgressBar {{ background: {theme.TRACK}; border: none; border-radius: 4px; }}"
                               f"QProgressBar::chunk {{ background: {theme.ACCENT}; border-radius: 4px; }}")
        progress.addWidget(self.bar)
        for part in (self.status, self.bar):
            keep = part.sizePolicy()
            keep.setRetainSizeWhenHidden(True)
            part.setSizePolicy(keep)
            part.hide()
        self.status.setText(" ")  # one line's room, before there's anything to say
        layout.addLayout(progress)

        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        self.skip_button = QPushButton("Skip this version")
        self.skip_button.setStyleSheet(theme.LINK_STYLE)
        buttons.addWidget(self.skip_button)
        buttons.addStretch(1)
        self.later_button = QPushButton("Later")
        self.later_button.setStyleSheet(theme.PILL_STYLE)
        buttons.addWidget(self.later_button)
        self.go_button = QPushButton()
        self.go_button.setObjectName("updateGo")
        self.go_button.setStyleSheet(theme.PRIMARY_STYLE)
        self.go_button.setDefault(True)
        buttons.addWidget(self.go_button)
        for button in (self.skip_button, self.later_button, self.go_button):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setAutoDefault(False)
        self.set_mode(busy, installed)
        tall = max(self.later_button.sizeHint().height(), self.go_button.sizeHint().height())
        for button in (self.later_button, self.go_button):  # side by side, one height
            button.setFixedHeight(tall)
        layout.addLayout(buttons)
        self.setMinimumWidth(self.WIDTH)
        self.setMinimumHeight(layout.totalMinimumSize().height())

    def set_mode(self, busy: bool, installed: bool) -> None:
        if not installed:
            self.go_button.setText("Open download page")
        elif busy:
            self.go_button.setText("When I close")
            self.go_button.setToolTip("You're live or recording: it installs once you close SprigCast.")
        else:
            self.go_button.setText("Update now")
            self.go_button.setToolTip("")

    def show_status(self, text: str, progress: tuple[int, int] | None = None) -> None:
        self.status.setText(text)
        self.status.show()
        self._room_for_status()
        if progress is None:
            self.bar.hide()
            return
        done, total = progress
        self.bar.show()
        if total:
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(1000 * done / total))
        else:
            self.bar.setRange(0, 0)  # size unknown: a moving bar

    def _room_for_status(self) -> None:
        """A message longer than its one kept line (a failed download says why): the pop-up grows to hold it,
        rather than the row squeezing into the notes."""
        layout = self.layout()
        layout.activate()
        need = layout.totalMinimumSize().height()
        self.setMinimumHeight(need)
        short = need - self.height()
        window = self.window()
        if short > 0 and window is not self:
            window.setMinimumHeight(window.minimumHeight() + short)
            window.resize(window.width(), window.height() + short)

    def working(self, on: bool) -> None:
        for button in (self.skip_button, self.go_button):
            button.setEnabled(not on)
        if on:  # the main button says what's happening (it's greyed out meanwhile)
            self._go_text = self.go_button.text()
            self.go_button.setText("Downloading…")
        elif getattr(self, "_go_text", None):
            self.go_button.setText(self._go_text)


def fit_to(dialog: QWidget, page: QWidget) -> None:
    """Grow the pop-up to hold its page (PopUp's own smallest size, 360x260, would squash it)."""
    layout = dialog.layout()
    if layout is not None:
        layout.activate()
    want = dialog.sizeHint().expandedTo(page.minimumSizeHint())
    extra_w = dialog.width() - page.width() if page.width() else 80
    extra_h = dialog.height() - page.height() if page.height() else 140
    width = max(want.width(), page.minimumWidth() + extra_w)
    height = max(want.height(), page.minimumHeight() + extra_h)
    dialog.resize(width, height)
    dialog.setMinimumSize(width, height)


class Updater(QObject):
    """Checks, downloads and hands over to Setup. One per main window."""

    checked = Signal(object, bool, str)  # Release or None, manual, error text ("" when it worked)
    progressed = Signal(int, int)
    downloaded = Signal(str, str)  # installer path, problem ("" when it's safe to run)

    def __init__(self, window: QWidget, busy: Callable[[], bool], installed: Path | None | bool = None):
        super().__init__(window)
        self.window = window
        self.busy = busy
        self.installed = (update.installed_dir() is not None) if installed is None else bool(installed)
        self.pending: Path | None = None  # a checked installer that runs when SprigCast closes
        self.restart = False
        self._checking = False
        self._cancel = False
        self._dialog = None
        self._prompt: UpdatePrompt | None = None
        self._waiting: update.Release | None = None  # found while live: shown once you're off air
        self.checked.connect(self._on_checked)
        self.progressed.connect(self._on_progress)
        self.downloaded.connect(self._on_downloaded)
        self._timer = QTimer(self)
        self._timer.timeout.connect(lambda: self.check(manual=False))
        self._busy_timer = QTimer(self, interval=BUSY_RETRY_MS)
        self._busy_timer.timeout.connect(self._show_waiting)

    def start(self) -> None:
        """Automatic checks: one shortly after start, then every 12 hours (3 on a test build). Installed copies only."""
        if not self.installed:
            return
        report = update.helper_report(downloads_dir())
        if report:  # how the last update went, in this run's log (the helper can't write ours)
            log.info("update: last update's helper said:\n%s", report)
        QTimer.singleShot(FIRST_CHECK_MS, lambda: self.check(manual=False))
        self._timer.start(check_every_s() * 1000)

    # Checking --------------------------------------------------------------------

    def check(self, manual: bool = True) -> None:
        if self._checking or self._dialog is not None:
            if manual and self._dialog is not None:
                self._dialog.raise_()
            return
        settings = QSettings()
        if not manual:
            if not auto_check_enabled() or self.pending is not None:
                return
            last = settings.value(LAST_CHECK, 0.0, type=float)
            if time.time() - last < check_every_s() - 60:
                return
        self._checking = True
        settings.setValue(LAST_CHECK, time.time())
        if manual and hasattr(self.window, "statusBar"):
            self.window.statusBar().showMessage("Checking for updates…", 4000)
        threading.Thread(target=self._check_worker, args=(manual,), name="update-check", daemon=True).start()

    def _check_worker(self, manual: bool) -> None:
        try:
            release = update.fetch_newest(channel())  # QSettings is safe to read here (its own instance)
        except Exception as exc:  # noqa: BLE001 (offline, GitHub down, odd reply: never crash over it)
            log.info("update check failed: %s", exc)
            self.checked.emit(None, manual, update.trouble(exc))
            return
        self.checked.emit(release, manual, "")

    def _on_checked(self, release: update.Release | None, manual: bool, error: str) -> None:
        self._checking = False
        if error:
            if manual:
                self._message("Couldn't check for updates", error)
            return
        if not update.newer_than_this(release):
            log.info("update check: %s is the newest (channel %s)", this_build(), channel())
            if manual:
                self._message("You're up to date", f"SprigCast {this_build()} is the newest version.")
            return
        log.info("update check: %s is out (you have %s)", release.label, this_build())
        if not manual and QSettings().value(SKIPPED, "", type=str) == release.skip_id:
            return
        if not manual and self.busy():
            self._waiting = release  # never pop up on air: wait until stream and recording are off
            self._busy_timer.start()
            return
        self.show_prompt(release)

    def _show_waiting(self) -> None:
        if self._waiting is None or self.busy() or self._dialog is not None:
            return
        release, self._waiting = self._waiting, None
        self._busy_timer.stop()
        self.show_prompt(release)

    # The pop-up ------------------------------------------------------------------

    def _popup(self, title: str):
        from sprigcast.app.transitions_panel import PopUp

        dialog = PopUp(title, self.window)
        dialog.done_button.hide()
        return dialog

    def _message(self, title: str, text: str) -> None:
        from sprigcast.app.card import section
        from sprigcast.app.empty import ArtPicture

        dialog = self._popup("Updates")
        page, inside = section(title)  # its own card, the brand look (user 2026-11-27: on brand, less gap)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(14)
        row.addWidget(ArtPicture("happy" if "up to date" in title else "trouble", 72),
                      0, Qt.AlignmentFlag.AlignTop)
        line = QLabel(text)
        line.setObjectName("updateMessage")
        line.setWordWrap(True)
        line.setStyleSheet(theme.MUTED_STYLE)
        row.addWidget(line, 1, Qt.AlignmentFlag.AlignVCenter)
        inside.addLayout(row)
        page.setMinimumWidth(400)
        dialog.add_tab("updates", "Updates", page)
        dialog.done_button.show()
        dialog.done_button.setText("OK")
        self._dialog = dialog
        try:
            modal(dialog)
        finally:
            self._dialog = None

    def show_prompt(self, release: update.Release) -> None:
        dialog = self._popup("Update")
        prompt = UpdatePrompt(release, self.busy(), self.installed)
        dialog.add_tab("update", "Update", prompt)
        fit_to(dialog, prompt)
        prompt.later_button.clicked.connect(dialog.reject)
        prompt.skip_button.clicked.connect(lambda: (QSettings().setValue(SKIPPED, release.skip_id), dialog.reject()))
        prompt.go_button.clicked.connect(lambda: self._go(release))
        dialog.finished.connect(lambda _code: setattr(self, "_cancel", self._downloading()))
        self._dialog, self._prompt = dialog, prompt
        try:
            modal(dialog)
        finally:
            self._dialog = self._prompt = None

    def _downloading(self) -> bool:
        return getattr(self, "_download_thread", None) is not None and self._download_thread.is_alive()

    def _go(self, release: update.Release) -> None:
        if not self.installed:
            QDesktopServices.openUrl(QUrl(release.page_url))
            if self._dialog is not None:
                self._dialog.accept()
            return
        self.restart = not self.busy()
        self._cancel = False
        if self._prompt is not None:
            self._prompt.working(True)
            self._prompt.show_status("Downloading…", (0, release.installer_size))
        self._download_thread = threading.Thread(target=self._download_worker, args=(release,),
                                                 name="update-download", daemon=True)
        self._download_thread.start()

    def _download_worker(self, release: update.Release) -> None:
        folder = downloads_dir()
        target = folder / release.installer_name
        try:
            update.clear_old(folder)
            expected = update.fetch_hash(release)
            actual = update.download(release.installer_url, target,
                                     progress=lambda done, total: self.progressed.emit(done, total or
                                                                                         release.installer_size),
                                     cancelled=lambda: self._cancel)
            problem = update.verify(target, actual, expected) or ""
        except InterruptedError:
            return
        except Exception as exc:  # noqa: BLE001
            log.warning("update download failed: %s", exc)
            problem = update.trouble(exc, fallback="The download didn't finish.")
        if problem:
            target.unlink(missing_ok=True)
        self.downloaded.emit(str(target), problem)

    def _on_progress(self, done: int, total: int) -> None:
        # At most PROGRESS_EVERY_S apart: each one laid the pop-up out again, and a stream of them held the
        # window 1,545 ms on a laptop (CRGPlays, 2026-11-27).
        now = time.monotonic()
        if (total and done < total) and now - getattr(self, "_progress_at", float("-inf")) < PROGRESS_EVERY_S:
            return
        self._progress_at = now
        if self._prompt is not None:
            text = f"Downloading… {_mb(done)} of {_mb(total)}" if total else f"Downloading… {_mb(done)}"
            self._prompt.show_status(text, (done, total))

    def _on_downloaded(self, path: str, problem: str) -> None:
        if problem:
            log.warning("update not installed: %s", problem)
            if self._prompt is not None:
                self._prompt.working(False)
                self._prompt.show_status(problem)
            return
        self.pending = Path(path)
        log.info("update: %s downloaded and checked", self.pending.name)
        if self.restart and not self.busy():
            if self._prompt is not None:
                self._prompt.show_status("Restarting to update…")
                self._prompt.go_button.setText("Restarting…")
            QTimer.singleShot(600, self._restart_now)
            return
        self.restart = False  # went live while it downloaded: install on quit instead
        if self._prompt is not None:
            self._prompt.show_status("Ready. It installs when you close SprigCast.")
            self._prompt.go_button.setText("OK")
            self._prompt.go_button.setEnabled(True)
            self._prompt.go_button.clicked.disconnect()
            self._prompt.go_button.clicked.connect(self._dialog.accept)
        elif hasattr(self.window, "statusBar"):
            self.window.statusBar().showMessage("Update ready: it installs when you close SprigCast.", 15000)

    def _restart_now(self) -> None:
        if self.busy():  # went live in the last moment: never cut a stream short
            self.restart = False
            if self._prompt is not None:
                self._prompt.show_status("Ready. It installs when you close SprigCast.")
            return
        if self._dialog is not None:
            self._dialog.accept()
        self.window.close()  # closeEvent calls install_on_quit

    def install_on_quit(self) -> bool:
        """Called as SprigCast closes: hand a downloaded, checked installer to Setup. True if one was."""
        if self.pending is None or not self.pending.exists():
            return False
        try:
            update.launch_installer(self.pending, os.getpid(), self.restart)
        except OSError as exc:
            log.warning("could not start the update: %s", exc)
            return False
        self.pending = None
        return True
