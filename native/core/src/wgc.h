// Screen, window and game capture inside the engine (capture_wgc.cpp, docs/native-engine.md "Capture"): Windows
// Graphics Capture hands each picture over on the engine's own device, and the frame thread copies the newest one
// into the source's texture. No picture of the screen passes through memory or Python.
#pragma once

#include "internal.h"

#include <cstdint>
#include <memory>
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

class ScreenCapture {
public:
    // A capture of `ask` on the engine's device: null when it can't start, `refusal` and `why` say why. Called on the
    // frame thread only.
    static std::unique_ptr<ScreenCapture> start(sc_device &dev, const CaptureAsk &ask, CaptureRefusal &refusal,
                                                std::string &why);
    ~ScreenCapture();
    ScreenCapture(const ScreenCapture &) = delete;
    ScreenCapture &operator=(const ScreenCapture &) = delete;

    // The newest picture into source `id` (copy = false: taken, so Windows can draw the next, but the source keeps
    // its picture: a freeze). True when the source got a new picture. Takes device.lock. Never throws: a capture
    // that broke says so through ended().
    bool take(sc_device &dev, uint64_t id, bool copy);
    bool ended() const;  // the window closed, the display went, or the capture broke
    ID3D11Device *device() const;
    int width() const;
    int height() const;

private:
    ScreenCapture();
    struct Impl;
    std::unique_ptr<Impl> impl;
};

// Source `id`'s picture (a capture's B8G8R8A8 texture) read back as BGRA rows, for a frame Python draws. `staging`
// keeps the read-back texture between calls. SC_ERR_ARGUMENT when the source has no picture or `max_w` x `max_h`
// is too small; `w` and `h` say its size either way. Takes device.lock.
int read_capture(sc_device &dev, uint64_t id, ComPtr<ID3D11Texture2D> &staging, uint8_t *bgra, int stride, int max_w,
                 int max_h, int *w, int *h);

}  // namespace sc
