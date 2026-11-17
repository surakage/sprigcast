// The engine's own frame loop (phase 2, docs/native-engine.md): a frame thread paced by a high-resolution timer
// draws every canvas and shows it in preview windows, and a watchdog thread keeps an eye on it. Python hands work
// over through the sc_engine_* calls, which only copy it into a queue: the frame thread takes the queue at the
// start of a frame, so neither side ever waits on the other's slow work.
//
// Crash gates: a fault inside a frame, or a lost graphics card, ends that device, not the engine. The engine
// builds a new device and gives it everything again from its own copies. Five rebuilds within a minute and it
// stops trying and says so (faulted), and Python switches to its own engine.
#include "internal.h"

#include <mmsystem.h>

#include <algorithm>
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <cstring>
#include <deque>
#include <iterator>
#include <set>
#include <stdexcept>

#ifndef CREATE_WAITABLE_TIMER_HIGH_RESOLUTION
#  define CREATE_WAITABLE_TIMER_HIGH_RESOLUTION 0x00000002
#endif

using namespace sc;

namespace {

constexpr double HANG_S = 2.0;           // one frame this long and the watchdog reports the engine hung
constexpr int REBUILDS_PER_MINUTE = 5;   // more than this and the engine gives up (faulted)
constexpr int BUILD_TRIES = 10;          // a new device that can't be made after this many tries: faulted
constexpr int WAIT_MS = 2000;            // how long Python waits for the frame thread before giving up

int64_t qpc() {
    LARGE_INTEGER v;
    QueryPerformanceCounter(&v);
    return v.QuadPart;
}

int64_t qpf() {
    static const int64_t f = [] {
        LARGE_INTEGER v;
        QueryPerformanceFrequency(&v);
        return v.QuadPart;
    }();
    return f;
}

// The device is gone or broken: the frame stops here and the engine rebuilds.
struct Gone : std::runtime_error {
    using std::runtime_error::runtime_error;
};

struct Pic {
    std::vector<uint8_t> data;
    sc_upload how{};
    bool keep = false;
};

struct LookData {
    std::vector<float> table;
    int n = 0;
};

struct MaskData {
    std::vector<uint8_t> rgba;
    int width = 0, height = 0;
    bool luma = false;
};

struct SnapData {
    std::vector<sc_scene> scenes;
    std::vector<sc_item> items;
    std::vector<sc_effects> effects;
};

struct CanvasCfg {
    int width = 0, height = 0;
    uint64_t scene = 0;
};

struct PreviewCfg {
    int id = 0;
    int canvas = 0;
    HWND hwnd = nullptr;
    int max_fps = 0;
    float background[4] = {0, 0, 0, 1};
};

struct PreviewGpu {
    ComPtr<IDXGISwapChain1> chain;
    ComPtr<ID3D11RenderTargetView> target;
    int width = 0, height = 0;
    int64_t last = 0;      // when it was last shown
    int64_t retry_at = 0;  // after a failure: when to try making it again
};

void name_thread(const wchar_t *name) {
    using Describe = HRESULT(WINAPI *)(HANDLE, PCWSTR);
    FARPROC found = GetProcAddress(GetModuleHandleW(L"kernel32.dll"), "SetThreadDescription");  // Windows 10 1607+
    if (found) reinterpret_cast<Describe>(reinterpret_cast<void (*)()>(found))(GetCurrentThread(), name);
}

}  // namespace

struct sc_engine {
    int adapter = -1, flags = 0;
    int64_t born = 0;

    // ---- shared with Python's threads: guarded by `state` ----
    std::mutex state;
    std::condition_variable changed;  // a frame finished, or a preview was let go
    int rate_num = 60, rate_den = 1;
    bool rate_changed = true;
    CanvasCfg canvases[SC_ENGINE_CANVASES];
    std::unordered_map<uint64_t, Pic> uploads;                  // the newest picture per source, not yet sent
    std::unordered_map<uint64_t, std::vector<uint8_t>> spare;   // buffers to reuse for the next picture
    std::set<uint64_t> forgets;
    std::unordered_map<uint64_t, LookData> looks;
    std::unordered_map<uint64_t, MaskData> masks;
    std::shared_ptr<const SnapData> snapshot;
    bool snapshot_new = false;
    std::vector<PreviewCfg> previews;
    int next_preview = 1;
    int fault = 0;
    uint64_t detach_asked = 0, detach_done = 0;
    sc_engine_stats stats{};

    // ---- the frame thread's own (canvas reads borrow them under `frame`) ----
    std::mutex frame;
    sc_device *dev = nullptr;
    sc_canvas *gpu[SC_ENGINE_CANVASES] = {};
    std::unordered_map<int, PreviewGpu> shown;
    std::unordered_map<uint64_t, Pic> kept_sources;
    std::unordered_map<uint64_t, LookData> kept_looks;
    std::unordered_map<uint64_t, MaskData> kept_masks;
    std::shared_ptr<const SnapData> kept_snapshot;
    std::deque<int64_t> rebuild_times;
    bool lost = false;
    int build_failures = 0;
    int64_t next_build = 0;

