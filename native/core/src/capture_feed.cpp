// A capture run for another process (sc_capture_feed_*, docs/native-engine.md "Capture helper"): the capture helper
// program captures a window or display on a device of its own, on the engine's graphics card, and shares each
// picture with the engine the way the browser helper shares its pages (page_texture.cpp): a texture shared by name
// with a keyed mutex, announced in a small block of shared memory. The engine takes it with
// sc_engine_source_shared; no picture passes through memory.
//
// Why another process: every capture Windows starts leaves about one picture of graphics memory behind until the
// program that started it ends (PC 2026-11-19, the same in OBS). The helper can be ended while nothing is
// captured, and Windows gives all of it back.
#include "wgc.h"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstring>
#include <mutex>
#include <string>
#include <thread>

struct sc_capture_feed {
    std::thread worker;
    std::atomic<bool> stop{false};
    std::mutex m;  // the four below
    int state = SC_SCREEN_STARTING;
    int width = 0, height = 0;
    uint64_t frames = 0;
    // The block, made before sc_capture_feed_create returns: the engine opens it once, when it is told its name.
    HANDLE mapping = nullptr;
    uint8_t *view = nullptr;
};

namespace {

constexpr size_t BLOCK_BYTES = 4096;  // the header only: the pictures are on the card
constexpr uint32_t ON_CARD = 2;       // the header's slot for a picture on the card (engine.cpp SharedPictures)
constexpr double START_S = 5.0;       // a capture Windows hasn't started by then is given up

struct Feed {
    sc_capture_feed &feed;
    sc::CaptureAsk ask;
    uint32_t luid_low;
    int32_t luid_high;
    std::wstring block;

    Feed(sc_capture_feed &its, const sc::CaptureAsk &what, uint32_t low, int32_t high, std::wstring name)
        : feed(its), ask(what), luid_low(low), luid_high(high), block(std::move(name)) {}

    uint8_t *view = nullptr;           // the feed's block
    sc_page_target *holder = nullptr;  // a 2x2 target that is never shown: it makes and keeps the device
    sc_page_target *target = nullptr;  // the texture the engine takes pictures from
    uint32_t number = 0;               // `target` is shared as "<block>-tex<number>"
    int target_w = 0, target_h = 0;
    ID3D11Device *device = nullptr;    // the holder's
    ID3D11DeviceContext *context = nullptr;
    ComPtr<ID3D11Texture2D> waiting;   // a picture the engine had no room for yet (it hadn't taken the last)
    int waiting_w = 0, waiting_h = 0;
    bool is_waiting = false;
    uint64_t seq = 0;

    void say(int state, int w = 0, int h = 0) {
        std::lock_guard<std::mutex> hold(feed.m);
        feed.state = state;
        if (w > 0) feed.width = w;
        if (h > 0) feed.height = h;
    }

    bool open() {
        view = feed.view;
        const std::wstring name = block + L"-tex0";
        if (sc::page_target_bgra(luid_low, luid_high, nullptr, 2, 2, name.c_str(), &holder) != SC_OK) return false;
        void *d = nullptr, *c = nullptr, *t = nullptr;
        if (sc_page_target_handles(holder, &d, &c, &t) != SC_OK) return false;
        device = static_cast<ID3D11Device *>(d);
        context = static_cast<ID3D11DeviceContext *>(c);
        return true;
    }

    void close() {
        waiting.Reset();
        if (target) sc_page_target_destroy(target);
        if (holder) sc_page_target_destroy(holder);
        target = holder = nullptr;
    }

    // The engine's texture for pictures of this size: a new one, with the next number, when the size changed.
    bool sized(int w, int h) {
        if (target && target_w == w && target_h == h) return true;
        if (target) sc_page_target_destroy(target);
        target = nullptr;
        ++number;
        const std::wstring name = block + L"-tex" + std::to_wstring(number);
        if (sc::page_target_bgra(luid_low, luid_high, device, w, h, name.c_str(), &target) != SC_OK) return false;
        target_w = w;
        target_h = h;
        return true;
    }

