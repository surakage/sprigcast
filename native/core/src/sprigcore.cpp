// SprigCast's native engine: the C interface in include/sprigcore.h. Phase 1 (docs/native-engine.md): the
// build, the link to Python, the log queue, graphics cards and devices, and a first canvas that pictures can be
// drawn on and read back, which the pixel tests use to compare the engine with what SprigCast draws today.
#include "internal.h"

#include <algorithm>
#include <cstdio>
#include <cstring>
#include <deque>
#include <iterator>
#include <stdexcept>

namespace {

thread_local std::string g_last_error;
std::mutex g_log_mutex;
std::deque<std::string> g_log;
constexpr size_t LOG_KEEP = 512;

std::string hr_text(HRESULT hr) {
    char buf[32];
    std::snprintf(buf, sizeof buf, "0x%08lX", static_cast<unsigned long>(hr));
    return buf;
}

}  // namespace

namespace sc {

void set_last_error(const std::string &message) { g_last_error = message; }

void log_line(int level, const std::string &message) {
    std::lock_guard<std::mutex> lock(g_log_mutex);
    g_log.push_back(std::to_string(level) + " " + message);
    while (g_log.size() > LOG_KEEP) g_log.pop_front();
}

int fail(int code, const std::string &message) {
    g_last_error = message;
    log_line(code == SC_ERR_ARGUMENT ? SC_LOG_WARNING : SC_LOG_ERROR, message);
    return code;
}

int fail_hr(int code, const char *what, HRESULT hr) {
    return fail(code, std::string(what) + " failed (" + hr_text(hr) + ")");
}

HRESULT compile(const char *source, size_t size, const char *entry, const char *profile, ComPtr<ID3DBlob> &out) {
    ComPtr<ID3DBlob> errors;
    HRESULT hr = D3DCompile(source, size, "sprigcore", nullptr, nullptr, entry, profile,
                            D3DCOMPILE_OPTIMIZATION_LEVEL3, 0, out.GetAddressOf(), errors.GetAddressOf());
    if (FAILED(hr) && errors) {
        log_line(SC_LOG_ERROR, std::string("shader: ") + static_cast<const char *>(errors->GetBufferPointer()));
    }
    return hr;
}

}  // namespace sc

using namespace sc;

namespace {

// ---- graphics cards -----------------------------------------------------------------------------------------

void fill_info(const DXGI_ADAPTER_DESC1 &desc, sc_adapter_info *out) {
    std::memset(out, 0, sizeof *out);
    for (size_t i = 0; i < 127 && desc.Description[i]; ++i) out->name[i] = desc.Description[i];
    out->vendor_id = desc.VendorId;
    out->device_id = desc.DeviceId;
    out->dedicated_video_memory = desc.DedicatedVideoMemory;
    out->shared_system_memory = desc.SharedSystemMemory;
    out->software = (desc.Flags & DXGI_ADAPTER_FLAG_SOFTWARE) ? 1 : 0;
}

HRESULT adapter_at(int index, ComPtr<IDXGIAdapter1> &out) {
    ComPtr<IDXGIFactory1> factory;
    HRESULT hr = CreateDXGIFactory1(__uuidof(IDXGIFactory1), reinterpret_cast<void **>(factory.GetAddressOf()));
    if (FAILED(hr)) return hr;
    return factory->EnumAdapters1(static_cast<UINT>(index), out.GetAddressOf());
}

// ---- the drawing shader -------------------------------------------------------------------------------------

// One quad per draw, made from the vertex number: no vertex buffer. `rect` is where it lands, in clip space.
const char SHADER[] = R"(
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
)";

struct DrawConstants {
    float rect[4];
    float opacity;
    float unused[3];
};

}  // namespace

