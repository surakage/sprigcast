// A planar picture repacked on the card (nv12_source.cpp).
// Baked into bytecode when the engine is built (tools/shader_bake.cpp): nothing is compiled at run time.
#pragma once

static const char REPACK_HLSL[] = R"HLSL(
cbuffer Repack : register(b0) {
    int width;    // the picture's, in pixels (even)
    int height;
    int2 unused;
};
Texture2D<float> luma : register(t0);
Texture2D<float2> chroma : register(t1);

float4 vs_main(uint id : SV_VertexID) : SV_Position {
    float2 p = float2((id << 1) & 2, id & 2);  // one triangle over the whole target
    return float4(p * float2(2.0, -2.0) + float2(-1.0, 1.0), 0.0, 1.0);
}

float4 ps_main(float4 pos : SV_Position) : SV_Target {
    int2 p = int2(pos.xy);
    if (p.y < height) return float4(luma.Load(int3(p, 0)), 0.0, 0.0, 1.0);
    int half_w = width / 2;
    int row = p.y - height;
    bool v = p.x >= half_w;
    float2 uv = chroma.Load(int3(v ? p.x - half_w : p.x, row, 0));
    return float4(v ? uv.y : uv.x, 0.0, 0.0, 1.0);
}
)HLSL";
