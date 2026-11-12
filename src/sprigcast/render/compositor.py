"""Draws scenes onto canvases on the GPU with moderngl, and reads the result back."""

from __future__ import annotations

import numpy as np

import moderngl

from sprigcast.core.filters import VideoEffects
from sprigcast.core.scene import Canvas, Scene

NO_EFFECTS = VideoEffects()

VERTEX_SHADER = """
#version 330
uniform vec2 canvas_size;
in vec2 in_pos;      // canvas pixels, origin top-left
in vec2 in_uv;
out vec2 uv;
void main() {
    // Map canvas pixels to clip space so that canvas row 0 lands in framebuffer row 0.
    // A plain fbo.read() then returns rows top to bottom, with no flip needed.
    vec2 ndc = in_pos / canvas_size * 2.0 - 1.0;
    gl_Position = vec4(ndc, 0.0, 1.0);
    uv = in_uv;
}
"""

# Source filters (core/filters.py) are uniforms here. With the defaults the shader just samples the texture.
FRAGMENT_SHADER = """
#version 330
uniform sampler2D tex;
uniform float opacity;
uniform bool premul;        // a part's texture: colors are multiplied by alpha already
uniform vec2 scroll;        // uv offset (already multiplied by time)
uniform bool wrap;
uniform float sharpen;
uniform int key_mode;       // 0 none, 1 chroma, 2 luma
uniform vec3 key_color;
uniform float similarity;
uniform float smoothness;
uniform float spill;
uniform vec3 luma_key;      // min, max, smoothness
uniform float gamma;
uniform float contrast;
uniform float brightness;
uniform float saturation;
uniform float hue;
uniform float fx_opacity;
uniform vec3 tint;
in vec2 uv;
out vec4 color;

const vec3 LUMA = vec3(0.2126, 0.7152, 0.0722);

vec2 cbcr(vec3 rgb) {
    return vec2(dot(rgb, vec3(-0.1146, -0.3854, 0.5)), dot(rgb, vec3(0.5, -0.4542, -0.0458)));
}

vec3 rotate_hue(vec3 c, float a) {
    // Rodrigues rotation around the grey axis.
    vec3 k = vec3(0.57735);
    float cs = cos(a);
    return c * cs + cross(k, c) * sin(a) + k * dot(k, c) * (1.0 - cs);
}

void main() {
    vec2 st = wrap ? fract(uv + scroll) : uv;
    vec4 c = texture(tex, st);
    if (premul && c.a > 0.0) {
        c.rgb /= c.a;
    }
    if (sharpen > 0.0) {
        vec2 px = 1.0 / vec2(textureSize(tex, 0));
        vec3 around = texture(tex, st + vec2(px.x, 0.0)).rgb + texture(tex, st - vec2(px.x, 0.0)).rgb
                    + texture(tex, st + vec2(0.0, px.y)).rgb + texture(tex, st - vec2(0.0, px.y)).rgb;
        c.rgb = clamp(c.rgb + (4.0 * c.rgb - around) * sharpen, 0.0, 1.0);
    }
    if (key_mode == 1) {
        float dist = distance(cbcr(c.rgb), cbcr(key_color));
        float base = dist - similarity;
        c.a *= pow(clamp(base / smoothness, 0.0, 1.0), 1.5);
        float keep = pow(clamp(base / spill, 0.0, 1.0), 1.5);
        c.rgb = mix(vec3(dot(c.rgb, LUMA)), c.rgb, keep);
    } else if (key_mode == 2) {
        float l = dot(c.rgb, LUMA);
        float s = max(luma_key.z, 0.0001);
        c.a *= smoothstep(luma_key.x - s, luma_key.x, l) * (1.0 - smoothstep(luma_key.y, luma_key.y + s, l));
    }
    c.rgb = pow(max(c.rgb, 0.0), vec3(gamma));
    c.rgb = (c.rgb - 0.5) * contrast + 0.5 + brightness;
    c.rgb = mix(vec3(dot(c.rgb, LUMA)), c.rgb, saturation);
    if (hue != 0.0) {
        c.rgb = rotate_hue(c.rgb, hue);
    }
    c.rgb = clamp(c.rgb * tint, 0.0, 1.0);
    color = c * vec4(1.0, 1.0, 1.0, opacity * fx_opacity);
}
"""


