import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")

from sprigcast.app import optimize  # noqa: E402
from sprigcast.core.scene import Canvas, EncoderSettings, Output, SceneCollection  # noqa: E402


def _collection(**kw):
    return SceneCollection(
        canvases=[Canvas(id="horizontal", width=1920, height=1080, fps=30),
                  Canvas(id="vertical", width=1080, height=1920, fps=30)],
        outputs=[Output(id="tw", type="stream", service="twitch", canvas="horizontal", bitrate_kbps=3000,
                        size=(1280, 720), enabled=True),
                 Output(id="tt", type="stream", service="tiktok", canvas="vertical", bitrate_kbps=6000, enabled=True)],
        stream_encoder=EncoderSettings(preset="p7", b_frames=2, lookahead=True),
        **kw,
    )


STRONG = dict(threads=16, memory_gb=32.0, codec="h264_nvenc")


def test_strong_nvenc_pc_with_good_upload_gets_1080p60_and_tiktok_stays_720p30():
    c = _collection()
    p = optimize.PcProfile(**STRONG, upload_kbps=20000)
    plan = optimize.plan(p, c)
    assert "1080p at 60 fps on NVENC" in plan.headline
    labels = [ch.label for ch in plan.changes]
    assert "Twitch: 1920×1080, 60 fps, 6000 kbps" in labels
    assert "TikTok: 720×1280, 30 fps, 4000 kbps" in labels
    assert any(lab.startswith("Horizontal canvas: 60 fps") for lab in labels)
    fps = plan.apply()
    tw, tt = c.outputs
    assert tw.size is None and tw.bitrate_kbps == 6000 and tw.fps == 60  # its own, never left to follow the canvas
    assert tt.size == (720, 1280) and tt.bitrate_kbps == 4000
    assert fps["horizontal"] == 60 and fps["vertical"] == 60  # vertical records at 60 too
    assert tt.fps == 30
    enc = c.stream_encoder
    assert (enc.b_frames, enc.lookahead, enc.preset) == (0, False, "p5")  # stays on the direct NVENC path
    assert c.recording.short_side is None and c.recording.bitrate_kbps == 12000
    assert optimize.plan(p, c, waiting_fps=fps).changes == []  # nothing left to suggest


def test_slow_upload_lowers_the_step_and_shares_upload_between_streams():
    c = _collection()
    p = optimize.PcProfile(**STRONG, upload_kbps=6000)  # 4500 for video, 2250 each minus audio
    plan = optimize.plan(p, c)
    tw = next(ch for ch in plan.changes if ch.label.startswith("Twitch"))
    assert tw.label == "Twitch: 1280×720, 30 fps, 2000 kbps"


def test_weak_pc_gets_light_settings():
    c = _collection()
    p = optimize.PcProfile(threads=4, memory_gb=8.0, codec="h264_qsv", upload_kbps=20000)
    plan = optimize.plan(p, c)
    assert "light side" in plan.headline
    plan.apply()
    assert c.outputs[0].size == (1280, 720) and c.recording.short_side == 720
    assert c.stream_encoder.preset == "p3" and c.stream_encoder.multipass == "disabled"


def test_cpu_encoding_follows_the_measured_speed():
    fast = optimize.PcProfile(threads=16, memory_gb=32.0, codec="libx264", x264_ms_720=1.0)
    slow = optimize.PcProfile(threads=8, memory_gb=16.0, codec="libx264", x264_ms_720=5.0)
    assert optimize.target(fast, encodes=1).step == (1080, 60)
    assert optimize.target(slow, encodes=1).step == (720, 60)  # 0.3 of the CPU
    assert optimize.target(slow, encodes=4).step == (480, 30)
    unknown = optimize.PcProfile(threads=16, memory_gb=32.0, codec="libx264")
    assert optimize.target(unknown).step == (720, 30)


def test_record_only_leaves_streams_alone_and_notes_are_given():
    c = _collection()
    p = optimize.PcProfile(**STRONG, wants_stream=False)
    plan = optimize.plan(p, c)
    assert not any(ch.group == "Stream" for ch in plan.changes)
    c2 = SceneCollection(canvases=[Canvas(id="horizontal", width=1920, height=1080)])
    plan2 = optimize.plan(optimize.PcProfile(**STRONG), c2)
    assert any("No stream set up" in n for n in plan2.notes) and any("Upload not tested" in n for n in plan2.notes)


