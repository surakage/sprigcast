import io
import json
import os
import urllib.error
import zipfile
from datetime import datetime

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtWidgets import QApplication  # noqa: E402

from sprigcast.app import problem_report, tester_report  # noqa: E402
from sprigcast.app.crash_report import Crash  # noqa: E402

CRASH = Crash(started="2026-11-16 21:42:05", cause="access violation", module="nvcuda64.dll",
              where="render.py, line 120 in draw")


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def logs(tmp_path, monkeypatch):
    folder = tmp_path / "Logs"
    folder.mkdir()
    monkeypatch.setenv("SPRIGCAST_LOG_DIR", str(folder))
    (folder / f"sprigcast {datetime.now():%Y-%m-%d}.log").write_text(
        "WARNING stream dropped in C:\\Users\\surar\\Videos\n", encoding="utf-8")
    monkeypatch.setattr(tester_report.Scrubber, "for_this_pc",
                        classmethod(lambda cls, secrets=(): cls(secrets, ["surar"])))
    import keyring

    monkeypatch.setattr(keyring, "get_password", lambda svc, ref: None)
    return folder


def _read(path):
    with zipfile.ZipFile(path) as z:
        return {n: z.read(n).decode("utf-8") for n in z.namelist()}


def test_with_the_box_ticked_it_is_the_export_report_plus_the_note(app, logs, tmp_path):
    path = problem_report.make(tmp_path / "r.zip", "Switched to BRB as surar, then it froze.", True, CRASH)
    files = _read(path)
    assert "about.txt" in files and any(n.startswith("logs/sprigcast ") for n in files)
    assert files["note.txt"] == "Switched to BRB as you, then it froze.\n"
    assert "Crash: Access violation in nvcuda64.dll (at render.py, line 120 in draw)" in files["summary.txt"]
    assert "surar" not in "".join(files.values())
    rows = problem_report.contents(path)
    assert rows[0][0] == "Your note"
    assert ("summary.txt", "Version, Windows build, what crashed") in rows
    assert any(name == "Log" for name, _ in rows)


def test_unticked_it_is_only_the_note_version_and_cause(app, logs, tmp_path):
    files = _read(problem_report.make(tmp_path / "r.zip", "", False, CRASH))
    assert sorted(files) == ["summary.txt"]
    assert "SprigCast " in files["summary.txt"] and "Report: crash" in files["summary.txt"]


def test_send_posts_the_zip_and_returns_the_number(tmp_path):
    path = tmp_path / "r.zip"
    path.write_bytes(b"PK\x03\x04zip")
    seen = {}

    class Reply(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def opener(request, timeout):
        seen.update(url=request.full_url, body=request.data, headers=dict(request.header_items()))
        return Reply(json.dumps({"id": "SC-7F3K2M"}).encode())

    assert problem_report.send(path, "crash", "Access violation in nvcuda64.dll", url="https://x/v1/report",
                               opener=opener) == "SC-7F3K2M"
    assert seen["url"] == "https://x/v1/report" and seen["body"] == b"PK\x03\x04zip"
    assert seen["headers"]["Content-type"] == "application/zip"
    assert seen["headers"]["X-sprigcast-kind"] == "crash"

    def offline(request, timeout):
        raise urllib.error.URLError("no connection")

    with pytest.raises(OSError, match="no connection"):
        problem_report.send(path, "crash", opener=offline)


def test_crash_card_reads_like_the_mock(app):
    card = problem_report.ReportCard(None, CRASH)
    texts = [w.text() for w in card.findChildren(type(card.send_button))]
    assert card.windowTitle() == "SprigCast closed unexpectedly"
    assert "Send report" in texts and "Not now" in texts and "See what's sent" in texts
    assert card.include.isChecked()
    assert problem_report.crash_line(CRASH).startswith("Access violation in nvcuda64.dll · started ")
    plain = problem_report.ReportCard(None)
    assert plain.windowTitle() == "Report a problem" and plain.kind() == "problem"


def _run(app, thread):
    thread.join(10)
    for _ in range(20):
        app.processEvents()


def test_send_report_from_the_help_menu_and_fall_back_to_the_desktop(app, logs, tmp_path, monkeypatch):
    from sprigcast.app.main_window import MainWindow
    from sprigcast.core.scene import Canvas, Scene, SceneCollection

    col = SceneCollection(canvases=[Canvas(id="horizontal", width=1920, height=1080)],
                          scenes=[Scene(id="h1", name="Main", canvas="horizontal")],
                          active_scenes={"horizontal": "h1"})
    w = MainWindow(col, tmp_path / "scenes.json")
    try:
        help_menu = [a.menu() for a in w.menuBar().actions() if a.text() == "&Help"][0]
        assert "Report a problem…" in [a.text() for a in help_menu.actions()]
        monkeypatch.setattr(problem_report.ReportCard, "exec", lambda self: 1)
        sent = []
        thread = problem_report.ask(w, sender=lambda path, kind, cause: sent.append((kind, path)) or "SC-ABC234",
                                    show_result=False)
        _run(app, thread)
        assert sent and sent[0][0] == "problem"

        desktop = tmp_path / "Desktop"
        monkeypatch.setattr(tester_report, "default_folder", lambda: desktop)

        def offline(path, kind, cause):
            raise OSError("no connection")

        _run(app, problem_report.ask(w, CRASH, sender=offline, show_result=False))
        saved = list(desktop.glob("SprigCast report *.zip"))
        assert len(saved) == 1 and "summary.txt" in zipfile.ZipFile(saved[0]).namelist()
    finally:
        w.close()


def test_the_inbox_config_serves_the_address_the_app_sends_to():
    import tomllib
    from pathlib import Path
    from urllib.parse import urlparse

    config = tomllib.loads((Path(__file__).parents[1] / "integrations/report-inbox/wrangler.toml").read_text())
    hosts = [r["pattern"] for r in config.get("routes", []) if r.get("custom_domain")]
    assert urlparse(problem_report.REPORT_URL).hostname in hosts  # top level, not filed under a table
    assert config["ratelimits"][0]["simple"] == {"limit": 5, "period": 60}
