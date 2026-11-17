import pytest


@pytest.fixture(autouse=True)
def _frames_faster_than_real_time(monkeypatch):
    """Tests hand an encoder a second of frames in a few milliseconds, often before it has opened.

    In the app that queue is kept short while the encoder opens (Recorder.opening_queue); here every
    frame has to arrive, so let the whole queue fill as it does once the encoder runs.
    """
    from sprigcast.output import recorder

    monkeypatch.setattr(recorder.Recorder, "opening_queue", recorder.VIDEO_QUEUE)


@pytest.fixture(autouse=True)
def _sources_closed_after_each_test():
    """Removed sources close on a helper thread (core/retire.py): let it finish and end after each test, so
    no test sees another's closing thread (some count threads)."""
    yield
    from sprigcast.core import retire

    retire.RETIRER.settle()
