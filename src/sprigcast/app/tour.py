"""The tour with Pip: a short walk through SprigCast that dims the window, lights up one spot at a time and says
what it's for, in Live, Build, Settings, then the Gardens, Paths, Tools and Help menus. Three stops ask the person
to do it themselves (make a bloom, add a camera, add a screen), so they finish with a basic scene of their own.

Offered once, after the first start's set-up (main_window.first_run_optimizer); never started on its own again.
Help → Take the tour with Pip starts it any time.

Light on any PC: nothing moves and nothing is drawn while a stop is showing. The shade and the card are two plain
widgets inside the window (no extra windows, no blur, no animation), drawn once per stop. The only timer runs
during a "try it" stop, twice a second, to see whether it's done. Pip's pictures are 300 px PNGs read on a worker
(empty.ArtPicture). All of it is let go when the tour ends.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QEvent, QObject, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from sprigcast.app import theme

log = logging.getLogger(__name__)

OFFERED = "tour/offered"  # QSettings: the first-start offer was made (taken or skipped)
PLAIN_S, TRY_S = 15, 45  # about how long a stop takes to read, and to do (the estimate on the welcome card)
CHECK_MS = 500  # a "try it" stop looks this often for it to be done
PAD = 8  # the lit-up spot reaches this far past the widget
CARD_W = 440
SHADE = QColor(5, 8, 7, 175)


@dataclass
class Stop:
    area: str  # Live, Build, Settings, Menus
    title: str
    text: str
    target: Callable[[Tour], tuple[QWidget, QRect] | None] | None = None  # the spot to light up, in host coords
    mode: str | None = None  # the main window's mode this stop shows (live or build)
    settings: str | None = None  # Settings open on this page; None: Settings closed
    done: Callable[[Tour], bool] | None = None  # a "try it" stop: Next waits until this is true
    pip: str = "pip-idle"


def minutes(stops: list[Stop]) -> int:
    seconds = sum(TRY_S if s.done else PLAIN_S for s in stops)
    return max(1, round(seconds / 60))


# Where each stop points. Each returns None when that part isn't showing (switched off in Settings → Features,
# say): the stop is then said in the middle of the window instead.

def _shown(widget) -> tuple[QWidget, QRect] | None:
    if widget is None or not widget.isVisible() or widget.width() <= 0:
        return None
    return widget, widget.rect()


def _attr(*path: str):
    def find(tour: Tour):
        obj = tour.window
        for name in path:
            obj = getattr(obj, name, None)
            if obj is None:
                return None
        return _shown(obj)

    return find


def _named(name: str):
    return lambda tour: _shown(tour.window.findChild(QWidget, name))


def _menu(title: str):
    """A menu's title in the menu bar (Gardens, Paths, Tools, Help)."""
    def find(tour: Tour):
        bar = tour.window.menuBar()
        for action in bar.actions():
            if action.text().replace("&", "") == title:
                return bar, bar.actionGeometry(action)
        return None

    return find


def _settings_tab(page: str):
    """A page's row in Settings' side menu."""
    def find(tour: Tour):
        settings = tour.settings_window()
        if settings is None:
            return None
        tabs = settings.tabs
        for row in range(tabs.count()):
            if tabs.item(row).text() == page:
                return tabs.viewport(), tabs.visualItemRect(tabs.item(row))
        return None

    return find


def _bloom_count(tour: Tour) -> int:
    return sum(1 for s in tour.window.collection.scenes if s.kind == "show")


def _leaf_count(tour: Tour) -> int:
    window = tour.window
    try:
        return len(window.collection.scene(window.selected_scene_id).items)
    except Exception:  # noqa: BLE001 (no bloom picked)
        return 0


def _grew(count: Callable[[Tour], int], key: str):
    """Done once `count` went up since the stop came up (a bloom made, a leaf added)."""
    def done(tour: Tour) -> bool:
        return count(tour) > tour.at_start.get(key, 0)

    done.key, done.count = key, count  # read when the stop comes up
    return done


