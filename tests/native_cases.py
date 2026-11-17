"""Blooms drawn by both engines and compared pixel by pixel (docs/native-engine.md, phase 2).

Each case is drawn by today's compositor (render/compositor.py, moderngl) into tests/native_golden/<name>.png
(`python tests/native_cases.py` remakes them), and by the native engine in tests/test_native_compositor.py.
test_native_golden_matches_python_compositor fails when the Python compositor's drawing changes, so the
pictures, and with them the native engine, follow it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from sprigcast.core.filters import Halo, VideoEffects
from sprigcast.core.looks import SHAPE_IDS
from sprigcast.core.scene import Scene, SceneItem
from sprigcast.render.native_snapshot import SceneDraw

GOLDEN = Path(__file__).parent / "native_golden"
W, H = 160, 90


@dataclass
class Case:
    name: str
    uploads: list  # (source id, array, channel order, layout size or None)
    scenes: list   # SceneDraw, sprigs first; the last one is drawn onto the canvas
    effects: dict = field(default_factory=dict)
    parts: dict = field(default_factory=dict)  # (source id, scene key) -> sprig scene key
    times: tuple = (0.0,)  # drawn at each, the last one compared (barrel rolls keep state)
    # A transition onto the last scene: (outgoing SceneDraw, core/scene.py Transition, progress, wipe picture)
    transition: tuple | None = None


def picture(w: int, h: int, seed: int = 0, alpha: bool = False) -> np.ndarray:
    """A smooth, busy RGBA test picture: gradients, rings and stripes, the same every time."""
    y, x = np.mgrid[0:h, 0:w].astype(np.float32)
    u, v = x / max(1, w - 1), y / max(1, h - 1)
    r = 0.5 + 0.5 * np.sin(6.0 * u + seed)
    g = 0.5 + 0.5 * np.cos(5.0 * v + 2 * seed)
    b = 0.5 + 0.5 * np.sin(18.0 * np.hypot(u - 0.5, v - 0.5) + seed)
    stripes = ((x // 4 + y // 6 + seed) % 2) * 0.25
    img = np.stack([r, g * (0.75 + stripes), b, np.ones_like(r)], -1)
    if alpha:
        img[..., 3] = np.clip(1.4 - 2.2 * np.hypot(u - 0.5, v - 0.5), 0, 1)
    return np.round(np.clip(img, 0, 1) * 255).astype(np.uint8)


def green_screen(w: int, h: int) -> np.ndarray:
    img = picture(w, h, seed=3)
    y, x = np.mgrid[0:h, 0:w]
    outside = np.hypot((x - w / 2) / (w * 0.3), (y - h / 2) / (h * 0.35)) > 1
    img[outside] = (40, 200, 60, 255)
    return img


def i420(rgba: np.ndarray, matrix=(0.2126, 0.0722), full=False, alpha=False) -> np.ndarray:
    """Packs a picture as sources.media.pack_i420 does: Y rows, then U | V rows, then (alpha) A rows."""
    kr, kb = matrix
    rgb = rgba[..., :3].astype(np.float64) / 255
    y = kr * rgb[..., 0] + (1 - kr - kb) * rgb[..., 1] + kb * rgb[..., 2]
    u = (rgb[..., 2] - y) / (2 * (1 - kb))
    v = (rgb[..., 0] - y) / (2 * (1 - kr))
    if full:
        Y, U, V = y * 255, u * 255 + 128, v * 255 + 128
    else:
        Y, U, V = 16 + 219 * y, 128 + 224 * u, 128 + 224 * v
    h, w = Y.shape
    sub = lambda p: p.reshape(h // 2, 2, w // 2, 2).mean(axis=(1, 3))  # noqa: E731
    uv = np.concatenate([sub(U), sub(V)], axis=1)
    rows = [Y, uv] + ([rgba[..., 3].astype(np.float64)] if alpha else [])
    return np.clip(np.round(np.concatenate(rows, axis=0)), 0, 255).astype(np.uint8)


def scene(key: str, items: list[SceneItem], size=(W, H)) -> Scene:
    return Scene(id=key, name=key, canvas=key, items=items)


def bloom(items, fades=None) -> SceneDraw:
    return SceneDraw("h", scene("h", items), (W, H), fades)


def mask_path(folder: Path) -> str:
    """A shape picture with no transparency (white keeps, black cuts), written once."""
    path = folder / "native-mask.png"
    if not path.exists():
        from PIL import Image

        y, x = np.mgrid[0:64, 0:64]
        keep = ((x // 16 + y // 16) % 2 == 0) | (np.hypot(x - 32, y - 32) < 14)
        Image.fromarray(np.where(keep, 255, 0).astype(np.uint8)).convert("RGB").save(path)
    return str(path)


def cases(folder: Path) -> list[Case]:
    pic = picture(64, 40)
    soft = picture(48, 48, seed=1, alpha=True)
    out = []

    bgra = pic[..., [2, 1, 0, 3]]
    premul = soft.astype(np.float32)
    premul[..., :3] *= premul[..., 3:4] / 255
    premul = np.round(premul).astype(np.uint8)[..., [2, 1, 0, 3]]
    out.append(Case("layout", [
        ("a", pic, "RGBA", None), ("b", bgra, "BGRA", None), ("c", np.ascontiguousarray(pic[..., [2, 1, 0]]), "BGR", None),
        ("d", np.ascontiguousarray(pic[..., :3]), "RGB", None), ("e", soft, "RGBA", None), ("f", premul, "pBGRA", None),
        ("g", bgra, "BGRX", None),
    ], [bloom([
        SceneItem(source="a", x=4, y=4, scale=0.75, align="top_left"),
        SceneItem(source="b", x=60, y=6, scale=0.6, rotation=30, align="center"),
        SceneItem(source="c", x=110, y=4, crop=(8, 4, 16, 8), align="top_left"),
        SceneItem(source="d", x=4, y=48, bounds="fit", bounds_size=(50, 36), bounds_align="bottom_right", align="top_left"),
        SceneItem(source="e", x=54, y=40, bounds="fill", bounds_size=(40, 30), align="top_left"),
        SceneItem(source="f", x=96, y=44, scale=0.9, scale_y=0.6, rotation=-15, align="bottom_left"),
        SceneItem(source="g", x=130, y=60, bounds="stretch", bounds_size=(26, 26), align="top_left"),
        SceneItem(source="a", x=150, y=80, visible=False),
    ])]))

    colour = {
        "a": VideoEffects(gamma=0.7, contrast=1.3, brightness=0.05, saturation=1.5, hue=0.6),
        "b": VideoEffects(tint=(1.0, 0.6, 0.3), opacity=0.7),
        "c": VideoEffects(blackout=0.6, sharpen=0.8),
        "d": VideoEffects(saturation=0.0, contrast=0.8),
    }
    out.append(Case("colour", [(k, picture(48, 32, seed=i), "RGBA", None) for i, k in enumerate("abcd")],
                    [bloom([SceneItem(source=k, x=4 + 40 * i, y=10 + 8 * i, align="top_left", scale=0.9)
                            for i, k in enumerate("abcd")])], colour))

    keys = {
        "g": VideoEffects(key_mode=1, key_color=(40 / 255, 200 / 255, 60 / 255), similarity=0.3, smoothness=0.1, spill=0.1),
        "l": VideoEffects(key_mode=2, luma=(0.25, 0.8, 0.1)),
        "c": VideoEffects(key_mode=3, key_color=(40 / 255, 200 / 255, 60 / 255), similarity=0.2, smoothness=0.15),
    }
    out.append(Case("keys", [("bg", picture(160, 90, seed=5), "RGBA", None), ("g", green_screen(64, 48), "RGBA", None),
                             ("l", picture(48, 48, seed=2), "RGBA", None), ("c", green_screen(48, 40), "RGBA", None)],
                    [bloom([SceneItem(source="bg", align="top_left"),
                            SceneItem(source="g", x=4, y=20, align="top_left"),
                            SceneItem(source="l", x=70, y=10, align="top_left"),
                            SceneItem(source="c", x=112, y=40, align="top_left")])], keys))

    detail = picture(128, 80, seed=4)  # drawn in a 64 x 40 layout box: twice the detail
    out.append(Case("blur_pixelate", [("a", pic, "RGBA", None), ("b", pic, "RGBA", None), ("s", detail, "RGBA", (64, 40)),
                                      ("t", detail, "RGBA", (64, 40))],
                    [bloom([SceneItem(source="a", x=2, y=2, align="top_left"), SceneItem(source="b", x=80, y=2, align="top_left"),
                            SceneItem(source="s", x=2, y=48, align="top_left"), SceneItem(source="t", x=80, y=48, align="top_left")])],
                    {"a": VideoEffects(blur=3.0), "b": VideoEffects(pixelate=5.0), "s": VideoEffects(blur=3.0),
                     "t": VideoEffects(pixelate=5.0)}))

    shapes = ["rounded", "circle", "hexagon", "leaf", "heart", "star"]
    shape_fx = {}
    shape_items = []
    for i, name in enumerate(shapes):
        sid = f"s{i}"
        shape_fx[sid] = VideoEffects(shape=SHAPE_IDS[name], roundness=0.25, feather=1.5 * (i % 2), border=3.0 if i % 3 else 0.0,
                                     border_color=(0.9, 0.2, 0.5), stretch=i == 5)
        shape_items.append(SceneItem(source=sid, x=4 + 52 * (i % 3), y=4 + 44 * (i // 3), scale=0.75, align="top_left"))
    out.append(Case("shapes", [(f"s{i}", picture(64, 54, seed=i), "RGBA", None) for i in range(6)],
                    [bloom(shape_items)], shape_fx))

    halos = {
        "r": VideoEffects(shape=SHAPE_IDS["rounded"], roundness=0.2, halos=(
            Halo(1, (0.0, 0.0, 0.0), 0.7, 6.0, (4.0, 4.0)), Halo(2, (1.0, 0.9, 0.2), 1.0, 2.0))),
        "t": VideoEffects(halos=(Halo(1, (0.2, 0.9, 0.5), 0.9, 5.0, (0.0, 0.0), 1.8),)),
        "u": VideoEffects(halos=(Halo(2, (0.9, 0.3, 0.6), 1.0, 2.5),)),
    }
    out.append(Case("halos", [("r", picture(48, 40), "RGBA", None), ("t", soft, "RGBA", None), ("u", soft, "RGBA", None)],
                    [bloom([SceneItem(source="r", x=10, y=20, align="top_left"),
                            SceneItem(source="t", x=64, y=20, rotation=10, align="top_left"),
                            SceneItem(source="u", x=112, y=20, align="top_left")])], halos))

    mask = mask_path(folder)
    out.append(Case("look_mask", [("a", picture(72, 80, seed=6), "RGBA", None), ("b", picture(72, 80, seed=7), "RGBA", None)],
                    [bloom([SceneItem(source="a", x=4, y=4, align="top_left"), SceneItem(source="b", x=84, y=4, align="top_left")])],
                    {"a": VideoEffects(lut="look:teal_orange", lut_amount=0.8),
                     "b": VideoEffects(shape=SHAPE_IDS["image"], mask=mask, lut="look:mono")}))

    video = picture(64, 48, seed=8, alpha=True)
    out.append(Case("video", [("v", i420(video), "I420/709/tv", None),
                              ("w", i420(video, (0.299, 0.114), full=True, alpha=True), "I420A/601/pc", None)],
                    [bloom([SceneItem(source="v", x=4, y=8, scale=1.1, align="top_left"),
                            SceneItem(source="w", x=84, y=8, scale=1.1, rotation=5, align="top_left")])]))

    sprig = SceneDraw("sprig", scene("sprig", [SceneItem(source="a", x=10, y=10, align="top_left"),
                                               SceneItem(source="e", x=40, y=20, align="top_left")]), (120, 80),
                      {"e": 0.5})
    out.append(Case("sprig", [("a", pic, "RGBA", None), ("e", soft, "RGBA", None), ("bg", picture(160, 90, seed=9), "RGBA", None)],
                    [sprig, bloom([SceneItem(source="bg", align="top_left"),
                                   SceneItem(source="p1", x=0, y=0, scale=0.6, align="top_left"),
                                   SceneItem(source="p2", x=110, y=50, scale=0.5, rotation=20, align="center")])],
                    {"p2": VideoEffects(shape=SHAPE_IDS["circle"], border=2.0)},
                    {("p1", "h"): "sprig", ("p2", "h"): "sprig"}))

    out.append(Case("scroll_spin", [("a", pic, "RGBA", None), ("b", pic, "RGBA", None)],
                    [bloom([SceneItem(source="a", x=4, y=10, align="top_left"),
                            SceneItem(source="b", x=100, y=40, align="center")])],
                    {"a": VideoEffects(scroll=(20.0, -10.0)), "b": VideoEffects(spin=(1.0, 2.0, 1))},
                    times=(10.0, 10.5)))

    # Transitions: a busy bloom changing to one that is partly see-through (the outgoing one shows through it).
    from sprigcast.core.scene import Transition

    old = SceneDraw("from", scene("from", [SceneItem(source="a", x=0, y=0, scale=2.6, align="top_left"),
                                           SceneItem(source="e", x=100, y=20, align="top_left")]), (W, H))
    new = [SceneItem(source="b", x=20, y=10, scale=1.5, align="top_left"),
           SceneItem(source="e", x=8, y=30, scale=1.2, align="top_left")]
    y, x = np.mgrid[0:54, 0:96]
    wipe = np.round(np.clip(np.hypot(x - 30, y - 30) / 70, 0, 1) * 255).astype(np.uint8)
    wipe = np.repeat(wipe[..., None], 3, axis=-1)
    for name, transition, progress, map_ in [
        ("t_fade", Transition(kind="fade"), 0.4, None),
        ("t_slide", Transition(kind="slide", direction="left"), 0.35, None),
        ("t_swipe", Transition(kind="swipe", direction="up"), 0.6, None),
        ("t_wipe", Transition(kind="wipe", direction="right", softness=0.2), 0.5, None),
        ("t_wipe_map", Transition(kind="wipe", softness=0.3), 0.45, wipe),
        ("t_leaf", Transition(kind="leaf"), 0.4, None),
        ("t_bloom", Transition(kind="bloom", softness=0.25), 0.55, None),
    ]:
        out.append(Case(name, [("a", pic, "RGBA", None), ("b", picture(64, 40, seed=5), "RGBA", None),
                               ("e", soft, "RGBA", None)],
                        [bloom(new)], transition=(old, transition, progress, map_)))
    return out


def layout_size(array: np.ndarray, order: str, size) -> tuple[int, int]:
    if size:
        return tuple(size)
    h, w = array.shape[:2]
    if order.startswith("I420A"):
        return w, h * 2 // 5
    if order.startswith("I420"):
        return w, h * 2 // 3
    return w, h


def draw_python(compositor, case: Case) -> np.ndarray:
    """The case drawn by today's compositor, as H x W x 4 RGBA."""
    from sprigcast.core.scene import Canvas

    for source_id, array, order, size in case.uploads:
        compositor.upload(source_id, array, order, size)
    sizes = {s: layout_size(a, o, z) for s, a, o, z in case.uploads}
    compositor.effects = dict(case.effects)
    *sprigs, main = case.scenes
    for t in case.times:
        compositor.time = t
        textures = {d.key: compositor.render_part(d.key, d.size, d.scene, sizes, d.fades) for d in sprigs}
        compositor.parts = {k: textures[v] for k, v in case.parts.items()}
        for (source_id, _), part_key in case.parts.items():
            sizes.setdefault(source_id, textures[part_key].size)
        canvas = Canvas(id=main.key, width=main.size[0], height=main.size[1])
        if case.transition is None:
            compositor.render(canvas, main.scene, sizes)
        else:
            old, transition, progress, wipe = case.transition
            if wipe is not None:
                compositor.upload("wipe", wipe, "RGB")
            compositor.render(canvas, main.scene, sizes, previous=old.scene, progress=progress,
                              transition=transition, wipe_map="wipe" if wipe is not None else None)
    data = np.frombuffer(compositor.read(main.key), dtype=np.uint8).reshape(main.size[1], main.size[0], 4).copy()
    for source_id, *_ in case.uploads:
        compositor.forget(source_id)
    if case.transition is not None:
        if case.transition[3] is not None:
            compositor.forget("wipe")
        compositor.end_transition(main.key)
    compositor.parts = {}
    compositor.effects = {}
    compositor._spins.clear()
    return data


