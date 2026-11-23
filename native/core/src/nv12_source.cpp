// NV12 pictures into a source on the card (phase 3): a video decoded by the graphics card reaches the
// compositor without passing through memory. The picture is copied into a texture of the source's own (a
// decoder's array slice can't be read in place), then repacked into the compositor's I420 layout (Y rows, then
// U | V side by side) with integer loads, so it is drawn exactly as an I420 upload of the same picture. An NV12
// upload from memory takes the same road (that's how the checks reach it without a decoder), and when the card
// can't read NV12 textures in shaders it is repacked on the CPU instead.
#include "internal.h"

#include <cstring>
#include <vector>

using namespace sc;

namespace {

#include "shader_repack.h"

struct RepackConstants {
    int width, height;
    int unused[2];
};
static_assert(sizeof(RepackConstants) == 16, "matches the shader's constant buffer");

// The source's texture as the repack's target: R8, the I420 layout's size, drawable.
int planar_target(sc_device &dev, Source &s, int w, int h) {
    const int rows = h + h / 2;
    if (s.texture && s.target && s.data_w == w && s.data_h == rows && s.planar == 1) return SC_OK;
    Source fresh;
    fresh.nv12 = s.nv12;  // the copy of the picture is kept: same size next time
    fresh.nv12_y = s.nv12_y;
    fresh.nv12_uv = s.nv12_uv;
    fresh.nv12_staging = s.nv12_staging;
    s = fresh;
    D3D11_TEXTURE2D_DESC td = {};
    td.Width = static_cast<UINT>(w);
    td.Height = static_cast<UINT>(rows);
    td.MipLevels = 1;
    td.ArraySize = 1;
    td.Format = DXGI_FORMAT_R8_UNORM;
    td.SampleDesc.Count = 1;
    td.Usage = D3D11_USAGE_DEFAULT;
    td.BindFlags = D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_RENDER_TARGET;
    HRESULT hr = dev.device->CreateTexture2D(&td, nullptr, &s.texture);
    if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "Creating a source's texture (NV12)", hr);
    hr = dev.device->CreateShaderResourceView(s.texture.Get(), nullptr, &s.view);
    if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "CreateShaderResourceView (NV12 source)", hr);
    hr = dev.device->CreateRenderTargetView(s.texture.Get(), nullptr, &s.target);
    if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "CreateRenderTargetView (NV12 source)", hr);
    s.data_w = w;
    s.data_h = rows;
    s.planar = 1;
    return SC_OK;
}

// The source's own NV12 texture (and its two plane views) at the size of `like`.
int nv12_copy_target(sc_device &dev, Source &s, const D3D11_TEXTURE2D_DESC &like) {
    if (s.nv12) {
        D3D11_TEXTURE2D_DESC have;
        s.nv12->GetDesc(&have);
        if (have.Width == like.Width && have.Height == like.Height) return SC_OK;
    }
    s.nv12.Reset();
    s.nv12_y.Reset();
    s.nv12_uv.Reset();
    D3D11_TEXTURE2D_DESC td = {};
    td.Width = like.Width;
    td.Height = like.Height;
    td.MipLevels = 1;
    td.ArraySize = 1;
    td.Format = DXGI_FORMAT_NV12;
    td.SampleDesc.Count = 1;
    td.Usage = D3D11_USAGE_DEFAULT;
    td.BindFlags = D3D11_BIND_SHADER_RESOURCE;
    HRESULT hr = dev.device->CreateTexture2D(&td, nullptr, &s.nv12);
    if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "Creating a source's NV12 copy", hr);
    D3D11_SHADER_RESOURCE_VIEW_DESC vd = {};
    vd.ViewDimension = D3D11_SRV_DIMENSION_TEXTURE2D;
    vd.Texture2D.MipLevels = 1;
    vd.Format = DXGI_FORMAT_R8_UNORM;
    hr = dev.device->CreateShaderResourceView(s.nv12.Get(), &vd, &s.nv12_y);
    if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "CreateShaderResourceView (NV12 Y)", hr);
    vd.Format = DXGI_FORMAT_R8G8_UNORM;
    hr = dev.device->CreateShaderResourceView(s.nv12.Get(), &vd, &s.nv12_uv);
    if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "CreateShaderResourceView (NV12 CbCr)", hr);
    return SC_OK;
}

void describe(Source &s, const sc_upload &how, int w, int h) {
    s.format = SC_FMT_I420;
    s.premultiplied = false;
    s.planar = 1;
    s.pic_w = w;
    s.pic_h = h;
    std::memcpy(s.yuv, how.yuv, sizeof s.yuv);
    const bool layout = how.layout_width > 0 && how.layout_height > 0;
    s.layout_w = layout ? how.layout_width : w;
    s.layout_h = layout ? how.layout_height : h;
}