def stops() -> list[Stop]:
    return [
        Stop("Live", "Live and Build", "SprigCast has two sides. Build is where you set up your show, and Live is where "
             "you run it. Switch any time, or press Ctrl+1 and Ctrl+2.", _named("modePills"), mode="live", pip="pip-hello"),
        Stop("Live", "What viewers see", "Your canvases, as they go out: horizontal, and vertical if you use it.",
             _attr("live_stage", "card"), mode="live"),
        Stop("Live", "Preview", "On a slower PC, right-click a canvas and pick Preview, then Light or Off for while you're live. "
             "Your stream looks the same, and your graphics card has more room for it.",
             _attr("live_stage", "card"), mode="live"),
        Stop("Live", "Go live", "Starts your stream on every place that's switched on.",
             _attr("live_side", "live"), mode="live", pip="pip-live"),
        Stop("Live", "Record", "Saves a copy on this PC. It can run with or without a stream.",
             _attr("live_side", "record"), mode="live"),
        Stop("Live", "Garden check", "How your PC and streams are doing. Green is good. Tap it for the details.",
             _attr("live_side", "garden"), mode="live", pip="pip-garden-check"),
        Stop("Live", "Blooms", "Your scenes. Tap one to put it on air.",
             _attr("live_stage", "blooms_card"), mode="live"),
        Stop("Live", "Sound", "Your mic and desktop sound. Mute or turn them down here while you're live.",
             _attr("live_side", "sound_card"), mode="live"),
        Stop("Live", "Destinations", "Where you stream. Switch a place on or off for the next Go live.",
             _attr("live_side", "where_card"), mode="live"),
        Stop("Live", "Chat", "Chat from your places, in one list.", _attr("chat_panel"), mode="live"),
        Stop("Build", "Blooms and sprigs", "Blooms are your scenes. Leaves are what's in them: your camera, screen, "
             "pictures and sound. Sprigs are groups of leaves you use in many blooms, like a camera with its frame.", _attr("library"), mode="build"),
        Stop("Build", "Make a bloom", "Try it: press + beside Blooms and pick New bloom. Next lights up once "
             "it's made.",
             _attr("library"), mode="build", done=_grew(_bloom_count, "blooms")),
        Stop("Build", "The canvas", "Drag, resize and crop what's in your bloom right here.",
             _attr("center"), mode="build"),
        Stop("Build", "Snapping", "As you drag, things snap to the canvas edges and to each other. Hold Ctrl to place "
             "freely, or right-click the canvas and pick Snapping to change it.", _attr("center"), mode="build"),
        Stop("Build", "Add your camera", "Try it: press + Add, then pick Video Capture Device. Next lights up once "
             "your camera is in.", _attr("add_button"), mode="build", done=_grew(_leaf_count, "camera")),
        Stop("Build", "Add your screen", "Try it: press + Add again and pick Display Capture for your screen, or Game "
             "Capture for a game.", _attr("add_button"), mode="build", done=_grew(_leaf_count, "screen")),
        Stop("Build", "Your phone", "+ Add has Phone Camera, your phone's camera over Wi-Fi with a code to scan, "
             "and Android Screen, an Android phone's screen over USB for mobile games.", _attr("add_button"),
             mode="build"),
        Stop("Build", "Sound", "Every sound in this bloom, with its own level, mute and settings.",
             _attr("sound_strips"), mode="build"),
        Stop("Build", "Filters", "Pick something on the canvas, then Filters adds looks like a background "
             "remover, color fixes or mic noise removal.", _attr("prop_filters"), mode="build"),
        Stop("Build", "Search", "Find any command, bloom or setting here, or press Ctrl+K.",
             _attr("palette_button"), mode="build"),
        Stop("Settings", "This PC", "Optimize for this PC picks the settings that fit your PC and your upload. Preview "
             "is here too.",
             _settings_tab("General"), settings="General/This PC"),
        Stop("Settings", "Stream", "Add each place you stream to, with its key or sign-in.",
             _settings_tab("Stream"), settings="Stream"),
        Stop("Settings", "Audio", "Pick your mic and the sounds SprigCast hears.",
             _settings_tab("Audio"), settings="Audio"),
        Stop("Settings", "Video", "Your canvas size and frame rate. 720p30 is lightest; 1080p60 needs a strong PC.",
             _settings_tab("Video"), settings="Video"),
        Stop("Menus", "Gardens", "A garden is a whole setup: blooms, sounds and places. Keep one per show or game.",
             _menu("Gardens")),
        Stop("Menus", "Paths", "A path is a set of places to stream to, like Weekdays or Events.", _menu("Paths")),
        Stop("Menus", "Tools", "Get started, the Stream doctor, Hotkeys, the Plugin store and Check for updates.", _menu("Tools")),
        Stop("Menus", "Help", "Shortcuts, help on Discord, reporting a problem, and this tour again.", _menu("Help")),
        Stop("Done", "You're all set!", "That's the tour. Press Go live when you're ready. Help → Take the tour "
             "with Pip shows it again.", pip="pip-live"),
    ]