int sc_device::prepare_drawing() {
    if (vs) return SC_OK;
    ComPtr<ID3DBlob> vs_code, ps_code;
    HRESULT hr = compile(SHADER, sizeof SHADER - 1, "vs_main", "vs_4_0", vs_code);
    if (FAILED(hr)) return fail_hr(SC_ERR_SHADER, "Compiling the vertex shader", hr);
    hr = compile(SHADER, sizeof SHADER - 1, "ps_main", "ps_4_0", ps_code);
    if (FAILED(hr)) return fail_hr(SC_ERR_SHADER, "Compiling the pixel shader", hr);
    hr = device->CreateVertexShader(vs_code->GetBufferPointer(), vs_code->GetBufferSize(), nullptr, &vs);
    if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "CreateVertexShader", hr);
    hr = device->CreatePixelShader(ps_code->GetBufferPointer(), ps_code->GetBufferSize(), nullptr, &ps);
    if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "CreatePixelShader", hr);

    D3D11_BUFFER_DESC cb = {};
    cb.ByteWidth = sizeof(DrawConstants);
    cb.Usage = D3D11_USAGE_DYNAMIC;
    cb.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
    cb.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
    hr = device->CreateBuffer(&cb, nullptr, &constants);
    if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "CreateBuffer", hr);

    D3D11_SAMPLER_DESC sd = {};
    sd.Filter = D3D11_FILTER_MIN_MAG_MIP_LINEAR;
    sd.AddressU = sd.AddressV = sd.AddressW = D3D11_TEXTURE_ADDRESS_CLAMP;
    sd.MaxLOD = D3D11_FLOAT32_MAX;
    hr = device->CreateSamplerState(&sd, &sampler);
    if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "CreateSamplerState", hr);

    D3D11_BLEND_DESC bd = {};
    auto &rt = bd.RenderTarget[0];
    rt.BlendEnable = TRUE;
    rt.SrcBlend = D3D11_BLEND_SRC_ALPHA;
    rt.DestBlend = D3D11_BLEND_INV_SRC_ALPHA;
    rt.BlendOp = D3D11_BLEND_OP_ADD;
    rt.SrcBlendAlpha = D3D11_BLEND_ONE;
    rt.DestBlendAlpha = D3D11_BLEND_INV_SRC_ALPHA;
    rt.BlendOpAlpha = D3D11_BLEND_OP_ADD;
    rt.RenderTargetWriteMask = D3D11_COLOR_WRITE_ENABLE_ALL;
    hr = device->CreateBlendState(&bd, &over);
    if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "CreateBlendState", hr);

    D3D11_RASTERIZER_DESC rd = {};
    rd.FillMode = D3D11_FILL_SOLID;
    rd.CullMode = D3D11_CULL_NONE;
    rd.DepthClipEnable = TRUE;
    hr = device->CreateRasterizerState(&rd, &raster);
    if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "CreateRasterizerState", hr);
    return SC_OK;
}

namespace sc {

int draw_view(sc_device &dev, ID3D11RenderTargetView *target, int tw, int th, ID3D11ShaderResourceView *picture,
              float x, float y, float w, float h, float opacity, bool blend) {
    int ready = dev.prepare_drawing();
    if (ready != SC_OK) return ready;
    DrawConstants k = {};
    k.rect[0] = x / tw * 2.0f - 1.0f;
    k.rect[1] = 1.0f - y / th * 2.0f;
    k.rect[2] = w / tw * 2.0f;
    k.rect[3] = -h / th * 2.0f;
    k.opacity = std::min(opacity, 1.0f);
    D3D11_MAPPED_SUBRESOURCE mapped;
    HRESULT hr = dev.context->Map(dev.constants.Get(), 0, D3D11_MAP_WRITE_DISCARD, 0, &mapped);
    if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "Map (constants)", hr);
    std::memcpy(mapped.pData, &k, sizeof k);
    dev.context->Unmap(dev.constants.Get(), 0);

    ID3D11DeviceContext *c = dev.context.Get();
    D3D11_VIEWPORT vp = {0, 0, static_cast<float>(tw), static_cast<float>(th), 0, 1};
    ID3D11RenderTargetView *targets[] = {target};
    ID3D11ShaderResourceView *views[] = {picture};
    ID3D11SamplerState *samplers[] = {dev.sampler.Get()};
    ID3D11Buffer *buffers[] = {dev.constants.Get()};
    c->OMSetRenderTargets(1, targets, nullptr);
    c->OMSetBlendState(blend ? dev.over.Get() : nullptr, nullptr, 0xFFFFFFFF);
    c->RSSetViewports(1, &vp);
    c->RSSetState(dev.raster.Get());
    c->IASetInputLayout(nullptr);
    c->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLESTRIP);
    c->VSSetShader(dev.vs.Get(), nullptr, 0);
    c->VSSetConstantBuffers(0, 1, buffers);
    c->PSSetShader(dev.ps.Get(), nullptr, 0);
    c->PSSetConstantBuffers(0, 1, buffers);
    c->PSSetShaderResources(0, 1, views);
    c->PSSetSamplers(0, 1, samplers);
    c->Draw(4, 0);
    ID3D11ShaderResourceView *none[] = {nullptr};
    c->PSSetShaderResources(0, 1, none);
    return SC_OK;
}

}  // namespace sc

