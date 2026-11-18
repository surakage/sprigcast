import threading

from sprigcast.core import thread_names


def test_threads_tell_windows_their_name_and_when_they_end(monkeypatch):
    original = threading.Thread._bootstrap_inner
    monkeypatch.setattr(thread_names, "_installed", False)
    told = []
    try:
        assert thread_names.install(describe=lambda handle, name: told.append(name))
        thread = threading.Thread(target=lambda: told.append("working"), name="replay-arm")
        thread.start()
        thread.join()
        assert told[-3:] == ["replay-arm", "working", "replay-arm" + thread_names.ENDING]
        assert thread_names.install()  # once only
    finally:
        threading.Thread._bootstrap_inner = original
        thread_names._describe = None


def test_not_on_other_systems(monkeypatch):
    monkeypatch.setattr(thread_names, "_installed", False)
    assert not thread_names.install(platform="linux")
