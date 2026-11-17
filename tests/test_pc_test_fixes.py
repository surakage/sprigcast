"""Fixes from the hand test on the PC (2026-11-15): the wheel, + Add → Plugins, the header clock, crash
notes, one copy at a time and the last stream's health on Live."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _wheel_event():
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent

    return QWheelEvent(QPointF(5, 5), QPointF(5, 5), QPoint(0, -120), QPoint(0, -120), Qt.MouseButton.NoButton,
                       Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)


def test_the_wheel_leaves_boxes_alone_until_clicked(qapp):
    from PySide6.QtWidgets import QComboBox, QDoubleSpinBox, QLineEdit, QVBoxLayout, QWidget

    from sprigcast.app.wheel_guard import WheelGuard

    page = QWidget()
    lay = QVBoxLayout(page)
    other, combo, spin = QLineEdit(), QComboBox(), QDoubleSpinBox()
    combo.addItems(["NVENC", "x264"])
    for w in (other, combo, spin):
        lay.addWidget(w)
    page.show()
    other.setFocus()
    qapp.processEvents()
    guard = WheelGuard()
    for box in (combo, spin):
        event = _wheel_event()
        assert guard.eventFilter(box, event) is True and not event.isAccepted()  # passed on to the page
    combo.setFocus()
    qapp.processEvents()
    if combo.hasFocus():
        assert guard.eventFilter(combo, _wheel_event()) is False  # clicked into: the wheel changes it
    assert not other.property("_sprigcast_wheel_guard")  # only drop-downs and number boxes get the guard
    page.close()


def test_a_click_opens_a_submenu(qapp):
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QMouseEvent

    from sprigcast.app.click_menu import ClickMenu

    menu = ClickMenu()
    menu.addAction("Chat")
    sub = menu.addMenu("Plugins")
    sub.addAction("Slide show")
    menu.popup(menu.pos())
    qapp.processEvents()
    at = QPointF(menu.actionGeometry(sub.menuAction()).center())
    release = QMouseEvent(QMouseEvent.Type.MouseButtonRelease, at, at, Qt.MouseButton.LeftButton,
                          Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
    menu.mouseReleaseEvent(release)
    qapp.processEvents()
    assert sub.isVisible()
    sub.close()
    menu.close()


def test_a_plugin_overlay_goes_into_the_sprig_you_have_open(tmp_path, monkeypatch, qapp):
    import keyring

    from sprigcast.app import main_window
    from sprigcast.app.main_window import MainWindow
    from sprigcast.core.scene import PART_KIND, Canvas, Scene, SceneCollection, SceneItem, SourceConfig

    monkeypatch.setattr(keyring, "get_password", lambda *a: None)
    col = SceneCollection(
        canvases=[Canvas(id="horizontal", width=1920, height=1080)],
        sources=[SourceConfig(id="ps", kind=PART_KIND, name="P", settings={"scene": "p"})],
        scenes=[Scene(id="a", name="A", canvas="horizontal", items=[SceneItem(source="ps")]),
                Scene(id="p", name="P", canvas="horizontal", kind="part")],
        active_scenes={"horizontal": "a"})
    w = MainWindow(col, tmp_path / "scenes.json")
    try:
        class Overlay:
            label, defaults, own_sprig = "Snap", {}, (800, 1000)

        class Accept:
            def __init__(self, *_a, **_kw):
                pass

            def exec(self):
                return True

            def values(self):
                return {}

        monkeypatch.setattr(main_window, "source_class", lambda kind: Overlay)
        monkeypatch.setattr(main_window, "SettingsDialog", Accept)
        w.last_bloom_id = "a"
        w.selected_scene_id = "p"  # the sprig is open
        w._add_from_class("snap", "Snap")
        sprig = col.scene("p")
        assert len(col.scene("a").items) == 1  # the bloom is as it was
        assert len(sprig.items) == 1 and col.source(sprig.items[0].source).kind == PART_KIND
    finally:
        w.close()


def test_the_header_clock_keeps_its_room_as_seconds_tick(tmp_path, monkeypatch, qapp):
    import keyring

    from sprigcast.app.main import load_collection
    from sprigcast.app.main_window import MainWindow

    monkeypatch.setattr(keyring, "get_password", lambda *a: None)
    w = MainWindow(load_collection(tmp_path / "scenes.json"), tmp_path / "scenes.json")
    try:
        widths = set()
        for seconds in (3671, 3608, 4111, 4444):  # 1:01:11 1:00:08 1:08:31 1:14:04
            w._side_status("live", seconds, "2 of 2 sending")
            widths.add(w.mode_clock.minimumWidth())
        assert len(widths) == 1
        assert "1:14:04" in w.mode_clock.text()  # shows the real time, measured as 8:88:88
    finally:
        w._side_status("live", None)
        w.close()


CRASH_LOG = """
=== SprigCast run 0.2.0 pid 4100 started 2026-11-15 18:00:00
=== SprigCast run 0.2.0 pid 5120 started 2026-11-15 19:00:00
Windows fatal exception: access violation