bool can_read_nv12(sc_device &dev) {
    if (dev.nv12_sources < 0) {
        UINT support = 0;
        const UINT need = D3D11_FORMAT_SUPPORT_TEXTURE2D | D3D11_FORMAT_SUPPORT_SHADER_LOAD;
        dev.nv12_sources = SUCCEEDED(dev.device->CheckFormatSupport(DXGI_FORMAT_NV12, &support)) &&
                           (support & need) == need ? 1 : 0;
        if (!dev.nv12_sources)
            log_line(SC_LOG_INFO, "this graphics card can't read NV12 textures in shaders: NV12 pictures are "
                                  "repacked in memory");
    }
    return dev.nv12_sources == 1;
}

// Repacks the NV12 copy in `s` into its I420 texture. The caller holds dev.lock.
int repack(sc_device &dev, Source &s, int w, int h) {
    int ready = dev.prepare_repack();
    if (ready != SC_OK) return ready;
    ready = dev.prepare_drawing();  // its rasterizer state
    if (ready != SC_OK) return ready;
    ID3D11DeviceContext *c = dev.context.Get();
    RepackConstants k = {w, h, {0, 0}};
    D3D11_MAPPED_SUBRESOURCE mapped;
    HRESULT hr = c->Map(dev.repack_constants.Get(), 0, D3D11_MAP_WRITE_DISCARD, 0, &mapped);
    if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "Map (NV12 repack)", hr);
    std::memcpy(mapped.pData, &k, sizeof k);
    c->Unmap(dev.repack_constants.Get(), 0);
    c->OMSetBlendState(nullptr, nullptr, 0xFFFFFFFF);
    c->RSSetState(dev.raster.Get());
    c->IASetInputLayout(nullptr);
    c->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    c->VSSetShader(dev.repack_vs.Get(), nullptr, 0);
    c->PSSetShader(dev.repack_ps.Get(), nullptr, 0);
    ID3D11Buffer *buffers[] = {dev.repack_constants.Get()};
    c->PSSetConstantBuffers(0, 1, buffers);
    ID3D11ShaderResourceView *none[] = {nullptr, nullptr};
    c->PSSetShaderResources(0, 2, none);  // the target may still be bound for reading from the last draw
    ID3D11RenderTargetView *targets[] = {s.target.Get()};
    c->OMSetRenderTargets(1, targets, nullptr);
    ID3D11ShaderResourceView *views[] = {s.nv12_y.Get(), s.nv12_uv.Get()};
    c->PSSetShaderResources(0, 2, views);
    D3D11_VIEWPORT vp = {0, 0, static_cast<float>(w), static_cast<float>(h + h / 2), 0, 1};
    c->RSSetViewports(1, &vp);
    c->Draw(3, 0);
    c->PSSetShaderResources(0, 2, none);
    ID3D11RenderTargetView *no_target[] = {nullptr};
    c->OMSetRenderTargets(1, no_target, nullptr);
    return SC_OK;
}

}  // namespace

void sc::warm_repack_shaders() {
    ComPtr<ID3DBlob> code;
    compile(REPACK_HLSL, sizeof REPACK_HLSL - 1, "vs_main", "vs_4_0", code);
    compile(REPACK_HLSL, sizeof REPACK_HLSL - 1, "ps_main", "ps_4_0", code);
}

int sc_device::prepare_repack() {
    if (repack_vs) return SC_OK;
    ComPtr<ID3DBlob> vs_code, ps_code;
    HRESULT hr = compile(REPACK_HLSL, sizeof REPACK_HLSL - 1, "vs_main", "vs_4_0", vs_code);
    if (FAILED(hr)) return fail_hr(SC_ERR_SHADER, "Compiling the NV12 repack vertex shader", hr);
    hr = compile(REPACK_HLSL, sizeof REPACK_HLSL - 1, "ps_main", "ps_4_0", ps_code);
    if (FAILED(hr)) return fail_hr(SC_ERR_SHADER, "Compiling the NV12 repack pixel shader", hr);
    ComPtr<ID3D11VertexShader> new_vs;
    hr = device->CreateVertexShader(vs_code->GetBufferPointer(), vs_code->GetBufferSize(), nullptr, &new_vs);
    if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "CreateVertexShader (NV12 repack)", hr);
    hr = device->CreatePixelShader(ps_code->GetBufferPointer(), ps_code->GetBufferSize(), nullptr, &repack_ps);
    if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "CreatePixelShader (NV12 repack)", hr);
    D3D11_BUFFER_DESC cb = {};
    cb.ByteWidth = sizeof(RepackConstants);
    cb.Usage = D3D11_USAGE_DYNAMIC;
    cb.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
    cb.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
    hr = device->CreateBuffer(&cb, nullptr, &repack_constants);
    if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "CreateBuffer (NV12 repack)", hr);
    repack_vs = new_vs;  // last: it says the rest is ready
    return SC_OK;
}

