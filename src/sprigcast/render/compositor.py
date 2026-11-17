"""Draws scenes onto canvases on the GPU with moderngl, and reads the result back."""

from __future__ import annotations

import dataclasses
import logging
import math
import os
import re
import time

import moderngl
import numpy as np

from sprigcast.core import looks
from sprigcast.core.filters import VideoEffects
from sprigcast.core.looks import SHAPE_IDS
from sprigcast.core.scene import Canvas, Scene, Transition

log = logging.getLogger(__name__)

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
uniform int planar;         // 0: RGBA texture; 1: I420 planes packed in one 1-channel texture; 2: I420 + alpha
uniform vec2 plane_size;    // planar: the picture's size (the Y plane), in pixels
uniform vec3 yuv;           // planar: Kr, Kb of the colour matrix, 1.0 for full range
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
uniform float blur;         // source pixels; 0 = off
uniform float pixelate;     // source pixels per block; 0 = off
uniform int sil;            // drawing a halo behind the source: 0 no, 1 soft (shadow, glow), 2 hard (stroke)
uniform vec4 sil_map;       // halo quad local -> the source's own local: xy scale, zw offset
uniform vec4 uv_rect;       // the source's uv at its top-left (xy) and bottom-right (zw) corners
uniform vec2 sil_radius;    // in the source's local units
uniform vec4 sil_color;     // rgb, alpha
uniform float sil_boost;
uniform float sil_px;       // the halo's size in canvas pixels
uniform float blackout;     // 0 .. 1 toward black, keeping the outline
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
float shape_distance(vec2 local) {
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

// A source's colour at st. Video files keep their decoded YUV planes (40% of the RGBA size, no CPU
// conversion) packed in one texture: Y rows, then rows of U | V side by side, then the alpha rows.
vec4 src(vec2 st) {
    if (planar == 0) {
        return texture(tex, st);
    }
    vec2 ts = vec2(textureSize(tex, 0));
    vec2 lp = plane_size;
    vec2 cp = lp * 0.5;
    vec2 p = clamp(st * lp, vec2(0.5), lp - 0.5);   // texel centres stay inside their own plane
    vec2 q = clamp(st * cp, vec2(0.5), cp - 0.5);
    float y = texture(tex, p / ts).r;
    float u = texture(tex, vec2(q.x, lp.y + q.y) / ts).r;
    float v = texture(tex, vec2(cp.x + q.x, lp.y + q.y) / ts).r;
    float a = planar == 2 ? texture(tex, vec2(p.x, lp.y + cp.y + p.y) / ts).r : 1.0;
    if (yuv.z > 0.5) {
        u -= 128.0 / 255.0;
        v -= 128.0 / 255.0;
    } else {
        y = (y * 255.0 - 16.0) / 219.0;
        u = (u * 255.0 - 128.0) / 224.0;
        v = (v * 255.0 - 128.0) / 224.0;
    }
    float kr = yuv.x, kb = yuv.y;
    float r = y + 2.0 * (1.0 - kr) * v;
    float b = y + 2.0 * (1.0 - kb) * u;
    float g = (y - kr * r - kb * b) / (1.0 - kr - kb);
    return vec4(clamp(vec3(r, g, b), 0.0, 1.0), a);
}

const float TAU = 6.28318531;

// The source's colour averaged over a disc (centre + two rings of 12), in src()'s own convention.
vec4 blurred(vec2 st, vec2 r) {
    vec4 acc = vec4(0.0);
    float total = 0.0;
    for (int ring = 0; ring <= 2; ring++) {
        int n = ring == 0 ? 1 : 12;
        float w = ring == 0 ? 1.0 : (ring == 1 ? 0.8 : 0.45);
        for (int i = 0; i < n; i++) {
            float a = (float(i) + 0.5 * float(ring)) * TAU / 12.0;
            vec4 c = src(clamp(st + vec2(cos(a), sin(a)) * r * (float(ring) / 2.0), vec2(0.0), vec2(1.0)));
            acc += (premul ? c : vec4(c.rgb * c.a, c.a)) * w;
            total += w;
        }
    }
    acc /= total;
    return premul ? acc : vec4(acc.rgb / max(acc.a, 0.0001), acc.a);
}

// How much a key leaves of a colour (1 = keep).
float key_keep(vec3 rgb) {
    if (key_mode == 1) {
        return pow(clamp((distance(cbcr(rgb), cbcr(key_color)) - similarity) / smoothness, 0.0, 1.0), 1.5);
    } else if (key_mode == 2) {
        float l = dot(rgb, LUMA);
        float s = max(luma_key.z, 0.0001);
        return smoothstep(luma_key.x - s, luma_key.x, l) * (1.0 - smoothstep(luma_key.y, luma_key.y + s, l));
    } else if (key_mode == 3) {
        return clamp((distance(rgb, key_color) - similarity) / smoothness, 0.0, 1.0);
    }
    return 1.0;
}

// The source's coverage at its own local point: its transparency, key and shape.
float body(vec2 l) {
    if (l.x < 0.0 || l.y < 0.0 || l.x > 1.0 || l.y > 1.0) {
        return 0.0;
    }
    vec4 c = src(mix(uv_rect.xy, uv_rect.zw, l));
    float a = c.a;
    if (key_mode > 0) {
        a *= key_keep(premul && c.a > 0.0 ? c.rgb / c.a : c.rgb);
    }
    if (shape == 7) {
        vec4 m = texture(mask, l);
        a *= mask_luma ? dot(m.rgb, LUMA) : m.a;
    } else if (shape > 0) {
        a *= 1.0 - smoothstep(-feather - 0.75, 0.75, shape_distance(l));
    }
    return a;
}

float hash(vec2 p) {
    return fract(sin(dot(p, vec2(12.9898, 78.233))) * 43758.5453);
}

vec4 halo() {
    vec2 l = local * sil_map.xy + sil_map.zw;
    float a;
    if (shape >= 1 && shape <= 6) {
        // A drawn shape: exact and smooth from its distance, at any size, for the cost of one sample.
        float d = shape_distance(l);
        float r = max(sil_px, 0.75);
        a = sil == 1 ? clamp((1.0 - smoothstep(-r, r, d)) * sil_boost, 0.0, 1.0)
                     : 1.0 - smoothstep(r - 0.75, r + 0.75, d);
    } else if (sil == 1) {
        // Transparent pictures: average the outline around, turned a little per pixel so no rings show.
        float turn = hash(gl_FragCoord.xy) * TAU;
        a = body(l);
        float total = 1.0;
        for (int ring = 1; ring <= 3; ring++) {
            float w = 1.0 - 0.25 * float(ring);
            for (int i = 0; i < 12; i++) {
                float t = turn + (float(i) + 0.33 * float(ring)) * TAU / 12.0;
                a += body(l + vec2(cos(t), sin(t)) * sil_radius * (float(ring) / 3.0)) * w;
                total += w;
            }
        }
        a = min(1.0, a / total * sil_boost);
    } else {
        a = body(l);
        for (int ring = 1; ring <= 2; ring++) {
            for (int i = 0; i < 16; i++) {
                float t = (float(i) + 0.5 * float(ring)) * TAU / 16.0;
                a = max(a, body(l + vec2(cos(t), sin(t)) * sil_radius * (float(ring) / 2.0)));
            }
        }
    }
    return vec4(sil_color.rgb, a * sil_color.a * fx_opacity * opacity);
}

void main() {
    if (sil > 0) {
        color = halo();
        return;
    }
    vec2 st = wrap ? fract(uv + scroll) : uv;
    vec2 tpx = 1.0 / (planar > 0 ? plane_size : vec2(textureSize(tex, 0)));
    if (pixelate > 0.0) {
        vec2 cell = tpx * pixelate;
        st = clamp((floor(st / cell) + 0.5) * cell, tpx * 0.5, 1.0 - tpx * 0.5);   // never wraps round the edge
    }
    vec4 c = blur > 0.0 ? blurred(st, tpx * blur) : src(st);
    if (premul && c.a > 0.0) {
        c.rgb /= c.a;
    }
    if (sharpen > 0.0) {
        vec2 px = tpx;
        vec3 around = src(st + vec2(px.x, 0.0)).rgb + src(st - vec2(px.x, 0.0)).rgb
                    + src(st + vec2(0.0, px.y)).rgb + src(st - vec2(0.0, px.y)).rgb;
        c.rgb = clamp(c.rgb + (4.0 * c.rgb - around) * sharpen, 0.0, 1.0);
    }
    if (key_mode == 1) {
        float base = distance(cbcr(c.rgb), cbcr(key_color)) - similarity;
        c.a *= key_keep(c.rgb);
        float keep = pow(clamp(base / spill, 0.0, 1.0), 1.5);
        c.rgb = mix(vec3(dot(c.rgb, LUMA)), c.rgb, keep);
    } else if (key_mode > 1) {
        c.a *= key_keep(c.rgb);
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
        float d = shape_distance(local);
        const float aa = 0.75;
        if (border > 0.0) {
            float ring = smoothstep(-border - aa, -border + aa, d);   // 1 within `border` pixels of the edge
            c.rgb = mix(c.rgb, border_color, ring);
            c.a = mix(c.a, 1.0, ring);
        }
        c.a *= 1.0 - smoothstep(-feather - aa, aa, d);
    }
    c.rgb = mix(c.rgb, vec3(0.0), blackout);
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
    """Pixel buffers used in turn, so reading a frame back from the GPU never waits for it.

    Each tick the GPU copies the new frame into a free buffer by itself, with a fence after it, while the CPU
    takes the oldest earlier frame the GPU has finished copying. The cost is a tick of delay: a frame reaches
    its preview or encoder a tick (16-33 ms) after it was drawn, with its own timestamp. When the card hasn't
    finished that copy yet (the 2026-11-16 PC retest: 100-140 ms about ten times an hour, 3-7 frames lost to
    the wait) nothing is taken that tick instead of waiting for it: the frame comes a tick later, and if the
    card is behind on every buffer the oldest gives way. Without fences (moderngl's own calls) the frame of
    the tick before is fetched as it is, as before.

    Where the driver can (OpenGL 4.4), the buffers stay mapped (gl_unlocked.MappedBuffer): a finished copy is
    fetched from memory with no OpenGL call. A fence seen done is remembered, so asking about it again (behind,
    stuck, take in one tick) doesn't ask the driver again.
    """

    STALE_S = 0.5  # a frame waiting longer than this (a preview hidden for a while) is thrown away
    COUNT = 3  # a preview's: one tick in flight, one being taken, one spare
    OUTPUT_COUNT = 4  # an encoder's: the card may be two ticks behind before a frame gives way

    def __init__(self, ctx: moderngl.Context, nbytes: int, fences=None, count: int | None = None):
        self.fences = fences if getattr(fences, "fences_ok", False) else None
        count = count or self.COUNT
        mapped = getattr(self.fences, "mapped", None)
        self.buffers = []
        for _ in range(count):
            buffer = mapped(nbytes) if mapped is not None else None
            self.buffers.append(buffer if buffer is not None else ctx.buffer(reserve=nbytes))
        self.pending: list[list] = []  # [buffer index, tag, when, fence, seen done], oldest first
        self.free = list(range(count))
        self.late = 0  # ticks the card hadn't finished the copy (nothing taken instead of waiting)
        self.size: tuple[int, int] | None = None  # what it was made for, where the owner needs to know

    def fill(self, tag: object, now: float, read_into) -> None:
        """Start copying this tick's frame into a free buffer (read_into(buffer) queues the copy)."""
        if not self.free:  # the card is behind on every buffer: the oldest frame gives way
            self._drop()
        index = self.free.pop(0)
        read_into(self.buffers[index])
        sync = self.fences.fence() if self.fences is not None else None
        self.pending.append([index, tag, now, sync, sync is None])

    def _done(self, entry: list) -> bool:
        if not entry[4]:
            entry[4] = self.fences.done(entry[3])
        return entry[4]

    def take(self, now: float, read=None) -> tuple[bytes | None, object]:
        """The oldest frame queued before this tick's, with its tag, or (None, None) when there is none or the
        card hasn't finished copying it. read(buffer) fetches it (UnlockedGL.read_buffer), else the buffer's
        own read() (moderngl's, or straight from memory for a mapped one)."""
        while len(self.pending) > 1 and now - self.pending[0][2] > self.STALE_S:
            self._drop()
        if len(self.pending) < 2:  # never the frame queued this tick
            return None, None
        entry = self.pending[0]
        if self.fences is not None and not self._done(entry):
            self.late += 1
            return None, None
        index, tag, _when, sync, _seen = self.pending.pop(0)
        self.free.append(index)
        if self.fences is not None:
            self.fences.forget(sync)
        if tag is None:
            return None, None
        buffer = self.buffers[index]
        data = read(buffer) if read is not None else buffer.read()
        return data, tag

    def behind(self) -> bool:
        """The card hasn't finished a copy older than the last one asked for: it's busy with something."""
        return self.fences is not None and any(not self._done(p) for p in self.pending[:-1])

    def stuck(self, now: float, after_s: float = 0.1) -> bool:
        """A copy asked for more than after_s ago still isn't done: the card is stuck, not merely busy."""
        return self.fences is not None and any(now - p[2] > after_s and not self._done(p) for p in self.pending)

    def _drop(self) -> None:
        index, _tag, _when, sync, _seen = self.pending.pop(0)
        self.free.append(index)
        if self.fences is not None:
            self.fences.forget(sync)

    def release(self) -> None:
        while self.pending:
            self._drop()
        for buffer in self.buffers:
            buffer.release()


class _ScaledTarget:
    def __init__(self, ctx: moderngl.Context, size: tuple[int, int], fences=None):
        self.color = ctx.texture(size, 4)
        self.fbo = ctx.framebuffer(color_attachments=[self.color])
        self.pbos = _Pbos(ctx, size[0] * size[1] * 4, fences)

    def release(self) -> None:
        self.fbo.release()
        self.color.release()
        self.pbos.release()


class _Nv12Target:
    def __init__(self, ctx: moderngl.Context, size: tuple[int, int], fences=None):
        w, h = size
        self.size = size
        self.y = ctx.texture((w, h), 1)
        self.uv = ctx.texture((w // 2, h // 2), 2)
        self.y_fbo = ctx.framebuffer(color_attachments=[self.y])
        self.uv_fbo = ctx.framebuffer(color_attachments=[self.uv])
        self.pbos = _Pbos(ctx, w * h * 3 // 2, fences, _Pbos.OUTPUT_COUNT)

    def release(self) -> None:
        for obj in (self.y_fbo, self.uv_fbo, self.y, self.uv):
            obj.release()
        self.pbos.release()


# How to read each source byte order as RGBA when sampling ("1" = fully opaque).
SWIZZLES = {"RGBA": "RGBA", "BGRA": "BGRA", "RGB": "RGB1", "BGR": "BGR1", "BGRX": "BGR1",  # X: alpha ignored
            "pBGRA": "BGRA"}  # p: colours already multiplied by alpha (a QPainter picture as it is)
# Colour matrices for planar YUV sources: Kr, Kb.
YUV_MATRICES = {"601": (0.299, 0.114), "709": (0.2126, 0.0722), "2020": (0.2627, 0.0593)}


def planar_layout(channel_order: str) -> tuple[int, tuple[float, float, float]] | None:
    """For a planar source ("I420/709/tv", "I420A/601/pc"): (mode, (Kr, Kb, full)); else None.

    Made by sources.media.pack_i420."""
    if not channel_order.startswith("I420"):
        return None
    kind, _, rest = channel_order.partition("/")
    matrix, _, rng = rest.partition("/")
    kr, kb = YUV_MATRICES.get(matrix, YUV_MATRICES["601"])
    return (2 if kind == "I420A" else 1), (kr, kb, 1.0 if rng == "pc" else 0.0)


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

    STUCK_S = 0.1  # an output's copy not done after this long: the card is stuck, ask it for no more (read_nv12_later)

    def __init__(self, ctx: moderngl.Context | None = None):
        self.ctx = ctx or create_context()
        # Uploads and read-backs through ctypes, without Python's lock while the driver makes them wait
        # (render/gl_unlocked.py). None: moderngl's own calls, as before.
        self.gl = None
        if not os.environ.get("SPRIGCAST_MODERNGL_CALLS"):
            from sprigcast.render.gl_unlocked import UnlockedGL

            gl = UnlockedGL(self.ctx)
            self.gl = gl if gl.ok else None
        self.program = self.ctx.program(vertex_shader=VERTEX_SHADER, fragment_shader=FRAGMENT_SHADER)
        self.vbo = self.ctx.buffer(reserve=4 * 4 * 4, dynamic=True)  # 4 vertices x (x, y, u, v) float32
        self.vao = self.ctx.vertex_array(self.program, [(self.vbo, "2f 2f", "in_pos", "in_uv")])
        self.textures: dict[str, moderngl.Texture] = {}
        self.full_pbos: dict[str, _Pbos] = {}  # canvas id -> read-back buffers for read_full_later
        # Planar YUV sources: id -> (mode, (Kr, Kb, full), picture size); see planar_layout.
        self.planar: dict[str, tuple[int, tuple[float, float, float], tuple[int, int]]] = {}
        self.premultiplied: set[str] = set()  # sources uploading "pBGRA": drawn like a part's texture
        # Sharp sources drawn with more (or fewer) pixels than their layout size: id -> (picture pixels per
        # layout pixel, layout size). Filters measured in source pixels (blur, pixelate) keep their look.
        self.detail: dict[str, tuple[float, tuple[int, int]]] = {}
        self._detailed: dict[tuple[int, float], tuple[VideoEffects, VideoEffects]] = {}
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
        # Frozen sprigs (the Freeze plugin, Snap's freeze): (placement source id, canvas id) -> (which freeze,
        # a copy of the sprig's picture when it froze). The sprig itself keeps drawing for anything else showing it.
        self.held: dict[tuple[str, str], tuple[object, moderngl.Texture]] = {}
        self.effects: dict[str, VideoEffects] = {}  # source id -> its video filters, set by the engine
        self._spins: dict[tuple[str, int], float] = {}  # (source id, run) -> when its barrel roll started
        self.downscale: dict[str, str] = {}  # canvas id -> how outputs smaller than it are scaled (Canvas.downscale)
        self.time = 0.0  # seconds, drives the Scroll filter
        self._applied: VideoEffects | None = None
        # Colour looks and shape pictures, loaded once each (key: "look:<name>" or a file path).
        self.program["lut"].value = 1
        self.program["mask"].value = 2
        self.looks: dict[str, moderngl.Texture3D | None] = {}
        self.masks: dict[str, tuple[moderngl.Texture, bool] | None] = {}
        self.load_errors: dict[str, str] = {}  # key -> why it couldn't be loaded (shown in the Filters window)
        # When each output-only target was last drawn: ("nv12" | "scaled", key) or ("canvas", "clean:<id>" | "shot:<id>").
        # prune() lets go of the ones nobody has drawn for a while (see prune).
        self._used: dict[tuple, float] = {}
        self._previewed: set[tuple] = set()  # scaled copies a preview reads (kept longer by prune)

    # Sources -------------------------------------------------------------

    def upload(self, source_id: str, frame: np.ndarray, channel_order: str = "RGBA",
               size: tuple[int, int] | None = None) -> None:
        """Copy a new frame for a source to the GPU, resizing its texture if needed.

        size: a sharp source's layout size, when its picture may have more or fewer pixels than that.

        Frames are uploaded in whatever byte order the source produces (RGBA, BGRA from screen capture,
        3-channel BGR from cameras) and swizzled when sampled, so there is no colour conversion on the CPU.
        """
        h, w = frame.shape[:2]
        components = frame.shape[2] if frame.ndim == 3 else 1
        layout = planar_layout(channel_order) if components == 1 else None
        if layout is not None:  # Y rows, half as many U|V rows, then (with alpha) as many alpha rows again
            mode = layout[0]
            pic_h = h * 2 // 5 if mode == 2 else h * 2 // 3
            self.planar[source_id] = (mode, layout[1], (w, pic_h))
        else:
            self.planar.pop(source_id, None)
        if size and size[0] > 0 and size[1] > 0 and abs(w / size[0] - 1.0) > 1e-3:
            # More or fewer pixels than its layout box: a sharp source, or a video decoded smaller (filters
            # measured in pixels look the same either way)
            self.detail[source_id] = (w / size[0], (int(size[0]), int(size[1])))
        else:
            self.detail.pop(source_id, None)
        tex = self.textures.get(source_id)
        if tex is None or tex.size != (w, h) or tex.components != components:
            if tex is not None:
                tex.release()
            tex = self.ctx.texture((w, h), components, alignment=1)
            tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
            self.textures[source_id] = tex
        tex.swizzle = SWIZZLES.get(channel_order, "RGBA")
        if channel_order == "pBGRA":
            self.premultiplied.add(source_id)
        else:
            self.premultiplied.discard(source_id)
        pixels = np.ascontiguousarray(frame, dtype=np.uint8)
        if self.gl is not None:
            self.gl.write_texture(tex, pixels)
        else:
            tex.write(pixels)

    def forget(self, source_id: str) -> None:
        self.planar.pop(source_id, None)
        self.premultiplied.discard(source_id)
        self.detail.pop(source_id, None)
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
        # A sprig nothing shows for a while (a bloom you left, a removed plugin's own sprig) lets go of its
        # full-canvas texture in prune(); it's made again the moment it's shown.
        self._used[("canvas", f"part:{part_id}")] = time.monotonic()
        self._draw_scene(target.fbo, size, scene, source_sizes, fades)
        return target.color

    def hold_copy(self, key: tuple[str, str], token: object, texture: moderngl.Texture) -> moderngl.Texture:
        """Copy a sprig's picture as it is now, kept as key's frozen picture (for token's freeze)."""
        old = self.held.get(key)
        copy = old[1] if old is not None and old[1].size == texture.size else None
        if copy is None:
            if old is not None:
                old[1].release()
            copy = self.ctx.texture(texture.size, 4)
            copy.filter = (moderngl.LINEAR, moderngl.LINEAR)
        source = self.ctx.framebuffer(color_attachments=[texture])
        try:
            self.ctx.copy_framebuffer(copy, source)
        finally:
            source.release()
        self.held[key] = (token, copy)
        return copy

    def let_go_held(self, frozen: dict) -> None:
        """Free the frozen copies of placements no longer frozen (frozen: placement source id -> its freeze)."""
        for key in [k for k, (token, _) in self.held.items() if frozen.get(k[0]) != token]:
            self.held.pop(key)[1].release()

    def forget_part(self, part_id: str) -> None:
        target = self.targets.pop(f"part:{part_id}", None)
        pbos = self.full_pbos.pop(f"part:{part_id}", None)
        if pbos is not None:
            pbos.release()
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
        move=None,
    ) -> None:
        """Draw a scene's visible items back to front onto its canvas.

        Empty canvas is see-through (alpha 0, colour black), so the previews can show a
        checkerboard there while the stream and recordings, which drop alpha, still get black.

        During a transition, `previous` is the outgoing scene and `progress` runs from 0 to 1. Both
        scenes are drawn to their own textures and mixed into the canvas the way `transition` says
        (a fade when None). For a stinger, `overlay` is the texture key of the clip's current frame
        and `cut` says whether the cut point has passed; `wipe_map` is the texture key of a wipe image.
        For a Move transition, `move()` gives this frame's pieces (render/move.py Draw): drawn straight
        onto the canvas, without the two scene textures the other kinds mix.
        """
        target = self._target(canvas)
        if canvas.id.startswith(("clean:", "shot:")):  # output-only copies: let go once unused (prune)
            self._used[("canvas", canvas.id)] = time.monotonic()
        size = (canvas.width, canvas.height)
        if previous is None or progress >= 1.0:
            self._draw_scene(target.fbo, size, scene, source_sizes)
            return
        if move is not None and transition is not None and transition.kind == "move":
            self._draw_move(target.fbo, size, move())
            return
        self._used.pop(("transition", canvas.id), None)  # in use again
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

    TRANSITION_KEEP_S = 30.0
    PREVIEW_KEEP_S = 600.0  # a preview's small copy (and its read-back buffers) waits this long for the window

    def end_transition(self, canvas_id: str) -> None:
        """A canvas's transition finished. Its two extra textures stay for the next switch (prune() frees
        them after TRANSITION_KEEP_S without one): making two canvas-sized textures on every switch
        cost a frame each time when switching quickly."""
        if canvas_id in self.targets:
            self._used[("transition", canvas_id)] = time.monotonic()

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
        self.program["planar"].value = 0
        self._applied = None
        self._draw_pieces(self._scene_pieces(scene, source_sizes, fades))

    def _draw_move(self, fbo, size: tuple[int, int], draws) -> None:
        """One frame of a Move transition: its pieces (render/move.py Draw), bottom first."""
        fbo.use()
        self.ctx.viewport = (0, 0, size[0], size[1])
        self.ctx.clear(0.0, 0.0, 0.0, 0.0)
        self._blend()
        self.program["canvas_size"].value = (float(size[0]), float(size[1]))
        self.program["tex"].value = 0
        self.program["opacity"].value = 1.0
        self.program["planar"].value = 0
        self._applied = None
        self._draw_pieces((d.source, d.canvas, d.quad, d.opacity) for d in draws)

    def _scene_pieces(self, scene: Scene, source_sizes: dict[str, tuple[int, int]], fades: dict[str, float] | None):
        """A scene's visible items as (source id, canvas id, quad, opacity), bottom first."""
        for item in scene.items:
            if not item.visible:
                continue
            part = self.parts.get((item.source, scene.canvas))
            size = part.size if part is not None else source_sizes.get(item.source)
            if size is None:
                continue
            quad = item.quad(size)
            if quad:
                yield item.source, scene.canvas, quad, fades.get(item.source, 1.0) if fades else 1.0

    def _draw_pieces(self, pieces) -> None:
        """Draw (source id, canvas id, quad, opacity) pieces in order, onto the framebuffer in use.

        A scene's items come here (see _scene_pieces), and so does each frame of a Move transition, whose
        quads are somewhere between two blooms."""
        planar_on = False
        opacity = 1.0
        for source_id, canvas_id, quad, alpha in pieces:
            part = self.parts.get((source_id, canvas_id))
            tex = part or self.textures.get(source_id)
            if tex is None:
                continue
            fx = self.effects.get(source_id, NO_EFFECTS)
            if fx.spin[0]:
                quad = self._spun(source_id, quad, fx.spin)
            self.vbo.write(np.array(quad, dtype="f4").tobytes())
            planar = None if part is not None else self.planar.get(source_id)
            if planar is not None:
                self.program["planar"].value = planar[0]
                self.program["yuv"].value = planar[1]
                self.program["plane_size"].value = planar[2]
                self._applied = None  # effects measured in texels: the picture's size, not the packed texture's
            elif planar_on:
                self.program["planar"].value = 0
                self._applied = None
            planar_on = planar is not None
            detail = None if part is not None else self.detail.get(source_id)
            if detail is not None:
                fx = self._in_detail(fx, detail[0])
                picture = detail[1]
            else:
                picture = planar[2] if planar is not None else tex.size
            self._apply_effects(fx, picture)
            if fx.shape or fx.halos:
                (x0, y0, _, _), (x1, y1, _, _), (x2, y2, _, _) = quad[:3]
                self.program["quad_size"].value = (math.hypot(x1 - x0, y1 - y0), math.hypot(x2 - x0, y2 - y0))
            if alpha != opacity:
                self.program["opacity"].value = opacity = alpha
            self.program["premul"].value = part is not None or source_id in self.premultiplied
            tex.use(location=0)
            if fx.halos:
                self._draw_halos(fx, quad)
                self.vbo.write(np.array(quad, dtype="f4").tobytes())
            self.vao.render(moderngl.TRIANGLE_STRIP, vertices=4)
        if opacity != 1.0:
            self.program["opacity"].value = 1.0

    def _in_detail(self, fx: VideoEffects, detail: float) -> VideoEffects:
        """The filters of a sharp source drawn with `detail` pixels per layout pixel: blur and pixelate are
        counted in the picture's pixels, so they grow with it and look the same at any detail."""
        if not (fx.blur or fx.pixelate):
            return fx
        key = (id(fx), detail)
        held = self._detailed.get(key)
        if held is not None and held[0] is fx:
            return held[1]
        if len(self._detailed) > 64:
            self._detailed.clear()
        out = dataclasses.replace(fx, blur=fx.blur * detail, pixelate=fx.pixelate * detail)
        self._detailed[key] = (fx, out)  # holds fx too, so its id isn't reused while the key stands
        return out

    def _draw_halos(self, fx: VideoEffects, quad) -> None:
        """Shadow, glow and stroke: one quad each behind the source, grown by their size, in its shape."""
        p = self.program
        (x0, y0, u0, v0), (x1, y1, _, _), (x2, y2, _, _), (_, _, u3, v3) = quad[:4]
        w, h = max(1e-3, math.hypot(x1 - x0, y1 - y0)), max(1e-3, math.hypot(x2 - x0, y2 - y0))
        ex, ey = ((x1 - x0) / w, (y1 - y0) / w), ((x2 - x0) / h, (y2 - y0) / h)
        p["uv_rect"].value = (u0, v0, u3, v3)
        for halo in fx.halos:
            pad = halo.radius + 2.0
            ox, oy = halo.offset
            corners = []
            for sx, sy in ((-1, -1), (1, -1), (-1, 1), (1, 1)):
                bx = x0 + (ex[0] * w if sx > 0 else 0) + (ey[0] * h if sy > 0 else 0)
                by = y0 + (ex[1] * w if sx > 0 else 0) + (ey[1] * h if sy > 0 else 0)
                corners.append((bx + sx * pad * ex[0] + sy * pad * ey[0] + ox,
                                by + sx * pad * ex[1] + sy * pad * ey[1] + oy, 0.0, 0.0))
            self.vbo.write(np.array(corners, dtype="f4").tobytes())
            off_x, off_y = ox * ex[0] + oy * ex[1], ox * ey[0] + oy * ey[1]
            p["sil_map"].value = ((w + 2 * pad) / w, (h + 2 * pad) / h, (-pad - off_x) / w, (-pad - off_y) / h)
            p["sil_radius"].value = (halo.radius / w, halo.radius / h)
            p["sil_color"].value = (*halo.color, halo.alpha)
            p["sil_boost"].value = halo.boost
            p["sil_px"].value = halo.radius
            p["sil"].value = halo.mode
            self.vao.render(moderngl.TRIANGLE_STRIP, vertices=4)
        p["sil"].value = 0

    def _spun(self, source_id: str, quad: list, spin: tuple[float, float, int]) -> list:
        """The quad turned around its middle: a barrel roll that eases in and out and lands where it began."""
        turns, seconds, run = spin
        if len(self._spins) > 64:  # old rolls
            self._spins.clear()
        start = self._spins.setdefault((source_id, run), self.time)
        t = min(1.0, max(0.0, (self.time - start) / seconds))
        eased = t * t * (3.0 - 2.0 * t)
        a = math.tau * turns * eased
        cx = sum(p[0] for p in quad) / 4
        cy = sum(p[1] for p in quad) / 4
        ca, sa = math.cos(a), math.sin(a)
        return [(cx + (x - cx) * ca - (y - cy) * sa, cy + (x - cx) * sa + (y - cy) * ca, u, v) for x, y, u, v in quad]

    def render_source(self, source_id: str, source_sizes: dict[str, tuple[int, int]], canvas_id: str,
                      limit: int = 1920) -> np.ndarray | None:
        """One source drawn on its own (with its filters), as an H x W x 4 RGBA array. For Snap."""
        from sprigcast.core.scene import SceneItem

        part = self.parts.get((source_id, canvas_id))
        size = part.size if part is not None else source_sizes.get(source_id)
        if not size or not size[0] or not size[1] or (part is None and source_id not in self.textures):
            return None
        scale = min(1.0, limit / max(size))
        out = (max(1, round(size[0] * scale)), max(1, round(size[1] * scale)))
        scene = Scene(id="grab", name="grab", canvas=canvas_id, items=[SceneItem(source=source_id, scale=scale)])
        target = self._target_sized("grab", out)
        try:
            self._draw_scene(target.fbo, out, scene, source_sizes)
            data = self._read_now(target.fbo)
        finally:
            target.release()
            self.targets.pop("grab", None)
        return np.frombuffer(data, dtype=np.uint8).reshape(out[1], out[0], 4).copy()

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
        p["blur"].value = fx.blur
        p["pixelate"].value = fx.pixelate
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
        p["blackout"].value = fx.blackout
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
        """Return the canvas as tightly packed RGBA rows, top row first. Waits for the card (screenshots and
        the like); previews and outputs use the *_later reads."""
        return self._read_now(self.targets[canvas_id].fbo)

    def _read_now(self, fbo) -> bytes:
        if self.gl is not None:  # waiting without Python's lock
            data = bytearray(fbo.size[0] * fbo.size[1] * 4)
            self.gl.read_into(fbo, data, 4)
            return bytes(data)
        return fbo.read(components=4, alignment=1)

    def _draw_scaled(self, canvas_id: str, size: tuple[int, int]) -> _ScaledTarget:
        source = self.targets[canvas_id]
        target = self.scaled_targets.get((canvas_id, size))
        if target is None:
            for key in [k for k in self.scaled_targets if k[0] == canvas_id]:  # the preview was resized
                self.scaled_targets.pop(key).release()
            target = self.scaled_targets[(canvas_id, size)] = _ScaledTarget(self.ctx, size, self.gl)
        self._used[("scaled", (canvas_id, size))] = time.monotonic()
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
                          now: float, optional: bool = False) -> tuple[bytes | None, object]:
        """Like read_scaled without waiting for the GPU: returns the previous call's frame and tag (see _Pbos).

        optional (a preview): while the card is behind on earlier copies, ask it for no new one. On the PC
        (2026-11-16) queuing a preview copy while the card was stuck held the renderer 332 ms."""
        known = self.scaled_targets.get((canvas_id, size))
        if optional:
            self._previewed = {k for k in self._previewed if k in self.scaled_targets} | {(canvas_id, size)}
        if optional and known is not None and known.pbos.behind():
            known.pbos.late += 1
            self._used[("scaled", (canvas_id, size))] = time.monotonic()
            return known.pbos.take(now, self.gl.read_buffer if self.gl is not None else None)
        target = self._draw_scaled(canvas_id, size)
        pbos = target.pbos
        if self.gl is not None:
            pbos.fill(tag, now, lambda buf: self.gl.read_into(target.fbo, buf, 4))
            return pbos.take(now, self.gl.read_buffer)
        pbos.fill(tag, now, lambda buf: target.fbo.read_into(buf, components=4, alignment=1))
        return pbos.take(now)

    def read_full_later(self, canvas_id: str, tag: object, now: float) -> tuple[bytes | None, object]:
        """read() without waiting for the GPU: the canvas as it is, a tick late (see _Pbos)."""
        fbo = self.targets[canvas_id].fbo
        w, h = fbo.size
        pbos = self.full_pbos.get(canvas_id)
        if pbos is None or pbos.size != (w, h):  # first time, or the canvas was resized
            if pbos is not None:
                pbos.release()
            pbos = self.full_pbos[canvas_id] = _Pbos(self.ctx, w * h * 4, self.gl, _Pbos.OUTPUT_COUNT)
            pbos.size = (w, h)
        self._used[("full", canvas_id)] = time.monotonic()
        if pbos.stuck(now, self.STUCK_S):  # see read_nv12_later
            pbos.late += 1
            return pbos.take(now, self.gl.read_buffer if self.gl is not None else None)
        if self.gl is not None:
            pbos.fill(tag, now, lambda buf: self.gl.read_into(fbo, buf, 4))
            return pbos.take(now, self.gl.read_buffer)
        pbos.fill(tag, now, lambda buf: fbo.read_into(buf, components=4, alignment=1))
        return pbos.take(now)

    def _draw_nv12(self, canvas_id: str, size: tuple[int, int]) -> _Nv12Target:
        source = self.targets[canvas_id]
        target = self.nv12_targets.get((canvas_id, size))
        if target is None:
            target = self.nv12_targets[(canvas_id, size)] = _Nv12Target(self.ctx, size, self.gl)
        self._used[("nv12", (canvas_id, size))] = time.monotonic()
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
        if self.gl is not None:
            self.gl.read_into(target.y_fbo, data, 1)
            self.gl.read_into(target.uv_fbo, data, 2, write_offset=w * h)
            return data
        target.y_fbo.read_into(data, components=1, alignment=1)
        target.uv_fbo.read_into(data, components=2, alignment=1, write_offset=w * h)
        return data

    def read_nv12_later(self, canvas_id: str, size: tuple[int, int], tag: object,
                        now: float) -> tuple[bytes | None, object]:
        """Like read_nv12 without waiting for the GPU: returns the previous call's frame and tag (see _Pbos).

        While the card is stuck on an earlier copy (over STUCK_S), no new one is asked for: asking anyway held
        the renderer 345 ms on the PC (2026-11-16) and every canvas missed 20 frames; now only this output
        misses the frames the card couldn't make anyway, and nothing else waits."""
        known = self.nv12_targets.get((canvas_id, size))
        if known is not None and known.pbos.stuck(now, self.STUCK_S):
            known.pbos.late += 1
            self._used[("nv12", (canvas_id, size))] = time.monotonic()
            return known.pbos.take(now, self.gl.read_buffer if self.gl is not None else None)
        target = self._draw_nv12(canvas_id, size)
        w, h = size

        def queue(buf):
            if self.gl is not None:
                self.gl.read_into(target.y_fbo, buf, 1)
                self.gl.read_into(target.uv_fbo, buf, 2, write_offset=w * h)
                return
            target.y_fbo.read_into(buf, components=1, alignment=1)
            target.uv_fbo.read_into(buf, components=2, alignment=1, write_offset=w * h)

        target.pbos.fill(tag, now, queue)
        return target.pbos.take(now, self.gl.read_buffer if self.gl is not None else None)

    def prune(self, idle_s: float = 3.0, now: float | None = None) -> int:
        """Let go of output-only targets nobody has drawn for idle_s: the NV12 and scaled copies (with
        their two read-back buffers each) of a recording, stream or preview that ended, and source
        record's clean copy of a canvas. They used to stay until SprigCast closed. Returns how many."""
        now = time.monotonic() if now is None else now

        def keep(kind, key) -> float:
            if kind == "transition":
                return max(idle_s, self.TRANSITION_KEEP_S)
            if kind == "scaled" and key in self._previewed:
                # A preview stops while the window is minimized or hidden. Made again when it comes back, the
                # first copy into fresh read-back buffers held the renderer 330 ms on the PC (2026-11-16, at
                # rest): the driver set aside their memory then. A few MB kept a while is cheaper.
                return max(idle_s, self.PREVIEW_KEEP_S)
            return idle_s

        stale = [(kind, key) for (kind, key), when in self._used.items() if now - when > keep(kind, key)]
        for kind, key in stale:
            del self._used[(kind, key)]
            if kind == "transition":
                if key in self.targets:
                    self.targets[key].release_transition()
                continue
            pool = {"nv12": self.nv12_targets, "scaled": self.scaled_targets, "canvas": self.targets,
                    "full": self.full_pbos}[kind]
            target = pool.pop(key, None)
            self._previewed.discard(key)
            if target is not None:
                target.release()
        return len(stale)

    def release(self) -> None:
        for tex in self.textures.values():
            tex.release()
        for _token, tex in self.held.values():
            tex.release()
        for target in self.targets.values():
            target.release()
        for nv12 in self.nv12_targets.values():
            nv12.release()
        for scaled in self.scaled_targets.values():
            scaled.release()
        for pbos in self.full_pbos.values():
            pbos.release()
        self.forget_looks()
        if self.gl is not None:
            self.gl.release()
        self.vao.release()
        self.vbo.release()
        self.program.release()
        self.ctx.release()


class GraphicsError(RuntimeError):
    """The graphics driver can't give SprigCast an OpenGL 3.3 context. The message is for the user."""


def gl_version(text: str) -> tuple[int, int] | None:
    """(4, 0) from a GL_VERSION string like "4.0.0 - Build 10.18.10.5161" or "3.1.0 - Build 9.17"."""
    match = re.match(r"\s*(?:OpenGL ES )?(\d+)\.(\d+)", text or "")
    return (int(match.group(1)), int(match.group(2))) if match else None


def graphics_message(version: str, renderer: str, detail: str = "") -> str:
    """What to tell the user when OpenGL 3.3 can't start, from what the driver says it offers."""
    name = (renderer or "").strip()
    if not name or any(word in name for word in ("GDI Generic", "Basic Render", "Basic Display")):
        return ("SprigCast can't start its graphics: Windows is running without a graphics driver"
                + (f" ({name})" if name else "") + ".\n\nInstall the graphics driver from your PC maker's support "
                "page, or from Intel, AMD or NVIDIA, then open SprigCast again.")
    found = gl_version(version)
    if found is not None and found < (3, 3):
        return (f"SprigCast needs OpenGL 3.3 graphics. This PC's graphics ({name}) offer OpenGL "
                f"{found[0]}.{found[1]}.\n\nA newer graphics driver from your PC maker, Intel or AMD may add it. "
                "Graphics from before 2012 (like Intel HD 3000) can't run SprigCast.")
    return (f"SprigCast couldn't start its graphics ({name or 'unknown graphics'}"
            + (f", OpenGL {found[0]}.{found[1]}" if found else "") + ").\n\nUpdate the graphics driver from your "
            "PC maker, Intel, AMD or NVIDIA, then open SprigCast again." + (f"\n\nDetails: {detail}" if detail else ""))


def probe_graphics() -> tuple[str, str]:
    """GL_VERSION and GL_RENDERER from whatever context the driver gives without a version asked for."""
    for kwargs in ({"require": 100}, {"require": 100, "backend": "egl"}):
        try:
            ctx = moderngl.create_standalone_context(**kwargs)
        except Exception:
            continue
        try:
            info = ctx.info
            return str(info.get("GL_VERSION", "")), str(info.get("GL_RENDERER", ""))
        finally:
            ctx.release()
    return "", ""


def create_context() -> moderngl.Context:
    """A headless OpenGL 3.3 context. On Windows this uses WGL; on Linux it falls back to EGL.

    Raises GraphicsError, with a message for the user, when the driver can't give one: no driver
    (Microsoft Basic Display Adapter), or graphics older than OpenGL 3.3 (Intel HD 2000/3000).
    Intel HD 2500/4000 and newer offer OpenGL 4.0 or more on Windows, so 2012 PCs onwards are fine."""
    try:
        ctx = moderngl.create_standalone_context(require=330)
    except Exception as first:
        try:
            ctx = moderngl.create_standalone_context(require=330, backend="egl")
        except Exception:
            version, renderer = probe_graphics()
            log.warning("OpenGL 3.3 unavailable: %s (driver offers %r on %r)", first, version, renderer)
            raise GraphicsError(graphics_message(version, renderer, str(first))) from first
    try:
        log.info("graphics: %s, OpenGL %s", ctx.info.get("GL_RENDERER"), ctx.info.get("GL_VERSION"))
    except Exception:
        pass
    return ctx
