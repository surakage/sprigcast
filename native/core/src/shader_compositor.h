// The compositor's shaders: render/compositor.py's VERTEX_SHADER and FRAGMENT_SHADER, ported line for line to
// HLSL so a bloom looks the same on either engine. Change one, change the other (tests/test_native_compositor.py
// compares them pixel by pixel).
#pragma once

static const char COMPOSITOR_HLSL[] = R"HLSL(
cbuffer Draw : register(b0) {
    float4 corners[4];     // x, y (canvas pixels), u, v: top-left, top-right, bottom-left, bottom-right
    float4 canvas_quad;    // canvas_size.xy, quad_size.zw
    float4 p0;             // opacity, premul, planar, swizzle
    float4 yuv4;           // Kr, Kb, full range
    float4 plane_scroll;   // plane_size.xy, scroll.zw
    float4 p1;             // wrap, sharpen, key_mode, similarity
    float4 key4;           // key_color.xyz, smoothness
    float4 p2;             // spill, gamma, contrast, brightness
    float4 luma4;          // luma_key.xyz (min, max, smoothness), saturation
    float4 p3;             // hue, fx_opacity, use_lut, lut_amount
    float4 tint4;          // tint.xyz, lut_size
    float4 p4;             // shape, roundness, stretch, feather
    float4 border4;        // border_color.xyz, border
    float4 p5;             // mask_luma, blur, pixelate, blackout
    float4 p6;             // sil, sil_boost, sil_px, unused
    float4 sil_map;
    float4 uv_rect;
    float4 sil_radius4;    // xy
    float4 sil_color;
};

Texture2D tex : register(t0);
Texture3D lut : register(t1);
Texture2D mask : register(t2);
SamplerState wrap_s : register(s0);    // source pictures repeat at their edges, as moderngl's do
SamplerState clamp_s : register(s1);   // looks and shape pictures

#define canvas_size canvas_quad.xy
#define quad_size canvas_quad.zw
#define opacity p0.x
#define premul (p0.y > 0.5)
#define planar ((int)p0.z)
#define swizzle ((int)p0.w)
#define plane_size plane_scroll.xy
#define scroll plane_scroll.zw
#define wrap (p1.x > 0.5)
#define sharpen p1.y
#define key_mode ((int)p1.z)
#define similarity p1.w
#define key_color key4.xyz
#define smoothness key4.w
#define spill p2.x
#define gamma p2.y
#define contrast p2.z
#define brightness p2.w
#define luma_key luma4.xyz
#define saturation luma4.w
#define hue p3.x
#define fx_opacity p3.y
#define use_lut (p3.z > 0.5)
#define lut_amount p3.w
#define tint tint4.xyz
#define lut_size tint4.w
#define shape ((int)p4.x)
#define roundness p4.y
#define stretch (p4.z > 0.5)
#define feather p4.w
#define border_color border4.xyz
#define border border4.w
#define mask_luma (p5.x > 0.5)
#define blur p5.y
#define pixelate p5.z
#define blackout p5.w
#define sil ((int)p6.x)
#define sil_boost p6.y
#define sil_px p6.z
#define sil_radius sil_radius4.xy

struct VOut {
    float4 pos : SV_Position;
    float2 uv : TEXCOORD0;
    float2 local : TEXCOORD1;
};

VOut vs_main(uint id : SV_VertexID) {
    VOut o;
    float4 c = corners[id];
    o.local = float2(id & 1, id >> 1);
    o.uv = c.zw;
    o.pos = float4(c.x / canvas_size.x * 2.0 - 1.0, 1.0 - c.y / canvas_size.y * 2.0, 0.0, 1.0);
    return o;
}

static const float3 LUMA = float3(0.2126, 0.7152, 0.0722);
static const float TAU = 6.28318531;

float2 tex_size() {
    uint w, h;
    tex.GetDimensions(w, h);
    return float2(w, h);
}

float4 sample_tex(float2 st) {
    float4 c = tex.Sample(wrap_s, st);
    if (swizzle == 1) return c.bgra;
    if (swizzle == 2) return float4(c.rgb, 1.0);
    if (swizzle == 3) return float4(c.bgr, 1.0);
    return c;
}

float2 cbcr(float3 rgb) {
    return float2(dot(rgb, float3(-0.1146, -0.3854, 0.5)), dot(rgb, float3(0.5, -0.4542, -0.0458)));
}

float3 rotate_hue(float3 c, float a) {
    float3 k = float3(0.57735, 0.57735, 0.57735);
    float cs = cos(a);
    return c * cs + cross(k, c) * sin(a) + k * dot(k, c) * (1.0 - cs);
}

float dot2(float2 v) { return dot(v, v); }
float3 grey(float3 c) { float l = dot(c, LUMA); return float3(l, l, l); }
float fsign(float x) { return x > 0.0 ? 1.0 : (x < 0.0 ? -1.0 : 0.0); }

