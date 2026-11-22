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
