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


@pytest.mark.skipif(n.sys.platform == "win32", reason="checks the message elsewhere")
def test_says_why_it_cannot_run():
    with pytest.raises(n.NvencError, match="Windows"):
        n.D3D11Encoder(64, 64, 30)
