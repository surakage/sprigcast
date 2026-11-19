// Transitions (transition.cpp): render/compositor.py's, line for line.
// Baked into bytecode when the engine is built (tools/shader_bake.cpp): nothing is compiled at run time.
#pragma once

static const char TRANSITION_HLSL[] = R"HLSL(
cbuffer Transition : register(b0) {
    float2 out_size;
    float progress;
    float softness;
    float2 dir;
    int mode;
    int use_map;
    float level_max;
    float3 unused;
    float4 overlay_rect;  // stinger: clip uv = xy + canvas uv * zw (the clip covers the canvas)
    float2 matte_offset;  // stinger: where the matte half starts, in clip uv; (0, 0) = no matte
    int show_overlay;
    int cut;              // stinger: past the cut point
    int overlay_swizzle;  // stinger: 1 = the clip's picture is BGRA
    float3 unused2;
};
Texture2D old_scene : register(t0);
Texture2D new_scene : register(t1);
Texture2D wipe_map : register(t2);
Texture2D overlay : register(t3);  // stinger: the clip's picture (straight alpha)
SamplerState smooth : register(s0);
SamplerState edge : register(s1);

static const float3 LUMA = float3(0.2126, 0.7152, 0.0722);
static const float LEAF_TURN = 0.785;
static const float LEAF_SPIN = 0.5;
static const float BLOOM_SPIN = 1.2;

float4 vs_main(uint id : SV_VertexID) : SV_Position {
    float2 p = float2((id << 1) & 2, id & 2);  // one triangle over the whole target
    return float4(p * 2.0 - 1.0, 0.0, 1.0);
}

bool inside(float2 uv) {
    return uv.x >= 0.0 && uv.y >= 0.0 && uv.x <= 1.0 && uv.y <= 1.0;
}

float4 at(Texture2D t, float2 uv) {
    if (!inside(uv)) return float4(0, 0, 0, 0);
    return t.SampleLevel(smooth, uv, 0);
}

float ease(float p) {
    return p * p * (3.0 - 2.0 * p);
}

float leaf_level(float2 q, float angle) {
    float c = cos(angle), s = sin(angle);
    float2 r = float2(c * q.x + s * q.y, -s * q.x + c * q.y);
    return abs(r.y) + length(r);
}

float bloom_level(float2 q, float spin) {
    float a = atan2(q.y, q.x);
    return length(q) / (0.72 + 0.28 * cos(5.0 * a + spin));
}

float4 blend(float4 a, float4 b, float amount) {
    float4 old = a * (1.0 - amount);
    return old + b * (1.0 - old.a);
}

float reveal(float level, float p) {
    float s = max(softness, 0.001);
    float edge = p * (1.0 + s);
    return 1.0 - smoothstep(edge - s, edge, level);
}

float4 ps_main(float4 pos : SV_Position) : SV_Target {
    float2 uv = pos.xy / out_size;
    float4 a = old_scene.SampleLevel(smooth, uv, 0);
    float4 b = new_scene.SampleLevel(smooth, uv, 0);
    float p = clamp(progress, 0.0, 1.0);
    float2 q = (uv - 0.5) * float2(out_size.x / out_size.y, 1.0);
    if (mode == 2) {
        float e = ease(p);
        float2 in_uv = uv + dir * (1.0 - e);
        return inside(in_uv) ? new_scene.SampleLevel(smooth, in_uv, 0) : at(old_scene, uv - dir * e);
    }
    if (mode == 3) {
        float2 in_uv = uv + dir * (1.0 - ease(p));
        return inside(in_uv) ? new_scene.SampleLevel(smooth, in_uv, 0) : a;
    }
    if (mode == 4) {
        float level = use_map ? dot(wipe_map.SampleLevel(smooth, uv, 0).rgb, LUMA)
                              : dot(uv, dir) + max(0.0, -(dir.x + dir.y));
        return blend(a, b, reveal(level, p));
    }
    if (mode == 5) return blend(a, b, reveal(leaf_level(q, LEAF_TURN + LEAF_SPIN * p) / level_max, p));
    if (mode == 6) return blend(a, b, reveal(bloom_level(q, BLOOM_SPIN * p) / level_max, p));
    if (mode == 8) {  // render/compositor.py's stinger, line for line
        float2 st = overlay_rect.xy + uv * overlay_rect.zw;
        float4 base;
        if (matte_offset.x != 0.0 || matte_offset.y != 0.0) {
            float4 m = overlay.SampleLevel(edge, st + matte_offset, 0);
            base = blend(a, b, dot((overlay_swizzle ? m.bgr : m.rgb), LUMA));
        } else {
            base = cut ? b : a;
        }
        if (show_overlay) {
            float4 o = overlay.SampleLevel(edge, st, 0);
            if (overlay_swizzle) o = o.bgra;
            base = float4(o.rgb * o.a, o.a) + base * (1.0 - o.a);
        }
        return base;
    }
    return blend(a, b, p);
}
)HLSL";