Thread 0x00001f44 (most recent call first):
  File "C:\\\\SprigCast\\\\sprigcast\\\\render\\\\engine.py", line 812 in _draw
  File "C:\\\\SprigCast\\\\sprigcast\\\\render\\\\engine.py", line 700 in run

=== SprigCast run 0.2.0 pid 6000 started 2026-11-15 20:00:00
"""

EVENTS = """Event[0]
  Log Name: Application
  Source: Application Error
Description:
Faulting application name: SprigCast.exe, version: 0.2.0.0, time stamp: 0x6710
Faulting module name: nvcuda64.dll, version: 32.0.15.6094, time stamp: 0x6700
Exception code: 0xc0000005
Fault offset: 0x00000000001a2b3c
Faulting process id: 0x1400
"""


def test_the_crash_log_says_what_happened_in_that_run():
    from sprigcast.app.crash_report import read_crash_log, read_windows_events

    found = read_crash_log(CRASH_LOG, 5120)
    assert found.cause == "access violation" and found.where == "engine.py, line 812 in _draw"
    assert read_crash_log(CRASH_LOG, 4100).cause == ""  # that run ended without a crash dump
    module = read_windows_events(EVENTS, 0x1400)
    assert (module.module, module.cause) == ("nvcuda64.dll", "access violation")
    assert read_windows_events(EVENTS, 999).module == ""


def test_the_next_start_notices_an_unclean_exit(tmp_path):
    import json

    from sprigcast.app import crash_report

    log = tmp_path / "crash.log"
    log.write_text(CRASH_LOG, encoding="utf-8")
    assert crash_report.check(tmp_path, log, events=lambda: "") is None  # first run: nothing to say
    crash_report.closed_cleanly(tmp_path)
    assert crash_report.check(tmp_path, log, events=lambda: "") is None  # a clean exit
    (tmp_path / "running.json").write_text(json.dumps({"pid": 5120, "started": "2026-11-15 19:00:00"}))
    found = crash_report.check(tmp_path, log, events=lambda: EVENTS.replace("0x1400", hex(5120)))
    assert found is not None and found.summary() == "Access violation in nvcuda64.dll"
    assert "closed unexpectedly: Access violation in nvcuda64.dll (at engine.py, line 812" in log.read_text()
    assert json.loads((tmp_path / "running.json").read_text())["pid"] == os.getpid()
    (tmp_path / "running.json").write_text(json.dumps({"pid": 7, "started": "x"}))
    assert crash_report.check(tmp_path, log, events=lambda: "").summary().startswith("No error was recorded")


def test_a_second_launch_brings_the_first_forward(qapp):
    from PySide6.QtCore import QElapsedTimer

    from sprigcast.app import single_instance

    name = f"SprigCastTest{os.getpid()}"
    asked = []
    server = single_instance.listen(lambda: asked.append(1), name=name)
    assert server is not None
    # The second launch is its own program (a thread would hold Python's lock in Qt's blocking wait).
    import subprocess
    import sys

    code = ("import time; from sprigcast.app import single_instance as s; t = time.monotonic(); "
            f"print(s.ask_running_copy({name!r}), time.monotonic() - t)")
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    second = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True, env=env)
    clock = QElapsedTimer()
    clock.start()
    while (second.poll() is None or not asked) and clock.elapsed() < 15000:
        qapp.processEvents()
    out = second.communicate(timeout=5)[0]
    answer, took = out.split()
    assert answer == "True" and asked == [1]
    assert float(took) < 1.5  # it quit once the first copy answered, not after its whole two-second wait
    server.close()
    assert single_instance.ask_running_copy(name, wait_ms=100) is False  # nobody running: start normally


def test_live_shows_how_the_last_stream_went(qapp):
    from sprigcast.app.health import HealthRecord, PlaceHealth
    from sprigcast.app.live_stage import LiveSide

    side = LiveSide(*(lambda *a: None for _ in range(9)))
    opened = []
    side.on_health = lambda: opened.append(1)
    side.set_last_stream(None)
    assert side.last_stream.isHidden()
    side.set_last_stream(HealthRecord(started="2026-11-15 20:10", seconds=3600))
    assert side.last_stream.text().strip() == "Last stream: smooth" and not side.last_stream.isHidden()
    place = PlaceHealth(name="Twitch", frames=200_000, dropped=12)
    side.set_last_stream(HealthRecord(started="2026-11-15 21:10", seconds=3600, places=[place]))
    assert side.last_stream.text().strip() == "Last stream: bumpy  ·  12 drops"
    side.last_stream.click()
    assert opened == [1]


def test_a_wheel_over_an_unclicked_drop_down_never_changes_it(qapp):
    """PC test 2026-11-17: scrolling over a place's Resolution set it to 480p. Qt hands a WheelFocus box the
    focus before any filter sees the wheel, so the guard now takes WheelFocus away as boxes are made."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QComboBox, QLineEdit, QSpinBox, QVBoxLayout, QWidget

    from sprigcast.app import wheel_guard

    from sprigcast.app import theme

    wheel_guard.install(qapp)
    before = qapp.style().name()
    qapp.setStyle(theme.guarded_style(before))  # the system look: the guard still comes with polishing
    page = QWidget()
    lay = QVBoxLayout(page)
    other, combo, spin = QLineEdit(), QComboBox(), QSpinBox()
    combo.setFocusPolicy(Qt.FocusPolicy.WheelFocus)  # what some styles give a drop-down
    combo.addItems(["1080p", "720p", "480p"])
    for w in (other, combo, spin):
        lay.addWidget(w)
    page.show()
    other.setFocus()
    qapp.processEvents()
    assert combo.focusPolicy() == Qt.FocusPolicy.StrongFocus
    assert spin.focusPolicy() != Qt.FocusPolicy.WheelFocus
    qapp.sendEvent(combo, _wheel_event())
    assert combo.currentIndex() == 0 and not combo.hasFocus()
    page.close()
    qapp.setStyle(before)