// ---- exports ------------------------------------------------------------------------------------------------

extern "C" {

SC_API int sc_interface_version(void) { return SC_INTERFACE_VERSION; }

SC_API const char *sc_build_info(void) {
#if defined(_MSC_VER)
#  define SC_COMPILER "MSVC " SC_STR(_MSC_VER)
#elif defined(__clang__)
#  define SC_COMPILER "clang " __clang_version__
#elif defined(__GNUC__)
#  define SC_COMPILER "gcc " __VERSION__
#else
#  define SC_COMPILER "unknown compiler"
#endif
#define SC_STR2(x) #x
#define SC_STR(x) SC_STR2(x)
    return "sprigcore interface " SC_STR(SC_INTERFACE_VERSION) ", " SC_COMPILER ", built " __DATE__;
}

SC_API const char *sc_last_error(void) { return g_last_error.c_str(); }

SC_API int sc_log_drain(char *buffer, int size) {
    if (!buffer || size <= 0) return 0;
    std::lock_guard<std::mutex> lock(g_log_mutex);
    int used = 0;
    while (!g_log.empty()) {
        const std::string &line = g_log.front();
        int need = static_cast<int>(line.size()) + 1;
        if (need > size) {  // longer than the whole buffer: cut it rather than block the queue forever
            if (used) break;
            std::memcpy(buffer, line.data(), size - 1);
            buffer[size - 1] = '\n';
            g_log.pop_front();
            return size;
        }
        if (used + need > size) break;
        std::memcpy(buffer + used, line.data(), line.size());
        buffer[used + line.size()] = '\n';
        used += need;
        g_log.pop_front();
    }
    return used;
}

SC_API int sc_adapter_count(void) {
    return guarded("sc_adapter_count", []() -> int {
        ComPtr<IDXGIFactory1> factory;
        HRESULT hr = CreateDXGIFactory1(__uuidof(IDXGIFactory1), reinterpret_cast<void **>(factory.GetAddressOf()));
        if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "CreateDXGIFactory1", hr);
        int n = 0;
        for (;; ++n) {
            ComPtr<IDXGIAdapter1> adapter;
            if (factory->EnumAdapters1(static_cast<UINT>(n), &adapter) == DXGI_ERROR_NOT_FOUND) break;
        }
        return n;
    });
}

SC_API int sc_adapter_info_get(int index, sc_adapter_info *out) {
    return guarded("sc_adapter_info_get", [&]() -> int {
        if (!out || index < 0) return fail(SC_ERR_ARGUMENT, "sc_adapter_info_get: bad index or output");
        ComPtr<IDXGIAdapter1> adapter;
        HRESULT hr = adapter_at(index, adapter);
        if (FAILED(hr)) return fail_hr(SC_ERR_ARGUMENT, "Finding that graphics card", hr);
        DXGI_ADAPTER_DESC1 desc;
        hr = adapter->GetDesc1(&desc);
        if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "GetDesc1", hr);
        fill_info(desc, out);
        return SC_OK;
    });
}

