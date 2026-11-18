// The compositor: sources' pictures, colour looks, shape pictures and a frozen snapshot of the blooms, drawn the
// way render/compositor.py draws them (same shader, same order, same maths). See docs/native-engine.md.
#include "internal.h"
#include "shader_compositor.h"

#include <algorithm>
#include <cmath>
#include <cstring>
#include <set>

using namespace sc;

namespace {

constexpr double PI = 3.14159265358979323846;

// The shader's constant buffer, float4 for float4 (shader_compositor.h).
struct DrawConstants {
    float corners[4][4];
    float canvas_quad[4];
    float p0[4];
    float yuv4[4];
    float plane_scroll[4];
    float p1[4];
    float key4[4];
    float p2[4];
    float luma4[4];
    float p3[4];
    float tint4[4];
    float p4[4];
    float border4[4];
    float p5[4];
    float p6[4];
    float sil_map[4];
    float uv_rect[4];
    float sil_radius4[4];
    float sil_color[4];
};
static_assert(sizeof(DrawConstants) % 16 == 0, "constant buffers come in 16-byte steps");

// core/filters.py VideoEffects() defaults: the picture untouched.
sc_effects default_effects() {
    sc_effects fx = {};
    fx.key_color[1] = 1.0f;
    fx.similarity = 0.4f;
    fx.smoothness = 0.08f;
    fx.spill = 0.1f;
    fx.luma[1] = 1.0f;
    fx.gamma = 1.0f;
    fx.contrast = 1.0f;
    fx.saturation = 1.0f;
    fx.opacity = 1.0f;
    fx.tint[0] = fx.tint[1] = fx.tint[2] = 1.0f;
    fx.lut_amount = 1.0f;
    fx.border_color[0] = 0.24f;
    fx.border_color[1] = 0.81f;
    fx.border_color[2] = 0.5f;
    return fx;
}

const sc_effects &no_effects() {
    static const sc_effects fx = default_effects();
    return fx;
}

struct Corner {
    double x, y, u, v;
};

// core/scene.py align_share: how far across and down a box the pin point sits.
void align_share(int align, double &ax, double &ay) {
    if (align < 0 || align > 8) align = 4;
    ax = (align % 3) / 2.0;
    ay = (align / 3) / 2.0;
}

// core/scene.py SceneItem.quad, line for line: the corners (top-left, top-right, bottom-left, bottom-right) as
// canvas x, y and texture u, v. Empty when nothing of it shows.
bool item_quad(const sc_item &it, int sw_i, int sh_i, Corner out[4]) {
    const double sw = sw_i, sh = sh_i;
    const double left = it.crop[0], top = it.crop[1], right = it.crop[2], bottom = it.crop[3];
    const double cw = std::max(0.0, sw - left - right), ch = std::max(0.0, sh - top - bottom);
    if (sw == 0 || sh == 0 || cw == 0 || ch == 0) return false;
    const double scale = it.scale, scale_y = it.scale_y < 0 ? it.scale : it.scale_y;
    const bool bounded = it.bounds >= 1 && it.bounds <= 3 && it.bounds_size[0] > 0 && it.bounds_size[1] > 0;
    double bw = cw * scale, bh = ch * scale_y;  // drawn_size
    double sx = scale, sy = scale_y;             // picture_scale
    if (bounded) {
        bw = it.bounds_size[0];
        bh = it.bounds_size[1];
        if (it.bounds == 3) {
            sx = bw / cw;
            sy = bh / ch;
        } else {
            double k = it.bounds == 1 ? std::min(bw / cw, bh / ch) : std::max(bw / cw, bh / ch);
            sx = sy = k;
        }
    }
    const double pw = cw * sx, ph = ch * sy;  // picture_rect
    double px = 0, py = 0;
    if (bounded) {
        double bx, by;
        align_share(it.bounds_align, bx, by);
        px = (bw - pw) * bx;
        py = (bh - ph) * by;
    }
    const double x0 = std::max(0.0, px), y0 = std::max(0.0, py);
    const double x1 = std::min(bw, px + pw), y1 = std::min(bh, py + ph);
    if (x1 <= x0 || y1 <= y0) return false;
    const double u0 = (left + (x0 - px) / sx) / sw, v0 = (top + (y0 - py) / sy) / sh;
    const double u1 = (left + (x1 - px) / sx) / sw, v1 = (top + (y1 - py) / sy) / sh;
    double ax, ay;
    align_share(it.align, ax, ay);
    const double cx = it.x + bw * ax, cy = it.y + bh * ay;
    const double rad = it.rotation * PI / 180.0;
    const double cos_r = std::cos(rad), sin_r = std::sin(rad);
    const double ox = -bw * ax, oy = -bh * ay;
    const Corner local[4] = {{ox + x0, oy + y0, u0, v0}, {ox + x1, oy + y0, u1, v0},
                             {ox + x0, oy + y1, u0, v1}, {ox + x1, oy + y1, u1, v1}};
    for (int i = 0; i < 4; ++i) {
        const Corner &c = local[i];
        out[i] = {cx + c.x * cos_r - c.y * sin_r, cy + c.x * sin_r + c.y * cos_r, c.u, c.v};
    }
    return true;
}

void set4(float *dst, double a, double b, double c, double d) {
    dst[0] = static_cast<float>(a);
    dst[1] = static_cast<float>(b);
    dst[2] = static_cast<float>(c);
    dst[3] = static_cast<float>(d);
}

double wrap01(double x) {  // Python's x % 1.0: always 0 <= result < 1
    double r = std::fmod(x, 1.0);
    return r < 0 ? r + 1.0 : r;
}

struct Piece {
    ID3D11ShaderResourceView *view = nullptr;
    int tex_w = 0, tex_h = 0;     // what is sampled
    int pic_w = 0, pic_h = 0;     // planar: the picture's size
    int planar = 0;
    int swizzle = 0;
    bool premul = false;
    float yuv[3] = {0, 0, 0};
    double detail = 0;            // picture pixels per layout pixel, 0 = none
    int size_w = 0, size_h = 0;   // the size the item is laid out with
};

struct Renderer {
    sc_device &dev;
    const Snapshot &snap;
    double time;
    ID3D11DeviceContext *c;
    bool in_drawn = false;  // drawing a list: its pictures are plain sources, never another list