def test_new_checks_and_rules_can_be_added():
    seen = []
    check = optimize.Check("extra-test", "Extra", lambda p: seen.append(p) or "ok")
    rule = lambda p, c: [optimize.Change("Extra", "Something", "Nothing", lambda: seen.append("applied"))]  # noqa: E731
    optimize.register_check(check, before="upload")
    optimize.register_rule(rule)
    try:
        keys = [c.key for c in optimize.CHECKS]
        assert keys.index("extra-test") == keys.index("upload") - 1
        plan = optimize.plan(optimize.PcProfile(**STRONG), _collection())
        extra = [ch for ch in plan.changes if ch.group == "Extra"]
        plan.apply(extra)
        assert seen == ["applied"]
    finally:
        optimize.CHECKS.remove(check)
        optimize.RULES.remove(rule)


def test_room_grows_with_the_stage():
    c = _collection()
    p = optimize.PcProfile(**STRONG, upload_kbps=18400)
    rooms = [optimize.room(p, c, step) for _, step in optimize.STAGES]
    assert rooms[0][0] < rooms[-1][0] and rooms[0][1] < rooms[-1][1]
    assert rooms[-1][0] <= 60 and rooms[-1][1] <= 60  # the suggestion for a strong PC sits in the green
    assert optimize.room(optimize.PcProfile(**STRONG), c, (1080, 60))[1] is None  # upload untested


def _line(kbps):
    """A connection that carries `kbps`: pieces leave at that pace until the deadline."""
    import time as t

    def sender(deadline, count):
        piece = 16 * 1024
        while t.monotonic() < deadline:
            t.sleep(piece * 8 / 1000 / kbps)
            count(piece)

    return sender


def test_upload_speed_counts_what_leaves_until_the_deadline():
    kbps = optimize.upload_speed(seconds=0.8, sender=_line(16000), connections=1)
    assert 13000 < kbps < 17000

    def offline(deadline, count):
        raise OSError("offline")

    assert optimize.upload_speed(seconds=0.2, sender=offline) is None


def test_x264_speed_measures_something():
    ms = optimize.x264_speed(frames=5, width=320, height=180)
    assert ms is not None and ms > 0


