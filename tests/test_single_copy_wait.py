"""A launch that finds SprigCast still starting waits for it instead of opening a second copy (tester PC 2026-11-22)."""

from sprigcast.app import single_instance


def test_waits_until_the_starting_copy_answers():
    answers = iter([False, False, False, True])  # listening only from the fourth try
    waited = []
    assert single_instance.wait_for_first_copy(lambda: next(answers), lambda ms: waited.append(ms) or False)
    assert waited == [500, 500, 500]


def test_opens_when_the_other_copy_closes_meanwhile():
    # Settings' Restart now: the old copy stops listening, then lets go of the mutex as it ends
    assert not single_instance.wait_for_first_copy(lambda: False, lambda ms: True)


def test_opens_anyway_when_the_other_copy_never_answers():
    assert not single_instance.wait_for_first_copy(lambda: False, lambda ms: False, longest_s=0.0)


def test_claim_is_always_first_off_windows(monkeypatch):
    monkeypatch.setattr(single_instance.sys, "platform", "linux")
    assert single_instance.claim()


def test_waiting_is_said_while_the_other_copy_starts():
    answers = iter([False, False, True])
    said = []
    assert single_instance.wait_for_first_copy(lambda: next(answers), lambda ms: False, waiting=lambda: said.append(1))
    assert said == [1, 1]


def test_the_cards_fit_their_words():
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from sprigcast.app.starting import WIDTH, StartingCard

    QApplication.instance() or QApplication([])
    plain = StartingCard()
    long = StartingCard(None, "SprigCast is still starting", "It opens by itself. Please wait")
    told = StartingCard(None, "SprigCast is already open", "It only runs once. Its window is in front.", dots=False,
                        ok=True)
    assert plain.width() == WIDTH and long.width() >= WIDTH
    assert told.ok_button is not None and told.ok_button.geometry().right() < told.width()
    for card in (plain, long, told):
        card.done()
