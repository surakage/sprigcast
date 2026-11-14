"""Draws scenes onto canvases on the GPU with moderngl, and reads the result back."""

from __future__ import annotations

import math

import moderngl
import numpy as np

from sprigcast.core import looks
from sprigcast.core.filters import VideoEffects
from sprigcast.core.looks import SHAPE_IDS
from sprigcast.core.scene import Canvas, Scene, Transition

NO_EFFECTS = VideoEffects()

VERTEX_SHADER = """
#version 330
uniform vec2 canvas_size;
in vec2 in_pos;      // canvas pixels, origin top-left
in vec2 in_uv;
out vec2 uv;
out vec2 local;      // 0..1 across the drawn quad (the corners come top-left, top-right, bottom-left, bottom-right)
void main() {
    local = vec2(float(gl_VertexID & 1), float(gl_VertexID >> 1));
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
uniform sampler3D lut;      // colour look, x = red
uniform bool use_lut;
uniform float lut_amount;
uniform float lut_size;
uniform int shape;          // 0 none, 1 rounded, 2 circle, 3 hexagon, 4 leaf, 5 heart, 6 star, 7 image
uniform vec2 quad_size;     // the quad's size on the canvas, in pixels
uniform float roundness;
uniform bool stretch;
uniform float feather;
uniform float border;
uniform vec3 border_color;
uniform sampler2D mask;
uniform bool mask_luma;     // the shape picture has no transparency: white keeps, black cuts
in vec2 uv;
in vec2 local;
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

float dot2(vec2 v) { return dot(v, v); }

// Signed distances (negative inside), after Inigo Quilez. y points down here, so some shapes flip it.
float sd_box(vec2 p, vec2 b, float r) {
    vec2 q = abs(p) - b + r;
    return length(max(q, 0.0)) + min(max(q.x, q.y), 0.0) - r;
}

float sd_hexagon(vec2 p, float r) {
    const vec3 k = vec3(-0.866025404, 0.5, 0.577350269);
    p = abs(p.yx);
    p -= 2.0 * min(dot(k.xy, p), 0.0) * k.xy;
    p -= vec2(clamp(p.x, -k.z * r, k.z * r), r);
    return length(p) * sign(p.y);
}

float sd_vesica(vec2 p, float r, float d) {
    p = abs(p);
    float b = sqrt(r * r - d * d);
    return ((p.y - b) * d > p.x * b) ? length(p - vec2(0.0, b)) : length(p - vec2(-d, 0.0)) - r;
}

float sd_heart(vec2 p) {
    p.x = abs(p.x);
    if (p.y + p.x > 1.0) return sqrt(dot2(p - vec2(0.25, 0.75))) - sqrt(2.0) / 4.0;
    return sqrt(min(dot2(p - vec2(0.0, 1.0)), dot2(p - 0.5 * max(p.x + p.y, 0.0)))) * sign(p.x - p.y);
}

float sd_star(vec2 p, float r, float rf) {
    const vec2 k1 = vec2(0.809016994375, -0.587785252292);
    const vec2 k2 = vec2(-k1.x, k1.y);
    p.x = abs(p.x);
    p -= 2.0 * max(dot(k1, p), 0.0) * k1;
    p -= 2.0 * max(dot(k2, p), 0.0) * k2;
    p.x = abs(p.x);
    p.y -= r;
    vec2 ba = rf * vec2(-k1.y, k1.x) - vec2(0.0, 1.0);
    float h = clamp(dot(p, ba) / dot(ba, ba), 0.0, r);
    return length(p - ba * h) * sign(p.y * ba.x - p.x * ba.y);
}

// Distance in canvas pixels from the shape's edge.
float shape_distance() {
    vec2 size = max(quad_size, vec2(1.0));
    float side = min(size.x, size.y);
    if (shape == 1) {
        return sd_box((local - 0.5) * size, size * 0.5, roundness * side);
    }
    // The other shapes sit in the middle at the short side's size, or stretch to fill.
    vec2 p = stretch ? (local - 0.5) * side : (local - 0.5) * size;
    float r = side * 0.5;
    float d;
    if (shape == 2) {
        d = length(p) - r;
    } else if (shape == 3) {
        d = sd_hexagon(p, r * 0.866);
    } else if (shape == 4) {
        vec2 q = vec2(p.x + p.y, p.y - p.x) * 0.70710678;   // turned 45 degrees, tip to the top right
        d = sd_vesica(q, r * 1.65, r * 1.0);
    } else if (shape == 5) {
        float s = r * 1.62;
        d = sd_heart(vec2(p.x, -p.y) / s + vec2(0.0, 0.53)) * s;
    } else {
        d = sd_star(vec2(p.x, -p.y + r * 0.1), r, 0.45);
    }
    if (stretch) {
        d *= min(size.x, size.y) / side;   // a rough pixel scale, enough for edges and borders
    }
    return d;
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
    if (use_lut) {
        vec3 at = c.rgb * ((lut_size - 1.0) / lut_size) + 0.5 / lut_size;   // sample texel centres
        c.rgb = mix(c.rgb, texture(lut, at).rgb, lut_amount);
    }
    if (shape == 7) {
        vec4 m = texture(mask, local);
        c.a *= mask_luma ? dot(m.rgb, LUMA) : m.a;
    } else if (shape > 0) {
        float d = shape_distance();
        const float aa = 0.75;
        if (border > 0.0) {
            float ring = smoothstep(-border - aa, -border + aa, d);   // 1 within `border` pixels of the edge
            c.rgb = mix(c.rgb, border_color, ring);
            c.a = mix(c.a, 1.0, ring);
        }
        c.a *= 1.0 - smoothstep(-feather - aa, aa, d);
    }
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
SCALE_MODES = {"bilinear": 0, "area": 1, "bicubic": 2, "lanczos": 3}
NV12_FRAGMENT = """
#version 330
uniform sampler2D canvas;
uniform vec2 out_size;      // size of the plane being drawn, in pixels
uniform bool chroma;        // false: Y plane, true: interleaved CbCr at half size
uniform vec2 texel;         // one output pixel of the full-size picture, in canvas uv
uniform int scale_mode;     // scaling down: 0 bilinear, 1 area, 2 bicubic, 3 lanczos (Settings → Video)
out vec4 color;

