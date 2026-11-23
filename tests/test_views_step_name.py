"""The late-frame line names the views step after what it drew: with the camera off it never says "virtual
camera" (Mikedog151, 2026-11-22: the camera was off, yet the lines blamed it)."""

from sprigcast.render.engine import views_step


def test_the_views_step_is_named_after_what_it_drew():
    assert views_step([], False) == "after the canvases (nothing to draw)"
    assert views_step(["camera"], False) == "virtual camera"
    assert views_step(["projector1", "screenshot"], False) == "projectors and previews"
    assert views_step(["camera", "projector1"], True) == "virtual camera and projectors and previews and sprig editor"
