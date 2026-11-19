// Screen, window and game capture inside the engine (wgc.h, docs/native-engine.md "Capture"). Windows Graphics
// Capture draws each picture into a frame pool made on the engine's own device; the frame thread takes the newest
// one each frame (TryGetNextFrame, so no thread of Windows' ever calls into the engine) and copies it on the card
// into the source's texture. A window that closes, a display that goes, or a capture that breaks says so through
// ended(), and SprigCast finds its target again (sources/capture.py).
#include <unknwn.h>
#include <inspectable.h>

#include "wgc.h"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <cstring>
#include <deque>
#include <functional>
#include <thread>

#ifdef GetCurrentTime
#undef GetCurrentTime
#endif
#pragma warning(push, 0)
#include <winrt/base.h>
#include <winrt/Windows.Foundation.h>
#include <winrt/Windows.Graphics.h>
#include <winrt/Windows.Graphics.Capture.h>
#include <winrt/Windows.Graphics.DirectX.h>
#include <winrt/Windows.Graphics.DirectX.Direct3D11.h>
#include <windows.graphics.capture.interop.h>
#include <windows.graphics.directx.direct3d11.interop.h>
#pragma warning(pop)

namespace wgc = winrt::Windows::Graphics::Capture;
namespace wdx = winrt::Windows::Graphics::DirectX;
using DxgiAccess = ::Windows::Graphics::DirectX::Direct3D11::IDirect3DDxgiInterfaceAccess;

namespace sc {

namespace {

constexpr auto PICTURE_FORMAT = wdx::DirectXPixelFormat::B8G8R8A8UIntNormalized;
constexpr int POOL_PICTURES = 2;

std::string hr_text(const char *what, HRESULT hr) {
    char code[16];
    std::snprintf(code, sizeof code, "0x%08lX", static_cast<unsigned long>(hr));
    return std::string(what) + " (" + code + ")";
}

// The capture thread: starts and closes captures, one after another, so a call into Windows that hangs holds up
// only other captures, never a frame. It lives as long as the process (never joined: it may be stuck for good).
// A call that hasn't come back in STUCK_S is left on its thread and a new capture thread takes the jobs after
// it: one slow start turned engine capture off for every source until a restart (PC, 2026-11-19).
class Keeper {
public:
    static Keeper &get() {
        static Keeper *keeper = new Keeper();  // never deleted: its threads may outlive everything
        return *keeper;
    }

    void post(std::function<void()> job) {
        {
            std::lock_guard<std::mutex> hold(m);
            jobs.push_back(std::move(job));
        }
        wake.notify_all();
    }

    bool on_thread() const { return keeper_thread; }

private:
    static constexpr auto STUCK_S = std::chrono::seconds(3);
    static constexpr int MOST_LEFT = 4;  // threads left stuck in Windows before the queue just waits

    Keeper() {
        std::lock_guard<std::mutex> hold(m);
        spawn();
        std::thread([this] { guard(); }).detach();
    }

    void spawn() {  // under m
        const uint64_t gen = ++generation;
        busy = false;
        std::thread([this, gen] { run(gen); }).detach();
    }

    void run(uint64_t gen) {
        keeper_thread = true;
        name_thread(L"SprigCast captures");
        try {
            winrt::init_apartment(winrt::apartment_type::multi_threaded);
        } catch (...) {  // already set up: fine
        }
        for (;;) {
            std::function<void()> job;
            {
                std::unique_lock<std::mutex> hold(m);
                wake.wait(hold, [&] { return gen != generation || !jobs.empty(); });
                if (gen != generation) return;  // left behind after a stuck call: a newer thread has the jobs
                job = std::move(jobs.front());
                jobs.pop_front();
                busy = true;
                busy_since = std::chrono::steady_clock::now();
            }
            watch.notify_all();
            try {
                job();
            } catch (...) {
            }
            std::lock_guard<std::mutex> hold(m);
            if (gen != generation) return;
            busy = false;
        }
    }