vec3 rgb_at(vec2 uv) {
    return texture(canvas, uv).rgb;  // already composited over black (alpha is dropped)
}

float cubic(float x) {  // Catmull-Rom: sharp, no ringing to speak of
    x = abs(x);
    if (x < 1.0) return 1.5 * x * x * x - 2.5 * x * x + 1.0;
    if (x < 2.0) return -0.5 * x * x * x + 2.5 * x * x - 4.0 * x + 2.0;
    return 0.0;
}

float lanczos(float x) {  // a = 3
    x = abs(x);
    if (x < 1e-5) return 1.0;
    if (x >= 3.0) return 0.0;
    float px = 3.14159265 * x;
    return 3.0 * sin(px) * sin(px / 3.0) / (px * px);
}

// The full-size picture at uv, scaled down with the chosen filter.
vec3 scaled_at(vec2 uv) {
    vec2 src = vec2(textureSize(canvas, 0));
    if (scale_mode == 0 || all(lessThan(abs(src * texel - vec2(1.0)), vec2(0.001)))) {
        return rgb_at(uv);  // bilinear, or no scaling at all
    }
    if (scale_mode == 1) {  // area: average the footprint of the output pixel
        vec2 q = texel * 0.25;
        return 0.25 * (rgb_at(uv + vec2(-q.x, -q.y)) + rgb_at(uv + vec2(q.x, -q.y))
                     + rgb_at(uv + vec2(-q.x, q.y)) + rgb_at(uv + vec2(q.x, q.y)));
    }
    vec2 p = uv * src - 0.5;
    vec2 base = floor(p);
    vec2 f = p - base;
    int r = scale_mode == 2 ? 2 : 3;
    vec3 sum = vec3(0.0);
    float total = 0.0;
    for (int j = -r + 1; j <= r; j++) {
        for (int i = -r + 1; i <= r; i++) {
            vec2 d = vec2(float(i), float(j)) - f;
            float w = scale_mode == 2 ? cubic(d.x) * cubic(d.y) : lanczos(d.x) * lanczos(d.y);
            sum += w * texelFetch(canvas, ivec2(clamp(base + vec2(i, j), vec2(0.0), src - 1.0)), 0).rgb;
            total += w;
        }
    }
    return clamp(sum / total, 0.0, 1.0);
}

