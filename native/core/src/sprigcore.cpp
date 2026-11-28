// SprigCast's native engine: the C interface in include/sprigcore.h. Phase 1 (docs/native-engine.md): the
// build, the link to Python, the log queue, graphics cards and devices, and a first canvas that pictures can be
// drawn on and read back, which the pixel tests use to compare the engine with what SprigCast draws today.
#include "internal.h"

#include <dxgi1_6.h>
#include "shader_hash.h"
#ifdef SPRIGCORE_BAKED
#include "baked_shaders.h"
#endif

#include <algorithm>
#include <condition_variable>
#include <cstdio>
#include <cstring>
#include <deque>
#include <iterator>
#include <stdexcept>

namespace {

std::string hr_text(HRESULT hr) {
    char buf[32];
    std::snprintf(buf, sizeof buf, "0x%08lX", static_cast<unsigned long>(hr));
    return buf;
}

}  // namespace

namespace sc {

HRESULT compile_now(const char *source, size_t size, const char *entry, const char *profile, ComPtr<ID3DBlob> &out);

int fail_hr(int code, const char *what, HRESULT hr) {
    return fail(code, std::string(what) + " failed (" + hr_text(hr) + ")");
}

// Compiled shaders, kept for the life of the process: a new device (a rebuild after a fault, a second engine)
// skips compiling again, which takes over a second on the software renderer and on slow PCs. A shader another
// thread is compiling right now (warm_shaders, at the engine's start) is waited for, not compiled twice.
// Bytecode baked when the engine was built (tools/shader_bake.cpp), for a shader whose source is unchanged since.
bool baked(const char *source, size_t size, const char *entry, const char *profile, ComPtr<ID3DBlob> &out) {
#ifdef SPRIGCORE_BAKED
    const uint64_t hash = shader_hash(source, size);
    for (const BakedShader &b : BAKED_SHADERS) {
        if (b.hash != hash || b.size != size || std::strcmp(b.entry, entry) != 0 || std::strcmp(b.profile, profile) != 0)
            continue;
        ComPtr<ID3DBlob> blob;
        if (FAILED(D3DCreateBlob(b.code_size, &blob))) return false;
        std::memcpy(blob->GetBufferPointer(), b.code, b.code_size);
        out = blob;
        return true;
    }
#else
    (void)source, (void)size, (void)entry, (void)profile, (void)out;
#endif
    return false;
}

HRESULT compile(const char *source, size_t size, const char *entry, const char *profile, ComPtr<ID3DBlob> &out) {
    if (baked(source, size, entry, profile, out)) return S_OK;  // nothing to compile: no frame ever waits for one
    struct Entry {
        bool done = false;
        ComPtr<ID3DBlob> blob;
    };
    static std::mutex cache_lock;
    static std::condition_variable cache_done;
    static std::map<std::string, Entry> cache;
    const std::string key = std::to_string(reinterpret_cast<uintptr_t>(source)) + ":" + std::to_string(size) + ":" +
                            entry + ":" + profile;
    {
        std::unique_lock<std::mutex> lock(cache_lock);
        for (;;) {
            auto found = cache.find(key);
            if (found == cache.end()) {
                cache[key];  // this thread compiles it; others wait for it
                break;
            }
            if (found->second.done) {
                out = found->second.blob;
                return S_OK;
            }
            cache_done.wait(lock);  // being compiled (or failed and gone: then this thread tries)
        }
    }
    HRESULT hr = E_FAIL;
    LARGE_INTEGER t0, t1, freq;
    QueryPerformanceCounter(&t0);
    try {
        hr = compile_now(source, size, entry, profile, out);
    } catch (...) {
        hr = E_FAIL;
    }
    QueryPerformanceCounter(&t1);
    QueryPerformanceFrequency(&freq);
    const double ms = 1000.0 * static_cast<double>(t1.QuadPart - t0.QuadPart) / static_cast<double>(freq.QuadPart);
    if (ms >= 100.0) {  // which shader is slow to make on this PC (a frame waits for it if it isn't ready)
        char line[128];
        std::snprintf(line, sizeof line, "shader: %s %s (%zu bytes of source) took %.0f ms to compile", profile, entry,
                      size, ms);
        log_line(SC_LOG_INFO, line);
    }
    {
        std::lock_guard<std::mutex> lock(cache_lock);
        if (SUCCEEDED(hr)) {
            cache[key].blob = out;
            cache[key].done = true;
        } else {
            cache.erase(key);
        }
    }
    cache_done.notify_all();
    return hr;
}

void warm_shaders() {
    // In the order a frame first needs them: drawing, the compositor, a planar picture's repack, outputs, then
    // transitions.
    warm_drawing_shaders();
    warm_compositor_shaders();
    warm_repack_shaders();
    warm_nv12_shaders();
    warm_transition_shaders();
}

HRESULT compile_now(const char *source, size_t size, const char *entry, const char *profile, ComPtr<ID3DBlob> &out) {
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

// The card to draw on when none is named: Windows' high-performance one. On a laptop with two (an Intel or AMD
// chip plus an NVIDIA or AMD card) the default is the small chip, which the screen hangs off: SprigCast drew there
// at 98-100% while NVENC encoded on the GTX 1070 (tester CRGPlays, i7-7700HQ + HD 630 + GTX 1070, 2026-11-27).
// A PC with one card gets that card, as before. Needs Windows 10 1803 (IDXGIFactory6); older ones keep the default.
HRESULT preferred_adapter(ComPtr<IDXGIAdapter1> &out) {
    ComPtr<IDXGIFactory1> factory;
    HRESULT hr = CreateDXGIFactory1(__uuidof(IDXGIFactory1), reinterpret_cast<void **>(factory.GetAddressOf()));
    if (FAILED(hr)) return hr;
    ComPtr<IDXGIFactory6> six;
    if (FAILED(factory.As(&six))) return E_NOINTERFACE;
    for (UINT i = 0;; ++i) {
        ComPtr<IDXGIAdapter1> adapter;
        hr = six->EnumAdapterByGpuPreference(i, DXGI_GPU_PREFERENCE_HIGH_PERFORMANCE, __uuidof(IDXGIAdapter1),
                                             reinterpret_cast<void **>(adapter.GetAddressOf()));
        if (FAILED(hr)) return hr;  // DXGI_ERROR_NOT_FOUND: only software ones
        DXGI_ADAPTER_DESC1 desc;
        if (SUCCEEDED(adapter->GetDesc1(&desc)) && !(desc.Flags & DXGI_ADAPTER_FLAG_SOFTWARE)) {
            out = adapter;
            return S_OK;
        }
    }
}

HRESULT adapter_at(int index, ComPtr<IDXGIAdapter1> &out) {
    ComPtr<IDXGIFactory1> factory;
    HRESULT hr = CreateDXGIFactory1(__uuidof(IDXGIFactory1), reinterpret_cast<void **>(factory.GetAddressOf()));
    if (FAILED(hr)) return hr;
    return factory->EnumAdapters1(static_cast<UINT>(index), out.GetAddressOf());
}

// ---- the drawing shader -------------------------------------------------------------------------------------

// One quad per draw, made from the vertex number: no vertex buffer. `rect` is where it lands, in clip space.
#include "shader_draw.h"

struct DrawConstants {
    float rect[4];
    float opacity;
    float unused[3];
};

}  // namespace

void sc::warm_drawing_shaders() {
    ComPtr<ID3DBlob> code;
    compile(SHADER, sizeof SHADER - 1, "vs_main", "vs_4_0", code);
    compile(SHADER, sizeof SHADER - 1, "ps_main", "ps_4_0", code);
}

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
        // On a hybrid laptop the screen hangs off the small chip and the NVIDIA or AMD card has none: every
        // picture drawn there crosses to the chip to be shown. Python eases the previews then.
        ComPtr<IDXGIOutput> output;
        for (UINT i = 0; i < 16 && SUCCEEDED(adapter->EnumOutputs(i, output.ReleaseAndGetAddressOf())); ++i)
            ++out->screens;
        return SC_OK;
    });
}

