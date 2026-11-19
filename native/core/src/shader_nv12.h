// Canvas to NV12 for the encoders (nv12.cpp).
// Baked into bytecode when the engine is built (tools/shader_bake.cpp): nothing is compiled at run time.
#pragma once

static const char NV12_HLSL[] = R"HLSL(
cbuffer Nv12 : register(b0) {
    float2 out_size;   // size of the plane being drawn, in pixels
    float2 texel;      // one output pixel of the full-size picture, in canvas uv
    int chroma;        // 0: Y plane, 1: interleaved CbCr at half size
    int scale_mode;    // scaling down: 0 bilinear, 1 area, 2 bicubic, 3 lanczos
    float2 unused;
};
Texture2D canvas : register(t0);
SamplerState smooth : register(s0);

float4 vs_main(uint id : SV_VertexID) : SV_Position {
    float2 p = float2((id << 1) & 2, id & 2);  // one triangle over the whole target
    return float4(p * 2.0 - 1.0, 0.0, 1.0);
}

float3 rgb_at(float2 uv) {
    return canvas.SampleLevel(smooth, uv, 0).rgb;  // premultiplied: already over black (alpha is dropped)
}

float cubic(float x) {  // Catmull-Rom
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

float3 scaled_at(float2 uv) {
    uint sw, sh;
    canvas.GetDimensions(sw, sh);
    float2 src = float2(sw, sh);
    if (scale_mode == 0 || all(abs(src * texel - 1.0) < 0.001)) return rgb_at(uv);
    if (scale_mode == 1) {
        float2 q = texel * 0.25;
        return 0.25 * (rgb_at(uv + float2(-q.x, -q.y)) + rgb_at(uv + float2(q.x, -q.y))
                     + rgb_at(uv + float2(-q.x, q.y)) + rgb_at(uv + float2(q.x, q.y)));
    }
    float2 p = uv * src - 0.5;
    float2 base = floor(p);
    float2 f = p - base;
    int r = scale_mode == 2 ? 2 : 3;
    float3 sum = 0.0;
    float total = 0.0;
    [loop] for (int j = -r + 1; j <= r; j++) {
        [loop] for (int i = -r + 1; i <= r; i++) {
            float2 d = float2(i, j) - f;
            float w = scale_mode == 2 ? cubic(d.x) * cubic(d.y) : lanczos(d.x) * lanczos(d.y);
            int2 at = int2(clamp(base + float2(i, j), 0.0, src - 1.0));
            sum += w * canvas.Load(int3(at, 0)).rgb;
            total += w;
        }
    }
    return clamp(sum / total, 0.0, 1.0);
}

float4 ps_main(float4 pos : SV_Position) : SV_Target {
    float2 uv = pos.xy / out_size;
    if (chroma == 0) {
        float3 c = scaled_at(uv);
        return float4((16.0 + 219.0 * dot(c, float3(0.2126, 0.7152, 0.0722))) / 255.0, 0.0, 0.0, 1.0);
    }
    float3 c = 0.25 * (rgb_at(uv + texel * float2(-0.5, -0.5)) + rgb_at(uv + texel * float2(0.5, -0.5))
                     + rgb_at(uv + texel * float2(-0.5, 0.5)) + rgb_at(uv + texel * float2(0.5, 0.5)));
    float cb = 128.0 + 224.0 * dot(c, float3(-0.1146, -0.3854, 0.5));
    float cr = 128.0 + 224.0 * dot(c, float3(0.5, -0.4542, -0.0458));
    return float4(cb / 255.0, cr / 255.0, 0.0, 1.0);
}
)HLSL";
