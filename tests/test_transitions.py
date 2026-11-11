from sprigcast.core.scene import Canvas, Scene, SceneCollection, Transition
from sprigcast.render.engine import Engine


def collection(active: str, transition: Transition | None = None) -> SceneCollection:
    return SceneCollection(
        canvases=[Canvas(id="h", width=64, height=36)],
        scenes=[Scene(id="a", name="A", canvas="h"), Scene(id="b", name="B", canvas="h")],
        active_scenes={"h": active},
        transition=transition or Transition(),
    )


def test_switching_scene_starts_a_fade_from_the_old_scene():
    engine = Engine(collection("a"))
    engine._start_transitions(collection("a"), collection("b"))
    outgoing, _began, duration = engine._transitions["h"]
    assert outgoing.id == "a" and duration == 0.3


def test_other_edits_and_cuts_do_not_fade():
    engine = Engine(collection("a"))
    engine._start_transitions(collection("a"), collection("a"))
    assert engine._transitions == {}
    engine._start_transitions(collection("a"), collection("b", Transition(kind="cut")))
    assert engine._transitions == {}