    void guard() {
        name_thread(L"SprigCast captures watch");
        std::unique_lock<std::mutex> hold(m);
        for (;;) {
            watch.wait(hold, [&] { return busy && left < MOST_LEFT; });  // asleep while nothing runs
            const auto since = busy_since;
            const uint64_t gen = generation;
            watch.wait_until(hold, since + STUCK_S);
            if (busy && busy_since == since && generation == gen && left < MOST_LEFT &&
                std::chrono::steady_clock::now() - since >= STUCK_S) {
                ++left;
                log_line(SC_LOG_WARNING, "capture: a call into Windows hasn't come back in 3 s; captures go on "
                                         "on a new thread");
                spawn();
                wake.notify_all();
            }
        }
    }

    static thread_local bool keeper_thread;
    std::mutex m;
    std::condition_variable wake, watch;
    std::deque<std::function<void()>> jobs;
    uint64_t generation = 0;
    bool busy = false;
    std::chrono::steady_clock::time_point busy_since{};
    int left = 0;
};

thread_local bool Keeper::keeper_thread = false;

// Captures started and not yet closed. Said in the log with each start and close: a capture left open keeps its
// frame pool on the card (2 pictures; PC soak 2026-11-19: 16 MB more graphics memory per wake of a display capture).
std::atomic<int> open_captures{0};

}  // namespace

void CaptureRetire::operator()(ScreenCapture *capture) const {
    if (!capture) return;
    Keeper &keeper = Keeper::get();
    if (keeper.on_thread()) {
        delete capture;
        return;
    }
    keeper.post([capture] { delete capture; });
}

std::shared_ptr<CaptureStart> start_capture(const ComPtr<ID3D11Device> &device, const CaptureAsk &ask) {
    auto slot = std::make_shared<CaptureStart>();
    Keeper::get().post([slot, device, ask] {
        CaptureRefusal refusal = CaptureRefusal::NONE;
        std::string why;
        CapturePtr cap = ScreenCapture::start(device, ask, refusal, why);
        std::lock_guard<std::mutex> hold(slot->m);
        slot->cap = std::move(cap);
        slot->refusal = refusal;
        slot->why = std::move(why);
        slot->done = true;
    });
    return slot;
}

struct ScreenCapture::Impl {
    HWND window = nullptr;           // a window capture's window: closed, the capture ends (Closed may not come)
    ID3D11Device *device = nullptr;  // the engine's: captures are dropped before it goes (engine.cpp drop_gpu)
    wdx::Direct3D11::IDirect3DDevice d3d{nullptr};
    wgc::GraphicsCaptureItem item{nullptr};
    wgc::Direct3D11CaptureFramePool pool{nullptr};
    wgc::GraphicsCaptureSession session{nullptr};
    wgc::GraphicsCaptureItem::Closed_revoker closed;
    std::shared_ptr<std::atomic<bool>> gone = std::make_shared<std::atomic<bool>>(false);
    bool broken = false;
    winrt::Windows::Graphics::SizeInt32 pool_size{};
    bool opaque = false;
    std::chrono::steady_clock::duration step{};
    std::chrono::steady_clock::time_point next_due{};
    int width = 0, height = 0;
    bool counted = false;  // started: one of open_captures

    void close() noexcept {
        const auto began = std::chrono::steady_clock::now();
        try {
            closed.revoke();
        } catch (...) {
        }
        try {
            if (session) session.Close();  // first: Windows draws no more pictures into the pool
        } catch (...) {
        }
        // Pictures Windows drew that were never taken go back before the pool closes: one still out keeps the
        // pool's textures on the card.
        int handed_back = 0;
        try {
            while (pool) {
                wgc::Direct3D11CaptureFrame left = pool.TryGetNextFrame();
                if (!left) break;
                left.Close();
                ++handed_back;
            }
        } catch (...) {
        }
        try {
            if (pool) pool.Close();
        } catch (...) {
        }
        session = nullptr;
        pool = nullptr;
        item = nullptr;
        d3d = nullptr;
        if (counted) {
            counted = false;
            const int open = --open_captures;
            const auto ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                                std::chrono::steady_clock::now() - began).count();
            try {
                log_line(SC_LOG_INFO, "capture: closed in " + std::to_string(ms) + " ms (" +
                                          std::to_string(handed_back) + " pictures handed back, " +
                                          std::to_string(open) + " still open)");
            } catch (...) {
            }
        }
    }