def draw_native(device, case: Case) -> np.ndarray:
    """The case drawn by the native engine, as H x W x 4 RGBA."""
    from sprigcast import sprigcore
    from sprigcast.render import native_snapshot

    for source_id, array, order, size in case.uploads:
        device.upload(source_id, array, order, size)
    old = case.transition[0] if case.transition else None
    built = native_snapshot.build(case.scenes + ([old] if old else []), case.effects, case.parts)
    native_snapshot.hand_over(device, built, native_snapshot.Resources())
    main = case.scenes[-1]
    with sprigcore.Canvas(device, main.size[0], main.size[1]) as canvas:
        for t in case.times:
            if case.transition is None:
                canvas.render(main.key, t)
                continue
            _old, transition, progress, wipe = case.transition
            if wipe is not None:
                rgba = np.concatenate([wipe, np.full(wipe.shape[:2] + (1,), 255, np.uint8)], axis=-1)
                device.set_mask("wipe", rgba, True)
            spec = sprigcore.transition_spec(transition, "wipe" if wipe is not None else None)
            canvas.render_transition(old.key, main.key, spec, progress, t)
        bgra = np.frombuffer(canvas.read_bgra(), dtype=np.uint8).reshape(main.size[1], main.size[0], 4)
    for source_id, *_ in case.uploads:
        device.forget(source_id)
    return np.ascontiguousarray(bgra[..., [2, 1, 0, 3]])


def compare(got: np.ndarray, want: np.ndarray) -> dict:
    """How far two drawings are apart, per channel value (0..255)."""
    diff = np.abs(got.astype(int) - want.astype(int))
    return {"mean": float(diff.mean()), "p99": float(np.percentile(diff, 99)), "max": int(diff.max()),
            "over_16": float((diff.max(axis=-1) > 16).mean())}


def close_enough(stats: dict) -> bool:
    """Two engines, two rasterizers: sampling rounds a little differently and edge pixels can differ, but a
    real difference (a wrong filter, a misplaced item) moves the mean and the share of far-off pixels."""
    return stats["mean"] <= 1.0 and stats["p99"] <= 12 and stats["over_16"] <= 0.01


if __name__ == "__main__":  # remake the pictures from today's compositor
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from PIL import Image

    from sprigcast.render.compositor import Compositor

    GOLDEN.mkdir(exist_ok=True)
    comp = Compositor()
    for case in cases(GOLDEN):
        Image.fromarray(draw_python(comp, case), "RGBA").save(GOLDEN / f"{case.name}.png")
        print("wrote", case.name)
    comp.release()
    _ = math
