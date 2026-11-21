// The person finder (segmenter.h). The model is tools/make_segmenter.py's program (models/selfie-wide.scseg,
// built into the DLL as segmenter_model.h): a list of layers over one buffer of tensors.
#include "segmenter.h"

#include "segmenter_model.h"
#include "shader_segmenter.h"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstring>
#include <string>
#include <vector>

namespace sc {

namespace {

enum Kind { CONV = 1, DWCONV = 2, GAP = 3, SCALE = 4, ADD = 5, RESIZE2X = 6, CONVT2X = 7 };

struct Op {
    int32_t kind, src, src2, dst, weights, bias, cin, cout, h, w, oh, ow, k, stride, pad, act;
};
static_assert(sizeof(Op) == 64, "16 numbers a layer, as make_segmenter.py writes them");

struct OpConstants {
    int32_t a[4], b[4], c[4], d[4];
    float f[4];
};

struct Program {
    int in_w = 0, in_h = 0, arena = 0, output = 0;
    std::vector<Op> ops;
    std::vector<float> weights;
};

float half_to_float(uint16_t h) {
    const uint32_t sign = (h & 0x8000u) << 16, exp = (h >> 10) & 0x1Fu, man = h & 0x3FFu;
    uint32_t bits;
    if (exp == 0) {
        if (man == 0) {
            bits = sign;
        } else {  // subnormal
            int e = -1;
            uint32_t m = man;
            do { ++e; m <<= 1; } while ((m & 0x400u) == 0);
            bits = sign | static_cast<uint32_t>(127 - 15 - e) << 23 | (m & 0x3FFu) << 13;
        }
    } else if (exp == 31) {
        bits = sign | 0x7F800000u | man << 13;
    } else {
        bits = sign | (exp + 127 - 15) << 23 | man << 13;
    }
    float f;
    std::memcpy(&f, &bits, sizeof f);
    return f;
}

bool read_program(const unsigned char *data, size_t size, Program &out) {
    if (size < 36 || std::memcmp(data, "SCSEG1\0\0", 8) != 0) return false;
    uint32_t head[7];
    std::memcpy(head, data + 8, sizeof head);
    if (head[0] != 1) return false;
    out.in_w = static_cast<int>(head[1]);
    out.in_h = static_cast<int>(head[2]);
    out.arena = static_cast<int>(head[3]);
    const size_t n_ops = head[4], n_weights = head[5];
    out.output = static_cast<int>(head[6]);
    if (size != 36 + n_ops * sizeof(Op) + n_weights * 2) return false;
    out.ops.resize(n_ops);
    std::memcpy(out.ops.data(), data + 36, n_ops * sizeof(Op));
    out.weights.resize(n_weights);
    const unsigned char *w = data + 36 + n_ops * sizeof(Op);
    for (size_t i = 0; i < n_weights; ++i) {
        uint16_t h;
        std::memcpy(&h, w + i * 2, 2);
        out.weights[i] = half_to_float(h);
    }
    for (const Op &op : out.ops) {  // every layer stays inside its buffers
        const int64_t in = int64_t(op.cin) * op.h * op.w, made = int64_t(op.cout) * op.oh * op.ow;
        if (op.kind < CONV || op.kind > CONVT2X || op.src < 0 || op.dst < 0 || op.src + in > out.arena ||
            op.dst + made > out.arena || op.weights < 0 || op.bias < 0 || op.bias + op.cout > int64_t(n_weights))
            return false;
    }
    return out.in_w > 0 && out.in_h > 0 && out.output >= 0 && out.output + out.in_w * out.in_h <= out.arena;
}

const char *const ENTRIES[] = {"cs_conv", "cs_dwconv", "cs_gap", "cs_scale", "cs_add", "cs_resize", "cs_convt",
                               "cs_input", "cs_mask", "cs_blur_across", "cs_blur_down"};
enum Entry { E_CONV, E_DWCONV, E_GAP, E_SCALE, E_ADD, E_RESIZE, E_CONVT, E_INPUT, E_MASK, E_ACROSS, E_DOWN, E_COUNT };

using Clock = std::chrono::steady_clock;

double now_seconds() {
    return std::chrono::duration<double>(Clock::now().time_since_epoch()).count();
}

// What sc_background_info reports (any device; the newest wins).
std::atomic<int> g_state{0};
std::atomic<float> g_gpu_ms{0.0f};
std::atomic<float> g_rate{0.0f};
std::atomic<double> g_last_used{0.0};

struct Texture {
    ComPtr<ID3D11Texture2D> texture;
    ComPtr<ID3D11ShaderResourceView> view;
    ComPtr<ID3D11UnorderedAccessView> uav;
};

int make_texture(ID3D11Device *device, int width, int height, DXGI_FORMAT format, Texture &out) {
    out = Texture();
    D3D11_TEXTURE2D_DESC td = {};
    td.Width = static_cast<UINT>(width);
    td.Height = static_cast<UINT>(height);
    td.MipLevels = 1;
    td.ArraySize = 1;
    td.Format = format;
    td.SampleDesc.Count = 1;
    td.Usage = D3D11_USAGE_DEFAULT;
    td.BindFlags = D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_UNORDERED_ACCESS;
    HRESULT hr = device->CreateTexture2D(&td, nullptr, &out.texture);
    if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "Creating the background filter's texture", hr);
    hr = device->CreateShaderResourceView(out.texture.Get(), nullptr, &out.view);
    if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "CreateShaderResourceView (background)", hr);
    hr = device->CreateUnorderedAccessView(out.texture.Get(), nullptr, &out.uav);
    if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "CreateUnorderedAccessView (background)", hr);
    return SC_OK;
}

}  // namespace

