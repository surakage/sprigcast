"""Control API app.stats: the native engine's frames and the canvases it really drew, so a tester can see a still
bloom kept instead of redrawn (Dew, 2026-11-23)."""

from types import SimpleNamespace


def test_app_stats_has_the_engines_ticks_and_canvas_draws(tmp_path):
    from sprigcast.app.control_server import WindowHost

    engine = SimpleNamespace(stats=lambda: {"ticks": 600, "canvas_draws": 12})
    window = SimpleNamespace(engine=SimpleNamespace(_native=SimpleNamespace(engine=engine, failed=False)),
                             streaming=None, _recordings_folder=lambda: tmp_path)
    host = WindowHost.__new__(WindowHost)
    host.w = window
    out = host.stats()
    assert out["engineTicks"] == 600 and out["canvasDraws"] == 12
    window.engine._native = None  # Python's engine draws: nothing to say
    assert "canvasDraws" not in host.stats()


def test_app_stats_names_the_sources_that_keep_a_bloom_being_redrawn(tmp_path):
    from sprigcast import sprigcore
    from sprigcast.app.control_server import WindowHost

    taken = {sprigcore.key("clock"): 40, sprigcore.key("alerts"): 3, 12345: 2}
    engine = SimpleNamespace(stats=lambda: {"ticks": 600, "canvas_draws": 45}, redraws_take=lambda: taken)
    sources = [SimpleNamespace(id="clock", name="Clock"), SimpleNamespace(id="alerts", name="Alerts")]
    window = SimpleNamespace(engine=SimpleNamespace(_native=SimpleNamespace(engine=engine, failed=False)),
                             collection=SimpleNamespace(sources=sources),
                             streaming=None, _recordings_folder=lambda: tmp_path)
    host = WindowHost.__new__(WindowHost)
    host.w = window
    assert host.stats()["redrawsBy"] == {"Clock": 40, "Alerts": 3, "Other": 2}