class _Shade(QWidget):
    """The dimmed window with the spot cut out and ringed in green. Clicks go through it (try-it stops).

    Drawn once per stop into a picture: the canvases under it repaint at their frame rate, and each repaint
    repaints this too, so that's one copy of the picture, not the shape worked out again (light PCs)."""

    def __init__(self, host: QWidget):
        super().__init__(host)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.hole: QRect | None = None
        self._picture: QPixmap | None = None
        self._made_for = None

    def _make(self) -> QPixmap:
        ratio = self.devicePixelRatioF()
        key = (self.size(), self.hole, ratio)
        if self._picture is None or self._made_for != key:
            picture = QPixmap(round(self.width() * ratio), round(self.height() * ratio))
            picture.setDevicePixelRatio(ratio)
            picture.fill(Qt.GlobalColor.transparent)
            p = QPainter(picture)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            shade = QPainterPath()
            shade.addRect(QRectF(self.rect()))
            if self.hole is not None:
                hole = QPainterPath()
                hole.addRoundedRect(QRectF(self.hole), 16, 16)
                shade = shade.subtracted(hole)
            p.fillPath(shade, SHADE)
            if self.hole is not None:
                p.setPen(QPen(QColor(theme.ACCENT), 3))
                p.drawRoundedRect(QRectF(self.hole), 16, 16)
            p.end()
            self._picture, self._made_for = picture, key
        return self._picture

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        rect = event.rect()
        p.drawPixmap(rect, self._make(), self._source(rect))  # only the part that's repainted
        p.end()

    def _source(self, rect: QRect) -> QRect:
        ratio = self.devicePixelRatioF()
        return QRect(round(rect.x() * ratio), round(rect.y() * ratio), round(rect.width() * ratio),
                     round(rect.height() * ratio))


class _Progress(QWidget):
    def __init__(self):
        super().__init__()
        self.setFixedHeight(6)
        self.part = 0.0

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(theme.RAISED_HOVER))
        p.drawRoundedRect(QRectF(self.rect()), 3, 3)
        if self.part > 0:
            p.setBrush(QColor(theme.ACCENT))
            p.drawRoundedRect(QRectF(0, 0, self.width() * self.part, self.height()), 3, 3)
        p.end()