# Encoders want NV12 (a full-size luma plane, then half-size interleaved chroma), BT.709 limited range.
# Converting and scaling here, on the GPU, means each output reads back 1.5 bytes per pixel at its own
# size instead of 4 bytes per canvas pixel, and the CPU does no colour conversion or scaling at all.
NV12_VERTEX = """
#version 330
void main() {
    // One triangle that covers the whole target.
    vec2 p = vec2((gl_VertexID << 1) & 2, gl_VertexID & 2);
    gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0);
}
"""
NV12_FRAGMENT = """
#version 330
uniform sampler2D canvas;
uniform vec2 out_size;      // size of the plane being drawn, in pixels
uniform bool chroma;        // false: Y plane, true: interleaved CbCr at half size
uniform vec2 texel;         // one output pixel of the full-size picture, in canvas uv
out vec4 color;

vec3 rgb_at(vec2 uv) {
    return texture(canvas, uv).rgb;  // already composited over black (alpha is dropped)
}

void main() {
    vec2 uv = gl_FragCoord.xy / out_size;  // row 0 of the target = top of the canvas
    if (!chroma) {
        vec3 c = rgb_at(uv);
        color = vec4((16.0 + 219.0 * dot(c, vec3(0.2126, 0.7152, 0.0722))) / 255.0, 0.0, 0.0, 1.0);
    } else {
        // Average the 2x2 block of full-size pixels this chroma sample covers.
        vec3 c = 0.25 * (rgb_at(uv + texel * vec2(-0.5, -0.5)) + rgb_at(uv + texel * vec2(0.5, -0.5))
                       + rgb_at(uv + texel * vec2(-0.5, 0.5)) + rgb_at(uv + texel * vec2(0.5, 0.5)));
        float cb = 128.0 + 224.0 * dot(c, vec3(-0.1146, -0.3854, 0.5));
        float cr = 128.0 + 224.0 * dot(c, vec3(0.5, -0.4542, -0.0458));
        color = vec4(cb / 255.0, cr / 255.0, 0.0, 1.0);
    }
}
"""


