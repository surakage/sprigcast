// Browser pages drawn straight onto the card (docs/native-engine.md): the browser helper process draws a page with
// Qt Quick on a Direct3D 11 device of ours, into a texture shared by name with the engine; the engine copies each
// new picture into the page's source on its own device. No picture of the page passes through memory.
//
// One texture, guarded by its keyed mutex: key 0 = the helper may draw, key 1 = a picture waits for the engine.
// The helper's FrameSlot header says when a new one is there (slot 2 = "on the card", and the texture's number).
#include "internal.h"

#include <d3d11_1.h>
#include <dxgi1_4.h>

#include <algorithm>
#include <string>

using Microsoft::WRL::ComPtr;

struct sc_page_target {
    ComPtr<ID3D11Device> device;
    ComPtr<ID3D11DeviceContext> context;
    ComPtr<ID3D11Texture2D> texture;
    ComPtr<IDXGIKeyedMutex> mutex;
    ComPtr<ID3D11Texture2D> staging;  // for sc_page_target_read
    HANDLE shared = nullptr;
    bool held = false;
    int width = 0, height = 0;
};

namespace {

// The adapter with this LUID: a card, or the software renderer (tests); null when there is none.
ComPtr<IDXGIAdapter1> adapter_with(uint32_t low, int32_t high) {
    ComPtr<IDXGIFactory1> factory;
    if (FAILED(CreateDXGIFactory1(IID_PPV_ARGS(&factory)))) return nullptr;
    ComPtr<IDXGIAdapter1> adapter;
    for (UINT i = 0; SUCCEEDED(factory->EnumAdapters1(i, &adapter)); ++i) {
        DXGI_ADAPTER_DESC1 desc = {};
        if (SUCCEEDED(adapter->GetDesc1(&desc)) && desc.AdapterLuid.LowPart == low && desc.AdapterLuid.HighPart == high)
            return adapter;
        adapter.Reset();
    }
    ComPtr<IDXGIFactory4> four;
    if (SUCCEEDED(factory.As(&four)) && SUCCEEDED(four->EnumWarpAdapter(IID_PPV_ARGS(&adapter)))) {
        DXGI_ADAPTER_DESC1 desc = {};
        if (SUCCEEDED(adapter->GetDesc1(&desc)) && desc.AdapterLuid.LowPart == low && desc.AdapterLuid.HighPart == high)
            return adapter;
    }
    return nullptr;
}

}  // namespace

