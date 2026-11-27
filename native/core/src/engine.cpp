// The engine's own frame loop (phase 2, docs/native-engine.md): a frame thread paced by a high-resolution timer
// draws every canvas and shows it in preview windows, and a watchdog thread keeps an eye on it. Python hands work
// over through the sc_engine_* calls, which only copy it into a queue: the frame thread takes the queue at the
// start of a frame, so neither side ever waits on the other's slow work.
//
// Crash gates: a fault inside a frame, or a lost graphics card, ends that device, not the engine. The engine
// builds a new device and gives it everything again from its own copies. Five rebuilds within a minute and it
// stops trying and says so (faulted), and Python switches to its own engine.
#include "internal.h"
#include "media.h"
#include "visual.h"
#include "wgc.h"
#include "writer.h"

#include <mmsystem.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstdio>
#include <cstring>
#include <deque>
#include <iterator>
#include <set>
#include <unordered_set>
#include <stdexcept>
#include <string>

#ifndef CREATE_WAITABLE_TIMER_HIGH_RESOLUTION
#  define CREATE_WAITABLE_TIMER_HIGH_RESOLUTION 0x00000002
#endif

using namespace sc;

namespace {

constexpr double HANG_S = 2.0;           // one frame this long and the watchdog reports the engine hung
constexpr int REBUILDS_PER_MINUTE = 5;   // more than this and the engine gives up (faulted)
constexpr int BUILD_TRIES = 10;          // a new device that can't be made after this many tries: faulted
constexpr int WAIT_MS = 2000;            // how long Python waits for the frame thread before giving up
constexpr int FRAME_WAIT_MS = 500;       // how long a Python call waits for a frame to let go (a stuck one never does)
// A capture whose source went to sleep stays open, parked, taking a picture a second, and is taken up again when
// the source wakes: every capture Windows starts leaves about one picture of graphics memory behind until the
// program ends, whoever starts it and however it is closed (PC 2026-11-19: +8.4 MB per wake of a 1080p window or
// display, the same with windows-capture on the old engine and in OBS; 5.5 MB a minute over an 80-minute soak of
// 15 s bloom switches). It is parked for as long as the source exists (user, 2026-11-19: no time limit, a bloom
// back after an hour would cost a start again); a capture helper that can be restarted is to replace this.
constexpr double CAPTURE_BACK_S = 0.5;   // back from parked with nothing new drawn by then: its picture stands
constexpr double CAPTURE_FRESH_S = 2.0;  // a parked capture's picture this new is shown at once on waking
constexpr double CAMERA_GAP_S = 1.0 / 30 - 0.004;  // the virtual camera's 30 fps, with room for a late draw
constexpr double CAPTURE_START_S = 5.0;  // a capture Windows hasn't started by then is given up (Python captures)

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

// A capture Python asked for (sc_engine_source_capture): a new serial each time it starts over.
struct CaptureWant {
    sc::CaptureAsk ask;
    uint64_t serial = 0;
};

// How it is doing, for sc_engine_capture_state (the serial it is about).
struct CaptureShown {
    int state = SC_SCREEN_NONE;
    int width = 0, height = 0;
    uint64_t frames = 0;
    uint64_t serial = 0;
};

// The frame thread's capture for a want: made on the device, again after a rebuild; `done` once it ended or
// couldn't start (Python asks again).
struct CaptureRun {
    sc::CapturePtr cap;                           // let go: closed on the capture thread (wgc.h)
    std::shared_ptr<sc::CaptureStart> starting;  // a start under way on the capture thread
    int64_t asked = 0;                            // when it was asked (qpc)
    uint64_t serial = 0;
    uint64_t frames = 0;
    bool done = false;
    sc::CaptureAsk ask;        // what `cap` captures
    int64_t parked = 0;        // when its source went to sleep (qpc), 0 while it is wanted
    int64_t copied = 0;        // when it last gave the source a picture (qpc)
    int64_t back = 0;          // when it came back from being parked (qpc), until its first news is said
};

bool same_ask(const sc::CaptureAsk &a, const sc::CaptureAsk &b) {
    return a.window == b.window && a.monitor == b.monitor && a.flags == b.flags && a.interval_ms == b.interval_ms;
}

struct DrawList {  // sc_engine_source_draws: count < 0 drops it
    int width = 0, height = 0, count = -1;
    std::vector<sc_item> items;
};

// A browser page's pictures in the browser helper's shared memory (sc_engine_source_shared; the writer is
// app/browser_helper.py FrameSlot): a 64-byte header, u64 counter then u32 width, height and slot, and two slots
// of the size the header gives at byte 32 (u64). Not half the rest of the block: the size Windows gives here is
// rounded up to whole pages, the helper's is not, and the second slot then sat 2016 bytes off for a 1920x1080
// page: every other picture came out 504 pixels to the side, wrapped around (2026-11-19).
struct SharedPictures {
    static constexpr size_t HEADER = 64;
    std::wstring name;
    HANDLE mapping = nullptr;
    const uint8_t *view = nullptr;
    size_t size = 0;
    uint64_t seen = 0;
    bool said = false;  // a block that won't open is said once
    sc::PageTexture page;  // the page's texture, when the helper draws it on the card (slot 2)
    bool page_said = false;
    int page_gone = 0;  // pictures in a row whose texture was closed: one after a page is shown again is expected
    static constexpr int PAGE_GONE_SAID = 3;

    SharedPictures() = default;
    SharedPictures(const SharedPictures &) = delete;
    SharedPictures &operator=(const SharedPictures &) = delete;
    ~SharedPictures() { close(); }

    void close() {
        if (view) UnmapViewOfFile(view);
        if (mapping) CloseHandle(mapping);
        view = nullptr;
        mapping = nullptr;
        size = 0;
        seen = 0;
    }
    bool open(const std::wstring &want) {
        if (name != want) said = false;
        close();
        name = want;
        mapping = OpenFileMappingW(FILE_MAP_READ, FALSE, want.c_str());
        if (mapping) view = static_cast<const uint8_t *>(MapViewOfFile(mapping, FILE_MAP_READ, 0, 0, 0));
        MEMORY_BASIC_INFORMATION about{};
        if (view && VirtualQuery(view, &about, sizeof about)) size = about.RegionSize;
        if (!view || size <= HEADER) {
            if (!said) {
                said = true;
                log_line(SC_LOG_WARNING, "engine: a browser page's pictures can't be read (error " +
                                             std::to_string(GetLastError()) + "); it keeps its picture");
            }
            close();
            return false;
        }
        return true;
    }
    uint64_t counter() const {
        const uint64_t value = *reinterpret_cast<const volatile uint64_t *>(view);
        std::atomic_thread_fence(std::memory_order_acquire);
        return value;
    }
    // A picture on the card: slot 2, then the number of the texture it is in (shared as "<block>-tex<n>").
    static constexpr uint32_t ON_CARD = 2;
    // The counter of a picture not taken yet, with where it is and how to read it; 0 when there is none. A
    // picture on the card gives no data: `texture` names its texture and `how` its size.
    uint64_t newest(const uint8_t **data, sc_upload *how, std::wstring *texture, uint32_t *flags) const {
        if (!view) return 0;
        const uint64_t seq = counter();
        if (seq == 0 || seq == seen) return 0;
        uint32_t head[5];
        std::memcpy(head, view + 8, sizeof head);
        *flags = head[4];  // sc::SHARED_*: 0 from a browser page
        if (head[2] == ON_CARD) {
            if (!head[0] || !head[1] || head[0] > 16384 || head[1] > 16384) return 0;
            *data = nullptr;
            *how = sc_upload{};
            how->format = SC_FMT_RGBA;
            how->width = static_cast<int>(head[0]);
            how->height = static_cast<int>(head[1]);
            *texture = name + L"-tex" + std::to_wstring(head[3]);
            return seq;
        }
        const size_t room = (size - HEADER) / 2;
        uint64_t slot_bytes = 0;
        std::memcpy(&slot_bytes, view + 32, sizeof slot_bytes);
        const size_t capacity = slot_bytes > 0 && slot_bytes <= room ? static_cast<size_t>(slot_bytes) : room;
        const size_t bytes = static_cast<size_t>(head[0]) * head[1] * 4;
        if (!head[0] || !head[1] || head[0] > 16384 || head[1] > 16384 || bytes > capacity || head[2] > 1) return 0;
        *data = view + HEADER + head[2] * capacity;
        *how = sc_upload{};
        how->format = SC_FMT_RGBA;
        how->width = static_cast<int>(head[0]);
        how->height = static_cast<int>(head[1]);
        how->stride = static_cast<int>(head[0]) * 4;
        return seq;
    }
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

struct HoldWant {  // shared: a still copy of a source's or a sprig's picture, for one frozen item
    uint64_t source = 0, scene = 0;
    uint64_t serial = 0;
};

struct PreviewCfg {
    int id = 0;
    int canvas = 0;
    HWND hwnd = nullptr;
    int max_fps = 0;
    float background[4] = {0, 0, 0, 1};
};

struct TransitionCfg {  // shared: the transition Python started last on a canvas
    uint64_t from = 0;
    sc_transition how{};
    std::shared_ptr<const sc::MovePlan> move;  // a Move's pieces (how.mode SC_TRANSITION_MOVE)
    uint64_t serial = 0;
};

struct RunningTransition {  // the frame thread's
    uint64_t from = 0;
    sc_transition how{};
    std::shared_ptr<const sc::MovePlan> move;
    double start = 0;
    uint64_t serial = 0;
    bool on = false;
};

struct Watch {  // shared: what Python asked for and the newest picture
    int width = 0, height = 0, max_fps = 0;
    std::vector<uint8_t> latest;
    int latest_w = 0, latest_h = 0;
    double drawn = 0;
    uint64_t seq = 0;
};

struct Reader {  // the frame thread's: a small picture and three copies on their way back
    sc::Target target;
    ComPtr<ID3D11Texture2D> staging[3];
    ComPtr<ID3D11Query> done[3];  // each copy's end: Map is tried only once the card says it's finished
    bool pending[3] = {false, false, false};
    double drawn[3] = {0, 0, 0};
    int next = 0;
    int64_t last = 0;
};

struct OutFrame {
    std::vector<uint8_t> data;
    double drawn = 0;
};

struct SharedFrame {  // a frame kept on the card: one of its pool's textures, with a reference held
    uint64_t pool = 0;
    int entry = 0;
    double drawn = 0;
    ID3D11Texture2D *texture = nullptr;
};

// The virtual camera's shared block (sc_engine_output_link_camera), mapped by the engine itself: whatever Python
// does with its own view of it, the frame thread writes only into this one, unmapped once the link has ended.
// Layout (output/virtualcam.py SharedBlock): magic u32 @0, frame width/height u32 @16/@20, a count u64 @24 that is
// odd while a frame is written, NV12 from byte 64.
struct CameraBlock {
    static constexpr uint32_t MAGIC = 0x43565053;
    static constexpr size_t HEADER = 64;
    HANDLE mapping = nullptr;
    uint8_t *view = nullptr;
    size_t size = 0;
    int width = 0, height = 0;  // the frame the camera shows: pictures of another shape go in the middle
    uint64_t written = 0;