    bool piece_for(const sc_item &it, Piece &p) {
        auto &comp = dev.comp;
        if (!in_drawn && !it.part) {
            auto drawn = comp.drawn.find(it.source);
            if (drawn != comp.drawn.end()) {
                const Target &t = drawn->second.target;
                if (!t.view) return false;
                p.view = t.view.Get();
                p.tex_w = p.size_w = t.width;
                p.tex_h = p.size_h = t.height;
                p.premul = true;
                return true;
            }
        }
        if (it.part) {
            auto found = comp.parts.find(it.part);
            if (found == comp.parts.end()) return false;
            const Target &t = found->second;
            p.view = t.view.Get();
            p.tex_w = p.size_w = t.width;
            p.tex_h = p.size_h = t.height;
            p.premul = true;
            return true;
        }
        auto found = comp.sources.find(it.source);
        if (found == comp.sources.end()) return false;
        const Source &s = found->second;
        p.view = s.view.Get();
        p.tex_w = s.data_w;
        p.tex_h = s.data_h;
        p.pic_w = s.pic_w;
        p.pic_h = s.pic_h;
        p.planar = s.planar;
        std::memcpy(p.yuv, s.yuv, sizeof p.yuv);
        p.premul = s.format == SC_FMT_PBGRA || s.premultiplied;
        switch (s.format) {
            case SC_FMT_BGRA: case SC_FMT_PBGRA: p.swizzle = 1; break;
            case SC_FMT_RGB: p.swizzle = 2; break;
            case SC_FMT_BGR: case SC_FMT_BGRX: p.swizzle = 3; break;
            default: p.swizzle = 0;
        }
        p.size_w = s.layout_w;
        p.size_h = s.layout_h;
        if (s.layout_w > 0 && std::abs(static_cast<double>(s.pic_w) / s.layout_w - 1.0) > 1e-3)
            p.detail = static_cast<double>(s.pic_w) / s.layout_w;
        return true;
    }

    void draw(const DrawConstants &k) {
        D3D11_MAPPED_SUBRESOURCE mapped;
        if (FAILED(c->Map(dev.comp.constants.Get(), 0, D3D11_MAP_WRITE_DISCARD, 0, &mapped))) return;
        std::memcpy(mapped.pData, &k, sizeof k);
        c->Unmap(dev.comp.constants.Get(), 0);
        c->Draw(4, 0);
    }

