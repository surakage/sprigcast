"""The ctypes structures must match nvEncodeAPI.h (SDK 12.1) byte for byte; sizes from the C header on x64."""

import ctypes

import pytest

from sprigcast.output import nvenc_d3d11 as n


def test_structures_match_the_sdk_header():
    assert ctypes.sizeof(n.OpenSessionParams) == 1552
    assert ctypes.sizeof(n.InitializeParams) == 1808
    assert ctypes.sizeof(n.CreateInputBuffer) == 776
    assert ctypes.sizeof(n.CreateBitstreamBuffer) == 776
    assert ctypes.sizeof(n.LockInputBuffer) == 1544
    assert ctypes.sizeof(n.LockBitstream) == 1552
    assert ctypes.sizeof(n.PicParams) >= 3360
    assert n.InitializeParams.tuningInfo.offset == 136 and n.InitializeParams.encodeConfig.offset == 88
    assert n.LockBitstream.bitstreamBufferPtr.offset == 56 and n.LockBitstream.bitstreamSizeInBytes.offset == 36
    assert n.PicParamsHead.pictureType.offset == 72
    # function pointer positions: offset in NV_ENCODE_API_FUNCTION_LIST = 8 + 8 * index
    assert [8 + 8 * i for i in (n.F_INITIALIZE, n.F_DESTROY_ENCODER, n.F_OPEN_SESSION_EX, n.F_LAST_ERROR)] == [
        96, 224, 240, 304]


def test_versions():
    assert n._struct_version(6, True) == (12 | 1 << 24 | 6 << 16 | 7 << 28 | 1 << 31)
    assert n._struct_version(7, True, (13, 0)) == (13 | 0 << 24 | 7 << 16 | 7 << 28 | 1 << 31)
    # NV_ENC_*_VER from nvEncodeAPI.h 12.1 / 12.2 / 13.0
    assert n.API_LAYOUTS[(12, 1)] == {"init": 6, "config": 8, "preset": 4, "pic": 6, "lock_bitstream": 1,
                                      "create_input": 1, "reconfigure": 1, "init_size": 1808}
    assert n.API_LAYOUTS[(13, 0)] == n.API_LAYOUTS[(12, 2)] == {
        "init": 7, "config": 9, "preset": 5, "pic": 7, "lock_bitstream": 2, "create_input": 2,
        "reconfigure": 2, "init_size": 1800}
    assert 8 + 8 * n.F_RECONFIGURE == 264  # nvEncReconfigureEncoder in NV_ENCODE_API_FUNCTION_LIST
    assert list(n.API_LAYOUTS) == sorted(n.API_LAYOUTS, reverse=True)  # newest first


def test_speaks_the_newest_version_and_falls_back_on_invalid_version(monkeypatch):
    """FFmpeg 8 speaks NVENC 13.0; a session open in another version fails until we switch (soak test)."""
    calls = []
    process_speaks = {"api": (13, 0)}  # what FFmpeg's CreateInstance left behind

    class Lib:
        @staticmethod
        def NvEncodeAPICreateInstance(functions):  # noqa: N802
            calls.append(("create", functions._obj.version & 0xFF, (functions._obj.version >> 24) & 0xFF))
            return 0

    monkeypatch.setattr(n, "_lib", Lib)
    monkeypatch.setattr(n, "_supported", (13, 1))
    monkeypatch.setattr(n, "_api", None)
    monkeypatch.setattr(n, "_api_for", None)
    monkeypatch.setattr(n, "_preferred", None)

    def fake_fn(index, *argtypes):
        def call(params, out):
            p = n.OpenSessionParams.from_address(params)
            api = (p.apiVersion & 0xFF, p.apiVersion >> 24)
            return 0 if api == process_speaks["api"] else 15  # 15 = INVALID_VERSION
        return call

    monkeypatch.setattr(n, "_fn", fake_fn)
    enc = n.D3D11Encoder.__new__(n.D3D11Encoder)
    enc.encoder, enc.device = ctypes.c_void_p(), ctypes.c_void_p()
    assert n.speakable() == [(13, 0), (12, 2), (12, 1)]
    enc._open_session()
    assert enc.api == (13, 0)
    process_speaks["api"] = (12, 1)  # an older FFmpeg
    enc._open_session()
    assert enc.api == (12, 1) and n._api_for == (12, 1)
    assert n.speakable()[0] == (12, 1)  # remembered for next time
    assert enc._v("pic", True) == n._struct_version(6, True, (12, 1))


@pytest.mark.skipif(n.sys.platform == "win32", reason="checks the message elsewhere")
def test_says_why_it_cannot_run():
    with pytest.raises(n.NvencError, match="Windows"):
        n.D3D11Encoder(64, 64, 30)