def test_wizard_runs_checks_and_applies_ticked_changes(monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from sprigcast.app.wizard import OptimizeWizard

    QApplication.instance() or QApplication([])

    def fake(p):
        p.threads, p.memory_gb, p.codec, p.upload_kbps = 16, 32.0, "h264_nvenc", 20000
        return "fake"

    monkeypatch.setattr(optimize, "CHECKS", [optimize.Check("all", "Everything", fake)])
    c = _collection()
    w = OptimizeWizard(c, run_in_thread=False)
    w.test_upload.setChecked(False)
    w.start_checks()
    assert w.pages.currentIndex() == 2 and w.upload.value() == 20.0
    assert "fake" in w.check_list.item(0).text()
    w.change_list.item(0).setCheckState(Qt.CheckState.Unchecked)
    w.upload.setValue(6.0)  # retyped: the suggestions follow
    assert any("2000 kbps" in w.change_list.item(i).text() for i in range(w.change_list.count()))
    w.change_list.item(0).setCheckState(Qt.CheckState.Unchecked)
    assert w.plan.recommended == (720, 30)
    assert [b.isChecked() for b in w.stages] == [False, True, False, False, False]  # Sprout
    assert "Sprout" in w.stage_title.text() and w.upload_gauge.pct is not None
    w.stages[-1].click()  # Bloom picked by hand: every stream follows, TikTok keeps its limit
    assert w.plan.quality == (1080, 60) and any("dropped frames" in n for n in w.plan.notes)
    assert w.stages[-1].isChecked() and "YOU PICKED" in w.grow_label.text()
    chips = [w.chips.itemAt(i).widget().text() for i in range(w.chips.count()) if w.chips.itemAt(i).widget()]
    assert any("TikTok · 720p30, its limit" in t for t in chips)
    assert w.upload_gauge.pct > 85  # 6 Mbps can't carry it: the gauge goes red
    assert any("Twitch: 1920×1080, 60 fps, 6000 kbps" in w.change_list.item(i).text()
               for i in range(w.change_list.count()))
    w.change_list.item(0).setCheckState(Qt.CheckState.Unchecked)
    w._apply()
    assert w.result() == w.DialogCode.Accepted
    assert len(w.chosen) == w.change_list.count() - 1
    w.plan.apply(w.chosen)


def test_first_run_offers_the_wizard_once(tmp_path, monkeypatch):
    import keyring
    from PySide6.QtCore import QCoreApplication, QSettings
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    monkeypatch.setattr(keyring, "get_password", lambda svc, ref: None)
    QCoreApplication.setOrganizationName("SprigCastTests")
    QCoreApplication.setApplicationName("OptimizeWindowTest")
    QSettings().clear()
    from sprigcast.app.main_window import MainWindow

    w = MainWindow(_collection(), tmp_path / "scenes.json")
    try:
        opened = []
        monkeypatch.setattr(w, "open_optimizer", lambda first_run=False: opened.append(first_run))
        monkeypatch.setattr(w, "open_starter", lambda: opened.append("starter"))
        monkeypatch.setattr(w, "open_getting_started", lambda: opened.append("guide"))  # closed: None
        w.first_run_optimizer()
        w.first_run_optimizer()
        # fresh: Get started first; closed, so the wizard and the starter kit are offered as before
        assert opened == ["guide", True, "starter"]
        assert any(c.title == "Optimize for this PC" for c in w._palette_commands())
    finally:
        w.close()
        QSettings().clear()


def _twitch_and_youtube():
    return SceneCollection(
        canvases=[Canvas(id="horizontal", width=1920, height=1080, fps=60)],
        outputs=[Output(id="tw", type="stream", service="twitch", canvas="horizontal", bitrate_kbps=6000, enabled=True),
                 Output(id="yt", type="stream", service="youtube", canvas="horizontal", bitrate_kbps=6000,
                        enabled=True)],
    )


def test_streams_on_one_canvas_get_one_bitrate_so_they_share_an_encoder():
    c = _twitch_and_youtube()
    p = optimize.PcProfile(**STRONG, upload_kbps=40000)
    kbps = {o.id: k for o, _, k in optimize.stream_steps(p, c, (1080, 60))}
    assert kbps == {"tw": 6000, "yt": 6000}  # YouTube would take 9000, but then it needs its own encode


def test_a_clean_stream_proves_the_upload_and_the_gauge_matches_apply():
    # The user's PC: the Twitch test read 11.2 Mbps, yet Twitch + YouTube at 6000 each ran clean.
    c = _twitch_and_youtube()
    p = optimize.PcProfile(**STRONG, upload_kbps=11179)
    tested = optimize.plan(p, c)
    assert "Twitch: 1920×1080, 60 fps, 4000 kbps" in [ch.label for ch in tested.changes]
    assert optimize.room(p, c, (1080, 60))[1] <= 100  # the gauge shows the 4000s Apply would set

    p = optimize.PcProfile(**STRONG, upload_kbps=11179)
    p.extra["proven_kbps"] = 2 * (6000 + c.stream_encoder.audio_kbps)
    proven = optimize.plan(p, c)
    assert not [ch for ch in proven.changes if ch.group == "Stream"]  # 6000 each stays
    assert optimize.room(p, c, (1080, 60))[1] <= 80


def test_proven_kbps_only_counts_clean_long_streams():
    from types import SimpleNamespace as NS

    def st(**kw):
        return NS(**{"frames": 3600, "dropped": 0, "reconnects": 0, "autopilot": None, "bitrate_kbps": 6000, **kw})

    assert optimize.proven_kbps([st(), st()], 160, 120) == 12320
    assert optimize.proven_kbps([st(), st()], 160, 20) is None  # too short to prove anything
    assert optimize.proven_kbps([st(), st(dropped=50)], 160, 120) == 0  # the network struggled
    assert optimize.proven_kbps([st(autopilot=NS(steps_down=1))], 160, 120) == 0


def test_a_place_that_never_connected_does_not_void_what_the_others_proved():
    from types import SimpleNamespace as NS

    def st(**kw):
        return NS(**{"frames": 3600, "dropped": 0, "reconnects": 0, "autopilot": None, "bitrate_kbps": 6000, **kw})

    assert optimize.proven_kbps([st(), st(frames=0)], 160, 3600) == 6160
    assert optimize.proven_kbps([st(frames=0)], 160, 3600) is None


def test_a_lighter_clean_stream_keeps_the_most_proven():
    class Settings(dict):
        def setValue(self, k, v):
            self[k] = v

        def value(self, k, default=None):
            return self.get(k, default)

        def remove(self, k):
            self.pop(k, None)

    s = Settings()
    optimize.remember_proven(s, 12320)
    optimize.remember_proven(s, 4160)  # an hour at 4000 is clean, but 12320 already ran clean
    assert optimize.load_proven(s) == 12320
    optimize.remember_proven(s, None)
    assert optimize.load_proven(s) == 12320
    optimize.remember_proven(s, 0)  # network trouble: start over
    assert optimize.load_proven(s) == 0


def test_upload_speed_adds_up_what_several_connections_carry():
    """PC check 2026-11-19: 11.4 Mbps measured on a line that had carried 23 Mbps of streams for an hour.
    One connection alone tops out below the line; streams to three places are three connections."""
    one = optimize.upload_speed(seconds=1.2, sender=_line(6000), connections=1)
    four = optimize.upload_speed(seconds=1.2, sender=_line(6000), connections=4)
    assert 4500 < one < 6600 and 18000 < four < 26000


def test_canvases_never_end_up_at_two_frame_rates():
    """PC check 2026-11-19: the horizontal canvas was set to 30 fps and the vertical one, with nothing to
    send, kept its 60."""
    c = _collection()
    c.outputs = [o for o in c.outputs if o.id == "tw"]
    for canvas in c.canvases:
        canvas.fps = 60
    p = optimize.PcProfile(**STRONG, upload_kbps=4000, wants_record=False)
    fps = optimize.plan(p, c).apply()
    assert fps == {"horizontal": 30, "vertical": 30}
    p = optimize.PcProfile(**STRONG, upload_kbps=4000)  # recordings at 60 on a strong PC: both canvases 60
    c = _collection()
    fps = optimize.plan(p, c).apply()
    assert fps == {"horizontal": 60, "vertical": 60}
    assert next(o for o in c.outputs if o.id == "tw").fps == 30  # the stream itself stays at what the upload carries


def test_three_places_on_a_fast_line_keep_full_quality():
    c = _collection()
    c.outputs = [Output(id=i, type="stream", service=i, canvas="horizontal", enabled=True)
                 for i in ("twitch", "youtube", "kick")]
    p = optimize.PcProfile(**STRONG, upload_kbps=30000)
    labels = [ch.label for ch in optimize.plan(p, c).changes]
    assert sum("1920×1080, 60 fps, 6000 kbps" in lab for lab in labels) == 3


def test_the_upload_can_be_tested_again_by_hand_and_shows_in_a_rounded_field(monkeypatch):
    """User 2026-11-19: the pop-up "still shows the old square looking input ... and there is no option to run
    it manually"."""
    from PySide6.QtWidgets import QApplication, QGroupBox

    QApplication.instance() or QApplication([])
    from sprigcast.app.wizard import OptimizeWizard

    readings = iter([11400, 24300, None])

    def upload(p):
        p.upload_kbps = next(readings)
        p.extra["upload_source"] = "Cloudflare" if p.upload_kbps else None
        return "tested"

    checks = [optimize.Check("encoder", "Video encoder", lambda p: setattr(p, "codec", "h264_nvenc") or "NVENC"),
              optimize.Check("upload", "Upload speed", upload)]
    monkeypatch.setattr(optimize, "CHECKS", checks)
    monkeypatch.setattr(optimize.PcProfile, "weak_reasons", property(lambda self: []))
    c = _collection()
    c.outputs = [Output(id=i, type="stream", service=i, canvas="horizontal", enabled=True)
                 for i in ("twitch", "youtube", "kick")]
    w = OptimizeWizard(c, run_in_thread=False)
    try:
        w.show()
        w.start_checks()
        assert w.pages.currentIndex() == 2 and w.upload.value() == 11.4
        assert isinstance(w.upload_row, QGroupBox) and w.upload_row.title() == "Upload speed"
        assert "border-radius: 13px" in w.upload.styleSheet() and "width: 0px" in w.upload.styleSheet()
        assert w.plan.quality == (720, 30)
        assert w.grow_label.text() == "YOUR UPLOAD CAN CARRY" and "2600 kbps" in w.stage_title.text()
        w.test_again.click()  # by hand, without starting over
        assert w.upload.value() == 24.3 and w.profile.upload_kbps == 24300
        assert w.grow_label.text() == "YOUR PC CAN GROW TO"
        assert w.plan.quality == (1080, 60) and w.test_again.isEnabled() and w.test_again.text() == "Test again"
        w.test_again.click()  # offline now: what was measured stays
        assert w.profile.upload_kbps == 24300 and "Couldn't measure" in w.upload_label.text()
        w.upload.setValue(30.0)
        assert w.upload_label.text() == "Typed in"
        # The list of changes opens without squeezing what's above it into each other.
        w.details_button.setChecked(True)
        QApplication.processEvents()
        assert all(b.height() == 108 for b in w.stages) and w.pc_gauge.height() == 136
        assert not w.cancel.autoDefault() and not w.back.autoDefault()
    finally:
        w.close()


def test_places_that_share_an_encoder_cost_the_pc_once():
    """Three places on one canvas at one quality are one encoder: the gauge counted three."""
    c = _collection()
    one = [Output(id="twitch", type="stream", service="twitch", canvas="horizontal", enabled=True)]
    three = [Output(id=i, type="stream", service=i, canvas="horizontal", enabled=True)
             for i in ("twitch", "youtube", "kick")]
    p = optimize.PcProfile(**STRONG, upload_kbps=60000)
    c.outputs = one
    alone = optimize.room(p, c, (1080, 60))[0]
    c.outputs = three
    assert optimize.room(p, c, (1080, 60))[0] == alone
    assert optimize._encodes(p, c) == optimize._encodes(p, _with(c, one))


def _with(c, outputs):
    copy = c.model_copy(deep=True)
    copy.outputs = outputs
    return copy


def _three_places():
    c = _collection()
    c.outputs = [Output(id=i, type="stream", service=i, canvas="horizontal", enabled=True)
                 for i in ("twitch", "youtube", "kick")]
    return c


def test_a_small_upload_is_named_as_the_limit_not_the_pc():
    """User 2026-11-19: "saying my computer is not good enough at 10.6 up speed". Three places share what the
    upload carries; the note says so and what fewer places would get."""
    c = _three_places()
    p = optimize.PcProfile(**STRONG, upload_kbps=10600)
    assert optimize.pc_step(p, c) == (1080, 60) and optimize.upload_is_the_limit(p, c)
    plan = optimize.plan(p, c)
    assert plan.quality == (720, 30)
    note = next(n for n in plan.notes if n.startswith("Your upload"))
    assert "10.6 Mbps" in note and "not your PC" in note and "7950 kbps, shared by 3 places" in note
    assert "2 places would get 720p · 60 fps." in note and "One place alone would get 1080p · 60 fps." in note

    c.outputs = c.outputs[:1]  # one place: the same upload carries all the PC can do
    assert not optimize.upload_is_the_limit(p, c) and optimize.upload_note(p, c) == ""
    assert optimize.plan(p, c).quality == (1080, 60)

    c = _three_places()
    fast = optimize.PcProfile(**STRONG, upload_kbps=30000)
    assert not optimize.upload_is_the_limit(fast, c) and not [n for n in optimize.plan(fast, c).notes
                                                              if n.startswith("Your upload")]


def test_the_upload_test_says_who_it_is_and_tries_a_second_shape_when_refused(monkeypatch):
    """PC check 2026-11-19: "upload test stopped: HTTP Error 403: Forbidden" on all four connections, with
    Python's own name on the request."""
    import urllib.error
    import urllib.request

    seen = []

    class Reply:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b""

    def urlopen(request, timeout=None):
        headers = {k.lower(): v for k, v in request.header_items()}
        seen.append(headers)
        assert "python" not in headers["user-agent"].lower()
        if "origin" not in headers:  # the plain shape is refused: the second one is taken
            raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", None, None)
        return Reply()

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    shape = optimize._upload_shape(optimize.UPLOAD_URL)
    assert len(seen) == 2 and "Origin" in shape and "python" not in shape["User-Agent"].lower()


def test_the_real_sender_measures_a_slow_server_without_waiting_for_answers():
    """PC check 2026-11-19 (#711): every connection ended in "The read operation timed out" and the test read
    15 Mbps on a line that carries 23. Bytes count as they leave; a request cut off by the deadline, or an
    answer that never comes, takes nothing away."""
    import socket
    import threading
    import time as t

    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 64 * 1024)
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    stop = threading.Event()
    rate = 250_000  # bytes a second each connection is read at: 2 Mbps, and never an answer

    def drain(conn):
        conn.settimeout(2)
        try:
            while not stop.is_set():
                if not conn.recv(25_000):
                    break
                t.sleep(0.1)
        except OSError:
            pass
        conn.close()

    def accept():
        server.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except OSError:
                continue
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 64 * 1024)
            threading.Thread(target=drain, args=(conn,), daemon=True).start()

    threading.Thread(target=accept, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.getsockname()[1]}/__up"
        began = t.monotonic()
        kbps = optimize.upload_speed(url, seconds=4.0, sender=optimize._upload_sender(url, {"User-Agent": "t"}),
                                     connections=2)
        assert t.monotonic() - began < 8  # cut off at the deadline, not held by the answer
        assert 2 * rate * 8 / 1000 * 0.6 < kbps < 2 * rate * 8 / 1000 * 1.5, kbps
    finally:
        stop.set()
        server.close()