    void draw_item(const sc_item &it, int canvas_w, int canvas_h) {
        Piece p;
        if (!piece_for(it, p) || !p.view) return;
        Corner q[4];
        if (!item_quad(it, p.size_w, p.size_h, q)) return;
        const sc_effects &fx = (it.effects >= 0 && static_cast<size_t>(it.effects) < snap.effects.size())
                                   ? snap.effects[static_cast<size_t>(it.effects)] : no_effects();
        auto &comp = dev.comp;

        if (fx.spin_turns != 0.0f) {  // a barrel roll: eases in and out and lands where it began
            if (comp.spins.size() > 64) comp.spins.clear();
            double start = comp.spins.emplace(std::make_pair(it.source, fx.spin_run), time).first->second;
            double t = fx.spin_seconds > 0 ? std::clamp((time - start) / fx.spin_seconds, 0.0, 1.0) : 1.0;
            double eased = t * t * (3.0 - 2.0 * t);
            double a = 2.0 * PI * fx.spin_turns * eased;
            double cx = (q[0].x + q[1].x + q[2].x + q[3].x) / 4, cy = (q[0].y + q[1].y + q[2].y + q[3].y) / 4;
            double ca = std::cos(a), sa = std::sin(a);
            for (auto &corner : q) {
                double dx = corner.x - cx, dy = corner.y - cy;
                corner.x = cx + dx * ca - dy * sa;
                corner.y = cy + dx * sa + dy * ca;
            }
        }

        double blur = fx.blur, pixelate = fx.pixelate;
        int pic_w, pic_h;
        if (p.detail > 0) {
            blur *= p.detail;
            pixelate *= p.detail;
            pic_w = p.size_w;
            pic_h = p.size_h;
        } else if (p.planar) {
            pic_w = p.pic_w;
            pic_h = p.pic_h;
        } else {
            pic_w = p.tex_w;
            pic_h = p.tex_h;
        }

        DrawConstants k = {};
        for (int i = 0; i < 4; ++i) set4(k.corners[i], q[i].x, q[i].y, q[i].u, q[i].v);
        double quad_w = std::hypot(q[1].x - q[0].x, q[1].y - q[0].y);
        double quad_h = std::hypot(q[2].x - q[0].x, q[2].y - q[0].y);
        set4(k.canvas_quad, canvas_w, canvas_h, quad_w, quad_h);
        set4(k.p0, it.fade, p.premul ? 1 : 0, p.planar, p.swizzle);
        set4(k.yuv4, p.yuv[0], p.yuv[1], p.yuv[2], 0);
        const bool moving = fx.scroll[0] != 0.0f || fx.scroll[1] != 0.0f;
        const double w = std::max(1, pic_w), h = std::max(1, pic_h);
        set4(k.plane_scroll, p.planar ? p.pic_w : 0, p.planar ? p.pic_h : 0,
             wrap01(fx.scroll[0] * time / w), wrap01(fx.scroll[1] * time / h));
        set4(k.p1, moving ? 1 : 0, fx.sharpen, fx.key_mode, fx.similarity);
        set4(k.key4, fx.key_color[0], fx.key_color[1], fx.key_color[2], fx.smoothness);
        set4(k.p2, fx.spill, fx.gamma, fx.contrast, fx.brightness);
        set4(k.luma4, fx.luma[0], fx.luma[1], fx.luma[2], fx.saturation);

        ID3D11ShaderResourceView *look = nullptr;
        int look_size = 0;
        if (fx.lut) {
            auto found = comp.looks.find(fx.lut);
            if (found != comp.looks.end()) {
                look = found->second.view.Get();
                look_size = found->second.size;
            }
        }
        set4(k.p3, fx.hue, fx.opacity, look ? 1 : 0, fx.lut_amount);
        set4(k.tint4, fx.tint[0], fx.tint[1], fx.tint[2], look_size);
        int shape = fx.shape;
        ID3D11ShaderResourceView *mask = nullptr;
        bool mask_luma = false;
        if (shape == 7) {
            auto found = fx.mask ? comp.masks.find(fx.mask) : comp.masks.end();
            if (found == comp.masks.end()) {
                shape = 0;
            } else {
                mask = found->second.view.Get();
                mask_luma = found->second.luma;
            }
        }
        set4(k.p4, shape, fx.roundness, fx.stretch ? 1 : 0, fx.feather);
        set4(k.border4, fx.border_color[0], fx.border_color[1], fx.border_color[2], fx.border);
        set4(k.p5, mask_luma ? 1 : 0, blur, pixelate, fx.blackout);

        ID3D11ShaderResourceView *views[3] = {p.view, look, mask};
        c->PSSetShaderResources(0, 3, views);

        // Shadow, glow and stroke: one quad each behind the source, grown by their size, in its shape.
        const int halos = std::clamp(fx.halo_count, 0, SC_MAX_HALOS);
        if (halos) {
            const double x0 = q[0].x, y0 = q[0].y, x1 = q[1].x, y1 = q[1].y, x2 = q[2].x, y2 = q[2].y;
            const double hw = std::max(1e-3, quad_w), hh = std::max(1e-3, quad_h);
            const double ex[2] = {(x1 - x0) / hw, (y1 - y0) / hw}, ey[2] = {(x2 - x0) / hh, (y2 - y0) / hh};
            DrawConstants hk = k;
            set4(hk.uv_rect, q[0].u, q[0].v, q[3].u, q[3].v);
            for (int i = 0; i < halos; ++i) {
                const sc_halo &halo = fx.halos[i];
                const double pad = halo.radius + 2.0, ox = halo.offset[0], oy = halo.offset[1];
                const int signs[4][2] = {{-1, -1}, {1, -1}, {-1, 1}, {1, 1}};
                for (int n = 0; n < 4; ++n) {
                    const int sx = signs[n][0], sy = signs[n][1];
                    double bx = x0 + (sx > 0 ? ex[0] * hw : 0) + (sy > 0 ? ey[0] * hh : 0);
                    double by = y0 + (sx > 0 ? ex[1] * hw : 0) + (sy > 0 ? ey[1] * hh : 0);
                    set4(hk.corners[n], bx + sx * pad * ex[0] + sy * pad * ey[0] + ox,
                         by + sx * pad * ex[1] + sy * pad * ey[1] + oy, 0, 0);
                }
                const double off_x = ox * ex[0] + oy * ex[1], off_y = ox * ey[0] + oy * ey[1];
                set4(hk.sil_map, (hw + 2 * pad) / hw, (hh + 2 * pad) / hh, (-pad - off_x) / hw, (-pad - off_y) / hh);
                set4(hk.sil_radius4, halo.radius / hw, halo.radius / hh, 0, 0);
                set4(hk.sil_color, halo.color[0], halo.color[1], halo.color[2], halo.alpha);
                set4(hk.p6, halo.mode, halo.boost, halo.radius, 0);
                draw(hk);
            }
        }
        draw(k);
    }