SC_API int sc_device_create(int index, int flags, sc_device **out) {
    return guarded("sc_device_create", [&]() -> int {
        if (!out) return fail(SC_ERR_ARGUMENT, "sc_device_create: no output");
        *out = nullptr;
        ComPtr<IDXGIAdapter1> adapter;
        D3D_DRIVER_TYPE type = D3D_DRIVER_TYPE_HARDWARE;
        bool picked = false;
        if (flags & SC_DEVICE_WARP) {
            type = D3D_DRIVER_TYPE_WARP;
        } else if (index >= 0) {
            HRESULT hr = adapter_at(index, adapter);
            if (FAILED(hr)) return fail_hr(SC_ERR_ARGUMENT, "Finding that graphics card", hr);
            type = D3D_DRIVER_TYPE_UNKNOWN;
        } else if (SUCCEEDED(preferred_adapter(adapter))) {
            type = D3D_DRIVER_TYPE_UNKNOWN;  // the high-performance card (a laptop's NVIDIA or AMD one)
            picked = true;
        } else {
            adapter.Reset();  // Windows' default, as before
        }
        // 10.0 is the floor: the oldest cards SprigCast supports (Intel HD 4000, Radeon R2) have 11.0 or better.
        const D3D_FEATURE_LEVEL levels[] = {D3D_FEATURE_LEVEL_11_1, D3D_FEATURE_LEVEL_11_0,
                                            D3D_FEATURE_LEVEL_10_1, D3D_FEATURE_LEVEL_10_0};
        auto dev = std::make_unique<sc_device>();
        // Asked with video support first, so videos can decode on this same device (media.cpp media_card); a
        // card or driver without it gets a device all the same.
        HRESULT hr = E_FAIL;
        if (type != D3D_DRIVER_TYPE_WARP)
            hr = D3D11CreateDevice(adapter.Get(), type, nullptr,
                                   D3D11_CREATE_DEVICE_BGRA_SUPPORT | D3D11_CREATE_DEVICE_VIDEO_SUPPORT, levels,
                                   static_cast<UINT>(std::size(levels)), D3D11_SDK_VERSION, &dev->device, &dev->level,
                                   &dev->context);
        if (FAILED(hr))
            hr = D3D11CreateDevice(adapter.Get(), type, nullptr, D3D11_CREATE_DEVICE_BGRA_SUPPORT, levels,
                                   static_cast<UINT>(std::size(levels)), D3D11_SDK_VERSION, &dev->device,
                                   &dev->level, &dev->context);
        if (FAILED(hr) && picked) {  // the high-performance card wouldn't make one: Windows' default, as before
            log_line(SC_LOG_WARNING, "device: the high-performance card made no device; using Windows' default");
            adapter.Reset();
            type = D3D_DRIVER_TYPE_HARDWARE;
            hr = D3D11CreateDevice(nullptr, type, nullptr, D3D11_CREATE_DEVICE_BGRA_SUPPORT, levels,
                                   static_cast<UINT>(std::size(levels)), D3D11_SDK_VERSION, &dev->device,
                                   &dev->level, &dev->context);
        }
        if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "D3D11CreateDevice", hr);
        // An encoder session on this device works from its own thread: Direct3D keeps the two apart.
        ComPtr<ID3D11Multithread> guard;
        if (SUCCEEDED(dev->device.As(&guard))) guard->SetMultithreadProtected(TRUE);
        UINT support = 0;
        dev->nv12_targets = SUCCEEDED(dev->device->CheckFormatSupport(DXGI_FORMAT_NV12, &support)) &&
                            (support & D3D11_FORMAT_SUPPORT_RENDER_TARGET) != 0;
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
