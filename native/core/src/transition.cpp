// Transitions (phase 2): the outgoing and incoming scenes are drawn to pictures of their own (colours already
// multiplied by alpha), then one pass mixes them into the canvas, line for line as render/compositor.py's
// TRANSITION_FRAGMENT does. Both use a top-left origin for uv (OpenGL's first row is the canvas's top row there),
// so the maths ports without flips.
#include "internal.h"

#include <algorithm>
#include <cmath>
#include <cstring>

using namespace sc;

namespace {

const char TRANSITION_HLSL[] = R"(
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
)";

struct TransitionConstants {
    float out_size[2];
    float progress;
    float softness;
    float dir[2];
    int mode;
    int use_map;
    float level_max;
    float unused[3];
    float overlay_rect[4];
    float matte_offset[2];
    int show_overlay;
    int cut;
    int overlay_swizzle;
    float unused2[3];
};
static_assert(sizeof(TransitionConstants) == 96, "matches the shader's constant buffer");

constexpr double LEAF_TURN = 0.785, LEAF_SPIN = 0.5, BLOOM_SPIN = 1.2;

// render/compositor.py shape_reach: how big the leaf or bloom must grow to cover the canvas (its level at the
// furthest of 64 points on each edge, centred, height 1), plus 2%.
float shape_reach(int mode, int width, int height, float progress) {
    const double hx = static_cast<double>(width) / std::max(1, height) / 2.0, hy = 0.5;
    double best = 0;
    for (int side = 0; side < 4; ++side) {
        for (int i = 0; i < 64; ++i) {
            const double t = i / 64.0;
            double qx, qy;
            if (side == 0) { qx = hx * (2 * t - 1); qy = -hy; }
            else if (side == 1) { qx = hx * (2 * t - 1); qy = hy; }
            else if (side == 2) { qx = -hx; qy = hy * (2 * t - 1); }
            else { qx = hx; qy = hy * (2 * t - 1); }
            double level;
            if (mode == SC_TRANSITION_LEAF) {
                const double angle = LEAF_TURN + LEAF_SPIN * progress, c = std::cos(angle), s = std::sin(angle);
                const double u = c * qx + s * qy, v = -s * qx + c * qy;
                level = std::abs(v) + std::hypot(u, v);
            } else {
                const double a = std::atan2(qy, qx);
                level = std::hypot(qx, qy) / (0.72 + 0.28 * std::cos(5 * a + BLOOM_SPIN * progress));
            }
            best = std::max(best, level);
        }
    }
    return static_cast<float>(best * 1.02);
}

}  // namespace

int sc_device::prepare_transitions() {
    if (trans_vs) return SC_OK;
    int ready = prepare_compositor();  // its wrap sampler
    if (ready != SC_OK) return ready;
    ComPtr<ID3DBlob> vs_code, ps_code;
    HRESULT hr = compile(TRANSITION_HLSL, sizeof TRANSITION_HLSL - 1, "vs_main", "vs_4_0", vs_code);
    if (FAILED(hr)) return fail_hr(SC_ERR_SHADER, "Compiling the transition's vertex shader", hr);
    hr = compile(TRANSITION_HLSL, sizeof TRANSITION_HLSL - 1, "ps_main", "ps_4_0", ps_code);
    if (FAILED(hr)) return fail_hr(SC_ERR_SHADER, "Compiling the transition's pixel shader", hr);
    ComPtr<ID3D11VertexShader> new_vs;
    hr = device->CreateVertexShader(vs_code->GetBufferPointer(), vs_code->GetBufferSize(), nullptr, &new_vs);
    if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "CreateVertexShader (transition)", hr);
    hr = device->CreatePixelShader(ps_code->GetBufferPointer(), ps_code->GetBufferSize(), nullptr, &trans_ps);
    if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "CreatePixelShader (transition)", hr);
    D3D11_BUFFER_DESC cb = {};
    cb.ByteWidth = sizeof(TransitionConstants);
    cb.Usage = D3D11_USAGE_DYNAMIC;
    cb.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
    cb.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
    hr = device->CreateBuffer(&cb, nullptr, &trans_constants);
    if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "CreateBuffer (transition)", hr);
    trans_vs = new_vs;  // last: it says the rest is ready
    return SC_OK;
}