float sd_box(float2 p, float2 b, float r) {
    float2 q = abs(p) - b + r;
    return length(max(q, 0.0)) + min(max(q.x, q.y), 0.0) - r;
}

float sd_hexagon(float2 p, float r) {
    const float3 k = float3(-0.866025404, 0.5, 0.577350269);
    p = abs(p.yx);
    p -= 2.0 * min(dot(k.xy, p), 0.0) * k.xy;
    p -= float2(clamp(p.x, -k.z * r, k.z * r), r);
    return length(p) * fsign(p.y);
}

float sd_vesica(float2 p, float r, float d) {
    p = abs(p);
    float b = sqrt(r * r - d * d);
    return ((p.y - b) * d > p.x * b) ? length(p - float2(0.0, b)) : length(p - float2(-d, 0.0)) - r;
}

float sd_heart(float2 p) {
    p.x = abs(p.x);
    if (p.y + p.x > 1.0) return sqrt(dot2(p - float2(0.25, 0.75))) - sqrt(2.0) / 4.0;
    return sqrt(min(dot2(p - float2(0.0, 1.0)), dot2(p - 0.5 * max(p.x + p.y, 0.0)))) * fsign(p.x - p.y);
}

float sd_star(float2 p, float r, float rf) {
    const float2 k1 = float2(0.809016994375, -0.587785252292);
    const float2 k2 = float2(-k1.x, k1.y);
    p.x = abs(p.x);
    p -= 2.0 * max(dot(k1, p), 0.0) * k1;
    p -= 2.0 * max(dot(k2, p), 0.0) * k2;
    p.x = abs(p.x);
    p.y -= r;
    float2 ba = rf * float2(-k1.y, k1.x) - float2(0.0, 1.0);
    float h = clamp(dot(p, ba) / dot(ba, ba), 0.0, r);
    return length(p - ba * h) * fsign(p.y * ba.x - p.x * ba.y);
}

float sd_octagon(float2 p, float r) {
    const float3 k = float3(-0.9238795325, 0.3826834323, 0.4142135623);
    p = abs(p);
    p -= 2.0 * min(dot(float2(k.x, k.y), p), 0.0) * float2(k.x, k.y);
    p -= 2.0 * min(dot(float2(-k.x, k.y), p), 0.0) * float2(-k.x, k.y);
    p -= float2(clamp(p.x, -k.z * r, k.z * r), r);
    return length(p) * fsign(p.y);
}

float sd_pentagon(float2 p, float r) {
    const float3 k = float3(0.809016994, 0.587785252, 0.726542528);
    p.x = abs(p.x);
    p -= 2.0 * min(dot(float2(-k.x, k.y), p), 0.0) * float2(-k.x, k.y);
    p -= 2.0 * min(dot(float2(k.x, k.y), p), 0.0) * float2(k.x, k.y);
    p -= float2(clamp(p.x, -r * k.z, r * k.z), r);
    return length(p) * fsign(p.y);
}

float sd_triangle(float2 p, float r) {
    const float k = 1.7320508;
    p.x = abs(p.x) - r;
    p.y = p.y + r / k;
    if (p.x + k * p.y > 0.0) p = float2(p.x - k * p.y, -k * p.x - p.y) / 2.0;
    p.x -= clamp(p.x, -2.0 * r, 0.0);
    return -length(p) * fsign(p.y);
}


float shape_distance(float2 l) {
    float2 size = max(quad_size, float2(1.0, 1.0));
    float side = min(size.x, size.y);
    if (shape == 1) {
        return sd_box((l - 0.5) * size, size * 0.5, roundness * side);
    }
    float2 p = stretch ? (l - 0.5) * side : (l - 0.5) * size;
    float r = side * 0.5;
    float d;
    if (shape == 2) {
        d = length(p) - r;
    } else if (shape == 3) {
        d = sd_hexagon(p, r * 0.866);
    } else if (shape == 4) {
        float2 q = float2(p.x + p.y, p.y - p.x) * 0.70710678;
        d = sd_vesica(q, r * 1.65, r * 1.0);
    } else if (shape == 5) {
        float s = r * 1.62;
        d = sd_heart(float2(p.x, -p.y) / s + float2(0.0, 0.53)) * s;
    } else if (shape == 6) {
        d = sd_star(float2(p.x, -p.y + r * 0.1), r, 0.45);
    } else if (shape == 8) {   // diamond
        d = (abs(p.x) + abs(p.y) - r) * 0.70710678;
    } else if (shape == 9) {   // octagon
        d = sd_octagon(p, r * 0.92);
    } else if (shape == 10) {  // pentagon, a point at the top
        d = sd_pentagon(float2(p.x, p.y - r * 0.1), r * 0.8);
    } else if (shape == 11) {  // triangle, a point at the top
        d = sd_triangle(float2(p.x, -p.y + r * 0.245), r * 0.85);
    } else if (shape == 12) {  // arch: a round top on a straight-sided bottom
        float2 q = float2(p.x, -p.y);
        float top = length(q - float2(0.0, r * 0.15)) - r * 0.75;
        float2 b = abs(q - float2(0.0, -r * 0.3)) - float2(r * 0.75, r * 0.45);
        d = min(top, length(max(b, 0.0)) + min(max(b.x, b.y), 0.0));
    } else if (shape == 13) {  // flower: five round petals, one at the top
        float a = atan2(p.y, p.x);
        d = (length(p) - r * (0.8 + 0.17 * cos(5.0 * a + 7.853982))) * 0.75;
    } else {                   // squircle
        float2 q = abs(p) / r;
        d = (pow(pow(q.x, 4.0) + pow(q.y, 4.0), 0.25) - 0.98) * r;
    }
    if (stretch) {
        d *= min(size.x, size.y) / side;
    }
    return d;
}

