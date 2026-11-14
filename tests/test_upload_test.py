import time

import pytest

av = pytest.importorskip("av")

from sprigcast.output import upload_test  # noqa: E402


def test_a_fast_connection_reaches_the_ceiling(tmp_path):
    path = tmp_path / "test.flv"
    kbps = upload_test.stream_test("unused", seconds=3.0, ceiling_kbps=4000,
                                   open_container=lambda u: av.open(str(path), "w", format="flv"))
    assert kbps is not None and 2500 < kbps < 6000  # about the ceiling: nothing held it back
    assert path.stat().st_size > 0


class _Slow:
    """A connection that takes 1 s per 250 kB: a 2 Mbps upload."""

    def __init__(self, path):
        self.inner = av.open(str(path), "w", format="flv")

    def add_stream(self, *a, **kw):
        return self.inner.add_stream(*a, **kw)

    def mux(self, packet):
        time.sleep(packet.size / 250_000)
        self.inner.mux(packet)

    def close(self):
        self.inner.close()


def test_a_slow_connection_is_measured(tmp_path):
    kbps = upload_test.stream_test("unused", seconds=4.0, ceiling_kbps=8000,
                                   open_container=lambda u: _Slow(tmp_path / "slow.flv"))
    assert kbps is not None and 1500 < kbps < 2800


def test_no_connection_gives_none_and_never_logs_the_key(caplog):
    def refuse(url):
        raise OSError(f"Connection refused: {url}")

    url = "rtmp://live.twitch.tv/app/live_123_SECRET?bandwidthtest=true"
    assert upload_test.stream_test(url, seconds=1.0, open_container=refuse) is None
    assert "SECRET" not in caplog.text


def test_twitch_key_comes_from_a_twitch_output(monkeypatch):
    from sprigcast.core.scene import Canvas, Output, SceneCollection
    from sprigcast.output import keys

    monkeypatch.setattr(keys, "get_key", lambda ref: {"tw": "live_abc"}.get(ref))
    c = SceneCollection(canvases=[Canvas(id="horizontal", width=1920, height=1080)],
                        outputs=[Output(id="yt", type="stream", service="youtube", canvas="horizontal", key_ref="yt"),
                                 Output(id="tw", type="stream", service="twitch", canvas="horizontal", key_ref="tw")])
    assert upload_test.twitch_key(c) == "live_abc"
    c.outputs.pop()
    assert upload_test.twitch_key(c) is None


def test_the_wizard_check_prefers_twitch(monkeypatch):
    from sprigcast.app import optimize

    monkeypatch.setattr(upload_test, "twitch_key", lambda c: "live_abc")
    monkeypatch.setattr(upload_test, "twitch_upload", lambda key: 9000)
    p = optimize.PcProfile()
    p.extra["collection"] = object()
    assert optimize._check_upload(p) == "9.0 Mbps (Twitch test stream)" and p.upload_kbps == 9000
    monkeypatch.setattr(upload_test, "twitch_upload", lambda key: None)  # Twitch unreachable: Cloudflare
    monkeypatch.setattr(optimize, "upload_speed", lambda: 7000)
    assert optimize._check_upload(p) == "7.0 Mbps" and p.extra["upload_source"] == "Cloudflare"
