// The person finder's compute shaders (segmenter.cpp): MediaPipe's selfie segmentation model run layer by layer
// on the graphics card, plus the blurred room behind the person. Every tensor lives in one float buffer
// (channels, rows, columns), as tools/make_segmenter.py lays it out; tools/make_segmenter.py run_program is the
// same arithmetic in numpy (tests/test_native_background.py compares them).
#pragma once

static const char SEGMENTER_HLSL[] = R"HLSL(
cbuffer Op : register(b0) {
    int4 a;    // src, src2, dst, weights
    int4 b;    // bias, cin, cout, h
    int4 c;    // w, oh, ow, k
    int4 d;    // stride, pad, act, total
    float4 f;  // cs_mask: how much of the new mask to take; cs_blur: radius, sigma
};

#define SRC a.x
#define SRC2 a.y
#define DST a.z
#define WTS a.w
#define BIAS b.x
#define CIN b.y
#define COUT b.z
#define H b.w
#define W c.x
#define OH c.y
#define OW c.z
#define K c.w
#define STRIDE d.x
#define PAD d.y
#define ACT d.z
#define TOTAL d.w

RWStructuredBuffer<float> arena : register(u0);
RWTexture2D<float> mask_rw : register(u1);
RWTexture2D<float4> blur_out : register(u2);
StructuredBuffer<float> weights : register(t0);
Texture2D<float4> picture : register(t1);
Texture2D<float> mask_in : register(t2);
Texture2D<float4> blur_in : register(t3);
SamplerState linear_clamp : register(s0);

float activate(float x) {
    if (ACT == 1) return max(x, 0.0);
    if (ACT == 2) return x * saturate((x + 3.0) / 6.0);
    if (ACT == 3) return 1.0 / (1.0 + exp(-x));
    return x;
}

uint flat(uint3 g) { return g.x + g.y * 65535u * 64u; }

[numthreads(64, 1, 1)]
void cs_conv(uint3 id : SV_DispatchThreadID) {
    int i = (int)flat(id);
    if (i >= TOTAL) return;
    int x = i % OW, y = (i / OW) % OH, o = i / (OW * OH);
    float sum = weights[BIAS + o];
    int y0 = y * STRIDE - PAD, x0 = x * STRIDE - PAD;
    [loop] for (int ci = 0; ci < CIN; ci++) {
        int wbase = WTS + (o * CIN + ci) * K * K;
        int sbase = SRC + ci * H * W;
        [loop] for (int ky = 0; ky < K; ky++) {
            int iy = y0 + ky;
            if (iy < 0 || iy >= H) continue;
            [loop] for (int kx = 0; kx < K; kx++) {
                int ix = x0 + kx;
                if (ix < 0 || ix >= W) continue;
                sum += weights[wbase + ky * K + kx] * arena[sbase + iy * W + ix];
            }
        }
    }
    arena[DST + i] = activate(sum);
}

[numthreads(64, 1, 1)]
void cs_dwconv(uint3 id : SV_DispatchThreadID) {
    int i = (int)flat(id);
    if (i >= TOTAL) return;
    int x = i % OW, y = (i / OW) % OH, o = i / (OW * OH);
    float sum = weights[BIAS + o];
    int y0 = y * STRIDE - PAD, x0 = x * STRIDE - PAD;
    int wbase = WTS + o * K * K;
    int sbase = SRC + o * H * W;
    [loop] for (int ky = 0; ky < K; ky++) {
        int iy = y0 + ky;
        if (iy < 0 || iy >= H) continue;
        [loop] for (int kx = 0; kx < K; kx++) {
            int ix = x0 + kx;
            if (ix < 0 || ix >= W) continue;
            sum += weights[wbase + ky * K + kx] * arena[sbase + iy * W + ix];
        }
    }
    arena[DST + i] = activate(sum);
}

[numthreads(64, 1, 1)]
void cs_gap(uint3 id : SV_DispatchThreadID) {
    int ch = (int)flat(id);
    if (ch >= TOTAL) return;
    float sum = 0.0;
    int n = H * W;
    int base = SRC + ch * n;
    [loop] for (int j = 0; j < n; j++) sum += arena[base + j];
    arena[DST + ch] = sum / float(n);
}

[numthreads(64, 1, 1)]
void cs_scale(uint3 id : SV_DispatchThreadID) {
    int i = (int)flat(id);
    if (i >= TOTAL) return;
    arena[DST + i] = arena[SRC + i] * arena[SRC2 + i / (H * W)];
}

[numthreads(64, 1, 1)]
void cs_add(uint3 id : SV_DispatchThreadID) {
    int i = (int)flat(id);
    if (i >= TOTAL) return;
    arena[DST + i] = arena[SRC + i] + arena[SRC2 + i];
}