class _Card(QFrame):
    """Pip, where this is, the stop's title and words, how far along, and the buttons."""

    def __init__(self, host: QWidget, tour: Tour):
        super().__init__(host)
        from sprigcast.app.empty import ArtPicture

        self.setObjectName("tourCard")
        self.setFixedWidth(CARD_W)
        self.setStyleSheet(f"#tourCard {{ background: {theme.PANEL}; border: 1px solid {theme.EDGE_STRONG};"
                           " border-radius: 24px; }")
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 18)
        root.setSpacing(12)
        top = QHBoxLayout()
        top.setSpacing(16)
        self.pip = ArtPicture("pip-idle", 64, height=90)
        top.addWidget(self.pip, 0, Qt.AlignmentFlag.AlignTop)
        words = QVBoxLayout()
        words.setSpacing(4)
        self.where = QLabel()
        self.where.setStyleSheet(f"color: {theme.MUTED}; font-size: 12px; font-weight: 600; background: transparent;")
        self.title = QLabel()
        self.title.setStyleSheet(f"color: {theme.TEXT_BRIGHT}; font-size: 19px; font-weight: 600;"
                                 " background: transparent;")
        self.text = QLabel()
        self.text.setWordWrap(True)
        self.text.setStyleSheet(f"color: {theme.TEXT}; font-size: 14px; background: transparent;")
        for label in (self.where, self.title, self.text):
            words.addWidget(label)
        top.addLayout(words, 1)
        root.addLayout(top)
        self.progress = _Progress()
        root.addWidget(self.progress)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.end = QPushButton("End tour")
        self.end.setStyleSheet(f"QPushButton {{ background: transparent; border: none; color: {theme.MUTED};"
                               f" font-weight: 600; padding: 6px 2px; }} QPushButton:hover {{ color: {theme.TEXT}; }}")
        self.back = QPushButton("Back")
        self.skip = QPushButton("Skip")
        for quiet in (self.back, self.skip):
            quiet.setStyleSheet(theme.button_style(17, "8px 18px"))
        self.next = QPushButton("Next")
        self.next.setStyleSheet(theme.PRIMARY_STYLE + "QPushButton { border-radius: 17px; padding: 8px 22px; }"
                                f"QPushButton:disabled {{ background: transparent; color: {theme.MUTED};"
                                f" border: 1px solid {theme.EDGE_STRONG}; }}")
        for button in (self.end, self.back, self.skip, self.next):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setAutoDefault(False)
        row.addWidget(self.end)
        row.addStretch(1)
        row.addWidget(self.back)
        row.addWidget(self.skip)
        row.addWidget(self.next)
        root.addLayout(row)
        self.end.clicked.connect(tour.end)
        self.back.clicked.connect(tour.back)
        self.skip.clicked.connect(tour.forward)
        self.next.clicked.connect(tour.forward)

    def show_stop(self, stop: Stop, number: int, total: int, ready: bool) -> None:
        self.pip.set_art(stop.pip)
        last = number == total
        self.where.setText("" if last else f"{stop.area.upper()}  ·  {number} OF {total - 1}")
        self.where.setVisible(not last)
        self.title.setText(stop.title)
        self.text.setText(stop.text)
        self.progress.part = number / total
        self.progress.update()
        self.back.setVisible(number > 1 and not last)
        self.skip.setVisible(stop.done is not None and not ready)
        self.end.setVisible(not last)
        self.next.setText("Done" if last else "Next")
        self.next.setEnabled(ready)
        self.fit()

    def fit(self) -> None:
        """As tall as its words wrap to at its width: adjustSize() went by a width it didn't have, and a stop's
        last line was cut off (Your phone, 2026-11-26)."""
        self.adjustSize()
        layout = self.layout()
        if layout is not None and layout.hasHeightForWidth():
            self.resize(CARD_W, max(self.height(), layout.totalHeightForWidth(CARD_W)))