    CameraBlock() = default;
    CameraBlock(const CameraBlock &) = delete;
    CameraBlock &operator=(const CameraBlock &) = delete;
    ~CameraBlock() {
        if (view) UnmapViewOfFile(view);
        if (mapping) CloseHandle(mapping);
    }
    bool open(const wchar_t *name) {
        mapping = OpenFileMappingW(FILE_MAP_READ | FILE_MAP_WRITE, FALSE, name);
        if (mapping) view = static_cast<uint8_t *>(MapViewOfFile(mapping, FILE_MAP_READ | FILE_MAP_WRITE, 0, 0, 0));
        MEMORY_BASIC_INFORMATION about{};
        if (view && VirtualQuery(view, &about, sizeof about)) size = about.RegionSize;
        return view && size > HEADER;
    }
    // One NV12 picture (w x h), in the middle of the camera's frame with black around it when it is smaller.
    bool write(const uint8_t *nv12, int w, int h) {
        const size_t bytes = static_cast<size_t>(width) * height * 3 / 2;
        if (!view || w > width || h > height || HEADER + bytes > size) return false;
        uint32_t magic = 0;
        std::memcpy(&magic, view, sizeof magic);
        if (magic != MAGIC) return false;  // the camera let go of it
        auto *count = reinterpret_cast<volatile uint64_t *>(view + 24);
        *count = *count + 1;  // odd: being written
        std::atomic_thread_fence(std::memory_order_release);
        const uint32_t size_wh[2] = {static_cast<uint32_t>(width), static_cast<uint32_t>(height)};
        std::memcpy(view + 16, size_wh, sizeof size_wh);
        uint8_t *y = view + HEADER, *uv = y + static_cast<size_t>(width) * height;
        if (w == width && h == height) {
            std::memcpy(y, nv12, bytes);
        } else {
            std::memset(y, 16, static_cast<size_t>(width) * height);  // black, limited range
            std::memset(uv, 128, static_cast<size_t>(width) * height / 2);
            const int x0 = ((width - w) / 2) & ~1, y0 = ((height - h) / 2) & ~1;
            for (int r = 0; r < h; ++r)
                std::memcpy(y + static_cast<size_t>(y0 + r) * width + x0, nv12 + static_cast<size_t>(r) * w, w);
            const uint8_t *src_uv = nv12 + static_cast<size_t>(w) * h;
            for (int r = 0; r < h / 2; ++r)
                std::memcpy(uv + static_cast<size_t>(y0 / 2 + r) * width + x0, src_uv + static_cast<size_t>(r) * w, w);
        }
        std::atomic_thread_fence(std::memory_order_release);
        *count = *count + 1;  // even: done
        ++written;
        return true;
    }
};

// An output linked to a writer (sc_engine_output_link): the frame thread hands it each finished frame itself.
struct Link {
    std::mutex use;                // held while a frame is handed over (and to change the fields below)
    std::shared_ptr<void> writer;  // the writer's insides (writer.h); empty once the link ended
    double start = 0, fill = 0;
    int fps_num = 30, fps_den = 1;
    int64_t last = -1;  // the last frame number handed over
    bool keep = false;  // still queue frames for sc_engine_output_take
    std::atomic<bool> ended{false};
    std::unique_ptr<CameraBlock> camera;  // instead of a writer: the virtual camera's block
    std::atomic<bool> camera_only{false};  // only the camera takes this output's frames (no writer, nothing queued)
};

struct Output {  // shared: an output Python asked for and its finished frames
    int width = 0, height = 0, scale = 0;
    bool share = false;   // frames stay on the card as NV12 textures (SC_OUTPUT_SHARE)
    uint64_t serial = 0;  // bumped on every change: frames made for an older setting are let go
    std::deque<OutFrame> ready;
    std::vector<std::vector<uint8_t>> spare;  // buffers to reuse
    uint64_t dropped = 0;
    int share_state = 0;  // 1: shared frames are being drawn, -1: this device can't
    uint64_t pool = 0;    // the texture pool the frame thread draws into now (0: none)
    bool busy[SC_ENGINE_SHARE_POOL] = {};  // waiting in `shared`, or out with Python until given back
    std::deque<SharedFrame> shared;
    std::shared_ptr<Link> link;  // the writer it feeds itself, if any
};

// Lets go of an output's shared frames and forgets its pool. `state` must be held.
void clear_shared(Output &o) {
    for (SharedFrame &f : o.shared) f.texture->Release();
    o.shared.clear();
    std::fill(std::begin(o.busy), std::end(o.busy), false);
    o.pool = 0;
}

// Hands a finished frame to a linked writer, numbered as Python's recorder numbers its frames (output/recorder.py
// _assign_pts): a draw a little late still gets the next number, a spare draw (a 60 fps canvas, a 30 fps file) is
// skipped, a draw really late leaves a gap, and a gap up to `fill` seconds is filled with this picture. Returns
// whether the frame should also be queued for Python (keep). Never throws: a link that fails just ends.
// The frame numbers a draw at `drawn` fills (first .. pts), or false for a spare draw. `use` must be held.
bool link_number(const Link &l, double drawn, int64_t &first, int64_t &pts) {
    const double fps = static_cast<double>(l.fps_num) / l.fps_den;
    const double x = (drawn - l.start) * fps;
    const int64_t expected = l.last + 1;
    if (x < static_cast<double>(expected) - 0.4) return false;
    if (l.last < 0) {
        pts = std::max<int64_t>(0, static_cast<int64_t>(std::floor(x + 0.3)));
    } else {
        pts = x < static_cast<double>(expected) + 0.9 ? expected : std::llround(x);
    }
    first = pts;
    if (l.last >= 0 && pts - l.last > 1 && static_cast<double>(pts - l.last - 1) <= l.fill * fps) first = l.last + 1;
    return true;
}

// What the writer said about a frame: the numbering goes on, or the link ends. `use` must be held.
void link_result(Link &l, int r, int64_t pts) {
    if (r == SC_WRITER_LATE) {
        l.last = std::max(pts, sc::writer_last(l.writer));
    } else if (r == SC_OK || r == SC_WRITER_DROPPED) {
        l.last = pts;
    } else {  // finished, failed, or not its size or device any more: the link ends
        sc::log_line(r == SC_WRITER_REFUSED ? SC_LOG_DEBUG : SC_LOG_WARNING,
                     r == SC_WRITER_REFUSED ? "output link: the writer is done; link ended"
                                            : "output link: the frame doesn't fit the writer (size or graphics card); "
                                              "link ended");
        l.writer.reset();
        l.ended = true;
    }
}

bool hand_over(Link &l, const OutFrame &f, int w, int h) {
    std::lock_guard<std::mutex> hold(l.use);
    if (l.camera) {
        try {
            l.camera->write(f.data.data(), w, h);
        } catch (...) {
        }
        return l.keep;
    }
    if (!l.writer) return true;
    try {
        int64_t first = 0, pts = 0;
        if (!link_number(l, f.drawn, first, pts)) return l.keep;
        link_result(l, sc::writer_feed(l.writer, f.data.data(), w, h, first, pts), pts);
        if (l.ended) return true;
    } catch (...) {
        sc::log_line(SC_LOG_WARNING, "output link: handing a frame to the writer failed; link ended");
        l.writer.reset();
        l.ended = true;
        return true;
    }
    return l.keep;
}

// Lets the writer give a texture back to the engine once its encoder is done, even after the engine is gone.
// The writer only queues what it gives back (any thread, maybe while the engine's state lock is held by whatever
// let go of the writer's last share); the frame thread takes them back before looking for a free texture.
struct Alive {
    struct Returned {
        int index, slot;
        uint64_t pool;
        int entry;
    };
    std::mutex m;
    sc_engine *engine = nullptr;
    std::vector<Returned> returned;
};

// A shared output's texture, to a linked writer whose encoder takes frames on the card. 1: the writer has it (it
// gives it back through `alive` when done). 0: a spare draw or a frame the writer didn't keep: give it back now.
// -1: the link ended: Python takes this frame.
int hand_over_texture(Link &l, const std::shared_ptr<Alive> &alive, int index, int slot, uint64_t pool, int entry,
                      ID3D11Texture2D *texture, double drawn);

struct OutputWant {  // the frame thread's copy of what Python asked for
    int width = 0, height = 0, scale = 0;
    uint64_t serial = 0;
    bool share = false;
};

struct PoolEntry {
    ComPtr<ID3D11Texture2D> texture;  // NV12
    ComPtr<ID3D11RenderTargetView> y, uv;
};

struct OutReader {  // the frame thread's: an output's two planes and three copies of them on their way back
    sc::Nv12Planes planes;
    ComPtr<ID3D11Texture2D> ys[3], uvs[3];
    ComPtr<ID3D11Query> done[3];  // as Reader's
    bool pending[3] = {false, false, false};
    double drawn[3] = {0, 0, 0};
    uint64_t serial[3] = {0, 0, 0};
    int next = 0;
    double camera_at = -1e9;  // when the last frame for the virtual camera alone was drawn
    // A shared output's instead: NV12 textures an encoder reads on the card.
    uint64_t pool = 0;
    int pool_w = 0, pool_h = 0;
    uint64_t pool_serial = 0;  // the output setting the pool was made for
    PoolEntry entries[SC_ENGINE_SHARE_POOL];
};

struct PreviewGpu {
    ComPtr<IDXGISwapChain1> chain;
    ComPtr<ID3D11RenderTargetView> target;
    int width = 0, height = 0;
    int64_t last = 0;      // when it was last shown
    int64_t retry_at = 0;  // after a failure: when to try making it again
};

}  // namespace

struct sc_engine {
    int adapter = -1, flags = 0;
    int64_t born = 0;

    std::shared_ptr<Alive> alive = std::make_shared<Alive>();

    // ---- shared with Python's threads: guarded by `state` ----
    std::mutex state;
    std::condition_variable changed;  // a frame finished, or a preview was let go
    int rate_num = 60, rate_den = 1;
    bool rate_changed = true;
    CanvasCfg canvases[SC_ENGINE_CANVASES];
    Watch watches[SC_ENGINE_CANVASES];
    TransitionCfg transitions[SC_ENGINE_CANVASES];
    Output outputs[SC_ENGINE_CANVASES][SC_ENGINE_OUTPUTS];
    std::unordered_map<uint64_t, Pic> uploads;                  // the newest picture per source, not yet sent
    std::unordered_map<uint64_t, std::vector<uint8_t>> spare;   // buffers to reuse for the next picture
    std::set<uint64_t> forgets;
    std::unordered_map<uint64_t, std::shared_ptr<void>> medias;  // sources shown straight from a media player
    // A media source laid out at another size than its player's picture (sc_engine_source_media_size): one camera
    // opened once for two sources asking 1080p and 720p gives both the 1080p picture, the second shown as 720p.
    std::unordered_map<uint64_t, std::pair<int, int>> media_sizes;
    std::unordered_map<uint64_t, std::pair<int, int>> media_sized;  // frame thread: the size its picture has now
    std::unordered_map<uint64_t, std::wstring> shareds;          // sources shown from another process's memory
    std::set<uint64_t> shared_held;  // shareds held still (a freeze): their pictures taken, not shown
    std::unordered_map<uint64_t, std::shared_ptr<void>> visuals; // sources an audio visualizer draws (visual.cpp)
    std::unordered_map<uint64_t, CaptureWant> capture_wants;     // sources captured in the engine (capture_wgc.cpp)
    std::set<uint64_t> capture_held;                             // captures held still (a freeze)
    std::unordered_map<uint64_t, CaptureShown> capture_states;
    uint64_t next_capture = 1;
    std::unordered_map<uint64_t, DrawList> draws;                // the newest list per source, not yet sent
    std::unordered_map<uint64_t, LookData> looks;
    std::unordered_map<uint64_t, MaskData> masks;
    std::shared_ptr<const SnapData> snapshot;
    bool snapshot_new = false;
    std::vector<PreviewCfg> previews;
    int next_preview = 1;
    int fault = 0;
    ComPtr<ID3D11Device> device_out;  // the device as encoders get it (sc_engine_device); none while rebuilding
    uint64_t detach_asked = 0, detach_done = 0;
    sc_engine_stats stats{};
    std::unordered_map<uint64_t, HoldWant> holds;  // still copies asked for (sc_engine_source_hold), by their id
    uint64_t hold_serial = 0;
    int read_asks[SC_ENGINE_CANVASES] = {};  // frames each canvas is still drawn for a sc_engine_canvas_read

    // ---- the frame thread's own (canvas reads borrow them under `frame`) ----
    std::timed_mutex frame;  // held by a frame; Python's calls that need it wait 0.5 s at most
    sc_device *dev = nullptr;
    sc_canvas *gpu[SC_ENGINE_CANVASES] = {};
    std::unordered_map<uint64_t, uint64_t> holds_made;  // still copy -> the ask (HoldWant.serial) it was made for
    uint32_t canvas_draws = 0;  // canvases drawn so far, over all frames (stats.canvas_draws)
    // A canvas whose picture is still the one drawn last: nothing it shows changed since (draw_frame).
    struct Still {
        bool valid = false;
        uint64_t scene = 0;
        int64_t drawn = 0;  // qpc() when it was last really drawn
        uint64_t why = 0;   // what made it need drawing, while it waits for a frame someone takes (SC_REDRAW_*, a source)
    };
    Still still[SC_ENGINE_CANVASES];
    // Canvases drawn again because of this source (its new picture, or a filter that moves), since Python last asked
    // (sc_engine_redraws_take): what keeps a bloom from being still. Under `state`.
    std::unordered_map<uint64_t, uint32_t> redrawn_by;
    std::unordered_map<int, PreviewGpu> shown;
    Reader readers[SC_ENGINE_CANVASES];
    OutReader out_readers[SC_ENGINE_CANVASES][SC_ENGINE_OUTPUTS];
    RunningTransition running[SC_ENGINE_CANVASES];
    std::unordered_map<uint64_t, Pic> kept_sources;
    std::unordered_map<uint64_t, DrawList> kept_draws;  // the lists drawn now, sent again after a rebuild
    std::unordered_map<uint64_t, std::pair<const void *, uint64_t>> media_seen;  // source -> (player, serial taken)
    std::unordered_map<uint64_t, SharedPictures> shared_open;  // the blocks of `shareds`, opened by the frame thread
    std::unordered_map<uint64_t, CaptureRun> capture_runs;      // the captures of `capture_wants`
    std::unordered_map<uint64_t, ComPtr<ID3D11Texture2D>> capture_staging;  // sc_engine_capture_read's, under `frame`
    std::unordered_map<uint64_t, LookData> kept_looks;
    std::unordered_map<uint64_t, MaskData> kept_masks;
    std::shared_ptr<const SnapData> kept_snapshot;
    std::deque<int64_t> rebuild_times;
    uint64_t next_pool = 1;
    bool lost = false;
    int build_failures = 0;
    int64_t next_build = 0;
    double trimmed_at = 0;  // when idle staging textures were last let go of (sc::trim_staging)