    void draw_items(const sc_item *items, int count, ID3D11RenderTargetView *target, int width, int height) {
        const float clear[4] = {0, 0, 0, 0};
        c->ClearRenderTargetView(target, clear);
        ID3D11RenderTargetView *targets[] = {target};
        c->OMSetRenderTargets(1, targets, nullptr);
        D3D11_VIEWPORT vp = {0, 0, static_cast<float>(width), static_cast<float>(height), 0, 1};
        c->RSSetViewports(1, &vp);
        for (int i = 0; i < count; ++i) draw_item(items[i], width, height);
        c->OMSetRenderTargets(0, nullptr, nullptr);  // the target may be sampled next (a sprig)
        ID3D11ShaderResourceView *none[3] = {nullptr, nullptr, nullptr};
        c->PSSetShaderResources(0, 3, none);
    }

    void draw_scene(const sc_scene &scene, ID3D11RenderTargetView *target, int width, int height) {
        draw_items(snap.items.data() + scene.first_item, scene.item_count, target, width, height);
    }

    // Sources drawn from a list whose list changed: drawn again, once, before any canvas uses them.
    int draw_lists() {
        for (auto &[id, d] : dev.comp.drawn) {
            if (!d.dirty) continue;
            Target &t = d.target;
            if (t.width != d.width || t.height != d.height) {
                const int code = make_target(dev.device.Get(), d.width, d.height, t);
                if (code != SC_OK) return code;
            }
            in_drawn = true;
            draw_items(d.items.data(), static_cast<int>(d.items.size()), t.target.Get(), t.width, t.height);
            in_drawn = false;
            d.dirty = false;
            (void)id;
        }
        return SC_OK;
    }

    // Draws every sprig `scene` shows, inner ones first, each once.
    int draw_parts(const sc_scene &scene, std::set<uint64_t> &done, std::set<uint64_t> &open, int depth) {
        if (depth > 16) return SC_OK;  // sprigs inside sprigs this deep: a loop, stop
        for (int i = 0; i < scene.item_count; ++i) {
            const sc_item &it = snap.items[static_cast<size_t>(scene.first_item + i)];
            if (!it.part || done.count(it.part) || open.count(it.part)) continue;
            auto found = snap.by_id.find(it.part);
            if (found == snap.by_id.end()) continue;
            const sc_scene &part = snap.scenes[found->second];
            if (part.width < 1 || part.height < 1) continue;
            open.insert(it.part);
            int code = draw_parts(part, done, open, depth + 1);
            open.erase(it.part);
            if (code != SC_OK) return code;
            Target &t = dev.comp.parts[it.part];
            if (t.width != part.width || t.height != part.height) {
                code = make_target(dev.device.Get(), part.width, part.height, t);
                if (code != SC_OK) return code;
            }
            draw_scene(part, t.target.Get(), t.width, t.height);
            done.insert(it.part);
        }
        return SC_OK;
    }
};

}  // namespace