namespace sc {

int source_page(sc_device &dev, uint64_t id, PageTexture &page, const std::wstring &name, int width, int height,
                bool copy, uint32_t flags) {
    std::lock_guard<std::mutex> lock(dev.lock);
    if (page.name != name || !page.texture) {
        page = PageTexture();
        ComPtr<ID3D11Device1> one;
        HRESULT hr = dev.device.As(&one);
        if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "the engine's device can't open shared textures", hr);
        hr = one->OpenSharedResourceByName(name.c_str(), DXGI_SHARED_RESOURCE_READ | DXGI_SHARED_RESOURCE_WRITE,
                                           IID_PPV_ARGS(&page.texture));
        if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "Opening a browser page's texture", hr);
        hr = page.texture.As(&page.mutex);
        if (FAILED(hr)) {
            page = PageTexture();
            return fail_hr(SC_ERR_DEVICE, "a browser page's texture has no keyed mutex", hr);
        }
        page.name = name;
    }
    D3D11_TEXTURE2D_DESC desc = {};
    page.texture->GetDesc(&desc);
    if (width < 1 || height < 1 || static_cast<UINT>(width) > desc.Width || static_cast<UINT>(height) > desc.Height)
        return fail(SC_ERR_ARGUMENT, "a browser page's picture is bigger than its texture");
    HRESULT hr = page.mutex->AcquireSync(1, 0);
    if (hr == static_cast<HRESULT>(WAIT_TIMEOUT)) return SC_NOT_YET;  // the helper is still drawing it
    if (FAILED(hr) || hr == static_cast<HRESULT>(WAIT_ABANDONED)) {
        page = PageTexture();
        return fail_hr(SC_ERR_DEVICE, "AcquireSync (browser page)", hr);
    }
    if (!copy) {
        page.mutex->ReleaseSync(0);  // the helper may draw the next one; the source keeps its picture
        return SC_OK;
    }
    Source &s = dev.comp.sources[id];
    D3D11_TEXTURE2D_DESC have = {};
    if (s.texture) s.texture->GetDesc(&have);
    if (!s.texture || s.data_w != width || s.data_h != height || s.planar || have.Format != desc.Format) {
        s = Source();
        D3D11_TEXTURE2D_DESC td = {};
        td.Width = static_cast<UINT>(width);
        td.Height = static_cast<UINT>(height);
        td.MipLevels = 1;
        td.ArraySize = 1;
        td.Format = desc.Format;  // the shared texture's own (a page's R8G8B8A8, a capture's B8G8R8A8)
        td.SampleDesc.Count = 1;
        td.Usage = D3D11_USAGE_DEFAULT;
        td.BindFlags = D3D11_BIND_SHADER_RESOURCE;
        hr = dev.device->CreateTexture2D(&td, nullptr, &s.texture);
        if (SUCCEEDED(hr)) hr = dev.device->CreateShaderResourceView(s.texture.Get(), nullptr, &s.view);
        if (FAILED(hr)) {
            page.mutex->ReleaseSync(1);  // still the engine's to take: it tries again next frame
            dev.comp.sources.erase(id);
            return fail_hr(SC_ERR_DEVICE, "Creating a browser page's source", hr);
        }
        s.data_w = s.pic_w = s.layout_w = width;
        s.data_h = s.pic_h = s.layout_h = height;
    }
    const D3D11_BOX box = {0, 0, 0, static_cast<UINT>(width), static_cast<UINT>(height), 1};
    dev.context->CopySubresourceRegion(s.texture.Get(), 0, 0, 0, 0, page.texture.Get(), 0, &box);
    page.mutex->ReleaseSync(0);  // the helper may draw the next one
    s.format = (flags & SHARED_OPAQUE) ? SC_FMT_RGB : SC_FMT_RGBA;
    s.premultiplied = !(flags & SHARED_STRAIGHT);  // Qt Quick draws premultiplied alpha
    return SC_OK;
}

}  // namespace sc

extern "C" {

SC_API int sc_d3d_device_luid(void *device, uint32_t *low, int32_t *high) {
    return sc::guarded("sc_d3d_device_luid", [&]() -> int {
        if (!device || !low || !high) return sc::fail(SC_ERR_ARGUMENT, "sc_d3d_device_luid: nothing given");
        ComPtr<IDXGIDevice> dxgi;
        ComPtr<IDXGIAdapter> adapter;
        DXGI_ADAPTER_DESC desc = {};
        HRESULT hr = static_cast<ID3D11Device *>(device)->QueryInterface(IID_PPV_ARGS(&dxgi));
        if (SUCCEEDED(hr)) hr = dxgi->GetAdapter(&adapter);
        if (SUCCEEDED(hr)) hr = adapter->GetDesc(&desc);
        if (FAILED(hr)) return sc::fail_hr(SC_ERR_DEVICE, "the device's graphics card", hr);
        *low = desc.AdapterLuid.LowPart;
        *high = desc.AdapterLuid.HighPart;
        return SC_OK;
    });
}

}  // extern "C"

