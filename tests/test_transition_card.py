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
        # A Between blooms rule wins for its own switch only.
        w.collection.transition_rules = [TransitionRule(from_scene=ANY, to_scene=shows[-1],
                                                        transition=Transition(kind="cut"))]
        assert w.collection.transition_for(shows[0], shows[-1]).kind == "cut"
        if len(shows) > 1:
            assert w.collection.transition_for(shows[-1], shows[0]).kind == "leaf"
    finally:
        w.close()