    // As sources/capture.py did: steady on a grid while pictures keep up, a little slack so 60 on a 60 Hz game
    // doesn't drop to 30.
    bool due() {
        if (step.count() <= 0) return true;
        const auto now = std::chrono::steady_clock::now();
        if (now < next_due - step / 4) return false;
        next_due = next_due > now - step ? next_due + step : now + step;
        return true;
    }
};

ScreenCapture::ScreenCapture() : impl(std::make_unique<Impl>()) {}

ScreenCapture::~ScreenCapture() {
    if (impl) impl->close();
}

CapturePtr ScreenCapture::start(const ComPtr<ID3D11Device> &device, const CaptureAsk &ask, CaptureRefusal &refusal,
                                std::string &why) {
    refusal = CaptureRefusal::NONE;
    why.clear();
    if (!ask.window == !ask.monitor) {
        refusal = CaptureRefusal::NOT_HERE;
        why = "a capture needs one window or one display";
        return nullptr;
    }
    CapturePtr made(new ScreenCapture());
    Impl &c = *made->impl;
    try {
        if (!wgc::GraphicsCaptureSession::IsSupported()) {
            refusal = CaptureRefusal::NOT_HERE;
            why = "Windows Graphics Capture isn't supported on this PC";
            return nullptr;
        }
        auto factory = winrt::get_activation_factory<wgc::GraphicsCaptureItem>();
        ComPtr<IGraphicsCaptureItemInterop> interop;
        HRESULT hr = static_cast<::IUnknown *>(winrt::get_abi(factory))->QueryInterface(IID_PPV_ARGS(&interop));
        if (FAILED(hr)) {
            refusal = CaptureRefusal::NOT_HERE;
            why = hr_text("this Windows can't capture windows or displays", hr);
            return nullptr;
        }
        const winrt::guid item_guid = winrt::guid_of<wgc::GraphicsCaptureItem>();
        GUID riid;
        std::memcpy(&riid, &item_guid, sizeof riid);
        hr = ask.window ? interop->CreateForWindow(reinterpret_cast<HWND>(static_cast<uintptr_t>(ask.window)), riid,
                                                   winrt::put_abi(c.item))
                        : interop->CreateForMonitor(reinterpret_cast<HMONITOR>(static_cast<uintptr_t>(ask.monitor)),
                                                    riid, winrt::put_abi(c.item));
        if (FAILED(hr) || !c.item) {
            refusal = CaptureRefusal::TARGET_GONE;
            why = hr_text(ask.window ? "the window can't be captured" : "the display can't be captured", hr);
            return nullptr;
        }
        ComPtr<IDXGIDevice> dxgi;
        hr = device.As(&dxgi);
        winrt::Windows::Foundation::IInspectable inspectable;
        if (SUCCEEDED(hr))
            hr = CreateDirect3D11DeviceFromDXGIDevice(dxgi.Get(),
                                                      reinterpret_cast<::IInspectable **>(winrt::put_abi(inspectable)));
        if (FAILED(hr)) {
            refusal = CaptureRefusal::NOT_HERE;
            why = hr_text("the engine's device can't take captured pictures", hr);
            return nullptr;
        }
        c.d3d = inspectable.as<wdx::Direct3D11::IDirect3DDevice>();
        c.device = device.Get();
        c.pool_size = c.item.Size();
        c.pool = wgc::Direct3D11CaptureFramePool::CreateFreeThreaded(c.d3d, PICTURE_FORMAT, POOL_PICTURES,
                                                                     c.pool_size);
        c.session = c.pool.CreateCaptureSession(c.item);
        auto gone = c.gone;
        c.closed = c.item.Closed(winrt::auto_revoke,
                                 [gone](const wgc::GraphicsCaptureItem &, const winrt::Windows::Foundation::IInspectable &) {
                                     gone->store(true);
                                 });
        // Each of these is newer than Windows 10 1903: an older Windows keeps its default (cursor shown, the yellow
        // border, every picture).
        try {
            c.session.IsCursorCaptureEnabled((ask.flags & SC_SCREEN_CURSOR) != 0);
        } catch (...) {
        }
        try {
            c.session.IsBorderRequired((ask.flags & SC_SCREEN_BORDER) != 0);
        } catch (...) {
        }
#if defined(NTDDI_WIN11_GE) && defined(WDK_NTDDI_VERSION) && WDK_NTDDI_VERSION >= NTDDI_WIN11_GE
        if (ask.interval_ms > 0) {
            try {
                c.session.MinUpdateInterval(std::chrono::duration_cast<winrt::Windows::Foundation::TimeSpan>(
                    std::chrono::milliseconds(ask.interval_ms * 9 / 10)));
            } catch (...) {
            }
        }
#endif
        c.opaque = (ask.flags & SC_SCREEN_OPAQUE) != 0;
        c.window = reinterpret_cast<HWND>(static_cast<uintptr_t>(ask.window));
        if (ask.interval_ms > 0)
            c.step = std::chrono::duration_cast<std::chrono::steady_clock::duration>(
                std::chrono::milliseconds(ask.interval_ms));
        c.session.StartCapture();
        c.counted = true;
        log_line(SC_LOG_INFO, std::string("capture: started a ") + (ask.window ? "window" : "display") + " (" +
                                  std::to_string(++open_captures) + " open)");
    } catch (const winrt::hresult_error &x) {
        refusal = x.code() == E_ACCESSDENIED ? CaptureRefusal::NOT_HERE : CaptureRefusal::TARGET_GONE;
        why = hr_text("the capture didn't start", x.code());
        return nullptr;
    } catch (...) {
        refusal = CaptureRefusal::NOT_HERE;
        why = "the capture didn't start";
        return nullptr;
    }
    return made;
}