namespace sc {

int make_target(ID3D11Device *device, int width, int height, Target &out, DXGI_FORMAT format) {
    out = Target();
    D3D11_TEXTURE2D_DESC td = {};
    td.Width = static_cast<UINT>(width);
    td.Height = static_cast<UINT>(height);
    td.MipLevels = 1;
    td.ArraySize = 1;
    td.Format = format;
    td.SampleDesc.Count = 1;
    td.Usage = D3D11_USAGE_DEFAULT;
    td.BindFlags = D3D11_BIND_RENDER_TARGET | D3D11_BIND_SHADER_RESOURCE;
    HRESULT hr = device->CreateTexture2D(&td, nullptr, &out.texture);
    if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "Creating a sprig's texture", hr);
    hr = device->CreateRenderTargetView(out.texture.Get(), nullptr, &out.target);
    if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "CreateRenderTargetView (sprig)", hr);
    hr = device->CreateShaderResourceView(out.texture.Get(), nullptr, &out.view);
    if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "CreateShaderResourceView (sprig)", hr);
    out.width = width;
    out.height = height;
    return SC_OK;
}

}  // namespace sc

int sc::upload_bytes(const sc_upload &how, size_t &bytes) {
    const int fmt = how.format, w = how.width, h = how.height, stride = how.stride;
    if (fmt < SC_FMT_RGBA || fmt > SC_FMT_NV12) return fail(SC_ERR_ARGUMENT, "Upload: unknown format");
    const bool planar = fmt == SC_FMT_I420 || fmt == SC_FMT_I420A || fmt == SC_FMT_NV12;
    const bool three = fmt == SC_FMT_RGB || fmt == SC_FMT_BGR;
    const int bpp = planar ? 1 : (three ? 3 : 4);
    if (w < 1 || h < 1 || w > 16384 || h > 16384 || stride < w * bpp || stride > 16384 * 4)
        return fail(SC_ERR_ARGUMENT, "Upload: bad size or stride");
    if (planar && (w % 2 || h < 3)) return fail(SC_ERR_ARGUMENT, "Upload: bad planar size");
    if (fmt == SC_FMT_NV12 && (h % 3 || (h / 3) % 2)) return fail(SC_ERR_ARGUMENT, "Upload: bad NV12 size");
    bytes = static_cast<size_t>(h - 1) * stride + static_cast<size_t>(w) * bpp;
    return SC_OK;
}

int sc_device::prepare_compositor() {
    if (comp.vs) return SC_OK;
    int ready = prepare_drawing();  // the blend and rasterizer states
    if (ready != SC_OK) return ready;
    ComPtr<ID3DBlob> vs_code, ps_code;
    HRESULT hr = compile(COMPOSITOR_HLSL, sizeof COMPOSITOR_HLSL - 1, "vs_main", "vs_4_0", vs_code);
    if (FAILED(hr)) return fail_hr(SC_ERR_SHADER, "Compiling the compositor's vertex shader", hr);
    hr = compile(COMPOSITOR_HLSL, sizeof COMPOSITOR_HLSL - 1, "ps_main", "ps_4_0", ps_code);
    if (FAILED(hr)) return fail_hr(SC_ERR_SHADER, "Compiling the compositor's pixel shader", hr);
    ComPtr<ID3D11VertexShader> new_vs;
    ComPtr<ID3D11PixelShader> new_ps;
    hr = device->CreateVertexShader(vs_code->GetBufferPointer(), vs_code->GetBufferSize(), nullptr, &new_vs);
    if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "CreateVertexShader (compositor)", hr);
    hr = device->CreatePixelShader(ps_code->GetBufferPointer(), ps_code->GetBufferSize(), nullptr, &new_ps);
    if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "CreatePixelShader (compositor)", hr);
    D3D11_BUFFER_DESC cb = {};
    cb.ByteWidth = sizeof(DrawConstants);
    cb.Usage = D3D11_USAGE_DYNAMIC;
    cb.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
    cb.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
    hr = device->CreateBuffer(&cb, nullptr, &comp.constants);
    if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "CreateBuffer (compositor)", hr);
    D3D11_SAMPLER_DESC sd = {};
    sd.Filter = D3D11_FILTER_MIN_MAG_MIP_LINEAR;
    sd.AddressU = sd.AddressV = sd.AddressW = D3D11_TEXTURE_ADDRESS_WRAP;
    sd.MaxLOD = D3D11_FLOAT32_MAX;
    hr = device->CreateSamplerState(&sd, &comp.wrap);
    if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "CreateSamplerState (wrap)", hr);
    sd.AddressU = sd.AddressV = sd.AddressW = D3D11_TEXTURE_ADDRESS_CLAMP;
    hr = device->CreateSamplerState(&sd, &comp.clamp);
    if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "CreateSamplerState (clamp)", hr);
    comp.ps = new_ps;
    comp.vs = new_vs;  // last: it says the rest is ready
    return SC_OK;
}