def test_no_app_wide_python_hooks_and_the_ui_gets_the_lock_while_building(qapp):
    """PC test 2026-11-17: opening Settings or any pop-up while live froze SprigCast for seconds. An app-wide
    wheel filter ran Python for each of ~36,000 events while worker threads held Python's lock. Now each
    drop-down and number box has its own guard, and Python hands the lock over quickly while widgets are made."""
    import sys

    from PySide6.QtWidgets import QComboBox

    from sprigcast.app import theme, wheel_guard

    the_guard = wheel_guard.install(qapp)
    combo = QComboBox()
    wheel_guard.guard(combo)
    wheel_guard.guard(combo)  # once per widget
    assert combo.property(wheel_guard.GUARDED)
    assert the_guard.eventFilter(combo, _wheel_event()) is True

    theme._burst_until = 0.0
    theme._burst_over()  # any burst left over from widgets made earlier
    usual = sys.getswitchinterval()
    theme.ui_burst()
    assert sys.getswitchinterval() <= theme.BURST_SWITCH_S
    theme._burst_until = 0.0
    theme._burst_over()
    assert sys.getswitchinterval() == usual


def test_restyling_on_the_live_screen_never_keeps_the_quick_hand_over_on(qapp, monkeypatch):
    """Build 44094725157's heavy run: any restyle (a label recoloured every second) restarted the burst, so the
    lock changed hands every 0.1 ms for good and the canvases fell to 14-41 fps. Only a window being shown
    starts one now, and one never lasts more than BURST_MAX_S."""
    import sys

    from PySide6.QtWidgets import QDialog, QLabel, QVBoxLayout, QWidget

    from sprigcast.app import theme

    theme.apply_theme(qapp)
    theme._burst_until = 0.0
    theme._burst_over()
    usual = sys.getswitchinterval()
    window = QWidget()
    QVBoxLayout(window).addWidget(label := QLabel("Live"))
    window.show()
    theme._burst_until = 0.0
    theme._burst_over()  # the window's own burst, over
    for i in range(5):
        label.setStyleSheet(f"color: #{i}{i}{i}")  # restyled while live
        chip = QLabel("Chip")  # a piece made again on a refresh, styled before it's placed
        chip.setStyleSheet("color: #fff")
        chip.ensurePolished()
        qapp.processEvents()
        assert sys.getswitchinterval() == usual
    dialog = QDialog(window)
    QVBoxLayout(dialog).addWidget(QLabel("Settings"))
    dialog.show()  # a pop-up: a burst while its widgets are made
    qapp.processEvents()
    assert sys.getswitchinterval() <= theme.BURST_SWITCH_S
    clock = [theme._burst_began + theme.BURST_MAX_S + 1]
    monkeypatch.setattr(theme.time, "monotonic", lambda: clock[0])
    before = theme._burst_until
    theme.ui_burst(starts=False)  # still restyling after the cap: not stretched any further
    assert theme._burst_until == before
    theme._burst_over()
    assert sys.getswitchinterval() == usual
    dialog.close()
    window.close()