SC_API int sc_device_create(int index, int flags, sc_device **out) {
    return guarded("sc_device_create", [&]() -> int {
        if (!out) return fail(SC_ERR_ARGUMENT, "sc_device_create: no output");
        *out = nullptr;
        ComPtr<IDXGIAdapter1> adapter;
        D3D_DRIVER_TYPE type = D3D_DRIVER_TYPE_HARDWARE;
        if (flags & SC_DEVICE_WARP) {
            type = D3D_DRIVER_TYPE_WARP;
        } else if (index >= 0) {
            HRESULT hr = adapter_at(index, adapter);
            if (FAILED(hr)) return fail_hr(SC_ERR_ARGUMENT, "Finding that graphics card", hr);
            type = D3D_DRIVER_TYPE_UNKNOWN;
        }
        // 10.0 is the floor: the oldest cards SprigCast supports (Intel HD 4000, Radeon R2) have 11.0 or better.
        const D3D_FEATURE_LEVEL levels[] = {D3D_FEATURE_LEVEL_11_1, D3D_FEATURE_LEVEL_11_0,
                                            D3D_FEATURE_LEVEL_10_1, D3D_FEATURE_LEVEL_10_0};
        auto dev = std::make_unique<sc_device>();
        HRESULT hr = D3D11CreateDevice(adapter.Get(), type, nullptr, D3D11_CREATE_DEVICE_BGRA_SUPPORT, levels,
                                       static_cast<UINT>(std::size(levels)), D3D11_SDK_VERSION, &dev->device,
                                       &dev->level, &dev->context);
        if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "D3D11CreateDevice", hr);
        sc_adapter_info info;
        int level = 0;
        if (sc_device_info(dev.get(), &info, &level) == SC_OK) {
            char name[256];
            WideCharToMultiByte(CP_UTF8, 0, info.name, -1, name, static_cast<int>(sizeof name), nullptr, nullptr);
            char line[400];
            std::snprintf(line, sizeof line, "device: %s, feature level %d.%d", name, level >> 12, (level >> 8) & 0xF);
            log_line(SC_LOG_INFO, line);
        }
        *out = dev.release();
        return SC_OK;
    });
}

SC_API void sc_device_destroy(sc_device *device) { delete device; }

SC_API int sc_device_info(sc_device *device, sc_adapter_info *out, int *feature_level) {
    return device_call(device, "sc_device_info", [&]() -> int {
        if (!out) return fail(SC_ERR_ARGUMENT, "sc_device_info: no device or output");
        ComPtr<IDXGIDevice> dxgi;
        HRESULT hr = device->device.As(&dxgi);
        if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "IDXGIDevice", hr);
        ComPtr<IDXGIAdapter> plain;
        hr = dxgi->GetAdapter(&plain);
        if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "GetAdapter", hr);
        ComPtr<IDXGIAdapter1> adapter;
        hr = plain.As(&adapter);
        if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "IDXGIAdapter1", hr);
        DXGI_ADAPTER_DESC1 desc;
        hr = adapter->GetDesc1(&desc);
        if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "GetDesc1", hr);
        fill_info(desc, out);
        if (feature_level) *feature_level = static_cast<int>(device->level);
        return SC_OK;
    });
}

SC_API int sc_canvas_create(sc_device *device, int width, int height, sc_canvas **out) {
    return device_call(device, "sc_canvas_create", [&]() -> int {
        if (!out) return fail(SC_ERR_ARGUMENT, "sc_canvas_create: no device or output");
        *out = nullptr;
        if (width < 1 || height < 1 || width > 16384 || height > 16384)
            return fail(SC_ERR_ARGUMENT, "sc_canvas_create: size must be 1 to 16384 each way");
        auto canvas = std::make_unique<sc_canvas>();
        canvas->device = device;
        canvas->width = width;
        canvas->height = height;
        D3D11_TEXTURE2D_DESC td = {};
        td.Width = static_cast<UINT>(width);
        td.Height = static_cast<UINT>(height);
        td.MipLevels = 1;
        td.ArraySize = 1;
        td.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
        td.SampleDesc.Count = 1;
        td.Usage = D3D11_USAGE_DEFAULT;
        td.BindFlags = D3D11_BIND_RENDER_TARGET | D3D11_BIND_SHADER_RESOURCE;
        std::lock_guard<std::mutex> lock(device->lock);
        HRESULT hr = device->device->CreateTexture2D(&td, nullptr, &canvas->texture);
        if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "Creating the canvas texture", hr);
        hr = device->device->CreateRenderTargetView(canvas->texture.Get(), nullptr, &canvas->target);
        if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "CreateRenderTargetView", hr);
        hr = device->device->CreateShaderResourceView(canvas->texture.Get(), nullptr, &canvas->view);
        if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "CreateShaderResourceView (canvas)", hr);
        *out = canvas.release();
        return SC_OK;
    });
}

SC_API void sc_canvas_destroy(sc_canvas *canvas) {
    if (!canvas) return;
    std::lock_guard<std::mutex> lock(canvas->device->lock);
    delete canvas;
}

SC_API int sc_canvas_clear(sc_canvas *canvas, float r, float g, float b, float a) {
    return device_call(canvas ? canvas->device : nullptr, "sc_canvas_clear", [&]() -> int {
        if (!canvas) return fail(SC_ERR_ARGUMENT, "sc_canvas_clear: no canvas");
        const float colour[4] = {r, g, b, a};
        std::lock_guard<std::mutex> lock(canvas->device->lock);
        canvas->device->context->ClearRenderTargetView(canvas->target.Get(), colour);
        return SC_OK;
    });
}