int sc::render_transition(sc_canvas &canvas, uint64_t from, uint64_t to, double time, const sc_transition &how,
                          float progress) {
    sc_device &dev = *canvas.device;
    if (progress >= 1.0f || !has_scene(dev, from))
        return render_scene(dev, canvas.target.Get(), canvas.width, canvas.height, to, time);
    int ready = dev.prepare_transitions();
    if (ready != SC_OK) return ready;
    const int w = canvas.width, h = canvas.height;
    for (Target *t : {&canvas.outgoing, &canvas.incoming}) {
        if (!t->texture || t->width != w || t->height != h) {
            int made = make_target(dev.device.Get(), w, h, *t);
            if (made != SC_OK) return made;
        }
    }
    canvas.transition_used = time;
    int code = render_scene(dev, canvas.outgoing.target.Get(), w, h, from, time);
    if (code != SC_OK) return code;
    code = render_scene(dev, canvas.incoming.target.Get(), w, h, to, time);
    if (code != SC_OK) return code;

    ID3D11ShaderResourceView *map = nullptr;
    if (how.mode == SC_TRANSITION_WIPE && how.wipe_map) {
        auto found = dev.comp.masks.find(how.wipe_map);
        if (found != dev.comp.masks.end()) map = found->second.view.Get();
    }
    TransitionConstants k = {};
    k.out_size[0] = static_cast<float>(w);
    k.out_size[1] = static_cast<float>(h);
    k.progress = std::max(0.0f, progress);
    k.softness = std::clamp(how.softness, 0.0f, 1.0f);
    k.dir[0] = how.direction[0];
    k.dir[1] = how.direction[1];
    if (k.dir[0] == 0 && k.dir[1] == 0) k.dir[0] = -1;  // left, as Python's default
    k.mode = how.mode;
    k.use_map = map ? 1 : 0;
    k.level_max = (how.mode == SC_TRANSITION_LEAF || how.mode == SC_TRANSITION_BLOOM)
                      ? shape_reach(how.mode, w, h, k.progress) : 1.0f;
    ID3D11ShaderResourceView *clip = nullptr;
    if (how.mode == SC_TRANSITION_STINGER) {
        k.cut = static_cast<double>(k.progress) * how.seconds >= how.cut_seconds ? 1 : 0;
        auto found = how.overlay ? dev.comp.sources.find(how.overlay) : dev.comp.sources.end();
        if (found != dev.comp.sources.end() && found->second.view && !found->second.planar) {
            const Source &src = found->second;
            clip = src.view.Get();
            k.overlay_swizzle = src.format == SC_FMT_BGRA || src.format == SC_FMT_PBGRA ? 1 : 0;
            // render/compositor.py: the clip (or its picture half, beside a matte) covers the canvas, centred
            const double tw = std::max(1, src.pic_w), th = std::max(1, src.pic_h);
            const double half_w = how.matte == SC_STINGER_MATTE_RIGHT ? tw / 2 : tw;
            const double half_h = how.matte == SC_STINGER_MATTE_BELOW ? th / 2 : th;
            const double pa = half_w / std::max(1e-6, half_h), ca = static_cast<double>(w) / std::max(1, h);
            double x = 0, y = 0, cw = 1, ch = 1;
            if (pa > ca) {
                cw = ca / pa;
                x = (1 - cw) / 2;
            } else {
                ch = pa / ca;
                y = (1 - ch) / 2;
            }
            const double sx = half_w / tw, sy = half_h / th;
            // the picture may sit in a wider texture (rows padded): uv in the texture, not the picture
            const double fx = static_cast<double>(src.pic_w) / std::max(1, src.data_w);
            const double fy = static_cast<double>(src.pic_h) / std::max(1, src.data_h);
            k.overlay_rect[0] = static_cast<float>(x * sx * fx);
            k.overlay_rect[1] = static_cast<float>(y * sy * fy);
            k.overlay_rect[2] = static_cast<float>(cw * sx * fx);
            k.overlay_rect[3] = static_cast<float>(ch * sy * fy);
            k.matte_offset[0] = how.matte == SC_STINGER_MATTE_RIGHT ? static_cast<float>(0.5 * fx) : 0.0f;
            k.matte_offset[1] = how.matte == SC_STINGER_MATTE_BELOW ? static_cast<float>(0.5 * fy) : 0.0f;
            k.show_overlay = (how.matte != SC_STINGER_MATTE_NONE && how.matte_only) ? 0 : 1;
        }
    }
    ID3D11DeviceContext *c = dev.context.Get();
    D3D11_MAPPED_SUBRESOURCE mapped;
    HRESULT hr = c->Map(dev.trans_constants.Get(), 0, D3D11_MAP_WRITE_DISCARD, 0, &mapped);
    if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "Map (transition)", hr);
    std::memcpy(mapped.pData, &k, sizeof k);
    c->Unmap(dev.trans_constants.Get(), 0);

    ID3D11RenderTargetView *targets[] = {canvas.target.Get()};
    c->OMSetRenderTargets(1, targets, nullptr);
    c->OMSetBlendState(nullptr, nullptr, 0xFFFFFFFF);  // the pass replaces the canvas
    D3D11_VIEWPORT vp = {0, 0, static_cast<float>(w), static_cast<float>(h), 0, 1};
    c->RSSetViewports(1, &vp);
    c->RSSetState(dev.raster.Get());
    c->IASetInputLayout(nullptr);
    c->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    c->VSSetShader(dev.trans_vs.Get(), nullptr, 0);
    c->PSSetShader(dev.trans_ps.Get(), nullptr, 0);
    ID3D11Buffer *buffers[] = {dev.trans_constants.Get()};
    c->PSSetConstantBuffers(0, 1, buffers);
    ID3D11ShaderResourceView *views[] = {canvas.outgoing.view.Get(), canvas.incoming.view.Get(),
                                         map ? map : canvas.outgoing.view.Get(),
                                         clip ? clip : canvas.outgoing.view.Get()};
    c->PSSetShaderResources(0, 4, views);
    ID3D11SamplerState *samplers[] = {dev.comp.wrap.Get(), dev.comp.clamp.Get()};
    c->PSSetSamplers(0, 2, samplers);
    c->Draw(3, 0);
    ID3D11ShaderResourceView *none[4] = {};
    c->PSSetShaderResources(0, 4, none);
    return SC_OK;
}