def test_a_place_keeps_the_frame_rate_optimize_chose_when_the_canvas_does_not_follow():
    """PC 2026-11-19: Optimize set three places to 720p, 30 fps, 2600 kbps and queued the canvas for 30 fps.
    The places were left "same as the canvas", the canvas stayed at 60, and they went out at 60 fps on 2600."""
    c = _three_places()
    p = optimize.PcProfile(**STRONG, upload_kbps=11400)
    plan = optimize.plan(p, c)
    assert plan.quality == (720, 30)
    plan.apply()
    assert [o.fps for o in c.outputs] == [30, 30, 30]  # whatever becomes of the canvas
    for canvas in c.canvases:
        canvas.fps = 60  # the queued change never arrived
    again = optimize.plan(p, c)
    assert not [ch for ch in again.changes if ch.group == "Stream"]  # still 30: nothing to put right


def test_apply_writes_the_path_file_at_once(tmp_path, monkeypatch):
    """PC 2026-11-19: after Apply the path's own file kept the old values until a path switch or a later save."""
    import keyring
    from PySide6.QtCore import QCoreApplication, QSettings
    from PySide6.QtWidgets import QApplication, QDialog

    QApplication.instance() or QApplication([])
    monkeypatch.setattr(keyring, "get_password", lambda svc, ref: None)
    QCoreApplication.setOrganizationName("SprigCastTests")
    QCoreApplication.setApplicationName("OptimizeApplyTest")
    QSettings().clear()
    from sprigcast.app import main_window
    from sprigcast.app.main_window import MainWindow
    from sprigcast.core.gardens import PathSettings

    c = _three_places()
    for o in c.outputs:
        o.bitrate_kbps, o.size = 3000, (1280, 720)
    w = MainWindow(c, tmp_path / "scenes.json")
    try:
        shelf = w.gardens.shelf
        assert {o.bitrate_kbps for o in PathSettings.load(shelf.path_file()).outputs} == {3000}

        def modal(wizard):
            wizard.profile = optimize.PcProfile(**STRONG, upload_kbps=40000)
            wizard.plan = optimize.plan(wizard.profile, wizard.collection, wizard.waiting_fps)
            wizard.chosen = list(wizard.plan.changes)
            wizard.close()
            return QDialog.DialogCode.Accepted

        monkeypatch.setattr(main_window, "modal", modal)
        assert w.open_optimizer()
        saved = PathSettings.load(shelf.path_file())
        assert [(o.bitrate_kbps, o.fps) for o in saved.outputs] == [(6000, 60)] * 3
    finally:
        w.close()
        QSettings().clear()
