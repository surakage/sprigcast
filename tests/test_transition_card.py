"""The Transitions card's pick is what every switch uses (user 2026-11-25), unless a Between blooms rule names it."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from test_live_layout import _window, app  # noqa: E402,F401


def test_the_card_still_counts_after_undo_and_a_restore(tmp_path, monkeypatch, app):  # noqa: F811
    from sprigcast.core.scene import ANY, Transition, TransitionRule

    w = _window(tmp_path, monkeypatch)
    try:
        shows = [s.id for s in w.collection.scenes if s.kind == "show" and not s.linked_to]
        w.live_transitions.set_kind("slide")
        w._undo_at = 0.0
        w.undo()  # the collection gets a new transition object
        assert w.live_transitions.transition is w.collection.transition
        assert w.transitions_panel.transition is w.collection.transition
        w.live_transitions.set_kind("move")
        assert w.collection.transition.kind == "move"
        assert w.collection.transition_for(shows[0], shows[-1]).kind == "move"
        w.transitions_panel.set_kind("wipe")  # Build's card too, and the two stay together
        assert w.collection.transition.kind == "wipe" and w.live_transitions.transition.kind == "wipe"
        w._restore_state(w.collection.to_text(), saved=False)  # a garden switch puts a whole garden back the same way
        w.live_transitions.set_kind("leaf")
        assert w.collection.transition_for(shows[0], shows[-1]).kind == "leaf"
        w.live_transitions.length.setValue(1200)  # Duration on Live's card (user 2026-11-25: didn't work either)
        assert w.collection.transition_for(shows[0], shows[-1]).duration_ms == 1200
        assert w.transitions_panel.length.value() == 1200
        sent = []
        w.engine.send = sent.append
        w.live_transitions.length.setValue(900)
        assert sent and sent[-1].collection.transition.duration_ms == 900  # the engine gets it at once
        # A Between blooms rule wins for its own switch only.
        w.collection.transition_rules = [TransitionRule(from_scene=ANY, to_scene=shows[-1],
                                                        transition=Transition(kind="cut"))]
        assert w.collection.transition_for(shows[0], shows[-1]).kind == "cut"
        if len(shows) > 1:
            assert w.collection.transition_for(shows[-1], shows[0]).kind == "leaf"
    finally:
        w.close()