    // ---- threads ----
    HANDLE quit = nullptr;  // manual reset: everything stops
    HANDLE wake = nullptr;  // auto reset: something changed, look again now
    HANDLE threads[2] = {nullptr, nullptr};
    std::atomic<bool> stop{false};
    std::atomic<bool> dead{false};          // faulted for good
    std::atomic<int64_t> tick_started{0};   // 0 = between frames
    std::atomic<bool> hung{false};
    std::atomic<const char *> doing{"between frames"};  // what the frame thread is on: the watchdog says it
    std::atomic<uint64_t> doing_id{0};                   // and for which source, when it's one
};

namespace {

// A frame of a Move on a canvas, under the crash gate: the scene it moves to, once the one it moves from is gone
// from the snapshot (as sc_canvas_render_transition does).
int render_move_on(sc_canvas *c, uint64_t from, uint64_t to, const sc::MovePlan &plan, double seconds,
                   float progress) {
    return sc::device_call(c->device, "Move", [&]() -> int {
        std::lock_guard<std::mutex> lock(c->device->lock);
        if (!sc::has_scene(*c->device, from))
            return sc::render_scene(*c->device, c->target.Get(), c->width, c->height, to, seconds);
        return sc::render_move(*c, plan, seconds, progress);
    });
}

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
void at(sc_engine &e, const char *what, uint64_t id = 0) {
    e.doing_id.store(id, std::memory_order_relaxed);
    e.doing.store(what, std::memory_order_release);
}

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
    for (auto &[id, run] : e.capture_runs) {  // made again on the new device (same serial)
        run.cap.reset();
        run.starting.reset();
    }
    e.capture_staging.clear();
    e.shown.clear();
    e.media_seen.clear();  // a new device gets each player's picture again
    for (auto &[id, block] : e.shared_open) {  // and each shared block's, its texture opened on the new device
        block.seen = 0;
        block.page = sc::PageTexture();
    }
    for (auto &r : e.readers) r = Reader();
    for (auto &row : e.out_readers)
        for (auto &r : row) r = OutReader();
    {
        std::lock_guard<std::mutex> lock(e.state);
        for (auto &row : e.outputs)
            for (Output &o : row) clear_shared(o);
        e.device_out.Reset();
    }
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
        media_card_gone(e.dev->device.Get());
        sc::writers_leave_device(e.dev->device.Get());  // encoders on it move to memory: the device can go
        sc_device_destroy(e.dev);
        e.dev = nullptr;
    }
}