class Tour(QObject):
    """One run of the tour over `window` (the main window). start() shows the first stop; it ends itself."""

    def __init__(self, window, stop_list: list[Stop] | None = None):
        super().__init__(window)
        self.window = window
        self.stops = stop_list if stop_list is not None else stops()
        self.at = -1
        self.at_start: dict[str, int] = {}
        self.host: QWidget | None = None
        self.shade: _Shade | None = None
        self.card: _Card | None = None
        self._settings = None
        self._check = QTimer(self)
        self._check.setInterval(CHECK_MS)
        self._check.timeout.connect(self._look)

    # Moving along --------------------------------------------------------------------------------

    def start(self) -> None:
        log.info("tour: started (%d stops)", len(self.stops))
        self.at = -1
        self.forward()

    def forward(self) -> None:
        if self.at + 1 >= len(self.stops):
            self.end(finished=True)
            return
        self._go(self.at + 1)

    def back(self) -> None:
        if self.at > 0:
            self._go(self.at - 1)

    def end(self, finished: bool = False) -> None:
        if self.at < 0 and self.card is None:
            return
        log.info("tour: %s at stop %d of %d", "finished" if finished else "ended", self.at + 1, len(self.stops))
        self._check.stop()
        self._leave_settings()
        self._let_go()
        self.at = -1
        self.deleteLater()

    def _go(self, index: int) -> None:
        self.at = index
        stop = self.stops[index]
        if stop.mode is not None and getattr(self.window, "mode", None) != stop.mode:
            self.window._set_mode(stop.mode)
        if stop.settings is None:
            self._leave_settings()
            self._show(stop)
        else:
            self._in_settings(stop)

    def _show(self, stop: Stop) -> None:
        """Put the shade and card on the window this stop is about, then light up its spot."""
        host = self.settings_window() if stop.settings is not None else self.window
        if host is None:
            host = self.window
        self._host(host)
        if stop.done is not None:
            self.at_start[stop.done.key] = stop.done.count(self)
            self._check.start()
        else:
            self._check.stop()
        self.card.show_stop(stop, self.at + 1, len(self.stops), ready=stop.done is None)
        self._place()
        QTimer.singleShot(0, self, self._place)  # again once a mode switch has laid the window out
        if host is not self.window:
            QTimer.singleShot(300, self, self._place)  # Settings fills its page in just after it shows

    def _look(self) -> None:
        stop = self.stops[self.at] if 0 <= self.at < len(self.stops) else None
        if stop is None or stop.done is None or self.card is None:
            self._check.stop()
            return
        if stop.done(self):
            self._check.stop()
            log.info("tour: %s done", stop.title)
            self.card.show_stop(stop, self.at + 1, len(self.stops), ready=True)
            self._place()

    # Settings stops -------------------------------------------------------------------------------

    def settings_window(self):
        if self._settings is not None:
            try:
                if self._settings.isVisible():
                    return self._settings
            except RuntimeError:  # closed and deleted
                pass
            self._settings = None
        from PySide6.QtWidgets import QApplication

        from sprigcast.app.settings_window import SettingsWindow

        self._settings = next((w for w in QApplication.topLevelWidgets() if isinstance(w, SettingsWindow)
                               and w.isVisible() and not w.testAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)), None)
        return self._settings

    def _in_settings(self, stop: Stop) -> None:
        settings = self.settings_window()
        if settings is not None:
            settings.open_tab(stop.settings)
            self._show(stop)
            return
        # Settings waits for itself (modal): opened a tick later, and the stop shown once it's up
        self._let_go()
        QTimer.singleShot(0, self.window, lambda: self.window._open_settings(stop.settings))
        QTimer.singleShot(150, self, lambda: self._when_settings_up(stop, 20))

    def _when_settings_up(self, stop: Stop, tries: int) -> None:
        if self.at < 0 or self.stops[self.at] is not stop:
            return
        if self.settings_window() is not None:
            self._show(stop)
        elif tries > 0:
            QTimer.singleShot(150, self, lambda: self._when_settings_up(stop, tries - 1))
        else:  # it didn't open (busy, say): said over the main window
            self._show(Stop(stop.area, stop.title, stop.text, _attr("settings_button"), pip=stop.pip))

    def _leave_settings(self) -> None:
        settings = self.settings_window()
        if settings is not None:
            if self.host is settings:
                self._let_go()
            settings.reject()
            self._settings = None

    # Drawing --------------------------------------------------------------------------------------

    def _host(self, host: QWidget) -> None:
        if self.host is host and self.card is not None:
            return
        self._let_go()
        self.host = host
        host.installEventFilter(self)
        self.shade = _Shade(host)
        self.card = _Card(host, self)

    def _let_go(self) -> None:
        if self.host is not None:
            try:
                self.host.removeEventFilter(self)
            except RuntimeError:
                pass
        for widget in (self.shade, self.card):
            if widget is not None:
                try:
                    widget.hide()
                    widget.deleteLater()
                except RuntimeError:
                    pass
        self.host = self.shade = self.card = None

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if watched is self.host and event.type() in (QEvent.Type.Resize, QEvent.Type.LayoutRequest):
            QTimer.singleShot(0, self, self._place)
        return False

    def _spot(self) -> QRect | None:
        stop = self.stops[self.at]
        if stop.target is None or self.host is None:
            return None
        try:
            found = stop.target(self)
        except Exception:  # noqa: BLE001 (a part that isn't there: said in the middle instead)
            log.debug("tour: no spot for %s", stop.title, exc_info=True)
            return None
        if found is None:
            return None
        widget, rect = found
        if widget.window() is not self.host.window():
            return None
        top_left = widget.mapTo(self.host, rect.topLeft())
        spot = QRect(top_left, rect.size()).adjusted(-PAD, -PAD, PAD, PAD)
        return spot.intersected(self.host.rect().adjusted(2, 2, -2, -2))

    def _place(self) -> None:
        if self.host is None or self.card is None or self.at < 0:
            return
        area = self.host.rect()
        self.shade.setGeometry(area)
        spot = self._spot()
        self.shade.hole = spot
        self.card.fit()
        w, h = self.card.width(), self.card.height()
        if spot is None:  # in the middle
            x, y = area.center().x() - w // 2, area.center().y() - h // 2
        else:  # below the spot, else above it, else beside it; always inside the window
            gap = 16
            x = spot.center().x() - w // 2
            if spot.width() < 260 and spot.top() > 40 and spot.right() + gap + w <= area.right() - 12:
                # a small spot under the menus: beside it, so a list (Settings' pages) stays readable
                x, y = spot.right() + gap, spot.center().y() - h // 2
            elif spot.bottom() + gap + h <= area.bottom():
                y = spot.bottom() + gap
            elif spot.top() - gap - h >= area.top():
                y = spot.top() - gap - h
            else:
                y = spot.center().y() - h // 2
                x = spot.right() + gap if spot.right() + gap + w <= area.right() else spot.left() - gap - w
        x = max(area.left() + 12, min(x, area.right() - w - 12))
        y = max(area.top() + 12, min(y, area.bottom() - h - 12))
        self.card.move(x, y)
        self.shade.show()
        self.shade.raise_()
        self.card.show()
        self.card.raise_()
        self.shade.update()