    // ---- threads ----
    HANDLE quit = nullptr;  // manual reset: everything stops
    HANDLE wake = nullptr;  // auto reset: something changed, look again now
    HANDLE threads[2] = {nullptr, nullptr};
    std::atomic<bool> stop{false};
    std::atomic<bool> dead{false};          // faulted for good
    std::atomic<int64_t> tick_started{0};   // 0 = between frames
    std::atomic<bool> hung{false};
};

namespace {

sc_snapshot view_of(const SnapData &s) {
    sc_snapshot v{};
    v.scenes = s.scenes.data();
    v.scene_count = static_cast<int>(s.scenes.size());
    v.items = s.items.data();
    v.item_count = static_cast<int>(s.items.size());
    v.effects = s.effects.data();
    v.effects_count = static_cast<int>(s.effects.size());
    return v;
}

// An engine call's answer, from the frame thread: a bad value was already logged and the frame goes on; a
// faulted or lost device ends the frame and the engine rebuilds.
void check(int code) {
    switch (code) {
    case SC_OK:
    case SC_ERR_ARGUMENT:
    case SC_ERR_DEVICE:          // one thing couldn't be made (too big, out of video memory): the rest goes on
    case SC_ERR_OUT_OF_MEMORY:
        return;
    default:
        throw Gone(sc_last_error());
    }
}

void drop_gpu(sc_engine &e) {
    e.shown.clear();
    for (auto &c : e.gpu) {
        sc_canvas_destroy(c);
        c = nullptr;
    }
    if (e.dev) {
        if (e.dev->context) {
            std::lock_guard<std::mutex> lock(e.dev->lock);
            e.dev->context->ClearState();
            e.dev->context->Flush();
        }
        sc_device_destroy(e.dev);
        e.dev = nullptr;
    }
}

void give_up(sc_engine &e, const std::string &why) {
    drop_gpu(e);
    e.kept_sources.clear();
    e.kept_looks.clear();
    e.kept_masks.clear();
    e.kept_snapshot.reset();
    e.dead.store(true);
    {
        std::lock_guard<std::mutex> lock(e.state);
        e.stats.faulted = 1;
        e.uploads.clear();
        e.spare.clear();
        e.looks.clear();
        e.masks.clear();
    }
    log_line(SC_LOG_ERROR, "engine: " + why + "; the engine has stopped (SprigCast's own engine takes over)");
    e.changed.notify_all();
}

// After a fault or a lost card: let go of the device. The next frame builds a new one.
void teardown(sc_engine &e, const std::string &why) {
    log_line(SC_LOG_ERROR, "engine: " + why + "; rebuilding the device");
    drop_gpu(e);
    e.lost = true;
    const int64_t now = qpc();
    e.rebuild_times.push_back(now);
    while (!e.rebuild_times.empty() && now - e.rebuild_times.front() > 60 * qpf()) e.rebuild_times.pop_front();
    if (static_cast<int>(e.rebuild_times.size()) > REBUILDS_PER_MINUTE)
        give_up(e, "more than " + std::to_string(REBUILDS_PER_MINUTE) + " rebuilds in a minute");
}

// Makes the device (again). After a loss, gives it back everything the engine kept.
bool build(sc_engine &e) {
    const int64_t now = qpc();
    if (now < e.next_build) return false;
    sc_device *made = nullptr;
    int code = sc_device_create(e.adapter, e.flags, &made);
    if (code != SC_OK) {
        ++e.build_failures;
        if (e.build_failures >= BUILD_TRIES) {
            give_up(e, "no graphics device after " + std::to_string(BUILD_TRIES) + " tries");
            return false;
        }
        e.next_build = now + qpf() * std::min(e.build_failures, 5);  // 1 s, 2 s ... 5 s apart
        return false;
    }
    e.dev = made;
    e.build_failures = 0;
    if (e.lost) {
        e.lost = false;
        for (auto &[id, pic] : e.kept_sources) check(sc_source_upload(e.dev, id, pic.data.data(), &pic.how));
        for (auto &[id, look] : e.kept_looks) check(sc_look_set(e.dev, id, look.table.data(), look.n));
        for (auto &[id, m] : e.kept_masks)
            check(sc_mask_set(e.dev, id, m.rgba.data(), m.width, m.height, m.luma ? 1 : 0));
        if (e.kept_snapshot) {
            sc_snapshot v = view_of(*e.kept_snapshot);
            check(sc_snapshot_set(e.dev, &v));
        }
        int rebuilds;
        {
            std::lock_guard<std::mutex> lock(e.state);
            rebuilds = ++e.stats.rebuilds;
        }
        log_line(SC_LOG_WARNING, "engine: the device is rebuilt (rebuild " + std::to_string(rebuilds) +
                                     "); kept pictures, looks, shapes and the snapshot are back");
    }
    return true;
}

HRESULT make_chain(sc_device &dev, HWND hwnd, ComPtr<IDXGISwapChain1> &out) {
    ComPtr<IDXGIDevice> dxgi;
    HRESULT hr = dev.device.As(&dxgi);
    if (FAILED(hr)) return hr;
    ComPtr<IDXGIAdapter> adapter;
    hr = dxgi->GetAdapter(&adapter);
    if (FAILED(hr)) return hr;
    ComPtr<IDXGIFactory2> factory;
    hr = adapter->GetParent(__uuidof(IDXGIFactory2), reinterpret_cast<void **>(factory.GetAddressOf()));
    if (FAILED(hr)) return hr;
    DXGI_SWAP_CHAIN_DESC1 desc = {};
    desc.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
    desc.SampleDesc.Count = 1;
    desc.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT;
    desc.BufferCount = 2;
    desc.Scaling = DXGI_SCALING_STRETCH;
    desc.AlphaMode = DXGI_ALPHA_MODE_IGNORE;
    // Flip model where Windows has it (10: discard, 8: sequential), else the old copy model.
    const DXGI_SWAP_EFFECT effects[] = {DXGI_SWAP_EFFECT_FLIP_DISCARD, DXGI_SWAP_EFFECT_FLIP_SEQUENTIAL,
                                        DXGI_SWAP_EFFECT_DISCARD};
    for (DXGI_SWAP_EFFECT effect : effects) {
        desc.SwapEffect = effect;
        out.Reset();
        hr = factory->CreateSwapChainForHwnd(dev.device.Get(), hwnd, &desc, nullptr, nullptr, &out);
        if (SUCCEEDED(hr)) break;
    }
    if (FAILED(hr)) return hr;
    factory->MakeWindowAssociation(hwnd, DXGI_MWA_NO_ALT_ENTER | DXGI_MWA_NO_WINDOW_CHANGES);
    return S_OK;
}

bool is_lost(HRESULT hr) {
    return hr == DXGI_ERROR_DEVICE_REMOVED || hr == DXGI_ERROR_DEVICE_RESET || hr == DXGI_ERROR_DEVICE_HUNG;
}

// One preview window: fit the canvas inside it and show it, never waiting on the window.
void show_preview(sc_engine &e, const PreviewCfg &cfg, PreviewGpu &pg, int64_t now, uint64_t &presented,
                  uint64_t &skipped) {
    if (now < pg.retry_at) return;
    if (!IsWindow(cfg.hwnd)) {
        pg = PreviewGpu();
        pg.retry_at = now + qpf();
        return;
    }
    if (cfg.max_fps > 0 && pg.last && (now - pg.last) < qpf() * 9 / (10 * cfg.max_fps)) return;
    RECT rc;
    if (!GetClientRect(cfg.hwnd, &rc)) return;
    const int w = rc.right - rc.left, h = rc.bottom - rc.top;
    if (w <= 0 || h <= 0) return;  // minimized or hidden away
    sc_device &dev = *e.dev;
    std::lock_guard<std::mutex> lock(dev.lock);
    HRESULT hr = S_OK;
    const char *what = "";
    if (!pg.chain) {
        hr = make_chain(dev, cfg.hwnd, pg.chain);
        what = "Making the preview";
        pg.width = pg.height = 0;
    }
    if (SUCCEEDED(hr) && (pg.width != w || pg.height != h)) {
        pg.target.Reset();
        dev.context->ClearState();
        hr = pg.chain->ResizeBuffers(0, static_cast<UINT>(w), static_cast<UINT>(h), DXGI_FORMAT_UNKNOWN, 0);
        what = "Resizing the preview";
        if (SUCCEEDED(hr)) {
            pg.width = w;
            pg.height = h;
        }
    }
    if (SUCCEEDED(hr) && !pg.target) {
        ComPtr<ID3D11Texture2D> back;
        hr = pg.chain->GetBuffer(0, __uuidof(ID3D11Texture2D), reinterpret_cast<void **>(back.GetAddressOf()));
        if (SUCCEEDED(hr)) hr = dev.device->CreateRenderTargetView(back.Get(), nullptr, &pg.target);
        what = "The preview's picture";
    }
    if (SUCCEEDED(hr)) {
        dev.context->ClearRenderTargetView(pg.target.Get(), cfg.background);
        const sc_canvas *canvas = (cfg.canvas >= 0 && cfg.canvas < SC_ENGINE_CANVASES) ? e.gpu[cfg.canvas] : nullptr;
        if (canvas) {
            const float fit = std::min(static_cast<float>(w) / canvas->width, static_cast<float>(h) / canvas->height);
            const float dw = canvas->width * fit, dh = canvas->height * fit;
            check(draw_view(dev, pg.target.Get(), w, h, canvas->view.Get(), (w - dw) / 2, (h - dh) / 2, dw, dh,
                            1.0f, false));
        }
        hr = pg.chain->Present(0, DXGI_PRESENT_DO_NOT_WAIT);
        what = "Showing the preview";
        if (hr == DXGI_ERROR_WAS_STILL_DRAWING) {
            ++skipped;
            return;
        }
    }
    if (is_lost(hr)) throw Gone("the graphics card was reset or removed (preview)");
    if (FAILED(hr)) {
        fail_hr(SC_ERR_DEVICE, what, hr);
        pg = PreviewGpu();
        pg.retry_at = now + qpf();  // try again in a second
        return;
    }
    pg.last = now;
    ++presented;
}

// One frame: take Python's work, send it to the card, draw every canvas, show the previews.
void draw_frame(sc_engine &e, double seconds, int64_t now, uint64_t &presented, uint64_t &skipped) {
    int fault;
    {
        std::lock_guard<std::mutex> lock(e.state);
        fault = e.fault;
        e.fault = 0;
    }
    if (fault == 1) throw std::runtime_error("a deliberate fault (crash gate check)");
    if (fault == 2) {
#ifdef _MSC_VER
        volatile int *nowhere = nullptr;
        *nowhere = 1;  // /EHa turns the access violation into something catch (...) stops
#endif
        throw std::runtime_error("a deliberate fault (crash gate check)");
    }
    if (fault == 3) throw Gone("a pretend lost graphics card (crash gate check)");
    if (fault == 4) Sleep(3000);  // a stuck frame: the watchdog reports it

    if (!e.dev && !build(e)) {
        std::lock_guard<std::mutex> lock(e.state);
        e.detach_done = e.detach_asked;  // no device, no windows held
        return;
    }

    CanvasCfg canvases[SC_ENGINE_CANVASES];
    std::unordered_map<uint64_t, Pic> uploads;
    std::set<uint64_t> forgets;
    std::unordered_map<uint64_t, LookData> looks;
    std::unordered_map<uint64_t, MaskData> masks;
    std::shared_ptr<const SnapData> snapshot;
    std::vector<PreviewCfg> previews;
    uint64_t detach_seen;
    {
        std::lock_guard<std::mutex> lock(e.state);
        std::copy(std::begin(e.canvases), std::end(e.canvases), canvases);
        uploads.swap(e.uploads);
        forgets.swap(e.forgets);
        looks.swap(e.looks);
        masks.swap(e.masks);
        if (e.snapshot_new) snapshot = e.snapshot;
        e.snapshot_new = false;
        previews = e.previews;
        detach_seen = e.detach_asked;
    }

    for (uint64_t id : forgets) {
        e.kept_sources.erase(id);
        check(sc_source_forget(e.dev, id));
    }
    std::vector<std::pair<uint64_t, std::vector<uint8_t>>> used;
    for (auto &[id, pic] : uploads) {
        check(sc_source_upload(e.dev, id, pic.data.data(), &pic.how));
        if (pic.keep) {
            e.kept_sources[id] = std::move(pic);
        } else {
            e.kept_sources.erase(id);
            used.emplace_back(id, std::move(pic.data));
        }
    }
    for (auto &[id, look] : looks) {
        check(sc_look_set(e.dev, id, look.table.data(), look.n));
        e.kept_looks[id] = std::move(look);
    }
    for (auto &[id, m] : masks) {
        check(sc_mask_set(e.dev, id, m.rgba.data(), m.width, m.height, m.luma ? 1 : 0));
        e.kept_masks[id] = std::move(m);
    }
    if (snapshot) {
        sc_snapshot v = view_of(*snapshot);
        check(sc_snapshot_set(e.dev, &v));
        e.kept_snapshot = snapshot;
    }

    for (int i = 0; i < SC_ENGINE_CANVASES; ++i) {
        const CanvasCfg &cfg = canvases[i];
        sc_canvas *&c = e.gpu[i];
        if (c && (c->width != cfg.width || c->height != cfg.height)) {
            sc_canvas_destroy(c);
            c = nullptr;
        }
        if (cfg.width <= 0) continue;
        if (!c) {
            int code = sc_canvas_create(e.dev, cfg.width, cfg.height, &c);
            check(code);
            if (code != SC_OK) continue;
        }
        if (cfg.scene) check(sc_canvas_render(c, cfg.scene, seconds));
        else check(sc_canvas_clear(c, 0, 0, 0, 0));
    }

    for (auto it = e.shown.begin(); it != e.shown.end();) {
        const bool wanted = std::any_of(previews.begin(), previews.end(),
                                        [&](const PreviewCfg &p) { return p.id == it->first; });
        it = wanted ? std::next(it) : e.shown.erase(it);
    }
    for (const PreviewCfg &cfg : previews) show_preview(e, cfg, e.shown[cfg.id], now, presented, skipped);
    if (detach_seen) {  // let the window go for good before telling Python it can close it
        std::lock_guard<std::mutex> lock(e.dev->lock);
        e.dev->context->ClearState();
        e.dev->context->Flush();
    }

    std::lock_guard<std::mutex> lock(e.state);
    e.detach_done = detach_seen;
    for (auto &[id, buffer] : used) {
        if (!e.uploads.count(id) && !e.forgets.count(id)) e.spare[id] = std::move(buffer);
    }
}

void tick(sc_engine &e) {
    std::lock_guard<std::mutex> frame(e.frame);
    const int64_t start = qpc();
    e.tick_started.store(start);
    const double seconds = static_cast<double>(start - e.born) / static_cast<double>(qpf());
    uint64_t presented = 0, skipped = 0;
    bool drew = false;
    if (!e.dead.load()) {
        std::string why;
        try {
            draw_frame(e, seconds, start, presented, skipped);
            drew = e.dev != nullptr;
        } catch (const Gone &g) {
            why = g.what();
        } catch (const std::exception &x) {
            why = std::string("a fault while drawing: ") + x.what();
        } catch (...) {
            why = "a fault while drawing";
        }
        if (!why.empty()) {
            try {
                teardown(e, why);
            } catch (...) {
                give_up(e, "a fault while rebuilding");
            }
        }
    } else {
        std::lock_guard<std::mutex> lock(e.state);
        e.detach_done = e.detach_asked;
    }
    const int64_t end = qpc();
    e.tick_started.store(0);
    if (e.hung.exchange(false)) {
        char line[160];
        std::snprintf(line, sizeof line, "engine: the frame thread is going again (that frame took %.1f s)",
                      static_cast<double>(end - start) / static_cast<double>(qpf()));
        log_line(SC_LOG_WARNING, line);
    }
    int sources = 0;
    if (e.dev) {
        std::lock_guard<std::mutex> lock(e.dev->lock);
        sources = static_cast<int>(e.dev->comp.sources.size());
    }
    {
        std::lock_guard<std::mutex> lock(e.state);
        sc_engine_stats &s = e.stats;
        const double ms = static_cast<double>(end - start) * 1000.0 / static_cast<double>(qpf());
        if (drew) {
            ++s.ticks;
            s.render_ms_last = ms;
            s.render_ms_avg = s.ticks == 1 ? ms : s.render_ms_avg + (ms - s.render_ms_avg) * 0.05;
            s.render_ms_max = std::max(s.render_ms_max, ms);
        }
        s.presented += presented;
        s.preview_skips += skipped;
        s.canvases = static_cast<int>(std::count_if(std::begin(e.gpu), std::end(e.gpu), [](sc_canvas *c) { return c != nullptr; }));
        s.previews = static_cast<int>(e.shown.size());
        s.sources = sources;
    }
    e.changed.notify_all();
}

// Nothing to draw and nothing to hand over: the frame thread sleeps instead of waking 60 times a second.
bool idle(sc_engine &e) {
    if (e.dead.load()) return true;
    if (e.dev && (!e.shown.empty() || std::any_of(std::begin(e.gpu), std::end(e.gpu), [](sc_canvas *c) { return c != nullptr; })))
        return false;
    std::lock_guard<std::mutex> lock(e.state);
    if (std::any_of(std::begin(e.canvases), std::end(e.canvases), [](const CanvasCfg &c) { return c.width > 0; }))
        return false;
    return e.previews.empty() && e.uploads.empty() && e.forgets.empty() && e.looks.empty() && e.masks.empty() &&
           !e.snapshot_new && !e.fault && e.detach_done == e.detach_asked;
}

// Frame n's time from the start, in performance-counter ticks, without overflowing for months.
int64_t frame_due(int64_t start, int64_t n, int num, int den) {
    const int64_t f = qpf();
    return start + (n / num) * den * f + ((n % num) * den * f) / num;
}

int64_t frame_at(int64_t start, int64_t now, int num, int den) {
    const int64_t span = static_cast<int64_t>(den) * qpf(), elapsed = now - start;
    return (elapsed / span) * num + ((elapsed % span) * num) / span;
}

DWORD WINAPI frame_main(void *arg) {
    sc_engine &e = *static_cast<sc_engine *>(arg);
    try {
        name_thread(L"SprigCast engine frames");
        SetThreadPriority(GetCurrentThread(), THREAD_PRIORITY_ABOVE_NORMAL);
        HANDLE timer = CreateWaitableTimerExW(nullptr, nullptr, CREATE_WAITABLE_TIMER_HIGH_RESOLUTION, TIMER_ALL_ACCESS);
        bool coarse = false;
        if (!timer) {  // before Windows 10 1803: an ordinary timer with the system tick at 1 ms
            timer = CreateWaitableTimerExW(nullptr, nullptr, 0, TIMER_ALL_ACCESS);
            timeBeginPeriod(1);
            coarse = true;
        }
        {
            std::lock_guard<std::mutex> lock(e.state);
            e.stats.running = 1;
        }
        int num = 60, den = 1;
        int64_t start = qpc(), n = 0;
        bool restart = true;
        while (!e.stop.load()) {
            {
                std::lock_guard<std::mutex> lock(e.state);
                if (e.rate_changed) {
                    num = e.rate_num;
                    den = e.rate_den;
                    e.rate_changed = false;
                    restart = true;
                }
            }
            if (idle(e)) {
                HANDLE wait[2] = {e.quit, e.wake};
                WaitForMultipleObjects(2, wait, FALSE, 500);
                restart = true;
                continue;
            }
            if (restart) {
                start = qpc();
                n = 0;
                restart = false;
            }
            const int64_t due = frame_due(start, n, num, den);
            int64_t now = qpc();
            if (due > now) {
                LARGE_INTEGER rel;
                rel.QuadPart = -std::max<int64_t>(1, (due - now) * 10000000 / qpf());  // 100 ns steps, relative
                SetWaitableTimer(timer, &rel, 0, nullptr, nullptr, FALSE);
                HANDLE wait[3] = {e.quit, e.wake, timer};
                DWORD woke = WaitForMultipleObjects(timer ? 3 : 2, wait, FALSE, timer ? INFINITE : 1);
                if (woke == WAIT_OBJECT_0 || woke == WAIT_OBJECT_0 + 1) continue;  // stop, or a change: look again
                if (!timer) continue;  // no timer at all: 1 ms naps until the frame is due
            }
            tick(e);
            now = qpc();
            const int64_t at = frame_at(start, now, num, den);
            if (at > n) {  // the frame ran into the next frame's time: skip those, never queue them up
                std::lock_guard<std::mutex> lock(e.state);
                e.stats.late += static_cast<uint64_t>(at - n);
                n = at + 1;
            } else {
                ++n;
            }
        }
        if (timer) CloseHandle(timer);
        if (coarse) timeEndPeriod(1);
    } catch (...) {
        log_line(SC_LOG_ERROR, "engine: the frame thread failed outside a frame");
        e.dead.store(true);
    }
    {
        std::lock_guard<std::mutex> lock(e.state);
        e.stats.running = 0;
    }
    e.changed.notify_all();
    return 0;
}

DWORD WINAPI watchdog_main(void *arg) {
    sc_engine &e = *static_cast<sc_engine *>(arg);
    name_thread(L"SprigCast engine watchdog");
    while (WaitForSingleObject(e.quit, 250) == WAIT_TIMEOUT) {
        const int64_t started = e.tick_started.load();
        if (started && static_cast<double>(qpc() - started) > HANG_S * static_cast<double>(qpf()) &&
            !e.hung.exchange(true)) {
            log_line(SC_LOG_ERROR, "engine: one frame has taken more than 2 s (the frame thread is stuck)");
        }
    }
    return 0;
}

template <typename F>
int engine_call(sc_engine *e, const char *name, F &&body) {
    if (!e) return fail(SC_ERR_ARGUMENT, std::string(name) + ": no engine");
    if (e->dead.load()) return fail(SC_ERR_FAULTED, std::string(name) + ": the engine stopped after repeated faults");
    return guarded(name, body);
}

void wake(sc_engine *e) { SetEvent(e->wake); }

}  // namespace