float4 src(float2 st) {
    if (planar == 0) {
        return sample_tex(st);
    }
    float2 ts = tex_size();
    float2 lp = plane_size;
    float2 cp = lp * 0.5;
    float2 p = clamp(st * lp, float2(0.5, 0.5), lp - 0.5);
    float2 q = clamp(st * cp, float2(0.5, 0.5), cp - 0.5);
    float y = tex.Sample(wrap_s, p / ts).r;
    float u = tex.Sample(wrap_s, float2(q.x, lp.y + q.y) / ts).r;
    float v = tex.Sample(wrap_s, float2(cp.x + q.x, lp.y + q.y) / ts).r;
    float a = planar == 2 ? tex.Sample(wrap_s, float2(p.x, lp.y + cp.y + p.y) / ts).r : 1.0;
    if (yuv4.z > 0.5) {
        u -= 128.0 / 255.0;
        v -= 128.0 / 255.0;
    } else {
        y = (y * 255.0 - 16.0) / 219.0;
        u = (u * 255.0 - 128.0) / 224.0;
        v = (v * 255.0 - 128.0) / 224.0;
    }
    float kr = yuv4.x, kb = yuv4.y;
    float r = y + 2.0 * (1.0 - kr) * v;
    float b = y + 2.0 * (1.0 - kb) * u;
    float g = (y - kr * r - kb * b) / (1.0 - kr - kb);
    return float4(saturate(float3(r, g, b)), a);
}

float4 blurred(float2 st, float2 r) {
    float4 acc = float4(0.0, 0.0, 0.0, 0.0);
    float total = 0.0;
    [loop] for (int ring = 0; ring <= 2; ring++) {
        int n = ring == 0 ? 1 : 12;
        float w = ring == 0 ? 1.0 : (ring == 1 ? 0.8 : 0.45);
        [loop] for (int i = 0; i < n; i++) {
            float a = (float(i) + 0.5 * float(ring)) * TAU / 12.0;
            float4 c = src(clamp(st + float2(cos(a), sin(a)) * r * (float(ring) / 2.0), float2(0.0, 0.0), float2(1.0, 1.0)));
            acc += (premul ? c : float4(c.rgb * c.a, c.a)) * w;
            total += w;
        }
    }
    acc /= total;
    return premul ? acc : float4(acc.rgb / max(acc.a, 0.0001), acc.a);
}

float key_keep(float3 rgb) {
    if (key_mode == 1) {
        return pow(saturate((distance(cbcr(rgb), cbcr(key_color)) - similarity) / smoothness), 1.5);
    } else if (key_mode == 2) {
        float l = dot(rgb, LUMA);
        float s = max(luma_key.z, 0.0001);
        return smoothstep(luma_key.x - s, luma_key.x, l) * (1.0 - smoothstep(luma_key.y, luma_key.y + s, l));
    } else if (key_mode == 3) {
        return saturate((distance(rgb, key_color) - similarity) / smoothness);
    }
    return 1.0;
}

float body(float2 l) {
    if (l.x < 0.0 || l.y < 0.0 || l.x > 1.0 || l.y > 1.0) {
        return 0.0;
    }
    float4 c = src(lerp(uv_rect.xy, uv_rect.zw, l));
    float a = c.a;
    if (key_mode > 0) {
        a *= key_keep(premul && c.a > 0.0 ? c.rgb / c.a : c.rgb);
    }
    if (shape == 7) {
        float4 m = mask.SampleLevel(clamp_s, l, 0);
        a *= mask_luma ? dot(m.rgb, LUMA) : m.a;
    } else if (shape > 0) {
        a *= 1.0 - smoothstep(-feather - 0.75, 0.75, shape_distance(l));
    }
    return a;
}

