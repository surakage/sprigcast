"""Help improve SprigCast: stream summaries go out only when turned on, after the stream, with nothing private."""

from __future__ import annotations

import zipfile
from concurrent.futures import Future
from datetime import datetime

import pytest

pytest.importorskip("PySide6")

from sprigcast.app import share_reports  # noqa: E402


class FakeSettings(dict):
    def value(self, key, default=None, type=None):  # noqa: A002 - QSettings' own name
        return self.get(key, default)

    def setValue(self, key, value):  # noqa: N802 - QSettings' own name
        self[key] = value


def _now_run(job, *args, name=""):
    done = Future()
    done.set_result(job(*args))
    return done


def test_off_unless_turned_on_and_never_for_a_quick_test(tmp_path):
    settings, sent = FakeSettings(), []
    assert not share_reports.enabled(settings)
    assert share_reports.send_after_stream(None, 600, settings=settings, run=_now_run,
                                           sender=lambda *a: sent.append(a)) is None
    share_reports.set_enabled(True, settings)
    assert share_reports.send_after_stream(None, 30, settings=settings, run=_now_run,
                                           sender=lambda *a: sent.append(a)) is None
    assert sent == [] and share_reports.asked(settings)


def test_the_summary_holds_the_stream_and_today_hides_whats_private(tmp_path, monkeypatch):
    from sprigcast.app import tester_report

    now = datetime(2026, 11, 21, 15, 6)
    logs = tmp_path / "logs"
    logs.mkdir()
    report = logs / "stream 2026-11-21 12-37-14.txt"
    report.write_text("Kick (horizontal): live, key live_123456_SECRETKEYabcdef, user surar\n", encoding="utf-8")
    (logs / "sprigcast 2026-11-21.log").write_text("went live from C:\\Users\\surar\\Videos\n", encoding="utf-8")
    (logs / "sprigcast 2026-11-20.log").write_text("yesterday\n", encoding="utf-8")
    monkeypatch.setattr(tester_report, "pc_lines", lambda: ["PC: test"])
    scrub = tester_report.Scrubber(secrets=["live_123456_SECRETKEYabcdef"], names=["surar"])

    target = share_reports.make(tmp_path / "out.zip", report, log_folder=logs, scrub=scrub, now=now)
    with zipfile.ZipFile(target) as z:
        names = set(z.namelist())
        text = "".join(z.read(n).decode("utf-8") for n in names)
        summary = z.read("summary.txt").decode("utf-8")
    assert names == {"summary.txt", "about.txt", f"logs/{report.name}", "logs/sprigcast 2026-11-21.log"}
    assert "Report: stream" in summary
    assert "SECRETKEY" not in text and "surar" not in text


def test_sends_after_a_real_stream_when_turned_on(tmp_path, monkeypatch):
    settings, sent = FakeSettings(), []
    share_reports.set_enabled(True, settings)
    monkeypatch.setattr(share_reports, "make", lambda target, *a, **k: target.write_bytes(b"PK\x03\x04") and target)

    def sender(path, kind, cause):
        sent.append((path.exists(), kind, cause))
        return "SC-ABCDEF"

    future = share_reports.send_after_stream(tmp_path / "x.txt", 8920, settings=settings, sender=sender,
                                             run=_now_run)
    assert future.result() == "SC-ABCDEF"
    assert sent == [(True, "stream", "stream of 148 min")]


def test_a_failed_send_is_only_a_log_line(tmp_path, monkeypatch):
    settings = FakeSettings()
    share_reports.set_enabled(True, settings)
    monkeypatch.setattr(share_reports, "make", lambda target, *a, **k: target.write_bytes(b"PK") and target)

    def sender(*_):
        raise OSError("no network")

    assert share_reports.send_after_stream(None, 120, settings=settings, sender=sender, run=_now_run).result() is None


def test_asked_once_after_the_first_real_stream():
    settings, asks = FakeSettings(), []
    assert share_reports.maybe_ask(None, 30, settings, ask=lambda: asks.append(1) or True) is None
    assert share_reports.maybe_ask(None, 300, settings, ask=lambda: asks.append(1) or False) is False
    assert not share_reports.enabled(settings)
    assert share_reports.maybe_ask(None, 300, settings, ask=lambda: asks.append(1) or True) is None
    assert asks == [1]


def test_the_answer_is_logged(caplog, monkeypatch):
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])

    def press():
        card = QApplication.activeModalWidget()
        card._shown_at = None  # past the moment a press is ignored
        card._press(card._buttons[-1])

    QTimer.singleShot(50, press)
    with caplog.at_level("INFO", logger="sprigcast.app.share_reports"):
        assert share_reports.maybe_ask(None, 300, FakeSettings()) is True
    assert f"answered: {share_reports.YES}" in caplog.text


def test_a_stream_only_yes_covers_streams_until_asked_again():
    settings = FakeSettings({share_reports.SETTING: True, share_reports.ASKED: True})  # said yes before version 2
    assert share_reports.covers("stream", settings) and not share_reports.covers("crash", settings)
    assert not share_reports.covers("import", settings) and share_reports.needs_ask(settings)
    share_reports.set_enabled(True, settings)
    assert share_reports.covers("crash", settings) and share_reports.covers("import", settings)
    assert settings[share_reports.CONSENT] == share_reports.VERSION and settings[share_reports.CONSENT_DATE]
    assert not share_reports.needs_ask(settings)
    share_reports.set_enabled(False, settings)
    assert not share_reports.covers("stream", settings) and not share_reports.needs_ask(settings)
    assert share_reports.needs_ask(FakeSettings())


def test_install_id_is_random_and_kept():
    settings = FakeSettings()
    first = share_reports.install_id(settings)
    assert len(first) == 36 and share_reports.install_id(settings) == first
    assert share_reports.install_id(FakeSettings()) != first


def test_an_import_that_missed_something_is_sent_only_when_covered(tmp_path, monkeypatch):
    monkeypatch.setattr(share_reports, "make", lambda target, *a, **k: zipfile.ZipFile(target, "w").close())
    settings, sent = FakeSettings(), []

    def sender(path, kind, cause):
        with zipfile.ZipFile(path) as z:
            sent.append((kind, cause, z.read("import.txt").decode()))
        return "SC-ABCDEF"

    missed = "source:vlc_source x2, filter:move_value_filter x1"
    assert share_reports.send_after_import("OBS", missed, 3, settings=settings, run=_now_run, sender=sender) is None
    share_reports.set_enabled(True, settings)
    assert share_reports.send_after_import("OBS", "", 0, settings=settings, run=_now_run, sender=sender) is None
    done = share_reports.send_after_import("OBS", missed, 3, settings=settings, run=_now_run, sender=sender)
    assert done.result() == "SC-ABCDEF"
    assert sent[0][0] == "import" and sent[0][1] == f"OBS: {missed}" and missed in sent[0][2]