SC_API int sc_canvas_draw_bgra(sc_canvas *canvas, const uint8_t *pixels, int width, int height, int stride,
                               float x, float y, float w, float h, float opacity) {
    return device_call(canvas ? canvas->device : nullptr, "sc_canvas_draw_bgra", [&]() -> int {
        if (!canvas || !pixels) return fail(SC_ERR_ARGUMENT, "sc_canvas_draw_bgra: no canvas or pixels");
        if (width < 1 || height < 1 || width > 16384 || height > 16384 || stride < width * 4)
            return fail(SC_ERR_ARGUMENT, "sc_canvas_draw_bgra: bad picture size or stride");
        if (w <= 0 || h <= 0 || opacity <= 0) return SC_OK;  // nothing to see
        sc_device &dev = *canvas->device;
        std::lock_guard<std::mutex> lock(dev.lock);
        int ready = dev.prepare_drawing();
        if (ready != SC_OK) return ready;

        D3D11_TEXTURE2D_DESC td = {};
        td.Width = static_cast<UINT>(width);
        td.Height = static_cast<UINT>(height);
        td.MipLevels = 1;
        td.ArraySize = 1;
        td.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
        td.SampleDesc.Count = 1;
        td.Usage = D3D11_USAGE_IMMUTABLE;
        td.BindFlags = D3D11_BIND_SHADER_RESOURCE;
        D3D11_SUBRESOURCE_DATA data = {pixels, static_cast<UINT>(stride), 0};
        ComPtr<ID3D11Texture2D> picture;
        HRESULT hr = dev.device->CreateTexture2D(&td, &data, &picture);
        if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "Uploading the picture", hr);
        ComPtr<ID3D11ShaderResourceView> view;
        hr = dev.device->CreateShaderResourceView(picture.Get(), nullptr, &view);
        if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "CreateShaderResourceView", hr);

        return draw_view(dev, canvas->target.Get(), canvas->width, canvas->height, view.Get(), x, y, w, h,
                         opacity, true);
    });
}

SC_API int sc_canvas_read_bgra(sc_canvas *canvas, uint8_t *out, int stride) {
    return device_call(canvas ? canvas->device : nullptr, "sc_canvas_read_bgra", [&]() -> int {
        if (!canvas || !out || stride < canvas->width * 4)
            return fail(SC_ERR_ARGUMENT, "sc_canvas_read_bgra: no canvas or output, or stride too small");
        sc_device &dev = *canvas->device;
        std::lock_guard<std::mutex> lock(dev.lock);
        if (!canvas->staging) {
            D3D11_TEXTURE2D_DESC td;
            canvas->texture->GetDesc(&td);
            td.Usage = D3D11_USAGE_STAGING;
            td.BindFlags = 0;
            td.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
            HRESULT hr = dev.device->CreateTexture2D(&td, nullptr, &canvas->staging);
            if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "Creating the read-back texture", hr);
        }
        dev.context->CopyResource(canvas->staging.Get(), canvas->texture.Get());
        D3D11_MAPPED_SUBRESOURCE mapped;
        HRESULT hr = dev.context->Map(canvas->staging.Get(), 0, D3D11_MAP_READ, 0, &mapped);
        if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "Map (read-back)", hr);
        const auto *src = static_cast<const uint8_t *>(mapped.pData);
        for (int row = 0; row < canvas->height; ++row)
            std::memcpy(out + static_cast<size_t>(row) * stride, src + static_cast<size_t>(row) * mapped.RowPitch,
                        static_cast<size_t>(canvas->width) * 4);
        dev.context->Unmap(canvas->staging.Get(), 0);
        return SC_OK;
    });
}

SC_API int sc_debug_fault(sc_device *device, int kind) {
    return device_call(device, "sc_debug_fault", [&]() -> int {
        if (kind == 2) {
#ifdef _MSC_VER
            volatile int *nowhere = nullptr;
            *nowhere = 1;  // an access violation: /EHa turns it into something catch (...) stops
#endif
        }
        if (kind == 1 || kind == 2) throw std::runtime_error("a deliberate fault (crash gate check)");
        return SC_OK;
    });
}

}  // extern "C"