class _Nv12Target:
    def __init__(self, ctx: moderngl.Context, size: tuple[int, int]):
        w, h = size
        self.size = size
        self.y = ctx.texture((w, h), 1)
        self.uv = ctx.texture((w // 2, h // 2), 2)
        self.y_fbo = ctx.framebuffer(color_attachments=[self.y])
        self.uv_fbo = ctx.framebuffer(color_attachments=[self.uv])

    def release(self) -> None:
        for obj in (self.y_fbo, self.uv_fbo, self.y, self.uv):
            obj.release()


# How to read each source byte order as RGBA when sampling ("1" = fully opaque).
SWIZZLES = {"RGBA": "RGBA", "BGRA": "BGRA", "RGB": "RGB1", "BGR": "BGR1"}


class _CanvasTarget:
    def __init__(self, ctx: moderngl.Context, size: tuple[int, int]):
        self.ctx = ctx
        self.size = size
        self.color = ctx.texture(self.size, 4)
        self.color.filter = (moderngl.LINEAR, moderngl.LINEAR)
        self.fbo = ctx.framebuffer(color_attachments=[self.color])
        self.aux_color: moderngl.Texture | None = None  # the outgoing scene during a transition
        self.aux_fbo: moderngl.Framebuffer | None = None

    def aux(self) -> moderngl.Framebuffer:
        if self.aux_fbo is None:
            self.aux_color = self.ctx.texture(self.size, 4)
            self.aux_color.filter = (moderngl.NEAREST, moderngl.NEAREST)
            self.aux_fbo = self.ctx.framebuffer(color_attachments=[self.aux_color])
        return self.aux_fbo

    def release(self) -> None:
        for obj in (self.fbo, self.color, self.aux_fbo, self.aux_color):
            if obj is not None:
                obj.release()


class Compositor:
    """Owns the GL context, one framebuffer per canvas, and one texture per source."""

    def __init__(self, ctx: moderngl.Context | None = None):
        self.ctx = ctx or create_context()
        self.program = self.ctx.program(vertex_shader=VERTEX_SHADER, fragment_shader=FRAGMENT_SHADER)
        self.vbo = self.ctx.buffer(reserve=4 * 4 * 4, dynamic=True)  # 4 vertices x (x, y, u, v) float32
        self.vao = self.ctx.vertex_array(self.program, [(self.vbo, "2f 2f", "in_pos", "in_uv")])
        self.textures: dict[str, moderngl.Texture] = {}
        self.targets: dict[str, _CanvasTarget] = {}
        self.nv12_program = self.ctx.program(vertex_shader=NV12_VERTEX, fragment_shader=NV12_FRAGMENT)
        self.nv12_vao = self.ctx.vertex_array(self.nv12_program, [])
        self.nv12_targets: dict[tuple[str, tuple[int, int]], _Nv12Target] = {}
        # (nested-part source id, canvas id of the scene being drawn) -> that part's layout for the canvas
        self.parts: dict[tuple[str, str], moderngl.Texture] = {}
        self.effects: dict[str, VideoEffects] = {}  # source id -> its video filters, set by the engine
        self.time = 0.0  # seconds, drives the Scroll filter
        self._applied: VideoEffects | None = None

    # Sources -------------------------------------------------------------

    def upload(self, source_id: str, frame: np.ndarray, channel_order: str = "RGBA") -> None:
        """Copy a new frame for a source to the GPU, resizing its texture if needed.

        Frames are uploaded in whatever byte order the source produces (RGBA, BGRA from screen capture,
        3-channel BGR from cameras) and swizzled when sampled, so there is no colour conversion on the CPU.
        """
        h, w = frame.shape[:2]
        components = frame.shape[2] if frame.ndim == 3 else 1
        tex = self.textures.get(source_id)
        if tex is None or tex.size != (w, h) or tex.components != components:
            if tex is not None:
                tex.release()
            tex = self.ctx.texture((w, h), components, alignment=1)
            tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
            self.textures[source_id] = tex
        tex.swizzle = SWIZZLES.get(channel_order, "RGBA")
        tex.write(np.ascontiguousarray(frame, dtype=np.uint8))

    def forget(self, source_id: str) -> None:
        tex = self.textures.pop(source_id, None)
        if tex is not None:
            tex.release()

    # Canvases ------------------------------------------------------------

    def _target(self, canvas: Canvas) -> _CanvasTarget:
        return self._target_sized(canvas.id, (canvas.width, canvas.height))

    def _target_sized(self, key: str, size: tuple[int, int]) -> _CanvasTarget:
        target = self.targets.get(key)
        if target is None or target.size != size:
            if target is not None:
                target.release()
            target = _CanvasTarget(self.ctx, size)
            self.targets[key] = target
        return target

    def render_part(self, part_id: str, size: tuple[int, int], scene: Scene,
                    source_sizes: dict[str, tuple[int, int]], fades: dict[str, float] | None = None) -> moderngl.Texture:
        """Draw a part onto its own transparent texture (key "part:<id>"), once per frame.

        Every scene that places the part then samples this texture, so a part used in five
        scenes on two canvases costs one render.
        """
        target = self._target_sized(f"part:{part_id}", size)
        self._draw_scene(target.fbo, size, scene, source_sizes, fades)
        return target.color

    def forget_part(self, part_id: str) -> None:
        target = self.targets.pop(f"part:{part_id}", None)
        if target is not None:
            target.release()

    def render(
        self,
        canvas: Canvas,
        scene: Scene | None,
        source_sizes: dict[str, tuple[int, int]],
        previous: Scene | None = None,
        progress: float = 1.0,
    ) -> None:
        """Draw a scene's visible items back to front onto its canvas.

        Empty canvas is see-through (alpha 0, colour black), so the previews can show a
        checkerboard there while the stream and recordings, which drop alpha, still get black.

        During a fade, `previous` is the outgoing scene and `progress` runs from 0 to 1: the new
        scene is drawn, then the old one on top at opacity 1 - progress.
        """
        target = self._target(canvas)
        size = (canvas.width, canvas.height)
        self._draw_scene(target.fbo, size, scene, source_sizes)
        if previous is None or progress >= 1.0:
            return
        aux = target.aux()
        self._draw_scene(aux, size, previous, source_sizes)
        target.fbo.use()
        self.ctx.viewport = (0, 0, canvas.width, canvas.height)
        self._blend()
        w, h = float(canvas.width), float(canvas.height)
        self.vbo.write(np.array([(0, 0, 0, 0), (w, 0, 1, 0), (0, h, 0, 1), (w, h, 1, 1)], dtype="f4").tobytes())
        self.program["opacity"].value = max(0.0, min(1.0, 1.0 - progress))
        self.program["premul"].value = True  # the outgoing scene was drawn over transparency
        self._apply_effects(NO_EFFECTS, (1, 1))
        target.aux_color.use(location=0)
        self.vao.render(moderngl.TRIANGLE_STRIP, vertices=4)

    def _blend(self) -> None:
        self.ctx.enable(moderngl.BLEND)
        self.ctx.blend_func = (moderngl.SRC_ALPHA, moderngl.ONE_MINUS_SRC_ALPHA, moderngl.ONE, moderngl.ONE_MINUS_SRC_ALPHA)

    def _draw_scene(self, fbo, size: tuple[int, int], scene: Scene | None, source_sizes: dict[str, tuple[int, int]],
                    fades: dict[str, float] | None = None) -> None:
        """fades: source id -> opacity, for items fading in or out (a sprig changing state)."""
        fbo.use()
        self.ctx.viewport = (0, 0, size[0], size[1])
        self.ctx.clear(0.0, 0.0, 0.0, 0.0)
        if scene is None:
            return
        self._blend()
        self.program["canvas_size"].value = (float(size[0]), float(size[1]))
        self.program["tex"].value = 0
        self.program["opacity"].value = 1.0
        self._applied = None
        for item in scene.items:
            part = self.parts.get((item.source, scene.canvas))
            tex = part or self.textures.get(item.source)
            size = part.size if part is not None else source_sizes.get(item.source)
            if not item.visible or tex is None or size is None:
                continue
            quad = item.quad(size)
            if not quad:
                continue
            self.vbo.write(np.array(quad, dtype="f4").tobytes())
            self._apply_effects(self.effects.get(item.source, NO_EFFECTS), tex.size)
            if fades:
                self.program["opacity"].value = fades.get(item.source, 1.0)
            self.program["premul"].value = part is not None
            tex.use(location=0)
            self.vao.render(moderngl.TRIANGLE_STRIP, vertices=4)

    def _apply_effects(self, fx: VideoEffects, size: tuple[int, int]) -> None:
        moving = fx.scroll != (0.0, 0.0)
        if fx is self._applied and not moving:
            return
        self._applied = fx
        p = self.program
        w, h = max(1, size[0]), max(1, size[1])
        p["wrap"].value = moving
        p["scroll"].value = (fx.scroll[0] * self.time / w % 1.0, fx.scroll[1] * self.time / h % 1.0)
        p["sharpen"].value = fx.sharpen
        p["key_mode"].value = fx.key_mode
        p["key_color"].value = fx.key_color
        p["similarity"].value = fx.similarity
        p["smoothness"].value = fx.smoothness
        p["spill"].value = fx.spill
        p["luma_key"].value = fx.luma
        p["gamma"].value = fx.gamma
        p["contrast"].value = fx.contrast
        p["brightness"].value = fx.brightness
        p["saturation"].value = fx.saturation
        p["hue"].value = fx.hue
        p["fx_opacity"].value = fx.opacity
        p["tint"].value = fx.tint

    def read(self, canvas_id: str) -> bytes:
        """Return the canvas as tightly packed RGBA rows, top row first."""
        return self.targets[canvas_id].fbo.read(components=4, alignment=1)

    def read_nv12(self, canvas_id: str, size: tuple[int, int]) -> bytearray:
        """The canvas scaled to size (even width and height) as NV12: Y rows, then CbCr rows, top first."""
        source = self.targets[canvas_id]
        target = self.nv12_targets.get((canvas_id, size))
        if target is None:
            target = self.nv12_targets[(canvas_id, size)] = _Nv12Target(self.ctx, size)
        w, h = size
        self.ctx.disable(moderngl.BLEND)
        source.color.use(0)
        program = self.nv12_program
        program["canvas"].value = 0
        program["texel"].value = (1.0 / w, 1.0 / h)
        for fbo, plane, chroma in ((target.y_fbo, (w, h), False), (target.uv_fbo, (w // 2, h // 2), True)):
            fbo.use()
            self.ctx.viewport = (0, 0, *plane)
            program["out_size"].value = plane
            program["chroma"].value = chroma
            self.nv12_vao.render(moderngl.TRIANGLES, vertices=3)
        # Both planes land in one buffer: one allocation per frame instead of three (Y, CbCr, joined).
        data = bytearray(w * h * 3 // 2)
        target.y_fbo.read_into(data, components=1, alignment=1)
        target.uv_fbo.read_into(data, components=2, alignment=1, write_offset=w * h)
        return data

    def release(self) -> None:
        for tex in self.textures.values():
            tex.release()
        for target in self.targets.values():
            target.release()
        for nv12 in self.nv12_targets.values():
            nv12.release()
        self.vao.release()
        self.vbo.release()
        self.program.release()
        self.ctx.release()


def create_context() -> moderngl.Context:
    """A headless OpenGL 3.3 context. On Windows this uses WGL; on Linux it falls back to EGL."""
    try:
        return moderngl.create_standalone_context(require=330)
    except Exception:
        return moderngl.create_standalone_context(require=330, backend="egl")
