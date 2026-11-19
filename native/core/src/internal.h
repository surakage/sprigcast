// What the engine's source files share: errors, the log, the crash gate, and the device and canvas.
#pragma once

#include "base.h"
#include "move.h"

#include <d3d11.h>
#include <d3d11_4.h>
#include <d3dcompiler.h>
#include <dxgi1_2.h>
#include <windows.h>
#include <wrl/client.h>

#include <atomic>
#include <cstdint>
#include <exception>
#include <map>
#include <memory>
#include <mutex>
#include <new>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>

using Microsoft::WRL::ComPtr;

namespace sc {

int fail_hr(int code, const char *what, HRESULT hr);

}  // namespace sc

struct sc_device;

namespace sc {

struct Source {
    ComPtr<ID3D11Texture2D> texture;
    ComPtr<ID3D11ShaderResourceView> view;
    int data_w = 0, data_h = 0;   // the texture
    int pic_w = 0, pic_h = 0;     // the picture (planar: the Y plane)
    int layout_w = 0, layout_h = 0;
    int format = SC_FMT_RGBA;
    int planar = 0;               // 0, 1 (I420) or 2 (I420A)
    float yuv[3] = {0, 0, 0};
    // An NV12 picture copied on the card (sc::source_nv12): the texture is also a target the repack draws into,
    // and the NV12 picture is copied here first (a decoder's array slice can't be read in place).
    ComPtr<ID3D11RenderTargetView> target;
    ComPtr<ID3D11Texture2D> nv12, nv12_staging;
    ComPtr<ID3D11ShaderResourceView> nv12_y, nv12_uv;
    bool premultiplied = false;   // RGBA with premultiplied alpha (a browser page drawn on the card)
};

// A browser page's texture shared by the browser helper, opened on the engine's device (sc::source_page).
struct PageTexture {
    std::wstring name;
    ComPtr<ID3D11Texture2D> texture;
    ComPtr<IDXGIKeyedMutex> mutex;
};

constexpr int SC_NOT_YET = 1;  // source_page: the picture isn't ready yet (taken next frame)

struct Look {
    ComPtr<ID3D11ShaderResourceView> view;
    int size = 0;
};

struct Mask {
    ComPtr<ID3D11ShaderResourceView> view;
    bool luma = false;
};

struct Target {
    ComPtr<ID3D11Texture2D> texture;
    ComPtr<ID3D11RenderTargetView> target;
    ComPtr<ID3D11ShaderResourceView> view;
    int width = 0, height = 0;
};

// A source drawn from a list of other sources' pictures (sc_source_draws): drawn into `target` when the list changes.
struct Drawn {
    int width = 0, height = 0;
    std::vector<sc_item> items;
    bool dirty = true;
    Target target;
};

struct Snapshot {
    std::vector<sc_scene> scenes;
    std::vector<sc_item> items;
    std::vector<sc_effects> effects;
    std::unordered_map<uint64_t, size_t> by_id;
};

struct Compositor {
    ComPtr<ID3D11VertexShader> vs;
    ComPtr<ID3D11PixelShader> ps;
    ComPtr<ID3D11Buffer> constants;
    ComPtr<ID3D11SamplerState> wrap, clamp;
    std::unordered_map<uint64_t, Source> sources;
    std::unordered_map<uint64_t, Look> looks;
    std::unordered_map<uint64_t, Mask> masks;
    std::unordered_map<uint64_t, Target> parts;          // a sprig's drawing, by snapshot scene id
    std::unordered_map<uint64_t, Drawn> drawn;           // sources drawn from a list (store plugins' animations)
    std::map<std::pair<uint64_t, int>, double> spins;    // (source, run) -> when its barrel roll started
    std::shared_ptr<const Snapshot> snapshot;
};

HRESULT compile(const char *source, size_t size, const char *entry, const char *profile, ComPtr<ID3DBlob> &out);
int make_target(ID3D11Device *device, int width, int height, Target &out,
                DXGI_FORMAT format = DXGI_FORMAT_R8G8B8A8_UNORM);
// Checks an upload's description; gives the bytes its data spans (the last row needs only its own pixels).
int upload_bytes(const sc_upload &how, size_t &bytes);
// An NV12 picture on the card (a texture, or slice `slice` of a decoder's texture array) as source `id`, without
// passing through memory: copied, then repacked into the compositor's I420 layout (line for line the same as an
// I420 upload of it). `how` gives the picture's size (format SC_FMT_NV12, height = picture height * 3 / 2), its
// colours and layout size. SC_ERR_DEVICE when this card can't read NV12 textures in shaders. Takes device.lock.
int source_nv12(sc_device &device, uint64_t id, ID3D11Texture2D *picture, unsigned slice, const sc_upload &how);
// An SC_FMT_NV12 upload from memory: `done` when it went onto the card as NV12 (source_nv12); otherwise `rows`
// holds the same picture as I420 rows (width bytes each) for the ordinary upload. Takes device.lock.
int upload_nv12(sc_device &device, uint64_t id, const uint8_t *data, const sc_upload &how,
                std::vector<uint8_t> &rows, bool &done);
// A browser page's newest picture, drawn by the browser helper into the texture shared as `name`
// (page_texture.cpp), copied into source `id` (its top-left `width` x `height`). SC_NOT_YET while the helper
// still holds the texture. `page` keeps the opened texture from one frame to the next. Takes device.lock.
// copy = false: the picture is taken (the helper may draw the next) but the source keeps the one it has (a freeze).
int source_page(sc_device &device, uint64_t id, PageTexture &page, const std::wstring &name, int width, int height,
                bool copy = true);

}  // namespace sc