namespace {

int make_page_target(uint32_t luid_low, int32_t luid_high, void *device, int width, int height, const wchar_t *name,
                DXGI_FORMAT format, sc_page_target **out) {
    return sc::guarded("sc_page_target_create", [&]() -> int {
        if (!out || !name || !*name || width < 1 || height < 1 || width > 8192 || height > 8192)
            return sc::fail(SC_ERR_ARGUMENT, "sc_page_target_create: no name, or a size it can't make");
        *out = nullptr;
        auto t = std::make_unique<sc_page_target>();
        HRESULT hr = S_OK;
        if (device) {  // the device an earlier target made: Qt Quick keeps drawing with it after a resize
            t->device = static_cast<ID3D11Device *>(device);
            t->device->GetImmediateContext(&t->context);
        } else {
            ComPtr<IDXGIAdapter1> adapter = adapter_with(luid_low, luid_high);
            if (!adapter)
                return sc::fail(SC_ERR_DEVICE, "sc_page_target_create: the engine's graphics card isn't here");
            const D3D_FEATURE_LEVEL levels[] = {D3D_FEATURE_LEVEL_11_1, D3D_FEATURE_LEVEL_11_0,
                                                D3D_FEATURE_LEVEL_10_1, D3D_FEATURE_LEVEL_10_0};
            hr = D3D11CreateDevice(adapter.Get(), D3D_DRIVER_TYPE_UNKNOWN, nullptr, D3D11_CREATE_DEVICE_BGRA_SUPPORT,
                                   levels, static_cast<UINT>(std::size(levels)), D3D11_SDK_VERSION, &t->device,
                                   nullptr, &t->context);
            if (FAILED(hr)) return sc::fail_hr(SC_ERR_DEVICE, "D3D11CreateDevice (browser page)", hr);
            ComPtr<ID3D11Multithread> guard;
            if (SUCCEEDED(t->device.As(&guard))) guard->SetMultithreadProtected(TRUE);
        }
        D3D11_TEXTURE2D_DESC td = {};
        td.Width = static_cast<UINT>(width);
        td.Height = static_cast<UINT>(height);
        td.MipLevels = 1;
        td.ArraySize = 1;
        td.Format = format;
        td.SampleDesc.Count = 1;
        td.Usage = D3D11_USAGE_DEFAULT;
        td.BindFlags = D3D11_BIND_RENDER_TARGET | D3D11_BIND_SHADER_RESOURCE;
        td.MiscFlags = D3D11_RESOURCE_MISC_SHARED_NTHANDLE | D3D11_RESOURCE_MISC_SHARED_KEYEDMUTEX;
        hr = t->device->CreateTexture2D(&td, nullptr, &t->texture);
        if (FAILED(hr)) return sc::fail_hr(SC_ERR_DEVICE, "Creating a browser page's shared texture", hr);
        ComPtr<IDXGIResource1> resource;
        hr = t->texture.As(&resource);
        if (SUCCEEDED(hr))
            hr = resource->CreateSharedHandle(nullptr, DXGI_SHARED_RESOURCE_READ | DXGI_SHARED_RESOURCE_WRITE, name,
                                              &t->shared);
        if (FAILED(hr)) return sc::fail_hr(SC_ERR_DEVICE, "Sharing a browser page's texture", hr);
        hr = t->texture.As(&t->mutex);
        if (FAILED(hr)) {
            CloseHandle(t->shared);
            return sc::fail_hr(SC_ERR_DEVICE, "a browser page's texture has no keyed mutex", hr);
        }
        t->width = width;
        t->height = height;
        *out = t.release();
        return SC_OK;
    });
}

}  // namespace

int sc::page_target_bgra(uint32_t luid_low, int32_t luid_high, void *device, int width, int height,
                         const wchar_t *name, sc_page_target **out) {
    return make_page_target(luid_low, luid_high, device, width, height, name, DXGI_FORMAT_B8G8R8A8_UNORM, out);
}