struct Segment {
    int pic_w = 0, pic_h = 0;
    float map[4] = {1, 1, 0, 0};
    Target prep, big;          // the picture drawn small for the model, and twice that for the blurred room
    Texture mask, across, room;
    bool fresh = true;          // no mask yet: the next one is taken whole
    double last_run = -1e9, last_used = 0;
};

struct Segmenter {
    bool tried = false, ready = false;
    Program program;
    ComPtr<ID3D11ComputeShader> shaders[E_COUNT];
    ComPtr<ID3D11Buffer> constants, weights, arena;
    ComPtr<ID3D11ShaderResourceView> weights_view;
    ComPtr<ID3D11UnorderedAccessView> arena_view;
    ComPtr<ID3D11SamplerState> sampler;
    std::unordered_map<uint64_t, Segment> segments;

    // Timing on the card: a few runs in flight, read back when the card is done (never waited for).
    struct Timing {
        ComPtr<ID3D11Query> disjoint, start, end;
        bool pending = false;
    };
    Timing timings[4];
    int next_timing = 0;
    float gpu_ms = -1.0f;       // smoothed
    int slow_runs = 0;
    double interval = 1.0 / 30.0;
    bool too_slow = false;
    int runs = 0;
    double rate_since = 0;

    int prepare(sc_device &dev) {
        if (tried) return ready ? SC_OK : SC_ERR_DEVICE;
        tried = true;
        if (dev.level < D3D_FEATURE_LEVEL_11_0) {
            log_line(SC_LOG_INFO, "background: this graphics card has no compute shaders (Direct3D 11); the "
                                  "Background filter leaves pictures as they are");
            return SC_ERR_DEVICE;
        }
        if (!read_program(SEGMENTER_MODEL, sizeof SEGMENTER_MODEL, program))
            return fail(SC_ERR_INTERNAL, "background: the built-in model doesn't read");
        ID3D11Device *d = dev.device.Get();
        for (int i = 0; i < E_COUNT; ++i) {
            ComPtr<ID3DBlob> code;
            HRESULT hr = compile(SEGMENTER_HLSL, sizeof SEGMENTER_HLSL - 1, ENTRIES[i], "cs_5_0", code);
            if (FAILED(hr)) return fail_hr(SC_ERR_SHADER, "Compiling the background filter's shaders", hr);
            hr = d->CreateComputeShader(code->GetBufferPointer(), code->GetBufferSize(), nullptr, &shaders[i]);
            if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "CreateComputeShader (background)", hr);
        }
        D3D11_BUFFER_DESC cb = {};
        cb.ByteWidth = sizeof(OpConstants);
        cb.Usage = D3D11_USAGE_DYNAMIC;
        cb.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
        cb.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
        HRESULT hr = d->CreateBuffer(&cb, nullptr, &constants);
        if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "CreateBuffer (background constants)", hr);

        D3D11_BUFFER_DESC wd = {};
        wd.ByteWidth = static_cast<UINT>(program.weights.size() * sizeof(float));
        wd.Usage = D3D11_USAGE_IMMUTABLE;
        wd.BindFlags = D3D11_BIND_SHADER_RESOURCE;
        wd.MiscFlags = D3D11_RESOURCE_MISC_BUFFER_STRUCTURED;
        wd.StructureByteStride = sizeof(float);
        D3D11_SUBRESOURCE_DATA init = {program.weights.data(), 0, 0};
        hr = d->CreateBuffer(&wd, &init, &weights);
        if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "CreateBuffer (background weights)", hr);
        hr = d->CreateShaderResourceView(weights.Get(), nullptr, &weights_view);
        if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "CreateShaderResourceView (background weights)", hr);

        D3D11_BUFFER_DESC ad = {};
        ad.ByteWidth = static_cast<UINT>(program.arena * sizeof(float));
        ad.Usage = D3D11_USAGE_DEFAULT;
        ad.BindFlags = D3D11_BIND_UNORDERED_ACCESS;
        ad.MiscFlags = D3D11_RESOURCE_MISC_BUFFER_STRUCTURED;
        ad.StructureByteStride = sizeof(float);
        hr = d->CreateBuffer(&ad, nullptr, &arena);
        if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "CreateBuffer (background tensors)", hr);
        hr = d->CreateUnorderedAccessView(arena.Get(), nullptr, &arena_view);
        if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "CreateUnorderedAccessView (background tensors)", hr);

        D3D11_SAMPLER_DESC sd = {};
        sd.Filter = D3D11_FILTER_MIN_MAG_MIP_LINEAR;
        sd.AddressU = sd.AddressV = sd.AddressW = D3D11_TEXTURE_ADDRESS_CLAMP;
        sd.MaxLOD = D3D11_FLOAT32_MAX;
        hr = d->CreateSamplerState(&sd, &sampler);
        if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "CreateSamplerState (background)", hr);

        for (Timing &t : timings) {
            D3D11_QUERY_DESC qd = {D3D11_QUERY_TIMESTAMP_DISJOINT, 0};
            if (FAILED(d->CreateQuery(&qd, &t.disjoint))) break;
            qd.Query = D3D11_QUERY_TIMESTAMP;
            if (FAILED(d->CreateQuery(&qd, &t.start)) || FAILED(d->CreateQuery(&qd, &t.end))) {
                t.disjoint.Reset();
                break;
            }
        }
        char line[160];
        std::snprintf(line, sizeof line, "background: person finder ready on the graphics card (%zu layers, %d x %d)",
                      program.ops.size(), program.in_w, program.in_h);
        log_line(SC_LOG_INFO, line);
        ready = true;
        return SC_OK;
    }

    void set_constants(ID3D11DeviceContext *c, const OpConstants &k) {
        D3D11_MAPPED_SUBRESOURCE mapped;
        if (FAILED(c->Map(constants.Get(), 0, D3D11_MAP_WRITE_DISCARD, 0, &mapped))) return;
        std::memcpy(mapped.pData, &k, sizeof k);
        c->Unmap(constants.Get(), 0);
    }

    // Reads back finished timings and slows down (or stops) on a card that takes too long.
    void read_timings(ID3D11DeviceContext *c) {
        for (Timing &t : timings) {
            if (!t.pending) continue;
            D3D11_QUERY_DATA_TIMESTAMP_DISJOINT dj;
            UINT64 a = 0, b = 0;
            if (c->GetData(t.disjoint.Get(), &dj, sizeof dj, D3D11_ASYNC_GETDATA_DONOTFLUSH) != S_OK) continue;
            if (c->GetData(t.start.Get(), &a, sizeof a, D3D11_ASYNC_GETDATA_DONOTFLUSH) != S_OK) continue;
            if (c->GetData(t.end.Get(), &b, sizeof b, D3D11_ASYNC_GETDATA_DONOTFLUSH) != S_OK) continue;
            t.pending = false;
            if (dj.Disjoint || dj.Frequency == 0 || b < a) continue;
            const float ms = static_cast<float>(double(b - a) * 1000.0 / double(dj.Frequency));
            gpu_ms = gpu_ms < 0 ? ms : gpu_ms * 0.9f + ms * 0.1f;
            g_gpu_ms.store(gpu_ms);
            const double was = interval;
            interval = gpu_ms > 8.0f ? 1.0 / 15.0 : 1.0 / 30.0;
            if (interval != was) {
                char line[160];
                std::snprintf(line, sizeof line, "background: %.1f ms a mask on the graphics card, %d masks a second",
                              gpu_ms, interval < 0.05 ? 30 : 15);
                log_line(SC_LOG_INFO, line);
            }
            slow_runs = ms > 40.0f ? slow_runs + 1 : 0;
            if (slow_runs >= 10 && !too_slow) {
                too_slow = true;
                char line[160];
                std::snprintf(line, sizeof line, "background: %.0f ms a mask is too slow for this graphics card; the "
                                                 "Background filter leaves pictures as they are", ms);
                log_line(SC_LOG_WARNING, line);
            }
        }
    }

    void dispatch(ID3D11DeviceContext *c, Entry e, int total) {
        c->CSSetShader(shaders[e].Get(), nullptr, 0);
        c->Dispatch(static_cast<UINT>((total + 63) / 64), 1, 1);
    }

    void run(sc_device &dev, Segment &s, bool room, float blur, const DrawSmall &draw) {
        ID3D11DeviceContext *c = dev.context.Get();
        const float clear[4] = {0, 0, 0, 0};
        c->ClearRenderTargetView(s.prep.target.Get(), clear);
        draw(s.prep.target.Get(), s.prep.width, s.prep.height, s.map);
        c->OMSetRenderTargets(0, nullptr, nullptr);  // the small picture is read next

        Timing *timing = nullptr;
        Timing &t = timings[next_timing];
        if (t.disjoint && !t.pending) {
            timing = &t;
            next_timing = (next_timing + 1) % 4;
            c->Begin(t.disjoint.Get());
            c->End(t.start.Get());
        }

        ID3D11Buffer *buffers[] = {constants.Get()};
        c->CSSetConstantBuffers(0, 1, buffers);
        ID3D11UnorderedAccessView *uavs[3] = {arena_view.Get(), s.mask.uav.Get(), nullptr};
        c->CSSetUnorderedAccessViews(0, 3, uavs, nullptr);
        ID3D11ShaderResourceView *views[2] = {weights_view.Get(), s.prep.view.Get()};
        c->CSSetShaderResources(0, 2, views);
        ID3D11SamplerState *samplers[] = {sampler.Get()};
        c->CSSetSamplers(0, 1, samplers);

        const int pixels = program.in_w * program.in_h;
        OpConstants k = {};
        k.a[2] = 0;  // the input is the first tensor
        k.c[0] = program.in_w;
        k.b[3] = program.in_h;
        k.d[3] = pixels;
        set_constants(c, k);
        dispatch(c, E_INPUT, pixels);
        for (const Op &op : program.ops) {
            OpConstants o = {{op.src, op.src2, op.dst, op.weights}, {op.bias, op.cin, op.cout, op.h},
                             {op.w, op.oh, op.ow, op.k}, {op.stride, op.pad, op.act, 0}, {0, 0, 0, 0}};
            Entry e = E_CONV;
            switch (op.kind) {
                case CONV: e = E_CONV; o.d[3] = op.cout * op.oh * op.ow; break;
                case DWCONV: e = E_DWCONV; o.d[3] = op.cout * op.oh * op.ow; break;
                case GAP: e = E_GAP; o.d[3] = op.cin; break;
                case SCALE: e = E_SCALE; o.d[3] = op.cin * op.h * op.w; break;
                case ADD: e = E_ADD; o.d[3] = op.cin * op.h * op.w; break;
                case RESIZE2X: e = E_RESIZE; o.d[3] = op.cin * op.oh * op.ow; break;
                case CONVT2X: e = E_CONVT; o.d[3] = op.cout * op.oh * op.ow; break;
                default: continue;
            }
            set_constants(c, o);
            dispatch(c, e, o.d[3]);
        }
        OpConstants m = {};
        m.a[0] = program.output;
        m.c[0] = program.in_w;
        m.b[3] = program.in_h;
        m.d[3] = pixels;
        m.f[0] = s.fresh ? 1.0f : 0.7f;  // most of the new mask: steady edges, little lag
        set_constants(c, m);
        dispatch(c, E_MASK, pixels);
        s.fresh = false;

        ID3D11UnorderedAccessView *no_uavs[3] = {nullptr, nullptr, nullptr};
        c->CSSetUnorderedAccessViews(0, 3, no_uavs, nullptr);
        ID3D11ShaderResourceView *no_views[4] = {nullptr, nullptr, nullptr, nullptr};
        c->CSSetShaderResources(0, 4, no_views);

        if (room) {
            c->ClearRenderTargetView(s.big.target.Get(), clear);
            draw(s.big.target.Get(), s.big.width, s.big.height, s.map);
            c->OMSetRenderTargets(0, nullptr, nullptr);
            OpConstants b = {};
            const float radius = std::round(2.0f + 30.0f * std::clamp(blur, 0.0f, 1.0f));
            b.f[0] = radius;
            b.f[1] = std::max(1.0f, radius / 2.5f);
            set_constants(c, b);
            ID3D11ShaderResourceView *across_in[3] = {nullptr, s.big.view.Get(), s.mask.view.Get()};
            c->CSSetShaderResources(0, 3, across_in);
            ID3D11UnorderedAccessView *across_out[3] = {nullptr, nullptr, s.across.uav.Get()};
            c->CSSetUnorderedAccessViews(0, 3, across_out, nullptr);
            c->CSSetShader(shaders[E_ACROSS].Get(), nullptr, 0);
            const UINT gx = static_cast<UINT>((s.big.width + 7) / 8), gy = static_cast<UINT>((s.big.height + 7) / 8);
            c->Dispatch(gx, gy, 1);
            c->CSSetUnorderedAccessViews(0, 3, no_uavs, nullptr);
            ID3D11ShaderResourceView *down_in[4] = {nullptr, nullptr, nullptr, s.across.view.Get()};
            c->CSSetShaderResources(0, 4, down_in);
            ID3D11UnorderedAccessView *down_out[3] = {nullptr, nullptr, s.room.uav.Get()};
            c->CSSetUnorderedAccessViews(0, 3, down_out, nullptr);
            c->CSSetShader(shaders[E_DOWN].Get(), nullptr, 0);
            c->Dispatch(gx, gy, 1);
            c->CSSetUnorderedAccessViews(0, 3, no_uavs, nullptr);
            c->CSSetShaderResources(0, 4, no_views);
        }
        c->CSSetShader(nullptr, nullptr, 0);

        if (timing) {
            c->End(timing->end.Get());
            c->End(timing->disjoint.Get());
            timing->pending = true;
        }
    }
};