struct sc_device {
    ComPtr<ID3D11Device> device;
    ComPtr<ID3D11DeviceContext> context;
    D3D_FEATURE_LEVEL level = D3D_FEATURE_LEVEL_10_0;
    std::mutex lock;  // the immediate context is used by one thread at a time
    std::atomic<bool> faulted{false};
    bool nv12_targets = false;  // the card draws into NV12 textures (frames shared with an encoder)

    // The phase 1 drawing (sc_canvas_draw_bgra), made the first time it's used.
    ComPtr<ID3D11VertexShader> vs;
    ComPtr<ID3D11PixelShader> ps;
    ComPtr<ID3D11Buffer> constants;
    ComPtr<ID3D11SamplerState> sampler;
    ComPtr<ID3D11BlendState> over;
    ComPtr<ID3D11RasterizerState> raster;
    int prepare_drawing();

    sc::Compositor comp;
    int prepare_compositor();

    // Transitions (made the first time one runs).
    ComPtr<ID3D11VertexShader> trans_vs;
    ComPtr<ID3D11PixelShader> trans_ps;
    ComPtr<ID3D11Buffer> trans_constants;
    int prepare_transitions();

    // NV12 for encoders (made the first time an output wants it).
    ComPtr<ID3D11VertexShader> nv12_vs;
    ComPtr<ID3D11PixelShader> nv12_ps;
    ComPtr<ID3D11Buffer> nv12_constants;
    int prepare_nv12();

    // NV12 pictures on the card into a source (sc::source_nv12).
    int nv12_sources = -1;  // -1 not checked yet, 0 this card can't read NV12 textures in shaders, 1 it can
    ComPtr<ID3D11VertexShader> repack_vs;
    ComPtr<ID3D11PixelShader> repack_ps;
    ComPtr<ID3D11Buffer> repack_constants;
    int prepare_repack();
};

struct sc_canvas {
    sc_device *device = nullptr;
    int width = 0, height = 0;
    ComPtr<ID3D11Texture2D> texture;
    ComPtr<ID3D11RenderTargetView> target;
    ComPtr<ID3D11ShaderResourceView> view;  // previews draw the canvas from this
    ComPtr<ID3D11Texture2D> staging;  // made on the first read
    sc::Target outgoing, incoming;    // transitions: the two scenes, let go after a while unused
    double transition_used = 0;
    std::vector<sc::MoveDraw> move_draws;  // a Move's pieces this frame, kept so frames don't allocate
};

namespace sc {

// Draws snapshot scene `scene` (sprigs first) into a target. The device's lock must be held.
int render_scene(sc_device &dev, ID3D11RenderTargetView *target, int width, int height, uint64_t scene, double time);
// Draws a transition onto the canvas. The device's lock must be held.
int render_transition(sc_canvas &canvas, uint64_t from, uint64_t to, double time, const sc_transition &how,
                      float progress);
// Draws a frame of a Move onto the canvas (move.h). The device's lock must be held.
int render_move(sc_canvas &canvas, const MovePlan &plan, double time, float progress);
// Whether the snapshot has this scene.
bool has_scene(sc_device &dev, uint64_t scene);

// Draws `picture` into the rectangle x, y, w, h of a target `tw` x `th` (the phase 1 drawing: bilinear, "over"
// blending, or a plain copy with blend = false). The device's lock must be held.
int draw_view(sc_device &dev, ID3D11RenderTargetView *target, int tw, int th, ID3D11ShaderResourceView *picture,
              float x, float y, float w, float h, float opacity, bool blend);

// An output's two planes: Y (one channel, width x height) and CbCr (two channels, half each way).
struct Nv12Planes {
    Target y, uv;
    int width = 0, height = 0;
};
int make_nv12(ID3D11Device *device, int width, int height, Nv12Planes &out);
// Draws `canvas` (cw x ch) scaled into the two planes. The device's lock must be held.
int draw_nv12(sc_device &dev, ID3D11ShaderResourceView *canvas, int cw, int ch, ID3D11RenderTargetView *y,
              ID3D11RenderTargetView *uv, int width, int height, int scale_mode);
// A CPU-readable copy of a texture's kind.
int make_staging(ID3D11Device *device, ID3D11Texture2D *like, ComPtr<ID3D11Texture2D> &out);
// Copies mapped staging planes into one NV12 buffer (Y rows, then CbCr rows).
void copy_nv12(const D3D11_MAPPED_SUBRESOURCE &y, const D3D11_MAPPED_SUBRESOURCE &uv, int width, int height,
               uint8_t *out);

}  // namespace sc

namespace sc {

// The crash gate for calls on a device: a faulted device refuses work; a fault inside marks it faulted; a
// removed or reset graphics card is reported as SC_ERR_DEVICE_LOST.
template <typename F>
int device_call(sc_device *device, const char *name, F &&body) {
    if (!device) return fail(SC_ERR_ARGUMENT, std::string(name) + ": no device");
    if (device->faulted.load()) return fail(SC_ERR_FAULTED, std::string(name) + ": this device faulted earlier");
    int code = guarded(name, body);
    if (code == SC_ERR_INTERNAL && !device->faulted.exchange(true)) {
        log_line(SC_LOG_ERROR, std::string(name) + ": the device is shut off after a fault");
    }
    if (code == SC_ERR_DEVICE && device->device) {
        HRESULT why = device->device->GetDeviceRemovedReason();
        if (FAILED(why)) {
            device->faulted.store(true);
            return fail_hr(SC_ERR_DEVICE_LOST, "The graphics card was reset or removed", why);
        }
    }
    return code;
}

}  // namespace sc