extern "C" {

SC_API int sc_page_target_create(uint32_t luid_low, int32_t luid_high, void *device, int width, int height,
                                 const wchar_t *name, sc_page_target **out) {
    return make_page_target(luid_low, luid_high, device, width, height, name, DXGI_FORMAT_R8G8B8A8_UNORM, out);
}

SC_API int sc_page_target_handles(sc_page_target *t, void **device, void **context, void **texture) {
    return sc::guarded("sc_page_target_handles", [&]() -> int {
        if (!t || !device || !context || !texture) return sc::fail(SC_ERR_ARGUMENT, "sc_page_target_handles");
        *device = t->device.Get();
        *context = t->context.Get();
        *texture = t->texture.Get();
        return SC_OK;
    });
}

SC_API int sc_page_target_begin(sc_page_target *t, int timeout_ms) {
    return sc::guarded("sc_page_target_begin", [&]() -> int {
        if (!t) return sc::fail(SC_ERR_ARGUMENT, "sc_page_target_begin: no target");
        if (t->held) return 1;
        const HRESULT hr = t->mutex->AcquireSync(0, static_cast<DWORD>(std::max(0, timeout_ms)));
        if (hr == static_cast<HRESULT>(WAIT_TIMEOUT)) return 0;  // the engine hasn't taken the last one yet
        if (FAILED(hr) || hr == static_cast<HRESULT>(WAIT_ABANDONED))
            return sc::fail_hr(SC_ERR_DEVICE, "AcquireSync (browser page)", hr);
        t->held = true;
        return 1;
    });
}

SC_API int sc_page_target_read(sc_page_target *t, uint8_t *rgba, int stride) {
    return sc::guarded("sc_page_target_read", [&]() -> int {
        if (!t || !t->held || !rgba || stride < t->width * 4)
            return sc::fail(SC_ERR_ARGUMENT, "sc_page_target_read: not drawing, or no room");
        if (!t->staging) {
            D3D11_TEXTURE2D_DESC td = {};
            t->texture->GetDesc(&td);
            td.Usage = D3D11_USAGE_STAGING;
            td.BindFlags = 0;
            td.MiscFlags = 0;
            td.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
            const HRESULT hr = t->device->CreateTexture2D(&td, nullptr, &t->staging);
            if (FAILED(hr)) return sc::fail_hr(SC_ERR_DEVICE, "Creating a browser page's read-back texture", hr);
        }
        t->context->CopyResource(t->staging.Get(), t->texture.Get());
        D3D11_MAPPED_SUBRESOURCE m;
        const HRESULT hr = t->context->Map(t->staging.Get(), 0, D3D11_MAP_READ, 0, &m);
        if (FAILED(hr)) return sc::fail_hr(SC_ERR_DEVICE, "Map (browser page read-back)", hr);
        for (int y = 0; y < t->height; ++y) {  // premultiplied, as Qt Quick draws, to straight alpha
            const uint8_t *from = static_cast<const uint8_t *>(m.pData) + static_cast<size_t>(y) * m.RowPitch;
            uint8_t *to = rgba + static_cast<size_t>(y) * stride;
            for (int x = 0; x < t->width; ++x) {
                const unsigned a = from[x * 4 + 3];
                for (int c = 0; c < 3; ++c)
                    to[x * 4 + c] = a == 0 ? 0 : static_cast<uint8_t>(std::min(255u, (from[x * 4 + c] * 255u + a / 2) / a));
                to[x * 4 + 3] = static_cast<uint8_t>(a);
            }
        }
        t->context->Unmap(t->staging.Get(), 0);
        return SC_OK;
    });
}

SC_API int sc_page_target_fill(sc_page_target *t, const float *rgba) {
    return sc::guarded("sc_page_target_fill", [&]() -> int {
        if (!t || !t->held || !rgba) return sc::fail(SC_ERR_ARGUMENT, "sc_page_target_fill: not drawing");
        ComPtr<ID3D11RenderTargetView> view;
        const HRESULT hr = t->device->CreateRenderTargetView(t->texture.Get(), nullptr, &view);
        if (FAILED(hr)) return sc::fail_hr(SC_ERR_DEVICE, "CreateRenderTargetView (browser page)", hr);
        t->context->ClearRenderTargetView(view.Get(), rgba);
        return SC_OK;
    });
}

SC_API int sc_page_target_end(sc_page_target *t, int publish) {
    return sc::guarded("sc_page_target_end", [&]() -> int {
        if (!t || !t->held) return sc::fail(SC_ERR_ARGUMENT, "sc_page_target_end: not drawing");
        t->context->Flush();
        t->held = false;
        const HRESULT hr = t->mutex->ReleaseSync(publish ? 1 : 0);
        if (FAILED(hr)) return sc::fail_hr(SC_ERR_DEVICE, "ReleaseSync (browser page)", hr);
        return SC_OK;
    });
}

SC_API void sc_page_target_destroy(sc_page_target *t) {
    if (!t) return;
    try {
        if (t->held) t->mutex->ReleaseSync(0);
        if (t->shared) CloseHandle(t->shared);
    } catch (...) {
    }
    delete t;
}

}  // extern "C"