    // `picture` (w x h of it) into the engine's texture, and said in the header. False when the engine hasn't taken
    // the last one yet (or the texture can't be made): the picture is then kept to be shown a moment later.
    bool publish(ID3D11Texture2D *picture, int w, int h) {
        if (!sized(w, h)) return false;
        if (sc_page_target_begin(target, 0) != 1) return false;
        void *d = nullptr, *c = nullptr, *t = nullptr;
        if (sc_page_target_handles(target, &d, &c, &t) != SC_OK) {
            sc_page_target_end(target, 0);
            return false;
        }
        const D3D11_BOX box = {0, 0, 0, static_cast<UINT>(w), static_cast<UINT>(h), 1};
        context->CopySubresourceRegion(static_cast<ID3D11Texture2D *>(t), 0, 0, 0, 0, picture, 0, &box);
        if (sc_page_target_end(target, 1) != SC_OK) return false;
        const uint32_t head[5] = {static_cast<uint32_t>(w), static_cast<uint32_t>(h), ON_CARD, number,
                                  sc::SHARED_STRAIGHT | ((ask.flags & SC_SCREEN_OPAQUE) ? sc::SHARED_OPAQUE : 0u)};
        std::memcpy(view + 8, head, sizeof head);
        std::atomic_thread_fence(std::memory_order_release);
        ++seq;
        std::memcpy(view, &seq, sizeof seq);  // last: the engine reads the counter first
        std::atomic_thread_fence(std::memory_order_release);
        {
            std::lock_guard<std::mutex> hold(feed.m);
            feed.state = SC_SCREEN_RUNNING;
            feed.width = w;
            feed.height = h;
            ++feed.frames;
        }
        return true;
    }

    // Windows' picture, while it is still Windows' to give: straight to the engine when it has room, else a copy
    // is kept (a still window draws once; that one picture mustn't be lost).
    bool took(ID3D11Texture2D *picture, int w, int h) {
        if (publish(picture, w, h)) {
            is_waiting = false;
            return true;
        }
        if (!waiting || waiting_w != w || waiting_h != h) {
            waiting.Reset();
            D3D11_TEXTURE2D_DESC td = {};
            td.Width = static_cast<UINT>(w);
            td.Height = static_cast<UINT>(h);
            td.MipLevels = 1;
            td.ArraySize = 1;
            td.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
            td.SampleDesc.Count = 1;
            td.Usage = D3D11_USAGE_DEFAULT;
            td.BindFlags = D3D11_BIND_SHADER_RESOURCE;
            if (FAILED(device->CreateTexture2D(&td, nullptr, &waiting))) return false;
            waiting_w = w;
            waiting_h = h;
        }
        const D3D11_BOX box = {0, 0, 0, static_cast<UINT>(w), static_cast<UINT>(h), 1};
        context->CopySubresourceRegion(waiting.Get(), 0, 0, 0, 0, picture, 0, &box);
        is_waiting = true;
        return true;
    }