void main() {
    vec2 uv = gl_FragCoord.xy / out_size;  // row 0 of the target = top of the canvas
    if (!chroma) {
        vec3 c = scaled_at(uv);
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


SCALE_FRAGMENT = """
#version 330
uniform sampler2D canvas;
uniform vec2 out_size;
out vec4 color;

void main() {
    // Four bilinear taps around each output pixel: a clean downscale up to about 4x, alpha included.
    vec2 uv = gl_FragCoord.xy / out_size;
    vec2 q = 0.25 / out_size;
    color = 0.25 * (texture(canvas, uv + vec2(-q.x, -q.y)) + texture(canvas, uv + vec2(q.x, -q.y))
                  + texture(canvas, uv + vec2(-q.x, q.y)) + texture(canvas, uv + vec2(q.x, q.y)));
}
"""


# Transitions (core/scene.py Transition). Both scenes are drawn to their own textures first (colours
# already multiplied by alpha), then this pass mixes them into the canvas. uv row 0 = top of the canvas.
TRANSITION_FRAGMENT = """
#version 330
uniform sampler2D old_scene;
uniform sampler2D new_scene;
uniform sampler2D overlay;     // stinger frame (straight alpha)
uniform sampler2D wipe_map;    // wipe: black and white image, dark changes first
uniform vec2 out_size;
uniform int mode;              // 1 fade, 2 slide, 3 swipe, 4 wipe, 5 leaf, 6 bloom, 7 stinger
uniform float progress;        // 0 .. 1
uniform vec2 dir;              // where slide/swipe/wipe move to
uniform float softness;
uniform bool use_map;
uniform vec4 overlay_rect;     // stinger: texture uv = xy + canvas uv * zw (fills the canvas)
uniform vec2 matte_offset;     // stinger: where the matte half starts, in texture uv; (0, 0) = no matte
uniform bool show_overlay;
uniform bool cut;              // stinger: past the cut point
uniform float level_max;       // leaf and bloom: their level at the furthest edge point (see shape_reach)
out vec4 color;

const vec3 LUMA = vec3(0.2126, 0.7152, 0.0722);
const float LEAF_TURN = 0.785;
const float LEAF_SPIN = 0.5;
const float BLOOM_SPIN = 1.2;

vec4 at(sampler2D t, vec2 uv) {
    if (uv.x < 0.0 || uv.y < 0.0 || uv.x > 1.0 || uv.y > 1.0) return vec4(0.0);
    return texture(t, uv);
}

bool inside(vec2 uv) {
    return uv.x >= 0.0 && uv.y >= 0.0 && uv.x <= 1.0 && uv.y <= 1.0;
}

float ease(float p) {
    return p * p * (3.0 - 2.0 * p);
}

// Leaf: (u along the midrib, v across) is inside a leaf of size r when r > |v| + length(u, v).
float leaf_level(vec2 q, float angle) {
    float c = cos(angle), s = sin(angle);
    vec2 r = vec2(c * q.x + s * q.y, -s * q.x + c * q.y);
    return abs(r.y) + length(r);
}

// Bloom: five petals; a point opens when the flower's size passes radius / petal reach.
float bloom_level(vec2 q, float spin) {
    float a = atan(q.y, q.x);
    return length(q) / (0.72 + 0.28 * cos(5.0 * a + spin));
}

// The old scene over the new one at (1 - amount): where the old scene is see-through, the new one
// already shows, so a half-empty scene never fades the other one through black.
vec4 blend(vec4 a, vec4 b, float amount) {
    vec4 old = a * (1.0 - amount);
    return old + b * (1.0 - old.a);
}

float reveal(float level, float p) {
    float s = max(softness, 0.001);
    float edge = p * (1.0 + s);
    return 1.0 - smoothstep(edge - s, edge, level);
}

void main() {
    vec2 uv = gl_FragCoord.xy / out_size;
    vec4 a = texture(old_scene, uv);
    vec4 b = texture(new_scene, uv);
    float p = clamp(progress, 0.0, 1.0);
    vec2 q = (uv - 0.5) * vec2(out_size.x / out_size.y, 1.0);
    if (mode == 2) {
        float e = ease(p);
        vec2 in_uv = uv + dir * (1.0 - e);
        color = inside(in_uv) ? texture(new_scene, in_uv) : at(old_scene, uv - dir * e);
    } else if (mode == 3) {
        vec2 in_uv = uv + dir * (1.0 - ease(p));
        color = inside(in_uv) ? texture(new_scene, in_uv) : a;
    } else if (mode == 4) {
        float level = use_map ? dot(texture(wipe_map, uv).rgb, LUMA)
                              : dot(uv, dir) + max(0.0, -(dir.x + dir.y));
        color = blend(a, b, reveal(level, p));
    } else if (mode == 5) {
        color = blend(a, b, reveal(leaf_level(q, LEAF_TURN + LEAF_SPIN * p) / level_max, p));
    } else if (mode == 6) {
        color = blend(a, b, reveal(bloom_level(q, BLOOM_SPIN * p) / level_max, p));
    } else if (mode == 7) {
        vec2 st = overlay_rect.xy + uv * overlay_rect.zw;
        vec4 base;
        if (matte_offset != vec2(0.0)) {
            base = blend(a, b, dot(texture(overlay, st + matte_offset).rgb, LUMA));
        } else {
            base = cut ? b : a;
        }
        if (show_overlay) {
            vec4 s = texture(overlay, st);
            base = vec4(s.rgb * s.a, s.a) + base * (1.0 - s.a);
        }
        color = base;
    } else {
        color = blend(a, b, p);
    }
}
"""

MODES = {"fade": 1, "slide": 2, "swipe": 3, "wipe": 4, "leaf": 5, "bloom": 6, "stinger": 7}
DIRECTIONS = {"left": (-1.0, 0.0), "right": (1.0, 0.0), "up": (0.0, -1.0), "down": (0.0, 1.0)}


LEAF_TURN, LEAF_SPIN, BLOOM_SPIN = 0.785, 0.5, 1.2  # match the shader's constants


def _edge_points(aspect: float, count: int = 64) -> np.ndarray:
    """Points around the canvas edge, centred, height 1 (the shader's q space)."""
    t = np.linspace(0.0, 1.0, count, endpoint=False)
    hx, hy = aspect / 2, 0.5
    sides = [np.stack([hx * (2 * t - 1), np.full_like(t, -hy)], 1), np.stack([hx * (2 * t - 1), np.full_like(t, hy)], 1),
             np.stack([np.full_like(t, -hx), hy * (2 * t - 1)], 1), np.stack([np.full_like(t, hx), hy * (2 * t - 1)], 1)]
    return np.concatenate(sides)


def shape_reach(kind: str, size: tuple[int, int], progress: float) -> float:
    """How big the leaf or bloom must grow to cover the whole canvas (its level at the furthest edge)."""
    q = _edge_points(size[0] / max(1, size[1]))
    if kind == "leaf":
        angle = LEAF_TURN + LEAF_SPIN * progress
        c, s = np.cos(angle), np.sin(angle)
        u, v = c * q[:, 0] + s * q[:, 1], -s * q[:, 0] + c * q[:, 1]
        level = np.abs(v) + np.hypot(u, v)
    else:
        a = np.arctan2(q[:, 1], q[:, 0])
        level = np.hypot(q[:, 0], q[:, 1]) / (0.72 + 0.28 * np.cos(5 * a + BLOOM_SPIN * progress))
    return float(level.max()) * 1.02


def cover_rect(picture: tuple[float, float], canvas: tuple[int, int]) -> tuple[float, float, float, float]:
    """(x, y, w, h) in picture uv of the centred part of the picture that fills the canvas."""
    pa = picture[0] / max(1e-6, picture[1])
    ca = canvas[0] / max(1, canvas[1])
    if pa > ca:  # wider picture: crop the sides
        w = ca / pa
        return (1 - w) / 2, 0.0, w, 1.0
    h = pa / ca
    return 0.0, (1 - h) / 2, 1.0, h


class _Pbos:
    """Two pixel buffers, used in turn, so reading a frame back from the GPU never waits for it.

    Each tick the GPU copies the new frame into one buffer by itself while the CPU takes the frame copied
    the tick before from the other, long finished by then. The cost is one tick of delay: a frame
    reaches its preview or encoder a tick (16-33 ms) after it was drawn, with its own timestamp.
    """

    STALE_S = 0.5  # a frame waiting longer than this (a preview hidden for a while) is thrown away

    def __init__(self, ctx: moderngl.Context, nbytes: int):
        self.buffers = [ctx.buffer(reserve=nbytes), ctx.buffer(reserve=nbytes)]
        self.tags: list[object] = [None, None]
        self.when = [0.0, 0.0]
        self.turn = 0

    def fill(self, tag: object, now: float, read_into) -> None:
        """Start copying this tick's frame into the free buffer (read_into(buffer) queues the copy)."""
        read_into(self.buffers[self.turn])
        self.tags[self.turn], self.when[self.turn] = tag, now

    def take(self, now: float) -> tuple[bytes | None, object]:
        """The frame queued the tick before, with its tag, or (None, None); then swap buffers."""
        other = 1 - self.turn
        data, tag = None, self.tags[other]
        if tag is not None and now - self.when[other] <= self.STALE_S:
            data = self.buffers[other].read()
        self.tags[other] = None
        self.turn = other
        return data, (tag if data is not None else None)

    def release(self) -> None:
        for buffer in self.buffers:
            buffer.release()


class _ScaledTarget:
    def __init__(self, ctx: moderngl.Context, size: tuple[int, int]):
        self.color = ctx.texture(size, 4)
        self.fbo = ctx.framebuffer(color_attachments=[self.color])
        self.pbos = _Pbos(ctx, size[0] * size[1] * 4)

    def release(self) -> None:
        self.fbo.release()
        self.color.release()
        self.pbos.release()


class _Nv12Target:
    def __init__(self, ctx: moderngl.Context, size: tuple[int, int]):
        w, h = size
        self.size = size
        self.y = ctx.texture((w, h), 1)
        self.uv = ctx.texture((w // 2, h // 2), 2)
        self.y_fbo = ctx.framebuffer(color_attachments=[self.y])
        self.uv_fbo = ctx.framebuffer(color_attachments=[self.uv])
        self.pbos = _Pbos(ctx, w * h * 3 // 2)

    def release(self) -> None:
        for obj in (self.y_fbo, self.uv_fbo, self.y, self.uv):
            obj.release()
        self.pbos.release()


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
        self.in_color: moderngl.Texture | None = None  # the incoming scene during a transition
        self.in_fbo: moderngl.Framebuffer | None = None

    def aux(self) -> moderngl.Framebuffer:
        if self.aux_fbo is None:
            self.aux_color = self.ctx.texture(self.size, 4)
            self.aux_color.filter = (moderngl.LINEAR, moderngl.LINEAR)
            self.aux_fbo = self.ctx.framebuffer(color_attachments=[self.aux_color])
        return self.aux_fbo

    def incoming(self) -> moderngl.Framebuffer:
        if self.in_fbo is None:
            self.in_color = self.ctx.texture(self.size, 4)
            self.in_color.filter = (moderngl.LINEAR, moderngl.LINEAR)
            self.in_fbo = self.ctx.framebuffer(color_attachments=[self.in_color])
        return self.in_fbo

    def release_transition(self) -> None:
        """Let go of the two transition textures (16 MB for a 1080p canvas) until the next one."""
        for obj in (self.aux_fbo, self.aux_color, self.in_fbo, self.in_color):
            if obj is not None:
                obj.release()
        self.aux_fbo = self.aux_color = self.in_fbo = self.in_color = None

    def release(self) -> None:
        for obj in (self.fbo, self.color, self.aux_fbo, self.aux_color, self.in_fbo, self.in_color):
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
        self.scale_program = self.ctx.program(vertex_shader=NV12_VERTEX, fragment_shader=SCALE_FRAGMENT)
        self.scale_vao = self.ctx.vertex_array(self.scale_program, [])
        self.scaled_targets: dict[tuple[str, tuple[int, int]], _ScaledTarget] = {}
        self.transition_program = self.ctx.program(vertex_shader=NV12_VERTEX, fragment_shader=TRANSITION_FRAGMENT)
        for unit, name in enumerate(("old_scene", "new_scene", "overlay", "wipe_map")):
            self.transition_program[name].value = unit
        self.transition_vao = self.ctx.vertex_array(self.transition_program, [])
        # (nested-part source id, canvas id of the scene being drawn) -> that part's layout for the canvas
        self.parts: dict[tuple[str, str], moderngl.Texture] = {}
        self.effects: dict[str, VideoEffects] = {}  # source id -> its video filters, set by the engine
        self.downscale: dict[str, str] = {}  # canvas id -> how outputs smaller than it are scaled (Canvas.downscale)
        self.time = 0.0  # seconds, drives the Scroll filter
        self._applied: VideoEffects | None = None
        # Colour looks and shape pictures, loaded once each (key: "look:<name>" or a file path).
        self.program["lut"].value = 1
        self.program["mask"].value = 2
        self.looks: dict[str, moderngl.Texture3D | None] = {}
        self.masks: dict[str, tuple[moderngl.Texture, bool] | None] = {}
        self.load_errors: dict[str, str] = {}  # key -> why it couldn't be loaded (shown in the Filters window)

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
        transition: Transition | None = None,
        overlay: str | None = None,
        cut: bool = False,
        wipe_map: str | None = None,
    ) -> None:
        """Draw a scene's visible items back to front onto its canvas.

        Empty canvas is see-through (alpha 0, colour black), so the previews can show a
        checkerboard there while the stream and recordings, which drop alpha, still get black.

        During a transition, `previous` is the outgoing scene and `progress` runs from 0 to 1. Both
        scenes are drawn to their own textures and mixed into the canvas the way `transition` says
        (a fade when None). For a stinger, `overlay` is the texture key of the clip's current frame
        and `cut` says whether the cut point has passed; `wipe_map` is the texture key of a wipe image.
        """
        target = self._target(canvas)
        size = (canvas.width, canvas.height)
        if previous is None or progress >= 1.0:
            self._draw_scene(target.fbo, size, scene, source_sizes)
            return
        self._draw_scene(target.aux(), size, previous, source_sizes)
        self._draw_scene(target.incoming(), size, scene, source_sizes)
        transition = transition or Transition()
        mode = MODES.get(transition.kind, 1)
        program = self.transition_program
        overlay_tex = self.textures.get(overlay) if overlay else None
        map_tex = self.textures.get(wipe_map) if wipe_map else None
        if mode == MODES["stinger"]:
            sting = transition.stinger
            if overlay_tex is not None:
                tw, th = overlay_tex.size
                half_w = tw / 2 if sting.matte == "right" else tw
                half_h = th / 2 if sting.matte == "below" else th
                x, y, w, h = cover_rect((half_w, half_h), size)
                sx, sy = half_w / tw, half_h / th
                program["overlay_rect"].value = (x * sx, y * sy, w * sx, h * sy)
                program["matte_offset"].value = {"right": (0.5, 0.0), "below": (0.0, 0.5)}.get(sting.matte, (0.0, 0.0))
                program["show_overlay"].value = not (sting.matte != "none" and sting.matte_only)
            else:  # the clip has not started (or failed): just cut at the cut point
                program["matte_offset"].value = (0.0, 0.0)
                program["show_overlay"].value = False
            program["cut"].value = cut
        if transition.kind in ("leaf", "bloom"):
            program["level_max"].value = shape_reach(transition.kind, size, progress)
        program["mode"].value = mode
        program["progress"].value = float(progress)
        program["dir"].value = DIRECTIONS.get(transition.direction, (-1.0, 0.0))
        program["softness"].value = max(0.0, min(1.0, float(transition.softness)))
        program["use_map"].value = map_tex is not None
        program["out_size"].value = (float(size[0]), float(size[1]))
        target.aux_color.use(location=0)
        target.in_color.use(location=1)
        (overlay_tex or target.aux_color).use(location=2)
        (map_tex or target.aux_color).use(location=3)
        target.fbo.use()
        self.ctx.viewport = (0, 0, canvas.width, canvas.height)
        self.ctx.disable(moderngl.BLEND)
        self.transition_vao.render(moderngl.TRIANGLES, vertices=3)

    def end_transition(self, canvas_id: str) -> None:
        """A canvas's transition finished: free its two extra textures."""
        target = self.targets.get(canvas_id)
        if target is not None:
            target.release_transition()

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
            fx = self.effects.get(item.source, NO_EFFECTS)
            self._apply_effects(fx, tex.size)
            if fx.shape:
                (x0, y0, _, _), (x1, y1, _, _), (x2, y2, _, _) = quad[:3]
                self.program["quad_size"].value = (math.hypot(x1 - x0, y1 - y0), math.hypot(x2 - x0, y2 - y0))
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
        look = self._look(fx.lut) if fx.lut else None
        p["use_lut"].value = look is not None
        if look is not None:
            look.use(location=1)
            p["lut_size"].value = float(look.size[0])
            p["lut_amount"].value = fx.lut_amount
        shape = fx.shape
        if shape == SHAPE_IDS["image"]:
            mask = self._mask(fx.mask)
            if mask is None:
                shape = 0
            else:
                mask[0].use(location=2)
                p["mask_luma"].value = mask[1]
        p["shape"].value = shape
        if shape:
            p["roundness"].value = fx.roundness
            p["stretch"].value = fx.stretch
            p["feather"].value = fx.feather
            p["border"].value = fx.border
            p["border_color"].value = fx.border_color

    def _look(self, key: str) -> moderngl.Texture3D | None:
        if key not in self.looks:
            self.looks[key] = None
            try:
                table = looks.look(key[5:]) if key.startswith("look:") else looks.read_cube(key)
            except looks.LutError as exc:
                self.load_errors[key] = str(exc)
                return None
            n = table.shape[0]
            tex = self.ctx.texture3d((n, n, n), 3, np.ascontiguousarray(table, dtype="f4").tobytes(), dtype="f4")
            tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
            tex.repeat_x = tex.repeat_y = tex.repeat_z = False
            self.looks[key] = tex
        return self.looks[key]

    def _mask(self, path: str) -> tuple[moderngl.Texture, bool] | None:
        if path not in self.masks:
            self.masks[path] = None
            try:
                from PIL import Image

                with Image.open(path) as img:
                    rgba = img.convert("RGBA")
            except Exception as exc:  # noqa: BLE001 (any unreadable picture just means no shape)
                self.load_errors[path] = f"Can't open that picture: {exc}"
                return None
            rgba.thumbnail((1024, 1024))
            data = np.asarray(rgba, dtype=np.uint8)
            tex = self.ctx.texture(rgba.size, 4, np.ascontiguousarray(data).tobytes())
            tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
            tex.repeat_x = tex.repeat_y = False
            self.masks[path] = (tex, bool(data[..., 3].min() == 255))  # no transparency: use brightness
        return self.masks[path]

    def forget_looks(self) -> None:
        """Drop loaded looks and shape pictures so edited files load again."""
        for tex in self.looks.values():
            if tex is not None:
                tex.release()
        for mask in self.masks.values():
            if mask is not None:
                mask[0].release()
        self.looks.clear()
        self.masks.clear()
        self.load_errors.clear()

    def read(self, canvas_id: str) -> bytes:
        """Return the canvas as tightly packed RGBA rows, top row first."""
        return self.targets[canvas_id].fbo.read(components=4, alignment=1)

    def _draw_scaled(self, canvas_id: str, size: tuple[int, int]) -> _ScaledTarget:
        source = self.targets[canvas_id]
        target = self.scaled_targets.get((canvas_id, size))
        if target is None:
            for key in [k for k in self.scaled_targets if k[0] == canvas_id]:  # the preview was resized
                self.scaled_targets.pop(key).release()
            target = self.scaled_targets[(canvas_id, size)] = _ScaledTarget(self.ctx, size)
        self.ctx.disable(moderngl.BLEND)
        source.color.use(0)
        self.scale_program["canvas"].value = 0
        self.scale_program["out_size"].value = size
        target.fbo.use()
        self.ctx.viewport = (0, 0, *size)
        self.scale_vao.render(moderngl.TRIANGLES, vertices=3)
        return target

    def read_scaled(self, canvas_id: str, size: tuple[int, int]) -> bytes:
        """The canvas shrunk to size on the GPU, as RGBA rows top first: what a small preview needs."""
        return self._draw_scaled(canvas_id, size).fbo.read(components=4, alignment=1)

    def read_scaled_later(self, canvas_id: str, size: tuple[int, int], tag: object,
                          now: float) -> tuple[bytes | None, object]:
        """Like read_scaled without waiting for the GPU: returns the previous call's frame and tag (see _Pbos)."""
        target = self._draw_scaled(canvas_id, size)
        pbos = target.pbos
        pbos.fill(tag, now, lambda buf: target.fbo.read_into(buf, components=4, alignment=1))
        return pbos.take(now)

    def _draw_nv12(self, canvas_id: str, size: tuple[int, int]) -> _Nv12Target:
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
        program["scale_mode"].value = SCALE_MODES.get(self.downscale.get(canvas_id, "bilinear"), 0)
        for fbo, plane, chroma in ((target.y_fbo, (w, h), False), (target.uv_fbo, (w // 2, h // 2), True)):
            fbo.use()
            self.ctx.viewport = (0, 0, *plane)
            program["out_size"].value = plane
            program["chroma"].value = chroma
            self.nv12_vao.render(moderngl.TRIANGLES, vertices=3)
        return target

    def read_nv12(self, canvas_id: str, size: tuple[int, int]) -> bytearray:
        """The canvas scaled to size (even width and height) as NV12: Y rows, then CbCr rows, top first."""
        target = self._draw_nv12(canvas_id, size)
        w, h = size
        # Both planes land in one buffer: one allocation per frame instead of three (Y, CbCr, joined).
        data = bytearray(w * h * 3 // 2)
        target.y_fbo.read_into(data, components=1, alignment=1)
        target.uv_fbo.read_into(data, components=2, alignment=1, write_offset=w * h)
        return data

    def read_nv12_later(self, canvas_id: str, size: tuple[int, int], tag: object,
                        now: float) -> tuple[bytes | None, object]:
        """Like read_nv12 without waiting for the GPU: returns the previous call's frame and tag (see _Pbos)."""
        target = self._draw_nv12(canvas_id, size)
        w, h = size

        def queue(buf):
            target.y_fbo.read_into(buf, components=1, alignment=1)
            target.uv_fbo.read_into(buf, components=2, alignment=1, write_offset=w * h)

        target.pbos.fill(tag, now, queue)
        return target.pbos.take(now)

    def release(self) -> None:
        for tex in self.textures.values():
            tex.release()
        for target in self.targets.values():
            target.release()
        for nv12 in self.nv12_targets.values():
            nv12.release()
        for scaled in self.scaled_targets.values():
            scaled.release()
        self.forget_looks()
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