bool segment(sc_device &dev, uint64_t key, int pic_w, int pic_h, bool room, float blur, const DrawSmall &draw,
             SegmentViews &out) {
    if (!dev.segmenter) dev.segmenter = std::make_shared<Segmenter>();
    Segmenter &sg = *dev.segmenter;
    const double now = now_seconds();
    g_last_used.store(now);
    if (sg.prepare(dev) != SC_OK) {
        g_state.store(2);
        return false;
    }
    ID3D11DeviceContext *c = dev.context.Get();
    sg.read_timings(c);
    if (sg.too_slow) {
        g_state.store(3);
        return false;
    }
    g_state.store(1);
    if (pic_w < 1 || pic_h < 1) return false;

    // Pictures not drawn with the filter for a while give their textures back.
    for (auto it = sg.segments.begin(); it != sg.segments.end();) {
        if (it->first != key && now - it->second.last_used > 10.0) it = sg.segments.erase(it);
        else ++it;
    }

    Segment &s = sg.segments[key];
    s.last_used = now;
    ID3D11Device *d = dev.device.Get();
    const int mw = sg.program.in_w, mh = sg.program.in_h;
    if (s.pic_w != pic_w || s.pic_h != pic_h || !s.mask.texture) {
        s = Segment();
        s.last_used = now;
        s.pic_w = pic_w;
        s.pic_h = pic_h;
        const double model = double(mw) / mh, pic = double(pic_w) / pic_h;
        if (pic >= model) {  // wider than the model: bars above and below
            s.map[0] = 1.0f;
            s.map[1] = static_cast<float>(model / pic);
        } else {
            s.map[0] = static_cast<float>(pic / model);
            s.map[1] = 1.0f;
        }
        s.map[2] = (1.0f - s.map[0]) * 0.5f;
        s.map[3] = (1.0f - s.map[1]) * 0.5f;
        if (make_target(d, mw, mh, s.prep) != SC_OK || make_texture(d, mw, mh, DXGI_FORMAT_R32_FLOAT, s.mask) != SC_OK)
            return false;
        const float zero[4] = {0, 0, 0, 0};  // until the first mask, all of it counts as room (never flashes)
        c->ClearUnorderedAccessViewFloat(s.mask.uav.Get(), zero);
    }
    if (room && !s.room.texture) {
        if (make_target(d, mw * 2, mh * 2, s.big) != SC_OK ||
            make_texture(d, mw * 2, mh * 2, DXGI_FORMAT_R16G16B16A16_FLOAT, s.across) != SC_OK ||
            make_texture(d, mw * 2, mh * 2, DXGI_FORMAT_R8G8B8A8_UNORM, s.room) != SC_OK) {
            s.room = Texture();
            return false;
        }
        s.last_run = -1e9;  // the room is wanted now: make it this frame
    }
    if (now - s.last_run >= sg.interval * 0.9 || now < s.last_run) {
        s.last_run = now;
        sg.run(dev, s, room, blur, draw);
        ++sg.runs;
        if (now - sg.rate_since >= 2.0) {  // masks a second, over the last two seconds or so
            if (sg.rate_since > 0) g_rate.store(static_cast<float>(sg.runs / (now - sg.rate_since)));
            sg.rate_since = now;
            sg.runs = 0;
        }
    }
    out.mask = s.mask.view.Get();
    out.room = room ? s.room.view.Get() : nullptr;
    std::memcpy(out.map, s.map, sizeof out.map);
    return true;
}

}  // namespace sc

extern "C" SC_API int sc_background_info(sc_background_status *out) {
    return sc::guarded("sc_background_info", [&]() -> int {
        if (!out) return sc::fail(SC_ERR_ARGUMENT, "sc_background_info: nowhere to put it");
        const bool recent = sc::now_seconds() - sc::g_last_used.load() < 3.0;
        out->state = recent ? sc::g_state.load() : 0;
        out->gpu_ms = sc::g_gpu_ms.load();
        out->masks_per_second = recent ? sc::g_rate.load() : 0.0f;
        return SC_OK;
    });
}