    void run() {
        sc::name_thread(L"SprigCast capture feed");
        if (!open()) {
            say(SC_SCREEN_REFUSED);
            return;
        }
        ComPtr<ID3D11Device> on(device);
        std::shared_ptr<sc::CaptureStart> starting = sc::start_capture(on, ask);
        sc::CapturePtr cap;
        const auto began = std::chrono::steady_clock::now();
        for (;;) {  // Windows starts it on the capture thread (a start can hang: capture_wgc.cpp)
            if (feed.stop.load()) return;
            {
                std::lock_guard<std::mutex> hold(starting->m);
                if (starting->done) {
                    cap = std::move(starting->cap);
                    if (!cap) {
                        sc::log_line(SC_LOG_INFO, "capture feed: the capture didn't start (" + starting->why + ")");
                        const bool here = starting->refusal != sc::CaptureRefusal::NOT_HERE;
                        std::lock_guard<std::mutex> tell(feed.m);
                        feed.state = here ? SC_SCREEN_ENDED : SC_SCREEN_REFUSED;
                        return;
                    }
                    break;
                }
            }
            if (std::chrono::duration<double>(std::chrono::steady_clock::now() - began).count() > START_S) {
                sc::log_line(SC_LOG_WARNING, "capture feed: Windows didn't start the capture in 5 s");
                say(SC_SCREEN_REFUSED);
                return;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
        }
        // Looked at twice per picture wanted, at least every 8 ms: Windows hands pictures over only when asked.
        const int nap_ms = ask.interval_ms > 0 ? std::min(8, std::max(1, ask.interval_ms / 2)) : 4;
        const sc::CapturePicture to = [this](ID3D11Texture2D *picture, int w, int h) { return took(picture, w, h); };
        while (!feed.stop.load()) {
            cap->take_to(to);
            if (is_waiting && publish(waiting.Get(), waiting_w, waiting_h)) is_waiting = false;
            if (cap->ended()) {
                say(SC_SCREEN_ENDED);
                break;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(nap_ms));
        }
    }
};

void feed_thread(sc_capture_feed *feed, sc::CaptureAsk ask, uint32_t low, int32_t high, std::wstring block) {
    Feed f(*feed, ask, low, high, std::move(block));
    try {
        f.run();
    } catch (...) {  // crash gate: this capture ends, the helper goes on
        std::lock_guard<std::mutex> hold(feed->m);
        feed->state = SC_SCREEN_ENDED;
    }
    try {
        f.close();
    } catch (...) {
    }
}

}  // namespace

extern "C" {

SC_API int sc_capture_feed_create(uint32_t luid_low, int32_t luid_high, uint64_t window, uint64_t monitor, int flags,
                                  int interval_ms, const wchar_t *block, sc_capture_feed **out) {
    return sc::guarded("sc_capture_feed_create", [&]() -> int {
        if (!out || !block || !*block) return sc::fail(SC_ERR_ARGUMENT, "sc_capture_feed_create: no block name");
        *out = nullptr;
        if (!window == !monitor || interval_ms < 0 || interval_ms > 10000 ||
            (flags & ~(SC_SCREEN_CURSOR | SC_SCREEN_BORDER | SC_SCREEN_OPAQUE)))
            return sc::fail(SC_ERR_ARGUMENT, "sc_capture_feed_create: one window or one display, or bad flags");
        sc::CaptureAsk ask;
        ask.window = window;
        ask.monitor = monitor;
        ask.flags = flags;
        ask.interval_ms = interval_ms;
        auto feed = std::make_unique<sc_capture_feed>();
        feed->mapping = CreateFileMappingW(INVALID_HANDLE_VALUE, nullptr, PAGE_READWRITE, 0, BLOCK_BYTES, block);
        const DWORD error = GetLastError();
        if (feed->mapping && error != ERROR_ALREADY_EXISTS)
            feed->view = static_cast<uint8_t *>(MapViewOfFile(feed->mapping, FILE_MAP_ALL_ACCESS, 0, 0, 0));
        if (!feed->view) {
            if (feed->mapping) CloseHandle(feed->mapping);
            return sc::fail(SC_ERR_DEVICE, "sc_capture_feed_create: its shared memory couldn't be made, or a block "
                                           "of that name is there already");
        }
        std::memset(feed->view, 0, BLOCK_BYTES);
        feed->worker = std::thread(feed_thread, feed.get(), ask, luid_low, luid_high, std::wstring(block));
        *out = feed.release();
        return SC_OK;
    });
}

SC_API int sc_capture_feed_state(sc_capture_feed *feed, int *state, int *width, int *height, uint64_t *frames) {
    return sc::guarded("sc_capture_feed_state", [&]() -> int {
        if (!feed) return sc::fail(SC_ERR_ARGUMENT, "sc_capture_feed_state: no feed");
        std::lock_guard<std::mutex> hold(feed->m);
        if (state) *state = feed->state;
        if (width) *width = feed->width;
        if (height) *height = feed->height;
        if (frames) *frames = feed->frames;
        return SC_OK;
    });
}

SC_API void sc_capture_feed_destroy(sc_capture_feed *feed) {
    if (!feed) return;
    try {
        feed->stop.store(true);
        if (feed->worker.joinable()) feed->worker.join();
        if (feed->view) UnmapViewOfFile(feed->view);
        if (feed->mapping) CloseHandle(feed->mapping);
    } catch (...) {
    }
    delete feed;
}

}  // extern "C"