float hash(float2 p) {
    return frac(sin(dot(p, float2(12.9898, 78.233))) * 43758.5453);
}

float4 halo(float2 local, float2 frag) {
    float2 l = local * sil_map.xy + sil_map.zw;
    float a;
    if (shape >= 1 && shape != 7) {   // drawn shapes (7 is a picture)
        float d = shape_distance(l);
        float r = max(sil_px, 0.75);
        a = sil == 1 ? saturate((1.0 - smoothstep(-r, r, d)) * sil_boost)
                     : 1.0 - smoothstep(r - 0.75, r + 0.75, d);
    } else if (sil == 1) {
        float turn = hash(frag) * TAU;
        a = body(l);
        float total = 1.0;
        [loop] for (int ring = 1; ring <= 3; ring++) {
            float w = 1.0 - 0.25 * float(ring);
            [loop] for (int i = 0; i < 12; i++) {
                float t = turn + (float(i) + 0.33 * float(ring)) * TAU / 12.0;
                a += body(l + float2(cos(t), sin(t)) * sil_radius * (float(ring) / 3.0)) * w;
                total += w;
            }
        }
        a = min(1.0, a / total * sil_boost);
    } else {
        a = body(l);
        [loop] for (int ring = 1; ring <= 2; ring++) {
            [loop] for (int i = 0; i < 16; i++) {
                float t = (float(i) + 0.5 * float(ring)) * TAU / 16.0;
                a = max(a, body(l + float2(cos(t), sin(t)) * sil_radius * (float(ring) / 2.0)));
            }
        }
    }
    return float4(sil_color.rgb, a * sil_color.a * fx_opacity * opacity);
}

float4 ps_main(VOut i) : SV_Target {
    // OpenGL counts fragment rows from the bottom: the same noise as the Python engine's halos.
    float2 frag = float2(i.pos.x, canvas_size.y - i.pos.y);
    if (sil > 0) {
        return halo(i.local, frag);
    }
    float2 st = wrap ? frac(i.uv + scroll) : i.uv;
    float2 tpx = 1.0 / (planar > 0 ? plane_size : tex_size());
    if (pixelate > 0.0) {
        float2 cell = tpx * pixelate;
        st = clamp((floor(st / cell) + 0.5) * cell, tpx * 0.5, 1.0 - tpx * 0.5);
    }
    float4 c = blur > 0.0 ? blurred(st, tpx * blur) : src(st);
    if (premul && c.a > 0.0) {
        c.rgb /= c.a;
    }
    if (sharpen > 0.0) {
        float2 px = tpx;
        float3 around = src(st + float2(px.x, 0.0)).rgb + src(st - float2(px.x, 0.0)).rgb
                      + src(st + float2(0.0, px.y)).rgb + src(st - float2(0.0, px.y)).rgb;
        c.rgb = saturate(c.rgb + (4.0 * c.rgb - around) * sharpen);
    }
    if (key_mode == 1) {
        float base = distance(cbcr(c.rgb), cbcr(key_color)) - similarity;
        c.a *= key_keep(c.rgb);
        float keep = pow(saturate(base / spill), 1.5);
        c.rgb = lerp(grey(c.rgb), c.rgb, keep);
    } else if (key_mode > 1) {
        c.a *= key_keep(c.rgb);
    }
    c.rgb = pow(max(c.rgb, 0.0), float3(gamma, gamma, gamma));
    c.rgb = (c.rgb - 0.5) * contrast + 0.5 + brightness;
    c.rgb = lerp(grey(c.rgb), c.rgb, saturation);
    if (hue != 0.0) {
        c.rgb = rotate_hue(c.rgb, hue);
    }
    c.rgb = saturate(c.rgb * tint);
    if (use_lut) {
        float3 at = c.rgb * ((lut_size - 1.0) / lut_size) + 0.5 / lut_size;
        c.rgb = lerp(c.rgb, lut.SampleLevel(clamp_s, at, 0).rgb, lut_amount);
    }
    if (shape == 7) {
        float4 m = mask.SampleLevel(clamp_s, i.local, 0);
        c.a *= mask_luma ? dot(m.rgb, LUMA) : m.a;
    } else if (shape > 0) {
        float d = shape_distance(i.local);
        const float aa = 0.75;
        if (border > 0.0) {
            float ring = smoothstep(-border - aa, -border + aa, d);
            c.rgb = lerp(c.rgb, border_color, ring);
            c.a = lerp(c.a, 1.0, ring);
        }
        c.a *= 1.0 - smoothstep(-feather - aa, aa, d);
    }
    c.rgb = lerp(c.rgb, float3(0.0, 0.0, 0.0), blackout);
    return c * float4(1.0, 1.0, 1.0, opacity * fx_opacity);
}
)HLSL";
