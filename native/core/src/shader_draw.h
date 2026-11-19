// The shaders the pixel checks and the first canvas draw with (sprigcore.cpp).
// Baked into bytecode when the engine is built (tools/shader_bake.cpp): nothing is compiled at run time.
#pragma once

static const char SHADER[] = R"HLSL(
cbuffer Draw : register(b0) { float4 rect; float opacity; float3 unused; };
Texture2D picture : register(t0);
SamplerState smooth : register(s0);
struct Out { float4 pos : SV_Position; float2 uv : TEXCOORD0; };
Out vs_main(uint id : SV_VertexID) {
    float2 uv = float2(id & 1, id >> 1);
    Out o;
    o.uv = uv;
    o.pos = float4(rect.xy + uv * rect.zw, 0, 1);
    return o;
}
float4 ps_main(Out i) : SV_Target {
    float4 c = picture.Sample(smooth, i.uv);
    c.a *= opacity;
    return c;
}
)HLSL";
