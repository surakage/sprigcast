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
