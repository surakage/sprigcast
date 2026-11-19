// Screen, window and game capture inside the engine (capture_wgc.cpp, docs/native-engine.md "Capture"): Windows
// Graphics Capture hands each picture over on the engine's own device, and the frame thread copies the newest one
// into the source's texture. No picture of the screen passes through memory or Python.
#pragma once

#include "internal.h"

#include <cstdint>
#include <memory>
#include <mutex>
#include <string>

namespace sc {

struct CaptureAsk {
    uint64_t window = 0;   // an HWND, or 0
    uint64_t monitor = 0;  // an HMONITOR, or 0 (one of the two is set)
    int flags = 0;         // SC_CAPTURE_*
    int interval_ms = 0;   // at most one picture this often (0 = every one Windows draws)
};

// What a capture that couldn't start means for SprigCast: try the target again later, or capture in Python.
enum class CaptureRefusal { NONE, TARGET_GONE, NOT_HERE };

class ScreenCapture;

// Starting and closing a capture call Windows' capture service in another process, and such a call can hang for
// good (a window closing just as its capture started: PC test 2026-11-18, the frame thread stuck in an RPC). So
// both happen on a thread of their own, never on the frame thread: a capture let go is closed there.
struct CaptureRetire {
    void operator()(ScreenCapture *capture) const;
};
using CapturePtr = std::unique_ptr<ScreenCapture, CaptureRetire>;

// A start under way on the capture thread: `done` once it ended, with the capture or why there is none.
struct CaptureStart {
    std::mutex m;
    bool done = false;
    CapturePtr cap;
    CaptureRefusal refusal = CaptureRefusal::NONE;
    std::string why;
};

// Asks the capture thread to start a capture of `ask` on `device` (the engine's). Returns at once.
std::shared_ptr<CaptureStart> start_capture(const ComPtr<ID3D11Device> &device, const CaptureAsk &ask);

class ScreenCapture {
public:
    // A capture of `ask` on `device`: null when it can't start, `refusal` and `why` say why. Called on the capture
    // thread only (start_capture).
    static CapturePtr start(const ComPtr<ID3D11Device> &device, const CaptureAsk &ask, CaptureRefusal &refusal,
                            std::string &why);
    ~ScreenCapture();  // closes it: on the capture thread only (CaptureRetire)
    ScreenCapture(const ScreenCapture &) = delete;
    ScreenCapture &operator=(const ScreenCapture &) = delete;

    // The newest picture into source `id` (copy = false: taken, so Windows can draw the next, but the source keeps
    // its picture: a freeze). True when the source got a new picture. Takes device.lock. Never throws: a capture
    // that broke says so through ended().
    bool take(sc_device &dev, uint64_t id, bool copy);
    bool ended() const;  // the window closed, the display went, or the capture broke
    // Parked (its source asleep, engine.cpp CAPTURE_PARK_S): at most a picture a second, until slow(false).
    void slow(bool on);
    ID3D11Device *device() const;
    int width() const;
    int height() const;

private:
    ScreenCapture();
    struct Impl;
    std::unique_ptr<Impl> impl;
};

// Source `id`'s picture (a capture's B8G8R8A8 texture) read back as BGRA rows, for a frame Python draws. `staging`
// keeps the read-back texture between calls. bgra = null asks for the size only. SC_ERR_ARGUMENT when the source
// has no picture or `max_w` x `max_h` is too small; `w` and `h` say its size either way. Takes device.lock.
int read_capture(sc_device &dev, uint64_t id, ComPtr<ID3D11Texture2D> &staging, uint8_t *bgra, int stride, int max_w,
                 int max_h, int *w, int *h);

}  // namespace sc