extern "C" {

SC_API int sc_engine_create(int adapter, int flags, sc_engine **out) {
    return guarded("sc_engine_create", [&]() -> int {
        if (!out) return fail(SC_ERR_ARGUMENT, "sc_engine_create: no output");
        *out = nullptr;
        auto e = std::make_unique<sc_engine>();
        e->adapter = adapter;
        e->flags = flags;
        e->born = qpc();
        int code = sc_device_create(adapter, flags, &e->dev);  // made here so a PC without one hears it now
        if (code != SC_OK) return code;
        e->quit = CreateEventW(nullptr, TRUE, FALSE, nullptr);
        e->wake = CreateEventW(nullptr, FALSE, FALSE, nullptr);
        if (!e->quit || !e->wake) {
            if (e->quit) CloseHandle(e->quit);
            if (e->wake) CloseHandle(e->wake);
            sc_device_destroy(e->dev);
            return fail(SC_ERR_INTERNAL, "sc_engine_create: no events");
        }
        e->threads[0] = CreateThread(nullptr, 0, frame_main, e.get(), 0, nullptr);
        e->threads[1] = e->threads[0] ? CreateThread(nullptr, 0, watchdog_main, e.get(), 0, nullptr) : nullptr;
        if (!e->threads[1]) {
            e->stop.store(true);
            SetEvent(e->quit);
            if (e->threads[0]) {
                WaitForSingleObject(e->threads[0], INFINITE);
                CloseHandle(e->threads[0]);
            }
            drop_gpu(*e);
            CloseHandle(e->quit);
            CloseHandle(e->wake);
            return fail(SC_ERR_INTERNAL, "sc_engine_create: couldn't start its threads");
        }
        log_line(SC_LOG_INFO, "engine: started");
        *out = e.release();
        return SC_OK;
    });
}

SC_API void sc_engine_destroy(sc_engine *engine) {
    if (!engine) return;
    try {
        engine->stop.store(true);
        SetEvent(engine->quit);
        engine->changed.notify_all();
        if (WaitForSingleObject(engine->threads[0], 5000) != WAIT_OBJECT_0) {
            // Stuck inside a driver: waiting would freeze SprigCast too. Leave the thread and what it uses.
            log_line(SC_LOG_ERROR, "engine: the frame thread didn't stop in 5 s; it is left behind");
            return;
        }
        WaitForSingleObject(engine->threads[1], 2000);
        for (HANDLE t : engine->threads) CloseHandle(t);
        drop_gpu(*engine);
        CloseHandle(engine->quit);
        CloseHandle(engine->wake);
        delete engine;
        log_line(SC_LOG_INFO, "engine: stopped");
    } catch (...) {
        log_line(SC_LOG_ERROR, "engine: a fault while stopping");
    }
}

SC_API int sc_engine_set_rate(sc_engine *engine, int fps_num, int fps_den) {
    return engine_call(engine, "sc_engine_set_rate", [&]() -> int {
        if (fps_num < 1 || fps_den < 1 || fps_num > 240 * fps_den || fps_num < fps_den)
            return fail(SC_ERR_ARGUMENT, "sc_engine_set_rate: 1 to 240 frames a second");
        {
            std::lock_guard<std::mutex> lock(engine->state);
            engine->rate_num = fps_num;
            engine->rate_den = fps_den;
            engine->rate_changed = true;
        }
        wake(engine);
        return SC_OK;
    });
}

SC_API int sc_engine_canvas_set(sc_engine *engine, int index, int width, int height) {
    return engine_call(engine, "sc_engine_canvas_set", [&]() -> int {
        if (index < 0 || index >= SC_ENGINE_CANVASES) return fail(SC_ERR_ARGUMENT, "sc_engine_canvas_set: no such slot");
        const bool off = width == 0 && height == 0;
        if (!off && (width < 1 || height < 1 || width > 16384 || height > 16384))
            return fail(SC_ERR_ARGUMENT, "sc_engine_canvas_set: size must be 1 to 16384 each way");
        {
            std::lock_guard<std::mutex> lock(engine->state);
            engine->canvases[index].width = width;
            engine->canvases[index].height = height;
            if (off) engine->canvases[index].scene = 0;
        }
        wake(engine);
        return SC_OK;
    });
}

SC_API int sc_engine_canvas_show(sc_engine *engine, int index, uint64_t scene) {
    return engine_call(engine, "sc_engine_canvas_show", [&]() -> int {
        if (index < 0 || index >= SC_ENGINE_CANVASES) return fail(SC_ERR_ARGUMENT, "sc_engine_canvas_show: no such slot");
        std::lock_guard<std::mutex> lock(engine->state);
        engine->canvases[index].scene = scene;
        return SC_OK;
    });
}

SC_API int sc_engine_source_upload(sc_engine *engine, uint64_t id, const uint8_t *data, const sc_upload *how,
                                   int flags) {
    return engine_call(engine, "sc_engine_source_upload", [&]() -> int {
        if (!data || !how || !id) return fail(SC_ERR_ARGUMENT, "sc_engine_source_upload: no id, data or description");
        size_t bytes = 0;
        int ok = upload_bytes(*how, bytes);
        if (ok != SC_OK) return ok;
        std::vector<uint8_t> buffer;
        {
            std::lock_guard<std::mutex> lock(engine->state);
            auto found = engine->spare.find(id);
            if (found != engine->spare.end()) {
                buffer.swap(found->second);
                engine->spare.erase(found);
            }
        }
        buffer.resize(bytes);
        std::memcpy(buffer.data(), data, bytes);  // outside the lock: the frame thread never waits on this copy
        std::lock_guard<std::mutex> lock(engine->state);
        Pic &pic = engine->uploads[id];
        std::swap(pic.data, buffer);
        if (buffer.capacity()) engine->spare[id] = std::move(buffer);  // a picture the frame thread never took
        pic.how = *how;
        pic.keep = (flags & SC_UPLOAD_KEEP) != 0;
        engine->forgets.erase(id);
        return SC_OK;
    });
}

SC_API int sc_engine_source_forget(sc_engine *engine, uint64_t id) {
    return engine_call(engine, "sc_engine_source_forget", [&]() -> int {
        std::lock_guard<std::mutex> lock(engine->state);
        engine->uploads.erase(id);
        engine->spare.erase(id);
        engine->forgets.insert(id);
        return SC_OK;
    });
}

SC_API int sc_engine_look_set(sc_engine *engine, uint64_t id, const float *table, int n) {
    return engine_call(engine, "sc_engine_look_set", [&]() -> int {
        if (!table || !id || n < 2 || n > 256) return fail(SC_ERR_ARGUMENT, "sc_engine_look_set: bad look");
        LookData look;
        look.n = n;
        look.table.assign(table, table + static_cast<size_t>(n) * n * n * 3);
        std::lock_guard<std::mutex> lock(engine->state);
        engine->looks[id] = std::move(look);
        return SC_OK;
    });
}

SC_API int sc_engine_mask_set(sc_engine *engine, uint64_t id, const uint8_t *rgba, int width, int height, int luma) {
    return engine_call(engine, "sc_engine_mask_set", [&]() -> int {
        if (!rgba || !id || width < 1 || height < 1 || width > 4096 || height > 4096)
            return fail(SC_ERR_ARGUMENT, "sc_engine_mask_set: bad picture");
        MaskData m;
        m.width = width;
        m.height = height;
        m.luma = luma != 0;
        m.rgba.assign(rgba, rgba + static_cast<size_t>(width) * height * 4);
        std::lock_guard<std::mutex> lock(engine->state);
        engine->masks[id] = std::move(m);
        return SC_OK;
    });
}

SC_API int sc_engine_snapshot_set(sc_engine *engine, const sc_snapshot *snapshot) {
    return engine_call(engine, "sc_engine_snapshot_set", [&]() -> int {
        if (!snapshot || snapshot->scene_count < 0 || snapshot->item_count < 0 || snapshot->effects_count < 0 ||
            (snapshot->scene_count && !snapshot->scenes) || (snapshot->item_count && !snapshot->items) ||
            (snapshot->effects_count && !snapshot->effects))
            return fail(SC_ERR_ARGUMENT, "sc_engine_snapshot_set: bad snapshot");
        auto snap = std::make_shared<SnapData>();
        snap->scenes.assign(snapshot->scenes, snapshot->scenes + snapshot->scene_count);
        snap->items.assign(snapshot->items, snapshot->items + snapshot->item_count);
        snap->effects.assign(snapshot->effects, snapshot->effects + snapshot->effects_count);
        for (const sc_scene &scene : snap->scenes) {
            if (scene.first_item < 0 || scene.item_count < 0 ||
                static_cast<int64_t>(scene.first_item) + scene.item_count > snapshot->item_count)
                return fail(SC_ERR_ARGUMENT, "sc_engine_snapshot_set: a scene's items are outside the item list");
        }
        std::lock_guard<std::mutex> lock(engine->state);
        engine->snapshot = std::move(snap);
        engine->snapshot_new = true;
        return SC_OK;
    });
}

SC_API int sc_engine_preview_attach(sc_engine *engine, int canvas, void *hwnd, int max_fps, uint32_t background,
                                    int *out_id) {
    return engine_call(engine, "sc_engine_preview_attach", [&]() -> int {
        if (!out_id || canvas < 0 || canvas >= SC_ENGINE_CANVASES || max_fps < 0)
            return fail(SC_ERR_ARGUMENT, "sc_engine_preview_attach: bad canvas, rate or output");
        if (!hwnd || !IsWindow(static_cast<HWND>(hwnd))) return fail(SC_ERR_ARGUMENT, "sc_engine_preview_attach: no window");
        PreviewCfg cfg;
        cfg.canvas = canvas;
        cfg.hwnd = static_cast<HWND>(hwnd);
        cfg.max_fps = max_fps;
        cfg.background[0] = ((background >> 16) & 0xFF) / 255.0f;
        cfg.background[1] = ((background >> 8) & 0xFF) / 255.0f;
        cfg.background[2] = (background & 0xFF) / 255.0f;
        {
            std::lock_guard<std::mutex> lock(engine->state);
            for (const PreviewCfg &p : engine->previews) {
                if (p.hwnd == cfg.hwnd) return fail(SC_ERR_ARGUMENT, "sc_engine_preview_attach: that window has one");
            }
            cfg.id = engine->next_preview++;
            engine->previews.push_back(cfg);
            *out_id = cfg.id;
        }
        wake(engine);
        return SC_OK;
    });
}

SC_API int sc_engine_preview_detach(sc_engine *engine, int id) {
    // Works on a faulted engine too: Python closes its windows whatever state the engine is in.
    return guarded("sc_engine_preview_detach", [&]() -> int {
        if (!engine) return fail(SC_ERR_ARGUMENT, "sc_engine_preview_detach: no engine");
        std::unique_lock<std::mutex> lock(engine->state);
        auto found = std::find_if(engine->previews.begin(), engine->previews.end(),
                                  [&](const PreviewCfg &p) { return p.id == id; });
        if (found == engine->previews.end()) return fail(SC_ERR_ARGUMENT, "sc_engine_preview_detach: no such preview");
        engine->previews.erase(found);
        const uint64_t ticket = ++engine->detach_asked;
        SetEvent(engine->wake);
        const bool done = engine->changed.wait_for(lock, std::chrono::milliseconds(WAIT_MS), [&] {
            return engine->detach_done >= ticket || !engine->stats.running || engine->stop.load();
        });
        if (!done) return fail(SC_ERR_TIMEOUT, "sc_engine_preview_detach: the frame thread didn't let go in time");
        return SC_OK;
    });
}

SC_API int sc_engine_stats_get(sc_engine *engine, sc_engine_stats *out) {
    return guarded("sc_engine_stats_get", [&]() -> int {
        if (!engine || !out) return fail(SC_ERR_ARGUMENT, "sc_engine_stats_get: no engine or output");
        std::lock_guard<std::mutex> lock(engine->state);
        *out = engine->stats;
        out->time = static_cast<double>(qpc() - engine->born) / static_cast<double>(qpf());
        out->hung = engine->hung.load() ? 1 : 0;
        out->faulted = engine->dead.load() ? 1 : 0;
        return SC_OK;
    });
}

SC_API int sc_engine_canvas_read(sc_engine *engine, int index, uint8_t *out, int width, int height) {
    return engine_call(engine, "sc_engine_canvas_read", [&]() -> int {
        if (!out || index < 0 || index >= SC_ENGINE_CANVASES)
            return fail(SC_ERR_ARGUMENT, "sc_engine_canvas_read: bad slot or output");
        {
            std::unique_lock<std::mutex> lock(engine->state);
            const CanvasCfg &cfg = engine->canvases[index];
            if (cfg.width != width || cfg.height != height)
                return fail(SC_ERR_ARGUMENT, "sc_engine_canvas_read: that isn't the canvas's size");
            // Two frames on: a frame already under way may have taken its work before this call's changes.
            const uint64_t want = engine->stats.ticks + 2;
            const bool drawn = engine->changed.wait_for(lock, std::chrono::milliseconds(WAIT_MS), [&] {
                return engine->stats.ticks >= want || engine->dead.load() || !engine->stats.running;
            });
            if (engine->dead.load()) return fail(SC_ERR_FAULTED, "sc_engine_canvas_read: the engine stopped");
            if (!drawn) return fail(SC_ERR_TIMEOUT, "sc_engine_canvas_read: no frame in 2 s");
        }
        std::lock_guard<std::mutex> frame(engine->frame);
        sc_canvas *canvas = engine->gpu[index];
        if (!canvas || canvas->width != width || canvas->height != height)
            return fail(SC_ERR_TIMEOUT, "sc_engine_canvas_read: the canvas isn't drawn yet");
        return sc_canvas_read_bgra(canvas, out, width * 4);
    });
}

SC_API int sc_engine_debug_fault(sc_engine *engine, int kind) {
    return engine_call(engine, "sc_engine_debug_fault", [&]() -> int {
        if (kind < 1 || kind > 4) return fail(SC_ERR_ARGUMENT, "sc_engine_debug_fault: kind 1 to 4");
        {
            std::lock_guard<std::mutex> lock(engine->state);
            engine->fault = kind;
        }
        wake(engine);
        return SC_OK;
    });
}

}  // extern "C"