int sc::render_scene(sc_device &dev, ID3D11RenderTargetView *target, int width, int height, uint64_t scene_id,
                     double time) {
    int ready = dev.prepare_compositor();
    if (ready != SC_OK) return ready;
    std::shared_ptr<const Snapshot> snap = dev.comp.snapshot;
    ID3D11DeviceContext *c = dev.context.Get();
    if (!snap || !snap->by_id.count(scene_id)) {
        const float clear[4] = {0, 0, 0, 0};
        c->ClearRenderTargetView(target, clear);
        return SC_OK;
    }
    c->OMSetBlendState(dev.over.Get(), nullptr, 0xFFFFFFFF);
    c->RSSetState(dev.raster.Get());
    c->IASetInputLayout(nullptr);
    c->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLESTRIP);
    c->VSSetShader(dev.comp.vs.Get(), nullptr, 0);
    c->PSSetShader(dev.comp.ps.Get(), nullptr, 0);
    ID3D11Buffer *buffers[] = {dev.comp.constants.Get()};
    c->VSSetConstantBuffers(0, 1, buffers);
    c->PSSetConstantBuffers(0, 1, buffers);
    ID3D11SamplerState *samplers[] = {dev.comp.wrap.Get(), dev.comp.clamp.Get()};
    c->PSSetSamplers(0, 2, samplers);

    Renderer r{dev, *snap, time, c};
    const sc_scene &scene = snap->scenes[snap->by_id.at(scene_id)];
    std::set<uint64_t> done, open{scene_id};
    int code = r.draw_lists();
    if (code != SC_OK) return code;
    code = r.draw_parts(scene, done, open, 0);
    if (code != SC_OK) return code;
    r.draw_scene(scene, target, width, height);
    return SC_OK;
}

bool sc::has_scene(sc_device &dev, uint64_t scene) {
    std::shared_ptr<const Snapshot> snap = dev.comp.snapshot;
    return snap && snap->by_id.count(scene);
}