bool ScreenCapture::take(sc_device &dev, uint64_t id, bool copy) {
    Impl &c = *impl;
    if (c.broken || !c.pool) return false;
    if (c.window && !IsWindow(c.window)) {  // Windows says Closed on the item's own time, or not at all (CI)
        c.gone->store(true);
        return false;
    }
    try {
        wgc::Direct3D11CaptureFrame newest{nullptr};
        for (;;) {  // only the newest is shown; the older ones go back to Windows at once
            wgc::Direct3D11CaptureFrame next = c.pool.TryGetNextFrame();
            if (!next) break;
            if (newest) newest.Close();
            newest = next;
        }
        if (!newest) return false;
        const winrt::Windows::Graphics::SizeInt32 size = newest.ContentSize();
        if (size.Width != c.pool_size.Width || size.Height != c.pool_size.Height) {
            c.pool_size = size;  // the window or display changed size: pictures of the new size from the next one
            c.pool.Recreate(c.d3d, PICTURE_FORMAT, POOL_PICTURES, size);
        }
        if (!copy || !c.due()) {
            newest.Close();
            return false;
        }
        ComPtr<DxgiAccess> access;
        ComPtr<ID3D11Texture2D> picture;
        HRESULT hr = static_cast<::IUnknown *>(winrt::get_abi(newest.Surface()))->QueryInterface(IID_PPV_ARGS(&access));
        if (SUCCEEDED(hr)) hr = access->GetInterface(IID_PPV_ARGS(&picture));
        if (FAILED(hr)) {
            newest.Close();
            return false;
        }
        D3D11_TEXTURE2D_DESC pd = {};
        picture->GetDesc(&pd);
        const int w = std::min(size.Width, static_cast<int32_t>(pd.Width));
        const int h = std::min(size.Height, static_cast<int32_t>(pd.Height));
        if (w < 1 || h < 1) {
            newest.Close();
            return false;
        }
        {
            std::lock_guard<std::mutex> lock(dev.lock);
            dev.comp.drawn.erase(id);  // a picture again: no longer drawn from a list
            Source &s = dev.comp.sources[id];
            D3D11_TEXTURE2D_DESC have = {};
            if (s.texture) s.texture->GetDesc(&have);
            if (!s.texture || s.data_w != w || s.data_h != h || have.Format != DXGI_FORMAT_B8G8R8A8_UNORM) {
                s = Source();
                D3D11_TEXTURE2D_DESC td = {};
                td.Width = static_cast<UINT>(w);
                td.Height = static_cast<UINT>(h);
                td.MipLevels = 1;
                td.ArraySize = 1;
                td.Format = DXGI_FORMAT_B8G8R8A8_UNORM;  // sampled as RGBA: no swizzle in the compositor
                td.SampleDesc.Count = 1;
                td.Usage = D3D11_USAGE_DEFAULT;
                td.BindFlags = D3D11_BIND_SHADER_RESOURCE;
                hr = dev.device->CreateTexture2D(&td, nullptr, &s.texture);
                if (SUCCEEDED(hr)) hr = dev.device->CreateShaderResourceView(s.texture.Get(), nullptr, &s.view);
                if (FAILED(hr)) {
                    dev.comp.sources.erase(id);
                    newest.Close();
                    return false;  // out of video memory: tried again with the next picture
                }
                s.data_w = w;
                s.data_h = h;
            }
            const D3D11_BOX box = {0, 0, 0, static_cast<UINT>(w), static_cast<UINT>(h), 1};
            dev.context->CopySubresourceRegion(s.texture.Get(), 0, 0, 0, 0, picture.Get(), 0, &box);
            s.pic_w = s.layout_w = w;
            s.pic_h = s.layout_h = h;
            s.format = c.opaque ? SC_FMT_RGB : SC_FMT_RGBA;  // RGB: alpha ignored (a game's picture is solid)
            s.premultiplied = false;
        }
        newest.Close();
        c.width = w;
        c.height = h;
        return true;
    } catch (...) {  // the device went, or Windows ended the capture: SprigCast starts it again
        c.broken = true;
        return false;
    }
}

