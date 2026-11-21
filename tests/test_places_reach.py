"""Offline on Live, a place whose server won't connect is named beside Go live before it's pressed."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtCore import QCoreApplication, QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from sprigcast.core.scene import Output  # noqa: E402


@pytest.fixture
def app():
    app = QApplication.instance() or QApplication([])
    QCoreApplication.setOrganizationName("SprigCastTests")
    QCoreApplication.setApplicationName("PlacesReachTest")
    QSettings().clear()
    yield app
    QSettings().clear()


def _window(tmp_path, monkeypatch):
    import keyring

    from sprigcast.app.main import load_collection
    from sprigcast.app.main_window import MainWindow

    monkeypatch.setattr(keyring, "get_password", lambda svc, ref: None)
    return MainWindow(load_collection(tmp_path / "scenes.json"), tmp_path / "scenes.json")


def _wait(app, w):
    from PySide6.QtTest import QTest

    for _ in range(100):
        QTest.qWait(20)
        if w._reach_job is not None and w._reach_job.done() and not w._reach_poll.isActive():
            return


def test_a_place_that_wont_connect_is_named_beside_go_live(app, tmp_path, monkeypatch):
    from sprigcast.app import doctor
    from sprigcast.output import preflight

    down = {"down.example"}
    monkeypatch.setattr(preflight, "reach", lambda host, port, timeout=1.5: "refused the connection"
                        if host in down else "")
    monkeypatch.setattr(preflight.failing, "__defaults__", (preflight.EACH_S, preflight.ALL_S, preflight.reach))
    from sprigcast.app import destinations

    monkeypatch.setattr(destinations, "problem", lambda *a, **k: None)  # keys aren't what's checked here
    w = _window(tmp_path, monkeypatch)
    try:
        col = w.collection
        col.outputs[:] = [o for o in col.outputs if o.type != "stream"]
        col.outputs += [Output(id="kk", type="stream", canvas="horizontal", service="custom",
                               server="rtmps://down.example/app", enabled=True),
                        Output(id="ok", type="stream", canvas="vertical", service="custom",
                               server="rtmp://fine.example/app", enabled=True),
                        Output(id="off", type="stream", canvas="horizontal", service="custom",
                               server="rtmp://down.example/app", enabled=False)]
        w.show()
        w._set_mode("live")
        w._live_destinations()
        w._check_reach()
        _wait(app, w)
        side = w.live_side
        assert set(w._unreachable) == {"kk"}  # a switched-off place isn't tried
        assert side.unreachable.isVisibleTo(side.header_box) and "won't connect" in side.unreachable.text()
        assert "down.example refused the connection" in side.unreachable.toolTip()
        _tile, _name, note, _switch = side.place_rows["kk"]
        assert "Won't connect" in note.text()
        found = doctor.examine(w._checkup())
        assert any(f.area == "Places" and f.level == "warning" for f in found)
        side.set_live(True)  # live: Destinations shows each place's health instead
        assert not side.unreachable.isVisibleTo(side.header_box)
        side.set_live(False)
        down.clear()
        w._check_reach()
        _wait(app, w)
        assert w._unreachable == {} and not side.unreachable.isVisibleTo(side.header_box)
        w._set_mode("build")  # not on Live: nothing is tried
        w._reach_job = None
        w._check_reach()
        assert w._reach_job is None
    finally:
        w.close()
