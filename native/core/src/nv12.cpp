// NV12 for encoders (phase 2): a canvas scaled to an output's size and turned into BT.709 limited-range Y and CbCr
// planes, line for line as render/compositor.py's NV12_FRAGMENT does (top-left uv origin in both, so no flips).
#include "internal.h"

#include <cstring>

using namespace sc;

namespace {

const char NV12_HLSL[] = R"(
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
)";

struct Nv12Constants {
    float out_size[2];
    float texel[2];
    int chroma;
    int scale_mode;
    float unused[2];
};
static_assert(sizeof(Nv12Constants) == 32, "matches the shader's constant buffer");

}  // namespace

int sc_device::prepare_nv12() {
    if (nv12_vs) return SC_OK;
    int ready = prepare_compositor();  // its clamp sampler
    if (ready != SC_OK) return ready;
    ComPtr<ID3DBlob> vs_code, ps_code;
    HRESULT hr = compile(NV12_HLSL, sizeof NV12_HLSL - 1, "vs_main", "vs_4_0", vs_code);
    if (FAILED(hr)) return fail_hr(SC_ERR_SHADER, "Compiling the NV12 vertex shader", hr);
    hr = compile(NV12_HLSL, sizeof NV12_HLSL - 1, "ps_main", "ps_4_0", ps_code);
    if (FAILED(hr)) return fail_hr(SC_ERR_SHADER, "Compiling the NV12 pixel shader", hr);
    ComPtr<ID3D11VertexShader> new_vs;
    hr = device->CreateVertexShader(vs_code->GetBufferPointer(), vs_code->GetBufferSize(), nullptr, &new_vs);
    if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "CreateVertexShader (NV12)", hr);
    hr = device->CreatePixelShader(ps_code->GetBufferPointer(), ps_code->GetBufferSize(), nullptr, &nv12_ps);
    if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "CreatePixelShader (NV12)", hr);
    D3D11_BUFFER_DESC cb = {};
    cb.ByteWidth = sizeof(Nv12Constants);
    cb.Usage = D3D11_USAGE_DYNAMIC;
    cb.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
    cb.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
    hr = device->CreateBuffer(&cb, nullptr, &nv12_constants);
    if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "CreateBuffer (NV12)", hr);
    nv12_vs = new_vs;  // last: it says the rest is ready
    return SC_OK;
}

int sc::make_nv12(ID3D11Device *device, int width, int height, Nv12Planes &out) {
    out = Nv12Planes();
    int code = make_target(device, width, height, out.y, DXGI_FORMAT_R8_UNORM);
    if (code != SC_OK) return code;
    code = make_target(device, width / 2, height / 2, out.uv, DXGI_FORMAT_R8G8_UNORM);
    if (code != SC_OK) {
        out = Nv12Planes();
        return code;
    }
    out.width = width;
    out.height = height;
    return SC_OK;
}

int sc::draw_nv12(sc_device &dev, ID3D11ShaderResourceView *canvas, int cw, int ch, ID3D11RenderTargetView *y,
                  ID3D11RenderTargetView *uv, int width, int height, int scale_mode) {
    (void)cw;
    (void)ch;
    int ready = dev.prepare_nv12();
    if (ready != SC_OK) return ready;
    ready = dev.prepare_drawing();  // its rasterizer state
    if (ready != SC_OK) return ready;
    ID3D11DeviceContext *c = dev.context.Get();
    c->OMSetBlendState(nullptr, nullptr, 0xFFFFFFFF);
    c->RSSetState(dev.raster.Get());
    c->IASetInputLayout(nullptr);
    c->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    c->VSSetShader(dev.nv12_vs.Get(), nullptr, 0);
    c->PSSetShader(dev.nv12_ps.Get(), nullptr, 0);
    ID3D11Buffer *buffers[] = {dev.nv12_constants.Get()};
    c->PSSetConstantBuffers(0, 1, buffers);
    ID3D11SamplerState *samplers[] = {dev.comp.clamp.Get()};
    c->PSSetSamplers(0, 1, samplers);
    for (int plane = 0; plane < 2; ++plane) {
        const int pw = plane ? width / 2 : width, ph = plane ? height / 2 : height;
        Nv12Constants k = {};
        k.out_size[0] = static_cast<float>(pw);
        k.out_size[1] = static_cast<float>(ph);
        k.texel[0] = 1.0f / width;
        k.texel[1] = 1.0f / height;
        k.chroma = plane;
        k.scale_mode = (scale_mode >= SC_SCALE_BILINEAR && scale_mode <= SC_SCALE_LANCZOS) ? scale_mode : 0;
        D3D11_MAPPED_SUBRESOURCE mapped;
        HRESULT hr = c->Map(dev.nv12_constants.Get(), 0, D3D11_MAP_WRITE_DISCARD, 0, &mapped);
        if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "Map (NV12)", hr);
        std::memcpy(mapped.pData, &k, sizeof k);
        c->Unmap(dev.nv12_constants.Get(), 0);
        ID3D11ShaderResourceView *none[] = {nullptr};
        c->PSSetShaderResources(0, 1, none);  // the canvas may still be bound as a target: unbind before setting
        ID3D11RenderTargetView *targets[] = {plane ? uv : y};
        c->OMSetRenderTargets(1, targets, nullptr);
        ID3D11ShaderResourceView *views[] = {canvas};
        c->PSSetShaderResources(0, 1, views);
        D3D11_VIEWPORT vp = {0, 0, static_cast<float>(pw), static_cast<float>(ph), 0, 1};
        c->RSSetViewports(1, &vp);
        c->Draw(3, 0);
    }
    ID3D11ShaderResourceView *none[] = {nullptr};
    c->PSSetShaderResources(0, 1, none);
    return SC_OK;
}