bool ScreenCapture::ended() const { return impl->broken || impl->gone->load(); }
ID3D11Device *ScreenCapture::device() const { return impl->device; }
int ScreenCapture::width() const { return impl->width; }
int ScreenCapture::height() const { return impl->height; }

int read_capture(sc_device &dev, uint64_t id, ComPtr<ID3D11Texture2D> &staging, uint8_t *bgra, int stride, int max_w,
                 int max_h, int *w, int *h) {
    std::lock_guard<std::mutex> lock(dev.lock);
    auto found = dev.comp.sources.find(id);
    *w = *h = 0;
    if (found == dev.comp.sources.end() || !found->second.texture)
        return fail(SC_ERR_ARGUMENT, "sc_engine_capture_read: no picture yet");
    Source &s = found->second;
    D3D11_TEXTURE2D_DESC td = {};
    s.texture->GetDesc(&td);
    if (td.Format != DXGI_FORMAT_B8G8R8A8_UNORM) return fail(SC_ERR_ARGUMENT, "sc_engine_capture_read: not a capture");
    *w = s.pic_w;
    *h = s.pic_h;
    if (!bgra) return SC_OK;  // asked for its size only
    if (s.pic_w > max_w || s.pic_h > max_h || stride < s.pic_w * 4) {
        // Not logged: Python asks again with the size given back (a capture that changed size, or woke up)
        set_last_error("sc_engine_capture_read: no room for the picture");
        return SC_ERR_ARGUMENT;
    }
    D3D11_TEXTURE2D_DESC sd = {};
    if (staging) staging->GetDesc(&sd);
    if (!staging || sd.Width != td.Width || sd.Height != td.Height) {
        staging.Reset();
        td.Usage = D3D11_USAGE_STAGING;
        td.BindFlags = 0;
        td.MiscFlags = 0;
        td.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
        const HRESULT hr = dev.device->CreateTexture2D(&td, nullptr, &staging);
        if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "Creating a capture's read-back texture", hr);
    }
    dev.context->CopyResource(staging.Get(), s.texture.Get());
    D3D11_MAPPED_SUBRESOURCE m;
    const HRESULT hr = dev.context->Map(staging.Get(), 0, D3D11_MAP_READ, 0, &m);
    if (FAILED(hr)) return fail_hr(SC_ERR_DEVICE, "Map (capture read-back)", hr);
    for (int y = 0; y < s.pic_h; ++y)
        std::memcpy(bgra + static_cast<size_t>(y) * stride,
                    static_cast<const uint8_t *>(m.pData) + static_cast<size_t>(y) * m.RowPitch,
                    static_cast<size_t>(s.pic_w) * 4);
    dev.context->Unmap(staging.Get(), 0);
    return SC_OK;
}

}  // namespace sc