def offer(window) -> None:
    """The first start's question: take the tour or skip it. Asked once, ever."""
    from PySide6.QtCore import QSettings

    settings = QSettings()
    if settings.value(OFFERED, False, type=bool):
        return
    settings.setValue(OFFERED, True)
    if ask(window):
        start(window)
    else:
        log.info("tour: skipped at the first start")


def ask(window) -> bool:
    """Pip's welcome card: True for Take the tour."""
    from sprigcast.app.card import CardDialog
    from sprigcast.app.empty import ArtPicture
    from sprigcast.app.modal import modal

    every = stops()
    dialog = CardDialog("Hi, I'm Pip!", window, width=500)
    dialog.heading.setAlignment(Qt.AlignmentFlag.AlignHCenter)
    dialog.body.insertWidget(0, ArtPicture("pip-hello", 104, height=150), 0, Qt.AlignmentFlag.AlignHCenter)
    words = QLabel("Want a quick tour? I'll show you around and help you make your first bloom.")
    words.setWordWrap(True)
    words.setAlignment(Qt.AlignmentFlag.AlignHCenter)
    words.setStyleSheet(f"color: {theme.TEXT}; font-size: 15px; background: transparent;")
    dialog.body.addWidget(words)
    how_long = QLabel(f"About {minutes(every)} minutes  ·  {len(every) - 1} stops")
    how_long.setAlignment(Qt.AlignmentFlag.AlignHCenter)
    how_long.setStyleSheet(f"color: {theme.MUTED}; font-weight: 600; background: transparent;")
    dialog.body.addWidget(how_long)
    take = dialog.add_buttons("Take the tour", cancel="Skip for now")
    take.setFocus()
    from PySide6.QtWidgets import QDialog

    return modal(dialog) == QDialog.DialogCode.Accepted


def start(window) -> Tour | None:
    """Start the tour (Help → Take the tour with Pip, or the first start's offer). One at a time."""
    running = getattr(window, "_tour", None)
    if running is not None and running.at >= 0:
        return running
    if getattr(window, "streaming", None) is not None or getattr(window, "recording", None) is not None:
        from sprigcast.app import msgbox

        msgbox.information(window, "Take the tour", "Stop streaming and recording first.")
        return None
    tour = Tour(window)
    window._tour = tour
    tour.start()
    return tour