void sc::copy_nv12(const D3D11_MAPPED_SUBRESOURCE &y, const D3D11_MAPPED_SUBRESOURCE &uv, int width, int height,
                   uint8_t *out) {
    const auto *ys = static_cast<const uint8_t *>(y.pData);
    for (int row = 0; row < height; ++row)
        std::memcpy(out + static_cast<size_t>(row) * width, ys + static_cast<size_t>(row) * y.RowPitch,
                    static_cast<size_t>(width));
    uint8_t *uv_out = out + static_cast<size_t>(width) * height;
    const auto *uvs = static_cast<const uint8_t *>(uv.pData);
    for (int row = 0; row < height / 2; ++row)
        std::memcpy(uv_out + static_cast<size_t>(row) * width, uvs + static_cast<size_t>(row) * uv.RowPitch,
                    static_cast<size_t>(width));
}

int sc::make_staging(ID3D11Device *device, ID3D11Texture2D *like, ComPtr<ID3D11Texture2D> &out) {
    D3D11_TEXTURE2D_DESC td;
    like->GetDesc(&td);
    td.Usage = D3D11_USAGE_STAGING;
    td.BindFlags = 0;
    td.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
    HRESULT hr = device->CreateTexture2D(&td, nullptr, &out);
    if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "Creating an NV12 plane's copy", hr);
    return SC_OK;
}

extern "C" {

SC_API int sc_canvas_read_nv12(sc_canvas *canvas, uint8_t *out, int width, int height, int scale_mode) {
    return device_call(canvas ? canvas->device : nullptr, "sc_canvas_read_nv12", [&]() -> int {
        if (!canvas || !out || width < 2 || height < 2 || width % 2 || height % 2 || width > 16384 || height > 16384)
            return fail(SC_ERR_ARGUMENT, "sc_canvas_read_nv12: no canvas or output, or a size that isn't even");
        sc_device &dev = *canvas->device;
        std::lock_guard<std::mutex> lock(dev.lock);
        Nv12Planes planes;
        int code = make_nv12(dev.device.Get(), width, height, planes);
        if (code != SC_OK) return code;
        code = draw_nv12(dev, canvas->view.Get(), canvas->width, canvas->height, planes.y.target.Get(),
                         planes.uv.target.Get(), width, height, scale_mode);
        if (code != SC_OK) return code;
        ComPtr<ID3D11Texture2D> ys, uvs;
        code = make_staging(dev.device.Get(), planes.y.texture.Get(), ys);
        if (code == SC_OK) code = make_staging(dev.device.Get(), planes.uv.texture.Get(), uvs);
        if (code != SC_OK) return code;
        dev.context->CopyResource(ys.Get(), planes.y.texture.Get());
        dev.context->CopyResource(uvs.Get(), planes.uv.texture.Get());
        D3D11_MAPPED_SUBRESOURCE my, muv;
        HRESULT hr = dev.context->Map(ys.Get(), 0, D3D11_MAP_READ, 0, &my);
        if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "Map (NV12 Y)", hr);
        hr = dev.context->Map(uvs.Get(), 0, D3D11_MAP_READ, 0, &muv);
        if (FAILED(hr)) {
            dev.context->Unmap(ys.Get(), 0);
            return fail_hr(SC_ERR_DEVICE, "Map (NV12 CbCr)", hr);
        }
        copy_nv12(my, muv, width, height, out);
        dev.context->Unmap(uvs.Get(), 0);
        dev.context->Unmap(ys.Get(), 0);
        return SC_OK;
    });
}

}  // extern "C"
