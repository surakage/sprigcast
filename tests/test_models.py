"""Downloading plugin models on first use (sprigcast/models.py)."""

import hashlib
import io
import threading
import urllib.error

import pytest

from sprigcast import models

DATA = b"model bytes " * 1000
SHA = hashlib.sha256(DATA).hexdigest()


class Reply(io.BytesIO):
    headers = {"Content-Length": str(len(DATA))}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture(autouse=True)
def where(tmp_path):
    models.set_folder(tmp_path)
    yield tmp_path
    models.set_folder(None)


def opener(calls):
    def open_(request, timeout=0):
        calls.append(request.full_url)
        return Reply(DATA)
    return open_


def test_downloads_once_into_a_subfolder(where):
    calls = []
    seen = []
    path = models.ensure("whisper-base/model.bin", "https://x/m", SHA, progress=lambda d, t: seen.append((d, t)),
                         opener=opener(calls))
    assert path == where / "whisper-base" / "model.bin"
    assert path.read_bytes() == DATA
    assert seen[-1] == (len(DATA), len(DATA))
    assert models.is_ready("whisper-base/model.bin", SHA)
    models.ensure("whisper-base/model.bin", "https://x/m", SHA, opener=opener(calls))
    assert len(calls) == 1  # already here: no second download


def test_wrong_hash_keeps_nothing(where):
    with pytest.raises(models.ModelError):
        models.ensure("a.onnx", "https://x/m", "0" * 64, opener=opener([]))
    assert not (where / "a.onnx").exists()
    assert not list(where.glob("*.part"))


def test_cancel_and_network_errors(where):
    stop = threading.Event()
    stop.set()
    with pytest.raises(InterruptedError):
        models.ensure("a.onnx", "https://x/m", SHA, cancel=stop, opener=opener([]))

    def offline(request, timeout=0):
        raise urllib.error.URLError("no internet")

    with pytest.raises(models.ModelError):
        models.ensure("a.onnx", "https://x/m", SHA, opener=offline)
    assert not models.is_ready("a.onnx")


def test_names_cannot_leave_the_folder():
    for bad in ("../x.onnx", "/abs.onnx", "a/../../b", "", "C:/x"):
        with pytest.raises(ValueError):
            models.path_for(bad)


def test_a_changed_file_is_checked_again(where):
    models.ensure("a.onnx", "https://x/m", SHA, opener=opener([]))
    (where / "a.onnx").write_bytes(b"broken")
    assert not models.is_ready("a.onnx", SHA)
    models.remove("a.onnx")
    assert not (where / "a.onnx").exists()


def test_two_callers_share_one_download(where):
    calls = []
    gate = threading.Event()

    def slow(request, timeout=0):
        calls.append(1)
        gate.wait(2)
        return Reply(DATA)

    out = []
    threads = [threading.Thread(target=lambda: out.append(models.ensure("a.onnx", "https://x/m", SHA, opener=slow)))
               for _ in range(2)]
    for t in threads:
        t.start()
    gate.set()
    for t in threads:
        t.join(5)
    assert len(out) == 2 and len(calls) == 1