// 2x bilinear, half-pixel centers, edges clamped (ONNX Resize "linear", "half_pixel").
[numthreads(64, 1, 1)]
void cs_resize(uint3 id : SV_DispatchThreadID) {
    int i = (int)flat(id);
    if (i >= TOTAL) return;
    int x = i % OW, y = (i / OW) % OH, ch = i / (OW * OH);
    float py = clamp((float(y) + 0.5) * 0.5 - 0.5, 0.0, float(H - 1));
    float px = clamp((float(x) + 0.5) * 0.5 - 0.5, 0.0, float(W - 1));
    int y0 = (int)floor(py), x0 = (int)floor(px);
    int y1 = min(y0 + 1, H - 1), x1 = min(x0 + 1, W - 1);
    float fy = py - float(y0), fx = px - float(x0);
    int base = SRC + ch * H * W;
    float top = lerp(arena[base + y0 * W + x0], arena[base + y0 * W + x1], fx);
    float bottom = lerp(arena[base + y1 * W + x0], arena[base + y1 * W + x1], fx);
    arena[DST + i] = lerp(top, bottom, fy);
}

// A 2x2, stride 2 transposed convolution (weights cin, cout, 2, 2).
[numthreads(64, 1, 1)]
void cs_convt(uint3 id : SV_DispatchThreadID) {
    int i = (int)flat(id);
    if (i >= TOTAL) return;
    int x = i % OW, y = (i / OW) % OH, o = i / (OW * OH);
    int sx = x / 2, sy = y / 2, k = (y % 2) * 2 + (x % 2);
    float sum = weights[BIAS + o];
    [loop] for (int ci = 0; ci < CIN; ci++)
        sum += weights[WTS + (ci * COUT + o) * 4 + k] * arena[SRC + (ci * H + sy) * W + sx];
    arena[DST + i] = activate(sum);
}

// The model's input: the picture (drawn small into `picture`) as red, green and blue planes, 0 to 1.
[numthreads(64, 1, 1)]
void cs_input(uint3 id : SV_DispatchThreadID) {
    int i = (int)flat(id);
    if (i >= TOTAL) return;
    int x = i % W, y = i / W;
    float4 p = picture.Load(int3(x, y, 0));
    float3 rgb = p.a > 0.0 ? saturate(p.rgb / p.a) : float3(0.5, 0.5, 0.5);
    rgb = lerp(float3(0.5, 0.5, 0.5), rgb, p.a);
    int n = W * H;
    arena[DST + i] = rgb.r;
    arena[DST + n + i] = rgb.g;
    arena[DST + 2 * n + i] = rgb.b;
}

// The model's answer into the mask texture, mixed with the last one so edges don't flicker.
[numthreads(64, 1, 1)]
void cs_mask(uint3 id : SV_DispatchThreadID) {
    int i = (int)flat(id);
    if (i >= TOTAL) return;
    uint2 at = uint2(i % W, i / W);
    float now = arena[SRC + i];
    mask_rw[at] = lerp(mask_rw[at], now, f.x);
}

float gauss(float x) { return exp(-0.5 * x * x / (f.y * f.y)); }

// The room blurred, leaving the person out (their colors don't bleed into it): across, then down.
// blur_out holds (color x room weight, room weight).
[numthreads(8, 8, 1)]
void cs_blur_across(uint3 id : SV_DispatchThreadID) {
    uint w, h;
    picture.GetDimensions(w, h);
    if (id.x >= w || id.y >= h) return;
    int r = (int)f.x;
    float4 acc = float4(0.0, 0.0, 0.0, 0.0);
    float total = 0.0;
    [loop] for (int t = -r; t <= r; t++) {
        int x = clamp((int)id.x + t, 0, (int)w - 1);
        float2 uv = (float2(x, id.y) + 0.5) / float2(w, h);
        float room = 1.0 - mask_in.SampleLevel(linear_clamp, uv, 0) + 0.002;
        float g = gauss(float(t));
        float4 p = picture.Load(int3(x, id.y, 0));
        float3 rgb = p.a > 0.0 ? saturate(p.rgb / p.a) : float3(0.0, 0.0, 0.0);
        acc += float4(rgb * room, room) * g;
        total += g;
    }
    blur_out[id.xy] = acc / total;
}

[numthreads(8, 8, 1)]
void cs_blur_down(uint3 id : SV_DispatchThreadID) {
    uint w, h;
    blur_in.GetDimensions(w, h);
    if (id.x >= w || id.y >= h) return;
    int r = (int)f.x;
    float4 acc = float4(0.0, 0.0, 0.0, 0.0);
    [loop] for (int t = -r; t <= r; t++) {
        int y = clamp((int)id.y + t, 0, (int)h - 1);
        acc += blur_in.Load(int3(id.x, y, 0)) * gauss(float(t));
    }
    blur_out[id.xy] = float4(acc.rgb / max(acc.a, 1e-5), 1.0);
}
)HLSL";