void give_up(sc_engine &e, const std::string &why) {
    drop_gpu(e);
    e.kept_sources.clear();
    e.kept_draws.clear();
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
    e.holds_made.clear();  // a new device has none of the still copies: made again from the pictures then
    if (e.lost) {
        e.lost = false;
        for (auto &[id, pic] : e.kept_sources) check(sc_source_upload(e.dev, id, pic.data.data(), &pic.how));
        for (auto &[id, d] : e.kept_draws)
            check(sc_source_draws(e.dev, id, d.width, d.height, d.items.data(), d.count));
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
    media_card(e.dev->device.Get());  // videos reopened from now on decode on the new device
    std::lock_guard<std::mutex> lock(e.state);
    e.device_out = e.dev->device;
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

// Whether a preview limited to max_fps pictures a second, last given one at `last`, takes one now. The one rule
// for read_back, show_preview and canvas_taken, so a canvas is drawn on exactly the frames its picture is taken.
bool picture_due(int max_fps, int64_t last, int64_t now) {
    return !(max_fps > 0 && last && (now - last) < qpf() * 9 / (10 * max_fps));
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
    if (!picture_due(cfg.max_fps, pg.last, now)) return;
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

// Whether a copy started earlier has finished on the card. Map with DO_NOT_WAIT should never wait, but the
// software renderer (WARP) can still hold the frame thread there for seconds while it catches up; asking an
// event query never waits on any driver. No query (it couldn't be made): Map decides, as before.
bool copy_done(sc_device &dev, ID3D11Query *query) {
    if (!query) return true;
    const HRESULT hr = dev.context->GetData(query, nullptr, 0, 0);
    if (is_lost(hr)) throw Gone("the graphics card was reset or removed (read-back)");
    return hr != S_FALSE;
}

// Marks the end of a copy just started, so copy_done can tell when it's finished.
void mark_copy(sc_device &dev, ComPtr<ID3D11Query> &query) {
    if (!query) {
        D3D11_QUERY_DESC qd{D3D11_QUERY_EVENT, 0};
        if (FAILED(dev.device->CreateQuery(&qd, &query))) query.Reset();
    }
    if (query) dev.context->End(query.Get());
}

// Copies of watched canvases: draw the small picture, start its copy back, and collect copies that are done,
// never waiting on the card (a copy still on its way is looked at again next frame).
void read_back(sc_engine &e, int index, const sc_canvas *canvas, const Watch &want, double seconds, int64_t now) {
    Reader &r = e.readers[index];
    sc_device &dev = *e.dev;
    at(e, "read-back: waiting for the device", static_cast<uint64_t>(index) + 1);
    std::lock_guard<std::mutex> lock(dev.lock);
    if (!canvas || want.width <= 0) {
        r = Reader();
        return;
    }
    at(e, "read-back: collecting finished copies", static_cast<uint64_t>(index) + 1);
    for (int k = 0; k < 3; ++k) {  // oldest first
        const int s = (r.next + k) % 3;
        if (!r.pending[s]) continue;
        if (!copy_done(dev, r.done[s].Get())) break;
        D3D11_MAPPED_SUBRESOURCE mapped;
        HRESULT hr = dev.context->Map(r.staging[s].Get(), 0, D3D11_MAP_READ, D3D11_MAP_FLAG_DO_NOT_WAIT, &mapped);
        if (hr == DXGI_ERROR_WAS_STILL_DRAWING) break;
        if (is_lost(hr)) throw Gone("the graphics card was reset or removed (read-back)");
        r.pending[s] = false;
        if (FAILED(hr)) {
            fail_hr(SC_ERR_DEVICE, "Map (canvas picture)", hr);
            continue;
        }
        const int w = r.target.width, h = r.target.height;
        std::vector<uint8_t> picture;
        {
            std::lock_guard<std::mutex> st(e.state);
            picture.swap(e.watches[index].latest);  // reuse the last buffer
        }
        picture.resize(static_cast<size_t>(w) * h * 4);
        const auto *src = static_cast<const uint8_t *>(mapped.pData);
        for (int y = 0; y < h; ++y)
            std::memcpy(picture.data() + static_cast<size_t>(y) * w * 4, src + static_cast<size_t>(y) * mapped.RowPitch,
                        static_cast<size_t>(w) * 4);
        dev.context->Unmap(r.staging[s].Get(), 0);
        std::lock_guard<std::mutex> st(e.state);
        Watch &out = e.watches[index];
        out.latest.swap(picture);
        out.latest_w = w;
        out.latest_h = h;
        out.drawn = r.drawn[s];
        ++out.seq;
    }
    if (!picture_due(want.max_fps, r.last, now)) return;
    if (r.target.width != want.width || r.target.height != want.height) {
        r = Reader();
        check(make_target(dev.device.Get(), want.width, want.height, r.target));
        if (!r.target.texture) return;
        D3D11_TEXTURE2D_DESC td;
        r.target.texture->GetDesc(&td);
        td.Usage = D3D11_USAGE_STAGING;
        td.BindFlags = 0;
        td.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
        for (auto &staging : r.staging) {
            HRESULT hr = dev.device->CreateTexture2D(&td, nullptr, &staging);
            if (is_lost(hr)) throw Gone("the graphics card was reset or removed (read-back)");
            if (FAILED(hr)) {
                fail_hr(SC_ERR_DEVICE, "Creating a canvas picture's copy", hr);
                r = Reader();
                return;
            }
        }
    }
    const int s = r.next;
    if (r.pending[s]) return;  // all three still on their way: the card is behind, skip this one
    at(e, "read-back: drawing the small picture", static_cast<uint64_t>(index) + 1);
    check(draw_view(dev, r.target.target.Get(), want.width, want.height, canvas->view.Get(), 0, 0,
                    static_cast<float>(want.width), static_cast<float>(want.height), 1.0f, false));
    at(e, "read-back: starting its copy", static_cast<uint64_t>(index) + 1);
    dev.context->CopyResource(r.staging[s].Get(), r.target.texture.Get());
    mark_copy(dev, r.done[s]);
    r.pending[s] = true;
    r.drawn[s] = seconds;
    r.next = (s + 1) % 3;
    r.last = now;
}

// A shared output's frame: drawn as NV12 into a free texture of its pool and queued for the encoder, which reads
// it on the card. No free texture (the encoder is behind): this frame is missed and counted.
void share_back(sc_engine &e, int index, int slot, const sc_canvas *canvas, const OutputWant &want, double drawn) {
    OutReader &r = e.out_readers[index][slot];
    sc_device &dev = *e.dev;
    auto cannot = [&] {
        r = OutReader();
        std::lock_guard<std::mutex> st(e.state);
        Output &out = e.outputs[index][slot];
        clear_shared(out);
        if (out.serial == want.serial) out.share_state = -1;
    };
    if (!dev.nv12_targets) return cannot();
    // A new setting gets a new pool, even at the same size: Python let go of the old one (crash run 2026-11-17: the
    // outputs were made again while this thread was stuck, so it never saw them stop, kept its old pool, and every
    // frame after that went nowhere, with nothing counted).
    if (!r.pool || r.pool_w != want.width || r.pool_h != want.height || r.pool_serial != want.serial) {
        r = OutReader();
        {
            std::lock_guard<std::mutex> st(e.state);
            clear_shared(e.outputs[index][slot]);
        }
        D3D11_TEXTURE2D_DESC td = {};
        td.Width = static_cast<UINT>(want.width);
        td.Height = static_cast<UINT>(want.height);
        td.MipLevels = 1;
        td.ArraySize = 1;
        td.Format = DXGI_FORMAT_NV12;
        td.SampleDesc.Count = 1;
        td.Usage = D3D11_USAGE_DEFAULT;
        td.BindFlags = D3D11_BIND_RENDER_TARGET;
        for (PoolEntry &entry : r.entries) {
            HRESULT hr = dev.device->CreateTexture2D(&td, nullptr, &entry.texture);
            D3D11_RENDER_TARGET_VIEW_DESC rd = {};
            rd.ViewDimension = D3D11_RTV_DIMENSION_TEXTURE2D;
            rd.Format = DXGI_FORMAT_R8_UNORM;  // the Y plane
            if (SUCCEEDED(hr)) hr = dev.device->CreateRenderTargetView(entry.texture.Get(), &rd, &entry.y);
            rd.Format = DXGI_FORMAT_R8G8_UNORM;  // the CbCr plane
            if (SUCCEEDED(hr)) hr = dev.device->CreateRenderTargetView(entry.texture.Get(), &rd, &entry.uv);
            if (is_lost(hr) || (FAILED(hr) && FAILED(dev.device->GetDeviceRemovedReason())))
                throw Gone("the graphics card was reset or removed (shared output)");
            if (FAILED(hr)) {
                fail_hr(SC_ERR_DEVICE, "Making an output's NV12 textures", hr);
                return cannot();
            }
        }
        r.pool = e.next_pool++;
        r.pool_w = want.width;
        r.pool_h = want.height;
        r.pool_serial = want.serial;
        std::lock_guard<std::mutex> st(e.state);
        Output &out = e.outputs[index][slot];
        if (out.serial == want.serial) {
            out.pool = r.pool;
            out.share_state = 1;
        }
    }
    int free = -1;
    {
        std::lock_guard<std::mutex> st(e.state);
        Output &out = e.outputs[index][slot];
        if (out.pool != r.pool) {  // Python changed the output: the next frame starts over (and says so)
            if (out.serial == want.serial) ++out.dropped;
            return;
        }
        {
            std::lock_guard<std::mutex> a(e.alive->m);  // textures a linked writer's encoder is done with
            for (const Alive::Returned &back : e.alive->returned) {
                Output &o = e.outputs[back.index][back.slot];
                if (o.pool == back.pool) o.busy[back.entry] = false;
            }
            e.alive->returned.clear();
        }
        for (int i = 0; i < SC_ENGINE_SHARE_POOL && free < 0; ++i) {
            if (!out.busy[i]) free = i;
        }
        if (free < 0) {
            ++out.dropped;
            return;
        }
    }
    const int drew = draw_nv12(dev, canvas->view.Get(), canvas->width, canvas->height, r.entries[free].y.Get(),
                               r.entries[free].uv.Get(), want.width, want.height, want.scale);
    check(drew);
    if (drew != SC_OK) return;  // nothing worth encoding in that texture
    dev.context->Flush();  // sent to the card before the encoder's thread asks for it
    std::shared_ptr<Link> link;
    {
        std::lock_guard<std::mutex> st(e.state);
        Output &out = e.outputs[index][slot];
        if (out.pool != r.pool) return;
        link = out.link;
        if (link) out.busy[free] = true;  // the writer's until it gives it back
    }
    if (link) {  // a writer whose encoder takes frames on the card: straight to it, nothing through Python
        const int taken =
            hand_over_texture(*link, e.alive, index, slot, r.pool, free, r.entries[free].texture.Get(), drawn);
        std::lock_guard<std::mutex> st(e.state);
        Output &out = e.outputs[index][slot];
        if (link->ended.load() && out.link == link) out.link.reset();
        if (taken == 1) return;
        if (out.pool != r.pool) return;
        out.busy[free] = false;
        if (taken == 0) return;
    }
    std::lock_guard<std::mutex> st(e.state);
    Output &out = e.outputs[index][slot];
    if (out.pool != r.pool) return;
    out.busy[free] = true;
    r.entries[free].texture->AddRef();
    out.shared.push_back(SharedFrame{r.pool, free, drawn, r.entries[free].texture.Get()});
    while (out.shared.size() > SC_ENGINE_SHARE_QUEUE) {  // Python fell behind: the oldest goes
        SharedFrame old = out.shared.front();
        out.shared.pop_front();
        out.busy[old.entry] = false;
        old.texture->Release();
        ++out.dropped;
    }
}

// An output's frames: collect the copies that are done into its queue, then draw this frame as NV12 and start its
// copy back, never waiting on the card (a copy still on its way is looked at again next frame).
void output_back(sc_engine &e, int index, int slot, const sc_canvas *canvas, const OutputWant &want, double drawn) {
    OutReader &r = e.out_readers[index][slot];
    sc_device &dev = *e.dev;
    std::lock_guard<std::mutex> lock(dev.lock);
    if (!canvas || want.width <= 0 || want.share != (r.pool != 0)) {
        const bool shared = r.pool != 0;
        r = OutReader();
        if (shared) {
            std::lock_guard<std::mutex> st(e.state);
            clear_shared(e.outputs[index][slot]);
        }
        if (!canvas || want.width <= 0) return;
    }
    if (want.share) return share_back(e, index, slot, canvas, want, drawn);
    const int w = r.planes.width, h = r.planes.height;
    for (int k = 0; k < 3; ++k) {  // oldest first
        const int s = (r.next + k) % 3;
        if (!r.pending[s]) continue;
        if (!copy_done(dev, r.done[s].Get())) break;
        D3D11_MAPPED_SUBRESOURCE my, muv;
        HRESULT hr = dev.context->Map(r.ys[s].Get(), 0, D3D11_MAP_READ, D3D11_MAP_FLAG_DO_NOT_WAIT, &my);
        if (hr == DXGI_ERROR_WAS_STILL_DRAWING) break;
        if (is_lost(hr)) throw Gone("the graphics card was reset or removed (output)");
        if (SUCCEEDED(hr)) {
            hr = dev.context->Map(r.uvs[s].Get(), 0, D3D11_MAP_READ, D3D11_MAP_FLAG_DO_NOT_WAIT, &muv);
            if (hr == DXGI_ERROR_WAS_STILL_DRAWING) {
                dev.context->Unmap(r.ys[s].Get(), 0);
                break;
            }
            if (FAILED(hr)) dev.context->Unmap(r.ys[s].Get(), 0);
        }
        if (is_lost(hr)) throw Gone("the graphics card was reset or removed (output)");
        r.pending[s] = false;
        if (FAILED(hr)) {
            fail_hr(SC_ERR_DEVICE, "Map (output frame)", hr);
            continue;
        }
        OutFrame frame;
        {
            std::lock_guard<std::mutex> st(e.state);
            auto &spare = e.outputs[index][slot].spare;
            if (!spare.empty()) {
                frame.data.swap(spare.back());
                spare.pop_back();
            }
        }
        frame.data.resize(static_cast<size_t>(w) * h * 3 / 2);
        copy_nv12(my, muv, w, h, frame.data.data());
        dev.context->Unmap(r.uvs[s].Get(), 0);
        dev.context->Unmap(r.ys[s].Get(), 0);
        frame.drawn = r.drawn[s];
        std::shared_ptr<Link> link;
        {
            std::lock_guard<std::mutex> st(e.state);
            const Output &out = e.outputs[index][slot];
            if (out.serial != r.serial[s]) continue;  // made for a setting Python has since changed
            link = out.link;
        }
        // Outside `state`: the copy into the writer never holds up Python's calls.
        const bool keep = !link || hand_over(*link, frame, w, h);
        std::lock_guard<std::mutex> st(e.state);
        Output &out = e.outputs[index][slot];
        if (link && link->ended.load() && out.link == link) out.link.reset();
        if (out.serial != r.serial[s]) continue;
        if (!keep) {  // the writer has it: nobody takes it here
            if (out.spare.size() < 2) out.spare.push_back(std::move(frame.data));
            continue;
        }
        out.ready.push_back(std::move(frame));
        while (out.ready.size() > SC_ENGINE_OUTPUT_QUEUE) {  // Python fell behind: the oldest goes
            out.spare.push_back(std::move(out.ready.front().data));
            out.ready.pop_front();
            ++out.dropped;
        }
    }
    if (r.planes.width != want.width || r.planes.height != want.height) {
        r = OutReader();
        check(make_nv12(dev.device.Get(), want.width, want.height, r.planes));
        if (!r.planes.y.texture) return;
        for (int s = 0; s < 3; ++s) {
            int code = make_staging(dev.device.Get(), r.planes.y.texture.Get(), r.ys[s]);
            if (code == SC_OK) code = make_staging(dev.device.Get(), r.planes.uv.texture.Get(), r.uvs[s]);
            if (code != SC_OK) {
                if (FAILED(dev.device->GetDeviceRemovedReason()))
                    throw Gone("the graphics card was reset or removed (output)");
                r = OutReader();
                return;
            }
        }
    }
    // The virtual camera shows 30 pictures a second (native/camera FRAME): an output only it takes is drawn and
    // copied back at most that often, not at the canvas's 60. Half the vertical canvas's output work on the PC, whose
    // vertical canvas fed only TikTok LIVE Studio through the camera (2026-11-21: 12-19 ms of a frame).
    bool camera_only = false;
    {
        std::lock_guard<std::mutex> st(e.state);
        const std::shared_ptr<Link> &l = e.outputs[index][slot].link;
        camera_only = l && !l->ended.load() && l->camera_only.load();
    }
    if (camera_only) {
        if (drawn - r.camera_at < CAMERA_GAP_S) return;
        r.camera_at = drawn;
    }
    const int s = r.next;
    if (r.pending[s]) {  // all three still on their way: the card is behind, this frame is missed
        std::lock_guard<std::mutex> st(e.state);
        ++e.outputs[index][slot].dropped;
        return;
    }
    check(draw_nv12(dev, canvas->view.Get(), canvas->width, canvas->height, r.planes.y.target.Get(),
                    r.planes.uv.target.Get(), want.width, want.height, want.scale));
    dev.context->CopyResource(r.ys[s].Get(), r.planes.y.texture.Get());
    dev.context->CopyResource(r.uvs[s].Get(), r.planes.uv.texture.Get());
    mark_copy(dev, r.done[s]);
    r.pending[s] = true;
    r.drawn[s] = drawn;
    r.serial[s] = want.serial;
    r.next = (s + 1) % 3;
}

int hand_over_texture(Link &l, const std::shared_ptr<Alive> &alive, int index, int slot, uint64_t pool, int entry,
                      ID3D11Texture2D *texture, double drawn) {
    std::lock_guard<std::mutex> hold(l.use);
    if (!l.writer) return -1;
    try {
        int64_t first = 0, pts = 0;
        if (!link_number(l, drawn, first, pts)) return 0;
        auto done = [alive, index, slot, pool, entry] {
            std::lock_guard<std::mutex> a(alive->m);
            if (alive->engine) alive->returned.push_back(Alive::Returned{index, slot, pool, entry});
        };
        const int r = sc::writer_feed_texture(l.writer, texture, first, pts, done);
        link_result(l, r, pts);
        if (r == SC_OK) return 1;
        return l.ended ? -1 : 0;
    } catch (...) {
        sc::log_line(SC_LOG_WARNING, "output link: handing a texture to the writer failed; link ended");
        l.writer.reset();
        l.ended = true;
        return -1;
    }
}

// Whether anything takes this canvas's picture this frame: an output (a recording, stream or the virtual camera
// takes every frame), a watch whose next copy is due, or a preview window whose next picture is due.
bool canvas_taken(sc_engine &e, int index, const Watch &watch, const OutputWant *outs,
                  const std::vector<PreviewCfg> &previews, int64_t now) {
    for (int o = 0; o < SC_ENGINE_OUTPUTS; ++o) {
        if (outs[o].width > 0) return true;
    }
    if (watch.width > 0 && picture_due(watch.max_fps, e.readers[index].last, now)) return true;
    for (const PreviewCfg &cfg : previews) {
        if (cfg.canvas != index) continue;
        const auto found = e.shown.find(cfg.id);
        if (found == e.shown.end()) return true;
        if (now >= found->second.retry_at && picture_due(cfg.max_fps, found->second.last, now)) return true;
    }
    return false;
}

// A source with no picture from memory this long lets its staging go. 3 s (#920) churned: a video or page that
// paused a few seconds made its staging again and again, and on the software renderer (WARP, the build's
// start-up check, 2026-11-24) an upload or read-back right after stalled the frame thread 2.2-2.6 s, in 3 of
// 5 runs. Off-air pages still give theirs back, a minute later.
constexpr double STAGING_IDLE_S = 60.0;
constexpr double STAGING_TRIM_EVERY_S = 10.0;

// One frame: take Python's work, send it to the card, draw every canvas something takes a picture of, show the
// previews.
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

    at(e, "making the graphics device");
    if (!e.dev && !build(e)) {
        std::lock_guard<std::mutex> lock(e.state);
        e.detach_done = e.detach_asked;  // no device, no windows held
        return;
    }
    {  // now and then: staging textures of sources that stopped getting pictures from memory go
        const double t = sc::steady_s();
        if (t - e.trimmed_at >= STAGING_TRIM_EVERY_S) {
            e.trimmed_at = t;
            sc::trim_staging(*e.dev, t, STAGING_IDLE_S);
        }
    }

    CanvasCfg canvases[SC_ENGINE_CANVASES];
    Watch watches[SC_ENGINE_CANVASES];
    TransitionCfg transitions[SC_ENGINE_CANVASES];
    OutputWant outputs[SC_ENGINE_CANVASES][SC_ENGINE_OUTPUTS];
    std::unordered_map<uint64_t, Pic> uploads;
    std::unordered_map<uint64_t, DrawList> draws;
    std::set<uint64_t> forgets;
    std::unordered_map<uint64_t, LookData> looks;
    std::unordered_map<uint64_t, MaskData> masks;
    std::shared_ptr<const SnapData> snapshot;
    std::vector<PreviewCfg> previews;
    std::vector<std::pair<uint64_t, std::shared_ptr<void>>> medias;
    std::unordered_map<uint64_t, std::pair<int, int>> media_sizes;
    std::vector<std::pair<uint64_t, std::shared_ptr<void>>> visuals;
    std::vector<std::pair<uint64_t, std::wstring>> shareds;
    std::set<uint64_t> shared_held;
    std::vector<std::pair<uint64_t, CaptureWant>> captures;
    std::set<uint64_t> captures_held;
    std::vector<std::pair<uint64_t, CaptureShown>> capture_news;
    uint64_t detach_seen;
    bool read_asked[SC_ENGINE_CANVASES] = {};
    std::vector<std::pair<uint64_t, HoldWant>> holds;
    // What has a new picture this frame (sources, lists, still copies), or everything (a new snapshot, look, mask,
    // a source let go): a canvas showing none of it keeps the picture it has (see the canvases below).
    std::unordered_set<uint64_t> changed;
    bool all_changed = false;
    at(e, "taking what Python sent");
    {
        std::lock_guard<std::mutex> lock(e.state);
        holds.assign(e.holds.begin(), e.holds.end());
        for (int i = 0; i < SC_ENGINE_CANVASES; ++i) {
            read_asked[i] = e.read_asks[i] > 0;
            if (read_asked[i]) --e.read_asks[i];
        }
        medias.assign(e.medias.begin(), e.medias.end());
        media_sizes = e.media_sizes;
        visuals.assign(e.visuals.begin(), e.visuals.end());
        shareds.assign(e.shareds.begin(), e.shareds.end());
        shared_held = e.shared_held;
        captures.assign(e.capture_wants.begin(), e.capture_wants.end());
        captures_held = e.capture_held;
        std::copy(std::begin(e.canvases), std::end(e.canvases), canvases);
        std::copy(std::begin(e.transitions), std::end(e.transitions), transitions);
        for (int i = 0; i < SC_ENGINE_CANVASES; ++i) {
            watches[i].width = e.watches[i].width;
            watches[i].height = e.watches[i].height;
            watches[i].max_fps = e.watches[i].max_fps;
            for (int o = 0; o < SC_ENGINE_OUTPUTS; ++o) {
                const Output &out = e.outputs[i][o];
                outputs[i][o] = OutputWant{out.width, out.height, out.scale, out.serial, out.share};
            }
        }
        uploads.swap(e.uploads);
        draws.swap(e.draws);
        forgets.swap(e.forgets);
        looks.swap(e.looks);
        masks.swap(e.masks);
        if (e.snapshot_new) snapshot = e.snapshot;
        e.snapshot_new = false;
        previews = e.previews;
        detach_seen = e.detach_asked;
    }

    if (!forgets.empty() || !looks.empty() || !masks.empty() || snapshot) all_changed = true;
    for (uint64_t id : forgets) {
        at(e, "letting a source go", id);
        e.kept_sources.erase(id);
        e.kept_draws.erase(id);
        check(sc_source_forget(e.dev, id));
    }
    // Videos, GIFs and sound files the engine plays: the picture due now, straight from the player.
    for (auto found = e.media_seen.begin(); found != e.media_seen.end();) {
        auto same = [&](const auto &m) { return m.first == found->first; };
        const bool here = std::any_of(medias.begin(), medias.end(), same) ||
                          std::any_of(visuals.begin(), visuals.end(), same);
        found = here ? std::next(found) : e.media_seen.erase(found);
    }
    for (auto found = e.media_sized.begin(); found != e.media_sized.end();) {
        const auto same = [&](const auto &m) { return m.first == found->first; };
        found = std::any_of(medias.begin(), medias.end(), same) ? std::next(found) : e.media_sized.erase(found);
    }
    for (auto &[id, player] : medias) {
        auto &seen = e.media_seen[id];
        if (seen.first != player.get()) seen = {player.get(), 0};
        const auto sized = media_sizes.find(id);
        const std::pair<int, int> size = sized == media_sizes.end() ? std::pair<int, int>{0, 0} : sized->second;
        auto &has = e.media_sized[id];
        if (has != size) {  // a new size: the picture it has is taken again, laid out at it
            has = size;
            seen.second = 0;
        }
        MediaDue due;
        at(e, "asking a video for its picture", id);
        media_due(player, &seen.second, &due);
        if (due.what == SC_MEDIA_PICTURE && size.first > 0 && size.second > 0) {
            due.how.layout_width = size.first;
            due.how.layout_height = size.second;
        }
        if (due.what == SC_MEDIA_PICTURE || due.what == SC_MEDIA_BLANK) changed.insert(id);
        if (due.what == SC_MEDIA_PICTURE && due.texture) {  // still on the card, where the decoder left it
            e.kept_sources.erase(id);
            at(e, "taking a video's picture on the card", id);
            ComPtr<ID3D11Device> on;
            auto *texture = static_cast<ID3D11Texture2D *>(due.texture);
            texture->GetDevice(&on);
            // One decoded on the device before a rebuild is skipped: the player's next ones come in memory.
            const int code = on.Get() == e.dev->device.Get() ? source_nv12(*e.dev, id, texture, due.slice, due.how)
                                                             : SC_OK;
            if (code != SC_OK) {
                log_line(SC_LOG_WARNING, "engine: a video's picture on the card couldn't be taken: " +
                                             std::string(sc_last_error()));
                media_card_refused();  // the next ones come in memory; this one is skipped
            }
        } else if (due.what == SC_MEDIA_PICTURE) {
            at(e, "uploading a video's picture", id);
            e.kept_sources.erase(id);
            check(sc_source_upload(e.dev, id, due.data, &due.how));
        } else if (due.what == SC_MEDIA_BLANK) {
            static const uint8_t nothing[16] = {};
            sc_upload how{};
            how.format = SC_FMT_RGBA;
            how.width = how.height = 2;
            how.stride = 8;
            e.kept_sources.erase(id);
            check(sc_source_upload(e.dev, id, nothing, &how));
        }
    }
    // Audio visualizers: the newest drawing, straight from the visualizer's thread.
    for (auto &[id, drawings] : visuals) {
        auto &seen = e.media_seen[id];
        if (seen.first != drawings.get()) seen = {drawings.get(), 0};
        MediaDue due;
        at(e, "a visualizer's drawing", id);
        visual_due(drawings, &seen.second, &due);
        if (due.what == SC_MEDIA_PICTURE) {
            changed.insert(id);
            e.kept_sources.erase(id);
            check(sc_source_upload(e.dev, id, due.data, &due.how));
        }
    }
    // Browser pages (another process's shared memory): the newest picture, copied onto the card from there.
    for (auto found = e.shared_open.begin(); found != e.shared_open.end();) {
        const bool here = std::any_of(shareds.begin(), shareds.end(),
                                      [&](const auto &s) { return s.first == found->first; });
        found = here ? std::next(found) : e.shared_open.erase(found);
    }
    for (auto &[id, name] : shareds) {
        at(e, "a browser page or helper's picture", id);
        SharedPictures &block = e.shared_open[id];
        if (block.name != name && !block.open(name)) continue;
        const uint8_t *data = nullptr;
        sc_upload how{};
        std::wstring texture;
        uint32_t flags = 0;
        const uint64_t seq = block.newest(&data, &how, &texture, &flags);
        if (!seq) continue;
        const bool held = shared_held.count(id) != 0;  // frozen: taken so the page goes on, the picture kept
        if (!held) e.kept_sources.erase(id);
        if (!data) {  // drawn on the card by the helper: copied there, never through memory
            const int code = source_page(*e.dev, id, block.page, texture, how.width, how.height, !held, flags);
            if (code == SC_NOT_YET) continue;
            if (code == SC_GONE) {  // its texture is closed: the page's next picture comes in a new one
                block.seen = seq;
                if (++block.page_gone == SharedPictures::PAGE_GONE_SAID && !block.page_said) {
                    block.page_said = true;
                    char hex[16];
                    std::snprintf(hex, sizeof hex, "0x%08lX", static_cast<unsigned long>(block.page.gone));
                    log_line(SC_LOG_WARNING, std::string("engine: a browser page's texture can't be opened (") + hex +
                                                 "), " + std::to_string(SharedPictures::PAGE_GONE_SAID) +
                                                 " pictures in a row");
                }
                continue;
            }
            block.page_gone = 0;
            if (code != SC_OK) {
                if (!block.page_said) {
                    block.page_said = true;
                    log_line(SC_LOG_WARNING, std::string("engine: a browser page's picture on the card couldn't be "
                                                         "taken: ") + sc_last_error());
                }
                block.seen = seq;  // the next one is tried afresh
                continue;
            }
            block.page_said = false;
            block.seen = seq;
            changed.insert(id);
            continue;
        }
        if (held) {
            block.seen = seq;
            continue;
        }
        check(sc_source_upload(e.dev, id, data, &how));
        changed.insert(id);
        if (block.counter() <= seq + 1) block.seen = seq;  // else overwritten while copied: taken again next frame
    }
    // Screen, window and game captures (capture_wgc.cpp): the newest picture, copied on the card.
    for (auto run = e.capture_runs.begin(); run != e.capture_runs.end();) {
        const bool here = std::any_of(captures.begin(), captures.end(),
                                      [&](const auto &c) { return c.first == run->first; });
        if (here) {
            ++run;
            continue;
        }
        e.capture_staging.erase(run->first);
        // Asleep: parked, not closed. It takes a picture a second so it is current on waking.
        CaptureRun &r = run->second;
        const int64_t tick_now = qpc();
        const bool forgotten = forgets.count(run->first) != 0;
        bool keep = !forgotten && r.cap && !r.done && r.cap->device() == e.dev->device.Get();
        if (keep && !r.parked) {
            r.parked = tick_now;
            r.starting.reset();
            r.cap->slow(true);
        }
        if (keep) {
            at(e, "a parked capture", run->first);
            if (r.cap->take(*e.dev, run->first, true)) {
                r.copied = tick_now;
                changed.insert(run->first);
            }
            keep = !r.cap->ended();
        }
        run = keep ? std::next(run) : e.capture_runs.erase(run);
    }
    for (auto &[id, want] : captures) {
        at(e, "a screen or window capture", id);
        CaptureRun &run = e.capture_runs[id];
        if (run.serial != want.serial) {
            if (run.parked && run.cap && same_ask(run.ask, want.ask) && !run.cap->ended()) {
                run.cap->slow(false);  // its source woke: the same capture goes on, none is started
                run.parked = 0;
                run.frames = 0;
                run.back = qpc();
                log_line(SC_LOG_INFO, "engine: a parked capture goes on (its source woke; none started)");
            } else {
                run = CaptureRun();
            }
            run.serial = want.serial;
            run.ask = want.ask;
        }
        if (run.done) continue;
        if (run.cap && run.cap->device() != e.dev->device.Get()) run.cap.reset();
        if (!run.cap) {
            if (!run.starting) {
                run.starting = sc::start_capture(e.dev->device, want.ask);
                run.asked = qpc();
            }
            sc::CaptureRefusal refusal = sc::CaptureRefusal::NONE;
            std::string why;
            bool answered = false;
            {
                std::lock_guard<std::mutex> hold(run.starting->m);
                if (run.starting->done) {
                    answered = true;
                    run.cap = std::move(run.starting->cap);
                    refusal = run.starting->refusal;
                    why = run.starting->why;
                }
            }
            if (!answered) {
                if (static_cast<double>(qpc() - run.asked) < CAPTURE_START_S * static_cast<double>(qpf()))
                    continue;  // still starting: asked again next frame
                refusal = sc::CaptureRefusal::NOT_HERE;  // Windows' capture service isn't answering
                why = "Windows didn't start it in 5 s";
            }
            run.starting.reset();
            if (run.cap && run.cap->device() != e.dev->device.Get()) run.cap.reset();  // started on an old device
            if (!run.cap && answered && refusal == sc::CaptureRefusal::NONE) continue;  // started again next frame
            if (!run.cap) {
                run.done = true;
                const bool here = refusal != sc::CaptureRefusal::NOT_HERE;
                log_line(here ? SC_LOG_INFO : SC_LOG_WARNING,
                         "engine: a capture didn't start (" + why + ")" +
                             (here ? "; SprigCast looks for it again" : "; SprigCast captures it another way"));
                capture_news.emplace_back(id, CaptureShown{here ? SC_SCREEN_ENDED : SC_SCREEN_REFUSED, 0, 0,
                                                           run.frames, want.serial});
                continue;
            }
        }
        const bool held = captures_held.count(id) != 0;  // frozen: taken so Windows draws on, the picture kept
        const bool got = run.cap->take(*e.dev, id, !held);
        if (got) {
            changed.insert(id);
            e.kept_sources.erase(id);
            ++run.frames;
            run.copied = qpc();
            run.back = 0;
        } else if (run.back && run.cap->width() > 0) {
            // Back from parked and Windows has drawn nothing new (a still window): the picture it took while
            // parked is the one to show. Said at once when that picture is fresh, else after a short wait for a
            // newer one.
            const double f = static_cast<double>(qpf());
            const int64_t tick_now = qpc();
            if (static_cast<double>(run.back - run.copied) < CAPTURE_FRESH_S * f ||
                static_cast<double>(tick_now - run.back) >= CAPTURE_BACK_S * f) {
                run.back = 0;
                run.frames = 1;
                if (!run.cap->ended())
                    capture_news.emplace_back(id, CaptureShown{SC_SCREEN_RUNNING, run.cap->width(),
                                                               run.cap->height(), run.frames, want.serial});
            }
        }
        if (run.cap->ended()) {
            run.done = true;
            run.cap.reset();
            log_line(SC_LOG_INFO, "engine: a capture ended (its window closed, or the display went)");
            capture_news.emplace_back(id, CaptureShown{SC_SCREEN_ENDED, 0, 0, run.frames, want.serial});
        } else if (got) {
            capture_news.emplace_back(id, CaptureShown{SC_SCREEN_RUNNING, run.cap->width(), run.cap->height(),
                                                       run.frames, want.serial});
        }
    }
    std::vector<std::pair<uint64_t, std::vector<uint8_t>>> used;
    for (auto &[id, pic] : uploads) {
        at(e, "uploading a picture from Python", id);
        check(sc_source_upload(e.dev, id, pic.data.data(), &pic.how));
        changed.insert(id);
        e.kept_draws.erase(id);
        if (pic.keep) {
            e.kept_sources[id] = std::move(pic);
        } else {
            e.kept_sources.erase(id);
            used.emplace_back(id, std::move(pic.data));
        }
    }
    // Sources drawn from a list (after the pictures above, which the lists use).
    for (auto &[id, d] : draws) {
        at(e, "drawing a source's list", id);
        check(sc_source_draws(e.dev, id, d.width, d.height, d.items.data(), d.count));
        changed.insert(id);
        if (d.count < 0) {
            e.kept_draws.erase(id);
        } else {
            e.kept_sources.erase(id);
            e.kept_draws[id] = std::move(d);
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
    at(e, "setting looks, masks and the blooms");
    if (snapshot) {
        sc_snapshot v = view_of(*snapshot);
        check(sc_snapshot_set(e.dev, &v));
        e.kept_snapshot = snapshot;
    }

    // Still copies for items frozen on their own: made once, from the pictures as they are this frame, before any
    // canvas is drawn. One with nothing to copy yet is tried again next frame; one that fails isn't tried again.
    for (auto made = e.holds_made.begin(); made != e.holds_made.end();) {
        const bool wanted = std::any_of(holds.begin(), holds.end(),
                                        [&](const auto &h) { return h.first == made->first; });
        made = wanted ? std::next(made) : e.holds_made.erase(made);
    }
    for (const auto &[id, want] : holds) {
        auto made = e.holds_made.find(id);
        if (made != e.holds_made.end() && made->second == want.serial) continue;
        at(e, "copying a frozen item's picture", id);
        const int code = sc_source_hold(e.dev, id, want.source, want.scene, seconds);
        if (code == SC_NOT_YET) continue;
        check(code);
        changed.insert(id);
        e.holds_made[id] = want.serial;
    }

    for (int i = 0; i < SC_ENGINE_CANVASES; ++i) {
        const CanvasCfg &cfg = canvases[i];
        sc_canvas *&c = e.gpu[i];
        if (c && (c->width != cfg.width || c->height != cfg.height)) {
            sc_canvas_destroy(c);
            c = nullptr;
        }
        if (cfg.width <= 0) continue;
        at(e, "drawing a canvas", static_cast<uint64_t>(i));
        auto &still = e.still[i];
        if (!c) {
            still.valid = false;
            int code = sc_canvas_create(e.dev, cfg.width, cfg.height, &c);
            check(code);
            if (code != SC_OK) continue;
        }
        RunningTransition &run = e.running[i];
        if (transitions[i].serial != run.serial) {  // a new one (or a stop) from Python: it starts this frame
            run.serial = transitions[i].serial;
            run.from = transitions[i].from;
            run.how = transitions[i].how;
            run.move = transitions[i].move;
            run.start = seconds;
            run.on = run.from != 0;
        }
        // Nothing it shows changed since it was last drawn (a still bloom: Starting Soon, BRB, a still layout): its
        // picture is the one it has, and drawing it again would only use the graphics card. It is drawn again once
        // a second all the same, so nothing missed here could stay wrong for long. Looked at on the frames it isn't
        // taken too, so a change on one of those isn't missed.
        // Every redraw is put down to something (sc_engine_redraws_take): a source, or one of SC_REDRAW_*. A change
        // on a frame nobody takes is remembered until the frame that is drawn, so it isn't lost on the way.
        uint64_t why = 0;
        bool kept = false;
        if (!still.valid) why = still.why ? still.why : SC_REDRAW_STARTED;
        else if (all_changed) why = SC_REDRAW_EVERYTHING;
        else if (run.on || still.scene != cfg.scene) why = SC_REDRAW_SWITCH;
        else if (!(kept = !cfg.scene || sc::scene_still(*e.dev, cfg.scene, changed, &why)) && !why)
            why = SC_REDRAW_OTHER;  // sprigs nested too deep, or a bloom the snapshot doesn't have
        if (!kept && still.valid) {
            still.valid = false;
            still.why = why;
        }
        // A canvas nobody takes a picture of this frame isn't drawn: one no preview, recording or stream uses (the
        // other canvas of a garden shown on one), and one only a preview takes on the frames between its pictures
        // (a preview takes 30 a second or fewer; the engine draws 60). The imported garden's two canvases were
        // both drawn 60 times a second with one preview at 30 watching: 71% of the graphics card (2026-11-19).
        if (!read_asked[i] && !canvas_taken(e, i, watches[i], outputs[i], previews, now)) continue;
        if (still.valid && now - still.drawn < qpf()) continue;
        ++e.canvas_draws;
        if (kept) why = SC_REDRAW_SECOND;  // still, drawn again on its once-a-second refresh
        {
            std::lock_guard<std::mutex> lock(e.state);
            if (e.redrawn_by.size() < 4096 || e.redrawn_by.count(why)) ++e.redrawn_by[why];
        }
        still.valid = false;  // until this frame's drawing is done (a transition's never counts)
        still.why = run.on ? SC_REDRAW_SWITCH : 0;
        if (run.on) {
            const double progress = (seconds - run.start) / std::max(0.001, static_cast<double>(run.how.seconds));
            if (progress < 1.0 && run.how.mode == SC_TRANSITION_MOVE) {
                if (run.move) {
                    check(render_move_on(c, run.from, cfg.scene, *run.move, seconds, static_cast<float>(progress)));
                    continue;
                }
            } else if (progress < 1.0) {
                check(sc_canvas_render_transition(c, run.from, cfg.scene, seconds, &run.how,
                                                  static_cast<float>(progress)));
                continue;
            }
            run.on = false;
        }
        if (cfg.scene) check(sc_canvas_render(c, cfg.scene, seconds));
        else check(sc_canvas_clear(c, 0, 0, 0, 0));
        still.valid = true;
        still.scene = cfg.scene;
        still.drawn = now;
    }
    at(e, "reading the canvases back for the previews");
    for (int i = 0; i < SC_ENGINE_CANVASES; ++i) read_back(e, i, e.gpu[i], watches[i], seconds, now);
    const double clock = static_cast<double>(now) / static_cast<double>(qpf());  // Python's perf_counter()
    at(e, "making the outputs' frames");
    for (int i = 0; i < SC_ENGINE_CANVASES; ++i) {
        for (int o = 0; o < SC_ENGINE_OUTPUTS; ++o) output_back(e, i, o, e.gpu[i], outputs[i][o], clock);
    }

    for (auto it = e.shown.begin(); it != e.shown.end();) {
        const bool wanted = std::any_of(previews.begin(), previews.end(),
                                        [&](const PreviewCfg &p) { return p.id == it->first; });
        it = wanted ? std::next(it) : e.shown.erase(it);
    }
    at(e, "showing the previews");
    for (const PreviewCfg &cfg : previews) show_preview(e, cfg, e.shown[cfg.id], now, presented, skipped);
    at(e, "finishing the frame");
    if (detach_seen) {  // let the window go for good before telling Python it can close it
        std::lock_guard<std::mutex> lock(e.dev->lock);
        e.dev->context->ClearState();
        e.dev->context->Flush();
    }

    std::lock_guard<std::mutex> lock(e.state);
    e.detach_done = detach_seen;
    for (auto &[id, news] : capture_news) {
        auto found = e.capture_states.find(id);
        if (found != e.capture_states.end() && found->second.serial == news.serial) found->second = news;
    }
    for (auto &[id, buffer] : used) {
        if (!e.uploads.count(id) && !e.forgets.count(id)) e.spare[id] = std::move(buffer);
    }
}

void tick(sc_engine &e) {
    std::lock_guard<std::timed_mutex> frame(e.frame);
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
                at(e, "rebuilding after a fault");
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
    at(e, "between frames");
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
        s.canvas_draws = static_cast<int32_t>(e.canvas_draws & 0x7fffffffu);
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
           !e.snapshot_new && !e.fault && e.detach_done == e.detach_asked && e.capture_wants.empty();
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

// Compiles every shader before the frame thread needs one (internal.h warm_shaders): once per process.
DWORD WINAPI warm_main(void *) {
    name_thread(L"SprigCast engine shaders");
    try {
        const int64_t start = qpc();
        warm_shaders();
        char line[96];
#ifdef SPRIGCORE_BAKED
        const char *how = "baked when built";
#else
        const char *how = "compiled here";
#endif
        std::snprintf(line, sizeof line, "engine: shaders ready in %.0f ms (%s)",
                      1000.0 * static_cast<double>(qpc() - start) / static_cast<double>(qpf()), how);
        log_line(SC_LOG_INFO, line);
    } catch (...) {
        log_line(SC_LOG_WARNING, "engine: compiling shaders ahead failed; each compiles when first used");
    }
    return 0;
}

void warm_once() {
    static std::atomic<bool> started{false};
    if (started.exchange(true)) return;
    HANDLE thread = CreateThread(nullptr, 0, warm_main, nullptr, 0, nullptr);
    if (thread) {
        SetThreadPriority(thread, THREAD_PRIORITY_BELOW_NORMAL);
        CloseHandle(thread);
    } else {
        started.store(false);
    }
}

DWORD WINAPI watchdog_main(void *arg) {
    sc_engine &e = *static_cast<sc_engine *>(arg);
    name_thread(L"SprigCast engine watchdog");
    while (WaitForSingleObject(e.quit, 250) == WAIT_TIMEOUT) {
        const int64_t started = e.tick_started.load();
        if (started && static_cast<double>(qpc() - started) > HANG_S * static_cast<double>(qpf()) &&
            !e.hung.exchange(true)) {
            const uint64_t id = e.doing_id.load(std::memory_order_relaxed);
            std::string line = "engine: one frame has taken more than 2 s (the frame thread is stuck) at: ";
            line += e.doing.load(std::memory_order_acquire);
            if (id) line += " (" + std::to_string(id) + ")";
            log_line(SC_LOG_ERROR, line);
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
        e->alive->engine = e.get();
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
        e->device_out = e->dev->device;
        media_card(e->dev->device.Get());  // videos asked to decode on the card decode on the engine's device
        warm_once();  // before the frame thread: its first frames find the shaders compiled, or being compiled
        e->stats.running = 1;  // from now: a call made before the thread's first moment still waits for frames
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
        {
            std::lock_guard<std::mutex> hold(engine->alive->m);  // a writer giving a texture back finds no engine
            engine->alive->engine = nullptr;
        }
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
        engine->draws.erase(id);  // a picture newer than a list sent before it
        return SC_OK;
    });
}

SC_API int sc_engine_source_forget(sc_engine *engine, uint64_t id) {
    return engine_call(engine, "sc_engine_source_forget", [&]() -> int {
        std::lock_guard<std::mutex> lock(engine->state);
        engine->uploads.erase(id);
        engine->spare.erase(id);
        engine->medias.erase(id);
        engine->media_sizes.erase(id);
        engine->visuals.erase(id);
        engine->draws.erase(id);
        engine->shareds.erase(id);
        engine->shared_held.erase(id);
        engine->capture_wants.erase(id);
        engine->capture_held.erase(id);
        engine->capture_states.erase(id);
        engine->holds.erase(id);
        engine->forgets.insert(id);
        return SC_OK;
    });
}

SC_API int sc_engine_source_hold(sc_engine *engine, uint64_t id, uint64_t source, uint64_t scene) {
    return engine_call(engine, "sc_engine_source_hold", [&]() -> int {
        if (!id || (source != 0) == (scene != 0) || id == source)
            return fail(SC_ERR_ARGUMENT, "sc_engine_source_hold: no id, or not exactly one of a source and a scene");
        std::lock_guard<std::mutex> lock(engine->state);
        engine->holds[id] = HoldWant{source, scene, ++engine->hold_serial};
        engine->forgets.erase(id);
        return SC_OK;
    });
}

SC_API int sc_engine_source_media(sc_engine *engine, uint64_t id, sc_media *media) {
    return engine_call(engine, "sc_engine_source_media", [&]() -> int {
        if (!id) return fail(SC_ERR_ARGUMENT, "sc_engine_source_media: no id");
        std::shared_ptr<void> player = media_hold(media);
        std::lock_guard<std::mutex> lock(engine->state);
        engine->visuals.erase(id);
        if (player) {
            engine->medias[id] = std::move(player);
            engine->uploads.erase(id);  // a picture Python sent before isn't drawn over the player's
            engine->forgets.erase(id);
        } else {
            engine->medias.erase(id);
        }
        return SC_OK;
    });
}

SC_API int sc_engine_source_media_size(sc_engine *engine, uint64_t id, int width, int height) {
    return engine_call(engine, "sc_engine_source_media_size", [&]() -> int {
        if (!id) return fail(SC_ERR_ARGUMENT, "sc_engine_source_media_size: no id");
        std::lock_guard<std::mutex> lock(engine->state);
        if (width > 0 && height > 0) engine->media_sizes[id] = {width, height};
        else engine->media_sizes.erase(id);
        return SC_OK;
    });
}

SC_API int sc_engine_source_draws(sc_engine *engine, uint64_t id, int width, int height, const sc_item *items,
                                  int count) {
    return engine_call(engine, "sc_engine_source_draws", [&]() -> int {
        if (!id) return fail(SC_ERR_ARGUMENT, "sc_engine_source_draws: no id");
        if (count > 4096 || (count > 0 && !items) || width > 8192 || height > 8192)
            return fail(SC_ERR_ARGUMENT, "sc_engine_source_draws: too big, or no items");
        DrawList d;
        d.width = width;
        d.height = height;
        d.count = width > 0 && height > 0 ? count : -1;
        if (d.count > 0) d.items.assign(items, items + count);
        std::lock_guard<std::mutex> lock(engine->state);
        engine->uploads.erase(id);  // the list is newer than a picture sent before it
        engine->forgets.erase(id);
        engine->draws[id] = std::move(d);
        return SC_OK;
    });
}

SC_API int sc_engine_source_visual(sc_engine *engine, uint64_t id, sc_visual *visual) {
    return engine_call(engine, "sc_engine_source_visual", [&]() -> int {
        if (!id) return fail(SC_ERR_ARGUMENT, "sc_engine_source_visual: no id");
        std::shared_ptr<void> drawings = visual_hold(visual);
        std::lock_guard<std::mutex> lock(engine->state);
        engine->medias.erase(id);
        if (drawings) {
            engine->visuals[id] = std::move(drawings);
            engine->uploads.erase(id);  // a picture Python sent before isn't drawn over the visualizer's
            engine->forgets.erase(id);
        } else {
            engine->visuals.erase(id);
        }
        return SC_OK;
    });
}

SC_API int sc_engine_source_shared(sc_engine *engine, uint64_t id, const wchar_t *name) {
    return engine_call(engine, "sc_engine_source_shared", [&]() -> int {
        if (!id) return fail(SC_ERR_ARGUMENT, "sc_engine_source_shared: no id");
        std::lock_guard<std::mutex> lock(engine->state);
        if (name && *name) {
            engine->shareds[id] = name;
            engine->uploads.erase(id);  // a picture Python sent before isn't drawn over the page's
            engine->forgets.erase(id);
        } else {
            engine->shareds.erase(id);
            engine->shared_held.erase(id);
        }
        return SC_OK;
    });
}

SC_API int sc_engine_source_shared_hold(sc_engine *engine, uint64_t id, int held) {
    return engine_call(engine, "sc_engine_source_shared_hold", [&]() -> int {
        if (!id) return fail(SC_ERR_ARGUMENT, "sc_engine_source_shared_hold: no id");
        std::lock_guard<std::mutex> lock(engine->state);
        if (held)
            engine->shared_held.insert(id);
        else
            engine->shared_held.erase(id);
        return SC_OK;
    });
}

SC_API int sc_engine_source_capture(sc_engine *engine, uint64_t id, uint64_t window, uint64_t monitor, int flags,
                                    int interval_ms) {
    return engine_call(engine, "sc_engine_source_capture", [&]() -> int {
        if (!id) return fail(SC_ERR_ARGUMENT, "sc_engine_source_capture: no id");
        if (interval_ms < 0 || interval_ms > 10000 || (flags & ~(SC_SCREEN_CURSOR | SC_SCREEN_BORDER | SC_SCREEN_OPAQUE)))
            return fail(SC_ERR_ARGUMENT, "sc_engine_source_capture: bad flags or interval");
        std::lock_guard<std::mutex> lock(engine->state);
        if (!window && !monitor) {
            engine->capture_wants.erase(id);
            engine->capture_held.erase(id);
            engine->capture_states.erase(id);
            return SC_OK;
        }
        sc::CaptureAsk ask;
        ask.window = window;
        ask.monitor = monitor;
        ask.flags = flags;
        ask.interval_ms = interval_ms;
        auto found = engine->capture_wants.find(id);
        if (found != engine->capture_wants.end()) {
            const sc::CaptureAsk &had = found->second.ask;
            const int state = engine->capture_states[id].state;
            if (had.window == window && had.monitor == monitor && had.flags == flags &&
                had.interval_ms == interval_ms && state != SC_SCREEN_ENDED && state != SC_SCREEN_REFUSED)
                return SC_OK;  // the same capture, still running
        }
        const uint64_t serial = engine->next_capture++;
        engine->capture_wants[id] = CaptureWant{ask, serial};
        engine->capture_states[id] = CaptureShown{SC_SCREEN_STARTING, 0, 0, 0, serial};
        engine->uploads.erase(id);  // a picture Python sent before isn't drawn over the capture's
        engine->forgets.erase(id);
        return SC_OK;
    });
}

SC_API int sc_engine_source_capture_hold(sc_engine *engine, uint64_t id, int held) {
    return engine_call(engine, "sc_engine_source_capture_hold", [&]() -> int {
        if (!id) return fail(SC_ERR_ARGUMENT, "sc_engine_source_capture_hold: no id");
        std::lock_guard<std::mutex> lock(engine->state);
        if (held && engine->capture_wants.count(id))
            engine->capture_held.insert(id);
        else
            engine->capture_held.erase(id);
        return SC_OK;
    });
}

SC_API int sc_engine_capture_state(sc_engine *engine, uint64_t id, int *state, int *width, int *height,
                                   uint64_t *frames) {
    return engine_call(engine, "sc_engine_capture_state", [&]() -> int {
        CaptureShown shown;
        {
            std::lock_guard<std::mutex> lock(engine->state);
            auto found = engine->capture_states.find(id);
            if (found != engine->capture_states.end()) shown = found->second;
        }
        if (state) *state = shown.state;
        if (width) *width = shown.width;
        if (height) *height = shown.height;
        if (frames) *frames = shown.frames;
        return SC_OK;
    });
}

namespace {

uint64_t texture_bytes(ID3D11Texture2D *t) {
    if (!t) return 0;
    D3D11_TEXTURE2D_DESC d = {};
    t->GetDesc(&d);
    uint64_t texel = 4;  // RGBA and BGRA, the most there is here
    if (d.Format == DXGI_FORMAT_R8_UNORM) texel = 1;
    else if (d.Format == DXGI_FORMAT_R8G8_UNORM) texel = 2;
    const uint64_t one = static_cast<uint64_t>(d.Width) * d.Height;
    if (d.Format == DXGI_FORMAT_NV12) return one * 3 / 2 * std::max(1u, d.ArraySize);
    return one * texel * std::max(1u, d.ArraySize);
}

}  // namespace

// What the engine holds for its sources (Gameplay with 34 browser pages added ~1 GB on its first wake, PC
// 2026-11-24, and nothing said whose): textures on the card, staging copies in memory, sprig drawings, browser
// pages' shared textures open, pictures kept for a rebuild.
SC_API int sc_engine_memory_note(sc_engine *engine, char *out, int size) {
    return engine_call(engine, "sc_engine_memory_note", [&]() -> int {
        if (!out || size < 2) return fail(SC_ERR_ARGUMENT, "sc_engine_memory_note: no room");
        out[0] = 0;
        std::unique_lock<std::timed_mutex> frame(engine->frame, std::chrono::milliseconds(FRAME_WAIT_MS));
        if (!frame.owns_lock()) {
            // Only a note for the memory map line: a frame still drawing (a slow chip, CRGPlays 2026-11-27) isn't
            // an error. A frame that really stays stuck is found and handled by the stuck-frame watch.
            std::snprintf(out, static_cast<size_t>(size), "engine busy drawing, not counted this time");
            return SC_OK;
        }
        if (!engine->dev) return fail(SC_ERR_ARGUMENT, "sc_engine_memory_note: no device yet");
        sc_device &dev = *engine->dev;
        uint64_t card = 0, staging = 0, parts = 0, drawn = 0, big = 0;
        size_t sources = 0, stagers = 0;
        {
            std::lock_guard<std::mutex> lock(dev.lock);
            for (auto &[id, src] : dev.comp.sources) {
                ++sources;
                const uint64_t mine = texture_bytes(src.texture.Get()) + texture_bytes(src.nv12.Get());
                card += mine;
                if (mine >= (uint64_t(8) << 20)) ++big;
                uint64_t up = 0;
                for (auto &t : src.up_staging) up += texture_bytes(t.Get());
                for (auto &t : src.nv12_staging) up += texture_bytes(t.Get());
                staging += up;
                if (up) ++stagers;
            }
            for (auto &[id, t] : dev.comp.parts) parts += texture_bytes(t.texture.Get());
            for (auto &[id, d] : dev.comp.drawn) drawn += texture_bytes(d.target.texture.Get());
        }
        size_t pages = 0;
        uint64_t page_bytes = 0;
        for (auto &[id, block] : engine->shared_open) {
            if (!block.page.texture) continue;
            ++pages;
            page_bytes += texture_bytes(block.page.texture.Get());
        }
        uint64_t kept = 0;
        for (auto &[id, pic] : engine->kept_sources) kept += pic.data.capacity();
        constexpr double MB = 1024.0 * 1024.0;
        std::snprintf(out, static_cast<size_t>(size),
                      "%zu sources %.0f MB on the card (%llu of 8 MB or more), staging %.0f MB in memory for %zu; "
                      "sprigs %.0f MB; drawn lists %.0f MB; browser pages open %zu (%.0f MB shared); kept for a "
                      "rebuild %.0f MB",
                      sources, card / MB, static_cast<unsigned long long>(big), staging / MB, stagers, parts / MB,
                      drawn / MB, pages, page_bytes / MB, kept / MB);
        return SC_OK;
    });
}

SC_API int sc_engine_capture_read(sc_engine *engine, uint64_t id, uint8_t *bgra, int stride, int max_width,
                                  int max_height, int *width, int *height) {
    return engine_call(engine, "sc_engine_capture_read", [&]() -> int {
        if (!width || !height) return fail(SC_ERR_ARGUMENT, "sc_engine_capture_read: nowhere for the size");
        *width = *height = 0;
        std::unique_lock<std::timed_mutex> frame(engine->frame, std::chrono::milliseconds(FRAME_WAIT_MS));
        if (!frame.owns_lock()) return fail(SC_ERR_TIMEOUT, "sc_engine_capture_read: the engine's frame is stuck");
        if (!engine->dev) return fail(SC_ERR_ARGUMENT, "sc_engine_capture_read: no device yet");
        return sc::read_capture(*engine->dev, id, engine->capture_staging[id], bgra, stride, max_width, max_height,
                                width, height);
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

SC_API int sc_engine_redraws_take(sc_engine *engine, uint64_t *ids, uint32_t *counts, int max, int *count) {
    return guarded("sc_engine_redraws_take", [&]() -> int {
        if (!engine || !count || max < 0 || (max > 0 && (!ids || !counts)))
            return fail(SC_ERR_ARGUMENT, "sc_engine_redraws_take: no engine or room");
        std::vector<std::pair<uint64_t, uint32_t>> all;
        {
            std::lock_guard<std::mutex> lock(engine->state);
            all.assign(engine->redrawn_by.begin(), engine->redrawn_by.end());
            engine->redrawn_by.clear();
        }
        std::sort(all.begin(), all.end(), [](const auto &a, const auto &b) { return a.second > b.second; });
        const int n = std::min(max, static_cast<int>(all.size()));
        for (int i = 0; i < n; ++i) {
            ids[i] = all[static_cast<size_t>(i)].first;
            counts[i] = all[static_cast<size_t>(i)].second;
        }
        *count = n;
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
            engine->read_asks[index] = 3;  // drawn on those frames even with nobody else taking its picture
            const uint64_t want = engine->stats.ticks + 2;
            const bool drawn = engine->changed.wait_for(lock, std::chrono::milliseconds(WAIT_MS), [&] {
                return engine->stats.ticks >= want || engine->dead.load() || !engine->stats.running;
            });
            if (engine->dead.load()) return fail(SC_ERR_FAULTED, "sc_engine_canvas_read: the engine stopped");
            if (!drawn) return fail(SC_ERR_TIMEOUT, "sc_engine_canvas_read: no frame in 2 s");
        }
        std::unique_lock<std::timed_mutex> frame(engine->frame, std::chrono::milliseconds(FRAME_WAIT_MS));
        if (!frame.owns_lock()) return fail(SC_ERR_TIMEOUT, "sc_engine_canvas_read: the engine's frame is stuck");
        sc_canvas *canvas = engine->gpu[index];
        if (!canvas || canvas->width != width || canvas->height != height)
            return fail(SC_ERR_TIMEOUT, "sc_engine_canvas_read: the canvas isn't drawn yet");
        return sc_canvas_read_bgra(canvas, out, width * 4);
    });
}

SC_API int sc_engine_canvas_transition(sc_engine *engine, int index, uint64_t from, const sc_transition *how) {
    return engine_call(engine, "sc_engine_canvas_transition", [&]() -> int {
        if (index < 0 || index >= SC_ENGINE_CANVASES)
            return fail(SC_ERR_ARGUMENT, "sc_engine_canvas_transition: no such slot");
        const bool stop = from == 0 || !how;
        const bool known = (how && how->mode >= SC_TRANSITION_FADE && how->mode <= SC_TRANSITION_BLOOM) ||
                           (how && how->mode == SC_TRANSITION_STINGER);
        if (!stop && (!known || !(how->seconds > 0)))
            return fail(SC_ERR_ARGUMENT, "sc_engine_canvas_transition: unknown kind or no length");
        std::lock_guard<std::mutex> lock(engine->state);
        TransitionCfg &t = engine->transitions[index];
        t.from = stop ? 0 : from;
        t.how = stop ? sc_transition{} : *how;
        t.move.reset();
        ++t.serial;
        return SC_OK;
    });
}

SC_API int sc_engine_canvas_move(sc_engine *engine, int index, uint64_t from, float seconds, const sc_move *move) {
    return engine_call(engine, "sc_engine_canvas_move", [&]() -> int {
        if (index < 0 || index >= SC_ENGINE_CANVASES || !from || !move || !(seconds > 0))
            return fail(SC_ERR_ARGUMENT, "sc_engine_canvas_move: no such slot, no scene or move, or no length");
        auto plan = std::make_shared<sc::MovePlan>();
        const char *why = nullptr;
        if (!sc::move_plan(*move, *plan, why))
            return fail(SC_ERR_ARGUMENT, std::string("sc_engine_canvas_move: ") + why);
        std::lock_guard<std::mutex> lock(engine->state);
        TransitionCfg &t = engine->transitions[index];
        t.from = from;
        t.how = sc_transition{};
        t.how.mode = SC_TRANSITION_MOVE;
        t.how.seconds = seconds;
        t.move = std::move(plan);
        ++t.serial;
        return SC_OK;
    });
}

SC_API int sc_engine_canvas_watch(sc_engine *engine, int index, int width, int height, int max_fps) {
    return engine_call(engine, "sc_engine_canvas_watch", [&]() -> int {
        if (index < 0 || index >= SC_ENGINE_CANVASES || max_fps < 0)
            return fail(SC_ERR_ARGUMENT, "sc_engine_canvas_watch: bad slot or rate");
        const bool off = width == 0 && height == 0;
        if (!off && (width < 1 || height < 1 || width > 16384 || height > 16384))
            return fail(SC_ERR_ARGUMENT, "sc_engine_canvas_watch: size must be 1 to 16384 each way");
        std::lock_guard<std::mutex> lock(engine->state);
        Watch &w = engine->watches[index];
        w.width = width;
        w.height = height;
        w.max_fps = max_fps;
        if (off) {
            w.latest = std::vector<uint8_t>();
            w.latest_w = w.latest_h = 0;
        }
        return SC_OK;
    });
}

SC_API int sc_engine_canvas_take(sc_engine *engine, int index, uint8_t *out, int width, int height, uint64_t *seq,
                                 int *fresh, double *drawn) {
    return engine_call(engine, "sc_engine_canvas_take", [&]() -> int {
        if (!out || !seq || !fresh || index < 0 || index >= SC_ENGINE_CANVASES)
            return fail(SC_ERR_ARGUMENT, "sc_engine_canvas_take: bad slot or output");
        *fresh = 0;
        std::lock_guard<std::mutex> lock(engine->state);
        const Watch &w = engine->watches[index];
        if (w.seq <= *seq || w.latest_w != width || w.latest_h != height || w.latest.empty()) return SC_OK;
        std::memcpy(out, w.latest.data(), w.latest.size());
        *seq = w.seq;
        *fresh = 1;
        if (drawn) *drawn = w.drawn;
        return SC_OK;
    });
}

SC_API int sc_engine_output_set(sc_engine *engine, int index, int slot, int width, int height, int scale_mode,
                                int flags) {
    return engine_call(engine, "sc_engine_output_set", [&]() -> int {
        if (index < 0 || index >= SC_ENGINE_CANVASES || slot < 0 || slot >= SC_ENGINE_OUTPUTS)
            return fail(SC_ERR_ARGUMENT, "sc_engine_output_set: no such canvas or output slot");
        if (scale_mode < SC_SCALE_BILINEAR || scale_mode > SC_SCALE_LANCZOS)
            return fail(SC_ERR_ARGUMENT, "sc_engine_output_set: unknown scaling");
        const bool off = width == 0 && height == 0;
        if (!off && (width < 2 || height < 2 || width % 2 || height % 2 || width > 16384 || height > 16384))
            return fail(SC_ERR_ARGUMENT, "sc_engine_output_set: width and height must be even, 2 to 16384");
        std::lock_guard<std::mutex> lock(engine->state);
        Output &out = engine->outputs[index][slot];
        const bool share = !off && (flags & SC_OUTPUT_SHARE) != 0;
        if (out.width == width && out.height == height && out.scale == scale_mode && out.share == share) return SC_OK;
        out.width = width;
        out.height = height;
        out.scale = scale_mode;
        out.share = share;
        ++out.serial;
        out.ready.clear();
        out.spare.clear();
        out.dropped = 0;
        out.share_state = 0;
        clear_shared(out);
        out.link.reset();  // a writer has one size: the caller links again if it wants
        return SC_OK;
    });
}

SC_API int sc_engine_output_link(sc_engine *engine, int index, int slot, const sc_output_link *link) {
    return engine_call(engine, "sc_engine_output_link", [&]() -> int {
        if (!link || !link->writer || index < 0 || index >= SC_ENGINE_CANVASES || slot < 0 ||
            slot >= SC_ENGINE_OUTPUTS || link->fps_num < 1 || link->fps_den < 1 || !(link->fill_seconds >= 0))
            return fail(SC_ERR_ARGUMENT, "sc_engine_output_link: bad slot, writer or rate");
        std::shared_ptr<void> share = sc::writer_share(link->writer);
        int ww = 0, wh = 0;
        sc::writer_size(share, &ww, &wh);
        std::shared_ptr<Link> l;
        {
            std::lock_guard<std::mutex> lock(engine->state);
            Output &out = engine->outputs[index][slot];
            if (out.width != ww || out.height != wh)
                return fail(SC_ERR_ARGUMENT, "sc_engine_output_link: the output isn't the writer's size");
            if (out.share && (!sc::writer_on_card(share) || link->keep))
                return fail(SC_ERR_ARGUMENT, "sc_engine_output_link: frames on the card go only to a writer that takes "
                                             "them there, and to nothing else");
            l = out.link;
            if (!l || l->ended.load()) {
                l = out.link = std::make_shared<Link>();  // its writer is set below
            }
        }
        std::lock_guard<std::mutex> hold(l->use);
        l->camera.reset();
        l->camera_only = false;
        if (l->writer.get() != share.get()) {  // another writer: this one starts afresh
            l->writer = share;
            l->last = -1;
        }
        l->start = link->start;
        l->fps_num = link->fps_num;
        l->fps_den = link->fps_den;
        l->keep = link->keep != 0;
        l->fill = std::min(link->fill_seconds, 60.0);
        l->last = std::max(l->last, link->last_pts);
        return SC_OK;
    });
}

SC_API int sc_engine_output_unlink(sc_engine *engine, int index, int slot, int64_t *last_pts, int *linked) {
    return guarded("sc_engine_output_unlink", [&]() -> int {  // works on a stopped engine too
        if (last_pts) *last_pts = -1;
        if (linked) *linked = 0;
        if (!engine || index < 0 || index >= SC_ENGINE_CANVASES || slot < 0 || slot >= SC_ENGINE_OUTPUTS)
            return fail(SC_ERR_ARGUMENT, "sc_engine_output_unlink: bad slot");
        std::shared_ptr<Link> l;
        {
            std::lock_guard<std::mutex> lock(engine->state);
            l.swap(engine->outputs[index][slot].link);
        }
        if (!l) return SC_OK;
        std::lock_guard<std::mutex> hold(l->use);  // waits for a frame being handed over
        if (linked) *linked = l->ended.load() ? 0 : 1;
        if (last_pts) *last_pts = l->writer ? std::max(l->last, sc::writer_last(l->writer)) : l->last;
        l->writer.reset();
        l->camera.reset();  // unmapped: nothing is written there once this returns
        l->ended = true;
        return SC_OK;
    });
}

SC_API int sc_engine_output_link_camera(sc_engine *engine, int index, int slot, const wchar_t *name, int width,
                                        int height, int keep) {
    return engine_call(engine, "sc_engine_output_link_camera", [&]() -> int {
        if (!name || !*name || index < 0 || index >= SC_ENGINE_CANVASES || slot < 0 || slot >= SC_ENGINE_OUTPUTS ||
            width < 2 || height < 2 || width > 16384 || height > 16384 || (width & 1) || (height & 1))
            return fail(SC_ERR_ARGUMENT, "sc_engine_output_link_camera: bad slot, name or size");
        auto block = std::make_unique<CameraBlock>();
        if (!block->open(name))
            return fail(SC_ERR_ARGUMENT, "sc_engine_output_link_camera: the camera's block can't be opened (error " +
                                             std::to_string(GetLastError()) + ")");
        block->width = width;
        block->height = height;
        if (CameraBlock::HEADER + static_cast<size_t>(width) * height * 3 / 2 > block->size)
            return fail(SC_ERR_ARGUMENT, "sc_engine_output_link_camera: the camera's block is too small");
        std::shared_ptr<Link> l;
        {
            std::lock_guard<std::mutex> lock(engine->state);
            Output &out = engine->outputs[index][slot];
            if (out.share || out.width > width || out.height > height)
                return fail(SC_ERR_ARGUMENT, "sc_engine_output_link_camera: the output doesn't fit the camera");
            l = out.link;
            if (!l || l->ended.load()) l = out.link = std::make_shared<Link>();
        }
        std::lock_guard<std::mutex> hold(l->use);
        l->writer.reset();
        l->camera = std::move(block);
        l->keep = keep != 0;
        l->camera_only = keep == 0;
        return SC_OK;
    });
}

SC_API int sc_engine_output_camera_frames(sc_engine *engine, int index, int slot, uint64_t *frames) {
    return guarded("sc_engine_output_camera_frames", [&]() -> int {
        if (frames) *frames = 0;
        if (!engine || !frames || index < 0 || index >= SC_ENGINE_CANVASES || slot < 0 || slot >= SC_ENGINE_OUTPUTS)
            return fail(SC_ERR_ARGUMENT, "sc_engine_output_camera_frames: bad slot");
        std::shared_ptr<Link> l;
        {
            std::lock_guard<std::mutex> lock(engine->state);
            l = engine->outputs[index][slot].link;
        }
        if (!l) return SC_OK;
        std::lock_guard<std::mutex> hold(l->use);
        if (l->camera) *frames = l->camera->written;
        return SC_OK;
    });
}

SC_API int sc_engine_output_take(sc_engine *engine, int index, int slot, uint8_t *out, size_t bytes, int *fresh,
                                 double *drawn, uint64_t *dropped) {
    return engine_call(engine, "sc_engine_output_take", [&]() -> int {
        if (!out || !fresh || index < 0 || index >= SC_ENGINE_CANVASES || slot < 0 || slot >= SC_ENGINE_OUTPUTS)
            return fail(SC_ERR_ARGUMENT, "sc_engine_output_take: bad slot or output");
        *fresh = 0;
        OutFrame frame;
        {
            std::lock_guard<std::mutex> lock(engine->state);
            Output &o = engine->outputs[index][slot];
            if (dropped) *dropped = o.dropped;
            if (bytes != static_cast<size_t>(o.width) * o.height * 3 / 2)
                return fail(SC_ERR_ARGUMENT, "sc_engine_output_take: that isn't the output's size");
            if (o.ready.empty()) return SC_OK;
            frame = std::move(o.ready.front());
            o.ready.pop_front();
        }
        if (frame.data.size() == bytes) {  // copied outside the lock: the frame thread never waits on it
            std::memcpy(out, frame.data.data(), bytes);
            *fresh = 1;
            if (drawn) *drawn = frame.drawn;
        }
        std::lock_guard<std::mutex> lock(engine->state);
        Output &o = engine->outputs[index][slot];
        if (o.spare.size() < 2 && frame.data.size() == bytes) o.spare.push_back(std::move(frame.data));
        return SC_OK;
    });
}

SC_API int sc_engine_device(sc_engine *engine, void **device, int *rebuilds) {
    return engine_call(engine, "sc_engine_device", [&]() -> int {
        if (!device) return fail(SC_ERR_ARGUMENT, "sc_engine_device: no output");
        *device = nullptr;
        std::lock_guard<std::mutex> lock(engine->state);
        if (!engine->device_out) return fail(SC_ERR_DEVICE, "sc_engine_device: the engine has no device right now");
        engine->device_out->AddRef();
        *device = engine->device_out.Get();
        if (rebuilds) *rebuilds = engine->stats.rebuilds;
        return SC_OK;
    });
}

SC_API int sc_engine_output_take_texture(sc_engine *engine, int index, int slot, void **texture, uint64_t *pool,
                                         int *entry, double *drawn, int *state, uint64_t *dropped) {
    return engine_call(engine, "sc_engine_output_take_texture", [&]() -> int {
        if (!texture || !pool || !entry || !state || index < 0 || index >= SC_ENGINE_CANVASES || slot < 0 ||
            slot >= SC_ENGINE_OUTPUTS)
            return fail(SC_ERR_ARGUMENT, "sc_engine_output_take_texture: bad slot or output");
        *texture = nullptr;
        std::lock_guard<std::mutex> lock(engine->state);
        Output &o = engine->outputs[index][slot];
        *state = o.share_state < 0 ? -1 : 0;
        if (dropped) *dropped = o.dropped;
        if (o.shared.empty()) return SC_OK;
        const SharedFrame frame = o.shared.front();  // its reference becomes the caller's
        o.shared.pop_front();
        *texture = frame.texture;
        *pool = frame.pool;
        *entry = frame.entry;
        if (drawn) *drawn = frame.drawn;
        *state = 1;
        return SC_OK;
    });
}

SC_API int sc_engine_output_give_back(sc_engine *engine, int index, int slot, uint64_t pool, int entry) {
    return guarded("sc_engine_output_give_back", [&]() -> int {
        if (!engine || index < 0 || index >= SC_ENGINE_CANVASES || slot < 0 || slot >= SC_ENGINE_OUTPUTS ||
            entry < 0 || entry >= SC_ENGINE_SHARE_POOL)
            return fail(SC_ERR_ARGUMENT, "sc_engine_output_give_back: bad slot or texture");
        std::lock_guard<std::mutex> lock(engine->state);
        Output &o = engine->outputs[index][slot];
        if (pool && o.pool == pool) o.busy[entry] = false;
        return SC_OK;
    });
}

SC_API void sc_com_release(void *unknown) {
    if (unknown) static_cast<IUnknown *>(unknown)->Release();
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