extern "C" {

SC_API int sc_source_upload(sc_device *device, uint64_t id, const uint8_t *data, const sc_upload *how) {
    return device_call(device, "sc_source_upload", [&]() -> int {
        if (!data || !how || !id) return fail(SC_ERR_ARGUMENT, "sc_source_upload: no id, data or description");
        size_t bytes = 0;
        int ok = upload_bytes(*how, bytes);
        if (ok != SC_OK) return ok;
        std::vector<uint8_t> repacked;
        sc_upload as_i420 = *how;
        if (how->format == SC_FMT_NV12) {  // on the card when it can; else repacked here into I420 rows
            bool done = false;
            ok = upload_nv12(*device, id, data, *how, repacked, done);
            if (ok != SC_OK || done) return ok;
            as_i420.format = SC_FMT_I420;
            as_i420.stride = as_i420.width;
            data = repacked.data();
            how = &as_i420;
        }
        const int fmt = how->format, w = how->width, h = how->height, stride = how->stride;
        const bool planar = fmt == SC_FMT_I420 || fmt == SC_FMT_I420A;
        const bool three = fmt == SC_FMT_RGB || fmt == SC_FMT_BGR;

        std::lock_guard<std::mutex> lock(device->lock);
        device->comp.drawn.erase(id);  // a picture again: no longer drawn from a list
        Source &s = device->comp.sources[id];
        const DXGI_FORMAT dxgi = planar ? DXGI_FORMAT_R8_UNORM : DXGI_FORMAT_R8G8B8A8_UNORM;
        if (!s.texture || s.data_w != w || s.data_h != h || (s.planar != 0) != planar) {
            s = Source();
            D3D11_TEXTURE2D_DESC td = {};
            td.Width = static_cast<UINT>(w);
            td.Height = static_cast<UINT>(h);
            td.MipLevels = 1;
            td.ArraySize = 1;
            td.Format = dxgi;
            td.SampleDesc.Count = 1;
            td.Usage = D3D11_USAGE_DEFAULT;
            td.BindFlags = D3D11_BIND_SHADER_RESOURCE;
            HRESULT hr = device->device->CreateTexture2D(&td, nullptr, &s.texture);
            if (FAILED(hr)) {
                device->comp.sources.erase(id);
                return fail_hr(SC_ERR_DEVICE, "Creating a source's texture", hr);
            }
            hr = device->device->CreateShaderResourceView(s.texture.Get(), nullptr, &s.view);
            if (FAILED(hr)) {
                device->comp.sources.erase(id);
                return fail_hr(SC_ERR_DEVICE, "CreateShaderResourceView (source)", hr);
            }
            s.data_w = w;
            s.data_h = h;
        }
        if (three) {  // no 3-byte format on graphics cards: add the opaque alpha here, off Python's lock
            std::vector<uint8_t> rgba(static_cast<size_t>(w) * h * 4);
            for (int y = 0; y < h; ++y) {
                const uint8_t *row = data + static_cast<size_t>(y) * stride;
                uint8_t *dst = rgba.data() + static_cast<size_t>(y) * w * 4;
                for (int x = 0; x < w; ++x) {
                    dst[x * 4] = row[x * 3];
                    dst[x * 4 + 1] = row[x * 3 + 1];
                    dst[x * 4 + 2] = row[x * 3 + 2];
                    dst[x * 4 + 3] = 255;
                }
            }
            device->context->UpdateSubresource(s.texture.Get(), 0, nullptr, rgba.data(), static_cast<UINT>(w * 4), 0);
        } else {
            device->context->UpdateSubresource(s.texture.Get(), 0, nullptr, data, static_cast<UINT>(stride), 0);
        }
        s.format = fmt;
        s.premultiplied = false;
        s.planar = fmt == SC_FMT_I420 ? 1 : (fmt == SC_FMT_I420A ? 2 : 0);
        s.pic_w = w;
        s.pic_h = s.planar == 2 ? h * 2 / 5 : (s.planar == 1 ? h * 2 / 3 : h);
        std::memcpy(s.yuv, how->yuv, sizeof s.yuv);
        const bool layout = how->layout_width > 0 && how->layout_height > 0;
        s.layout_w = layout ? how->layout_width : s.pic_w;
        s.layout_h = layout ? how->layout_height : s.pic_h;
        return SC_OK;
    });
}

SC_API int sc_source_forget(sc_device *device, uint64_t id) {
    return device_call(device, "sc_source_forget", [&]() -> int {
        std::lock_guard<std::mutex> lock(device->lock);
        device->comp.sources.erase(id);
        device->comp.drawn.erase(id);
        return SC_OK;
    });
}

SC_API int sc_source_draws(sc_device *device, uint64_t id, int width, int height, const sc_item *items, int count) {
    return device_call(device, "sc_source_draws", [&]() -> int {
        if (!id) return fail(SC_ERR_ARGUMENT, "sc_source_draws: no id");
        std::lock_guard<std::mutex> lock(device->lock);
        if (count < 0 || width <= 0 || height <= 0) {
            device->comp.drawn.erase(id);
            return SC_OK;
        }
        if (width > 8192 || height > 8192 || count > 4096 || (count > 0 && !items))
            return fail(SC_ERR_ARGUMENT, "sc_source_draws: too big, or no items");
        Drawn &d = device->comp.drawn[id];
        d.width = width;
        d.height = height;
        d.items.assign(items, items + count);
        for (sc_item &it : d.items) {
            it.part = 0;        // pictures only: no sprigs...
            it.effects = -1;    // ...and no snapshot effects (they belong to the snapshot)
        }
        d.dirty = true;
        return SC_OK;
    });
}

SC_API int sc_look_set(sc_device *device, uint64_t id, const float *table, int n) {
    return device_call(device, "sc_look_set", [&]() -> int {
        if (!table || !id || n < 2 || n > 256) return fail(SC_ERR_ARGUMENT, "sc_look_set: bad look");
        const size_t count = static_cast<size_t>(n) * n * n;
        std::vector<float> rgba(count * 4);
        for (size_t i = 0; i < count; ++i) {
            rgba[i * 4] = table[i * 3];
            rgba[i * 4 + 1] = table[i * 3 + 1];
            rgba[i * 4 + 2] = table[i * 3 + 2];
            rgba[i * 4 + 3] = 1.0f;
        }
        D3D11_TEXTURE3D_DESC td = {};
        td.Width = td.Height = td.Depth = static_cast<UINT>(n);
        td.MipLevels = 1;
        td.Format = DXGI_FORMAT_R32G32B32A32_FLOAT;
        td.Usage = D3D11_USAGE_IMMUTABLE;
        td.BindFlags = D3D11_BIND_SHADER_RESOURCE;
        D3D11_SUBRESOURCE_DATA init = {rgba.data(), static_cast<UINT>(n * 16), static_cast<UINT>(n * n * 16)};
        std::lock_guard<std::mutex> lock(device->lock);
        ComPtr<ID3D11Texture3D> texture;
        HRESULT hr = device->device->CreateTexture3D(&td, &init, &texture);
        if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "Creating a look", hr);
        Look look;
        hr = device->device->CreateShaderResourceView(texture.Get(), nullptr, &look.view);
        if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "CreateShaderResourceView (look)", hr);
        look.size = n;
        device->comp.looks[id] = look;
        return SC_OK;
    });
}