int sc::source_nv12(sc_device &dev, uint64_t id, ID3D11Texture2D *picture, unsigned slice, const sc_upload &how) {
    const int w = how.width, h = how.height * 2 / 3;
    if (!picture || !id || how.format != SC_FMT_NV12 || w < 2 || h < 2 || w % 2 || h % 2)
        return fail(SC_ERR_ARGUMENT, "source_nv12: no picture or id, or a size that isn't even");
    std::lock_guard<std::mutex> lock(dev.lock);
    if (!can_read_nv12(dev)) return fail(SC_ERR_DEVICE, "this graphics card can't read NV12 textures in shaders");
    D3D11_TEXTURE2D_DESC desc;
    picture->GetDesc(&desc);
    if (desc.Format != DXGI_FORMAT_NV12 || static_cast<int>(desc.Width) < w || static_cast<int>(desc.Height) < h ||
        slice >= desc.ArraySize)
        return fail(SC_ERR_ARGUMENT, "source_nv12: not an NV12 picture of that size");
    Source &s = dev.comp.sources[id];
    int code = nv12_copy_target(dev, s, desc);
    if (code == SC_OK) code = planar_target(dev, s, w, h);
    if (code != SC_OK) {
        dev.comp.sources.erase(id);
        return code;
    }
    // Both planes, as FFmpeg's own transfer from a decoder does (one subresource per array slice).
    dev.context->CopySubresourceRegion(s.nv12.Get(), 0, 0, 0, 0, picture, slice, nullptr);
    code = repack(dev, s, w, h);
    if (code != SC_OK) return code;
    describe(s, how, w, h);
    return SC_OK;
}

namespace sc {

// An NV12 upload from memory (sc_source_upload): onto the card as NV12 and repacked there, or, on a card that
// can't read NV12 textures, repacked here into I420 rows for the ordinary upload. `rows` receives those.
int upload_nv12(sc_device &dev, uint64_t id, const uint8_t *data, const sc_upload &how, std::vector<uint8_t> &rows,
                bool &done) {
    done = false;
    const int w = how.width, h = how.height * 2 / 3, stride = how.stride;
    bool gpu;
    {
        std::lock_guard<std::mutex> lock(dev.lock);
        gpu = can_read_nv12(dev);
    }
    if (gpu) {
        ComPtr<ID3D11Texture2D> staging;
        {
            std::lock_guard<std::mutex> lock(dev.lock);
            Source &s = dev.comp.sources[id];
            // The next staging texture the card isn't still reading: tried without waiting. All three busy, this
            // picture is skipped and the last one stays up, instead of holding the frame until the card catches up.
            D3D11_MAPPED_SUBRESOURCE m;
            HRESULT hr = DXGI_ERROR_WAS_STILL_DRAWING;
            for (int tries = 0; tries < 3 && hr == DXGI_ERROR_WAS_STILL_DRAWING; ++tries) {
                ComPtr<ID3D11Texture2D> &slot = s.nv12_staging[s.nv12_next];
                s.nv12_next = (s.nv12_next + 1) % 3;
                D3D11_TEXTURE2D_DESC have = {};
                if (slot) slot->GetDesc(&have);
                if (!slot || static_cast<int>(have.Width) != w || static_cast<int>(have.Height) != h) {
                    D3D11_TEXTURE2D_DESC td = {};
                    td.Width = static_cast<UINT>(w);
                    td.Height = static_cast<UINT>(h);
                    td.MipLevels = 1;
                    td.ArraySize = 1;
                    td.Format = DXGI_FORMAT_NV12;
                    td.SampleDesc.Count = 1;
                    td.Usage = D3D11_USAGE_STAGING;
                    td.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
                    slot.Reset();
                    HRESULT made = dev.device->CreateTexture2D(&td, nullptr, &slot);
                    if (FAILED(made)) return fail_hr(SC_ERR_DEVICE, "Creating an NV12 upload texture", made);
                }
                staging = slot;
                hr = dev.context->Map(staging.Get(), 0, D3D11_MAP_WRITE, D3D11_MAP_FLAG_DO_NOT_WAIT, &m);
            }
            if (hr == DXGI_ERROR_WAS_STILL_DRAWING) {
                done = true;  // the card is behind: this picture is let go, the one showing stays
                return SC_OK;
            }
            if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "Map (NV12 upload)", hr);
            auto *out = static_cast<uint8_t *>(m.pData);
            for (int row = 0; row < h + h / 2; ++row)  // the CbCr plane follows the Y plane, same pitch
                std::memcpy(out + static_cast<size_t>(row) * m.RowPitch, data + static_cast<size_t>(row) * stride,
                            static_cast<size_t>(w));
            dev.context->Unmap(staging.Get(), 0);
        }
        int code = source_nv12(dev, id, staging.Get(), 0, how);
        if (code != SC_OK) return code;
        done = true;
        return SC_OK;
    }
    const int cw = w / 2, ch = h / 2;  // as media.cpp's make_picture does it
    rows.resize(static_cast<size_t>(w) * (h + ch));
    for (int row = 0; row < h; ++row)
        std::memcpy(rows.data() + static_cast<size_t>(row) * w, data + static_cast<size_t>(row) * stride,
                    static_cast<size_t>(w));
    uint8_t *uv = rows.data() + static_cast<size_t>(w) * h;
    for (int row = 0; row < ch; ++row) {
        const uint8_t *from = data + static_cast<size_t>(h + row) * stride;
        uint8_t *to = uv + static_cast<size_t>(row) * w;
        for (int x = 0; x < cw; ++x) {
            to[x] = from[2 * x];
            to[cw + x] = from[2 * x + 1];
        }
    }
    return SC_OK;
}

}  // namespace sc