SC_API int sc_mask_set(sc_device *device, uint64_t id, const uint8_t *rgba, int width, int height, int luma) {
    return device_call(device, "sc_mask_set", [&]() -> int {
        if (!rgba || !id || width < 1 || height < 1 || width > 4096 || height > 4096)
            return fail(SC_ERR_ARGUMENT, "sc_mask_set: bad picture");
        D3D11_TEXTURE2D_DESC td = {};
        td.Width = static_cast<UINT>(width);
        td.Height = static_cast<UINT>(height);
        td.MipLevels = 1;
        td.ArraySize = 1;
        td.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
        td.SampleDesc.Count = 1;
        td.Usage = D3D11_USAGE_IMMUTABLE;
        td.BindFlags = D3D11_BIND_SHADER_RESOURCE;
        D3D11_SUBRESOURCE_DATA init = {rgba, static_cast<UINT>(width * 4), 0};
        std::lock_guard<std::mutex> lock(device->lock);
        ComPtr<ID3D11Texture2D> texture;
        HRESULT hr = device->device->CreateTexture2D(&td, &init, &texture);
        if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "Creating a shape picture", hr);
        Mask mask;
        hr = device->device->CreateShaderResourceView(texture.Get(), nullptr, &mask.view);
        if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "CreateShaderResourceView (shape)", hr);
        mask.luma = luma != 0;
        device->comp.masks[id] = mask;
        return SC_OK;
    });
}

SC_API int sc_snapshot_set(sc_device *device, const sc_snapshot *snapshot) {
    return device_call(device, "sc_snapshot_set", [&]() -> int {
        if (!snapshot || snapshot->scene_count < 0 || snapshot->item_count < 0 || snapshot->effects_count < 0 ||
            (snapshot->scene_count && !snapshot->scenes) || (snapshot->item_count && !snapshot->items) ||
            (snapshot->effects_count && !snapshot->effects))
            return fail(SC_ERR_ARGUMENT, "sc_snapshot_set: bad snapshot");
        auto snap = std::make_shared<Snapshot>();
        snap->scenes.assign(snapshot->scenes, snapshot->scenes + snapshot->scene_count);
        snap->items.assign(snapshot->items, snapshot->items + snapshot->item_count);
        snap->effects.assign(snapshot->effects, snapshot->effects + snapshot->effects_count);
        for (size_t i = 0; i < snap->scenes.size(); ++i) {
            const sc_scene &scene = snap->scenes[i];
            if (scene.first_item < 0 || scene.item_count < 0 ||
                static_cast<int64_t>(scene.first_item) + scene.item_count > snapshot->item_count)
                return fail(SC_ERR_ARGUMENT, "sc_snapshot_set: a scene's items are outside the item list");
            snap->by_id[scene.id] = i;
        }
        for (auto &fx : snap->effects) fx.halo_count = std::clamp(fx.halo_count, 0, SC_MAX_HALOS);
        std::lock_guard<std::mutex> lock(device->lock);
        device->comp.snapshot = std::move(snap);
        // Sprigs no scene uses any more let go of their textures.
        for (auto it = device->comp.parts.begin(); it != device->comp.parts.end();) {
            if (device->comp.snapshot->by_id.count(it->first)) ++it;
            else it = device->comp.parts.erase(it);
        }
        return SC_OK;
    });
}

SC_API int sc_canvas_render(sc_canvas *canvas, uint64_t scene_id, double time) {
    return device_call(canvas ? canvas->device : nullptr, "sc_canvas_render", [&]() -> int {
        if (!canvas) return fail(SC_ERR_ARGUMENT, "sc_canvas_render: no canvas");
        std::lock_guard<std::mutex> lock(canvas->device->lock);
        if (canvas->outgoing.texture && time - canvas->transition_used > 30.0) {
            canvas->outgoing = Target();  // no transition for a while: let go of its two pictures
            canvas->incoming = Target();
        }
        return render_scene(*canvas->device, canvas->target.Get(), canvas->width, canvas->height, scene_id, time);
    });
}

SC_API int sc_canvas_render_transition(sc_canvas *canvas, uint64_t from, uint64_t to, double time,
                                       const sc_transition *how, float progress) {
    return device_call(canvas ? canvas->device : nullptr, "sc_canvas_render_transition", [&]() -> int {
        if (!canvas || !how || how->mode < SC_TRANSITION_FADE || how->mode > SC_TRANSITION_BLOOM)
            return fail(SC_ERR_ARGUMENT, "sc_canvas_render_transition: no canvas, or an unknown kind");
        std::lock_guard<std::mutex> lock(canvas->device->lock);
        return render_transition(*canvas, from, to, time, *how, progress);
    });
}

}  // extern "C"
