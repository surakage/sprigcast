// Video, GIF and sound files (phase 3, docs/native-engine.md): sources/media.py's player, moved here method for
// method so a file plays the same on either engine. A thread of the engine's own reads and decodes a little
// ahead; the render and mixer threads take what is due on the file's clock. None of it needs Python's lock.
//
// FFmpeg is not linked: the engine loads the copy SprigCast already ships with PyAV (sc_media_ffmpeg_load) and
// calls it through the table below. The headers it is compiled against (native/third_party/ffmpeg) are the same
// version, and the libraries' major versions are checked before anything is called.
//
// This file needs no graphics card and nothing of Windows but the library loader, so its checks also run on
// Linux (tests/test_native_media.py).
#include "base.h"
#include "media.h"

#include "ffmpeg.h"

#ifdef _WIN32
#  include <windows.h>
#  include <d3d11.h>
extern "C" {
#  include <libavutil/hwcontext_d3d11va.h>
}
#else
#  include <dlfcn.h>
#endif

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstdio>
#include <cstring>
#include <deque>
#include <filesystem>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <thread>
#include <utility>
#include <vector>

namespace {

using sc::ffm::av_text;
using sc::ffm::ff;
using sc::ffm::ff_lock;
using sc::ffm::ff_ready;
std::atomic<bool> devices_ready{false};  // libavdevice loaded and registered (cameras: sc_media_set_device)
using sc::ffm::find_symbol;
using sc::ffm::FFmpeg;
using sc::ffm::open_library;

// ---- decoding on the graphics card --------------------------------------------------------------------------

// One device for every player that decodes on the graphics card (Direct3D 11 on Windows), made when first wanted.
// Making one per file started a device per video; if it can't be made, every file decodes on the CPU and the log
// says why once.
std::mutex hw_lock;
AVBufferRef *hw_device = nullptr;
AVHWDeviceType hw_type = AV_HWDEVICE_TYPE_NONE;
bool hw_tried = false;

// The engine's own Direct3D device (sc::media_card): videos decoded on it hand their pictures to the compositor
// on the card, with no copy to memory and back. Its serial changes with the device (a rebuild).
void *card_device = nullptr;  // ID3D11Device, with a reference of ours
#ifdef _WIN32
AVBufferRef *card_ref = nullptr;
#endif
uint64_t card_serial = 0;
bool card_failed = false;  // FFmpeg wouldn't decode on it: FFmpeg's own device, as before
std::atomic<void *> card_now{nullptr};
std::atomic<bool> card_refused{false};  // the engine couldn't take a picture on the card: memory it is

#ifdef _WIN32
// Called with hw_lock held.
AVBufferRef *card_device_ref() {
    if (!card_device || card_failed) return nullptr;
    if (!card_ref) {
        AVBufferRef *made = ff.av_hwdevice_ctx_alloc(AV_HWDEVICE_TYPE_D3D11VA);
        if (!made) return nullptr;
        auto *hw = reinterpret_cast<AVHWDeviceContext *>(made->data);
        auto *d3d = static_cast<AVD3D11VADeviceContext *>(hw->hwctx);
        d3d->device = static_cast<ID3D11Device *>(card_device);
        d3d->device->AddRef();  // FFmpeg lets go of it when the last decoder and picture do
        const int r = ff.av_hwdevice_ctx_init(made);
        if (r < 0) {
            ff.av_buffer_unref(&made);
            card_failed = true;
            sc::log_line(SC_LOG_INFO, std::string("media: videos can't decode on the engine's graphics device (") +
                                          av_text(r) + "); they decode on their own and are copied");
            return nullptr;
        }
        card_ref = made;
        sc::log_line(SC_LOG_INFO, "media: videos decode on the engine's graphics device: pictures stay on the card");
    }
    return ff.av_buffer_ref(card_ref);
}
#endif

AVBufferRef *hw_device_ref(AVHWDeviceType &type) {
    std::lock_guard<std::mutex> hold(hw_lock);
#ifdef _WIN32
    if (AVBufferRef *engine = card_device_ref()) {
        type = AV_HWDEVICE_TYPE_D3D11VA;
        return engine;
    }
#endif
    if (!hw_tried) {
        hw_tried = true;
#ifdef _WIN32
        const char *name = "d3d11va";
#else
        const char *name = "vaapi";
#endif
        hw_type = ff.av_hwdevice_find_type_by_name(name);
        int r = hw_type == AV_HWDEVICE_TYPE_NONE ? AVERROR(ENOSYS)
                                                 : ff.av_hwdevice_ctx_create(&hw_device, hw_type, nullptr, nullptr, 0);
        if (r < 0) {
            hw_device = nullptr;
            sc::log_line(SC_LOG_INFO, std::string("media: decoding on the graphics card isn't available (") + name +
                                          ": " + av_text(r) + "); videos decode on the CPU");
        } else {
            sc::log_line(SC_LOG_INFO, std::string("media: videos asked to can decode on the graphics card (") + name +
                                          ")");
        }
    }
    type = hw_type;
    return hw_device ? ff.av_buffer_ref(hw_device) : nullptr;
}

// The decoder's choice of picture format: the graphics card's when it offers it, else the first the CPU makes.
AVPixelFormat pick_format(AVCodecContext *ctx, const AVPixelFormat *offered) {
    const auto want = static_cast<AVPixelFormat>(reinterpret_cast<intptr_t>(ctx->opaque));
    for (const AVPixelFormat *p = offered; *p != AV_PIX_FMT_NONE; ++p) {
        if (*p == want) return *p;
    }
    for (const AVPixelFormat *p = offered; *p != AV_PIX_FMT_NONE; ++p) {
        const AVPixFmtDescriptor *d = ff.av_pix_fmt_desc_get(*p);
        if (d && !(d->flags & AV_PIX_FMT_FLAG_HWACCEL)) return *p;
    }
    return offered[0];
}

// ---- the player -----------------------------------------------------------------------------------------------

constexpr size_t AHEAD_FRAMES = 3;       // pictures decoded ahead of the one on screen (a 1080p picture is 3-5 MB)
constexpr double AHEAD_AUDIO_S = 1.0;    // sound decoded ahead of what's playing
constexpr double SYNC_S = 0.08;          // sound further off the clock than this is skipped or waited for
constexpr size_t LOOP_KEEP_BYTES = 48u * 1024 * 1024;  // a silent loop (a GIF) this small is decoded once
constexpr int MAX_W = 1920, MAX_H = 1920;  // bigger videos are shrunk while decoding: no canvas needs more
constexpr int RATE = 48000;
constexpr int BAD_PACKETS = 50;          // this many packets in a row that won't decode: the file is given up

double now_s() {
    return std::chrono::duration<double>(std::chrono::steady_clock::now().time_since_epoch()).count();
}

float clamp_speed(float value) {
    if (!(value == value)) return 1.0f;
    return std::min(4.0f, std::max(0.1f, value));
}

// Decoder threads for a video this size. FFmpeg's own choice is one per CPU thread, started again each time a
// looping file comes round; two decode up to 1080p far faster than real time (sources/media.py decode_threads).
int decode_threads(int width, int height) { return width * height <= 1920 * 1088 ? 2 : 4; }

struct Error : std::runtime_error {
    using std::runtime_error::runtime_error;
};

struct Picture {
    std::vector<uint8_t> data;
    int format = SC_FMT_RGBA;
    int width = 0, height = 0, stride = 0;
    int matrix = 601, full = 0;
    // Left on the graphics card (the engine's own device): the decoder's picture, `width` x `height` NV12 in one
    // slice of its texture, with nothing in `data`. Only the engine can take it (media_due).
    std::shared_ptr<AVFrame> card;
};
using PicturePtr = std::shared_ptr<const Picture>;

// The memory of pictures let go of, taken again for the next ones. A new 3-8 MB picture each frame was fresh
// memory from Windows each time, zeroed page by page: live with three cameras and a clip the PC took ~420,000
// page faults a second (2026-11-21), kernel time ~30%. Kept: at most SPARE_PICTURES of them, SPARE_BYTES in all.
constexpr size_t SPARE_PICTURES = 16;
constexpr size_t SPARE_BYTES = size_t(96) << 20;
constexpr size_t SPARE_AT_LEAST = size_t(256) << 10;  // smaller ones the heap reuses well by itself

struct Spares {
    std::mutex lock;
    std::deque<std::vector<uint8_t>> kept;  // oldest first
    size_t bytes = 0;
};

Spares &spares() {
    static Spares *all = new Spares();  // never destroyed: a picture may be let go of after statics are gone
    return *all;
}

// Memory for n bytes of picture: one let go of of about that size (not one far bigger), else none (empty).
std::vector<uint8_t> spare_bytes(size_t n) {
    if (n < SPARE_AT_LEAST) return {};
    Spares &s = spares();
    std::lock_guard<std::mutex> hold(s.lock);
    for (auto it = s.kept.rbegin(); it != s.kept.rend(); ++it) {  // newest first: still in the CPU's caches
        if (it->capacity() >= n && it->capacity() <= n + n / 2) {
            std::vector<uint8_t> found = std::move(*it);
            s.kept.erase(std::next(it).base());
            s.bytes -= found.capacity();
            return found;
        }
    }
    return {};
}

void keep_bytes(std::vector<uint8_t> &&bytes) {
    if (bytes.capacity() < SPARE_AT_LEAST || bytes.capacity() > SPARE_BYTES) return;
    std::vector<std::vector<uint8_t>> dropped;  // freed after the lock
    Spares &s = spares();
    std::lock_guard<std::mutex> hold(s.lock);
    while (!s.kept.empty() && (s.kept.size() >= SPARE_PICTURES || s.bytes + bytes.capacity() > SPARE_BYTES)) {
        s.bytes -= s.kept.front().capacity();
        dropped.push_back(std::move(s.kept.front()));
        s.kept.pop_front();
    }
    s.bytes += bytes.capacity();
    s.kept.push_back(std::move(bytes));
}

// A new picture whose memory is kept for the next one when it is let go of.
std::shared_ptr<Picture> new_picture() {
    return std::shared_ptr<Picture>(new Picture(), [](Picture *p) {
        keep_bytes(std::move(p->data));
        delete p;
    });
}

// The picture's memory, n bytes: a spare one when there is one (already that size: nothing is zeroed again).
void size_data(Picture &pic, size_t n) {
    if (pic.data.capacity() < n) {
        std::vector<uint8_t> spare = spare_bytes(n);
        if (spare.capacity() >= n) pic.data = std::move(spare);
    }
    pic.data.resize(n);
}

struct Chunk {
    double start = 0;                              // timeline time of the sample at `at`
    std::shared_ptr<std::vector<float>> samples;   // stereo, interleaved
    size_t at = 0;                                 // frames already played
    size_t frames() const { return samples->size() / 2 - at; }
};

// The open file and decoders of one decoder thread, kept from one play to the next: a wake, seek or restart
// reuses them instead of opening the file and starting FFmpeg's threads again. Only that thread touches them.
struct Held {
    std::string key;
    AVFormatContext *fmt = nullptr;
    AVCodecContext *vdec = nullptr, *adec = nullptr;
    int vi = -1, ai = -1;
    AVPacket *pkt = nullptr;
    AVFrame *frame = nullptr;
    AVFrame *cpu = nullptr;  // a picture decoded on the graphics card, copied here to be packed
    bool hardware = false;   // the video decoder decodes on the graphics card
    SwsContext *sws = nullptr;
    int sws_key[7] = {0, 0, 0, 0, 0, 0, 0};
    std::vector<uint8_t> planes[4];  // where the scaler writes (padded rows), before a picture is packed

    void release() {
        key.clear();
        if (sws) ff.sws_freeContext(sws);
        sws = nullptr;
        if (vdec) ff.avcodec_free_context(&vdec);
        if (adec) ff.avcodec_free_context(&adec);
        if (fmt) ff.avformat_close_input(&fmt);
        if (pkt) ff.av_packet_free(&pkt);
        if (frame) ff.av_frame_free(&frame);
        if (cpu) ff.av_frame_free(&cpu);
        hardware = false;
        vi = ai = -1;
    }
    // After a fault inside FFmpeg nothing of it is touched again: what it held is left behind.
    void abandon() {
        fmt = nullptr;
        vdec = adec = nullptr;
        pkt = nullptr;
        frame = nullptr;
        cpu = nullptr;
        sws = nullptr;
    }
};

struct Resampler {
    SwrContext *ctx = nullptr;
    int rate = 0, format = -1, channels = 0, out_rate = RATE;
    ~Resampler() {
        if (ctx) ff.swr_free(&ctx);
    }
};

struct Core : std::enable_shared_from_this<Core> {
    std::mutex lock;
    std::condition_variable wake;

    // settings
    std::string path;
    // A camera (sc_media_set_device): opened through an FFmpeg input device, its newest picture always shown,
    // no clock and no end; unplugged, it is opened again every 2 s.
    bool live = false;
    std::string device_format;
    std::vector<std::pair<std::string, std::string>> device_choices;  // (input name, "key=value:..." options)
    double live_gap = 0;           // packets closer than this are skipped (a camera faster than wanted)
    std::string refusal_said;      // the modes a camera refused, as last logged (said again only when it changes)
    bool opening_said = false;     // "opening a camera" is in the log for this run of tries (until it opens)
    std::atomic<bool> mirror{false};
    bool loop = false, clear_on_end = false, restart_on_show = true;
    float speed_setting = 1.0f;

    // what's decoded and waiting
    std::deque<std::pair<double, PicturePtr>> frames;
    std::deque<Chunk> audio;
    double audio_s = 0;

    // the clock and the state (sources/media.py MediaSource, field for field)
    int state = SC_MEDIA_PLAYING;
    double base = 0;             // position when the clock last stopped or jumped
    bool running = false;        // the clock runs from `anchor`
    double anchor = 0;
    uint64_t generation = 0;     // bumped by a seek or restart: the decoder starts over
    double start_at = 0;
    bool finished = false;       // the file (not looping) is fully decoded: the timeline ends at finished_at
    double finished_at = 0;
    bool has_shown = false;
    double shown = 0;
    // The picture on screen now, for whoever shows it (Python's renderer, the engine's compositor): each remembers
    // the serial it last took, so a newcomer gets the picture at once and nobody takes one twice.
    PicturePtr on_screen;
    bool on_screen_blank = false;
    uint64_t screen_serial = 0;
    bool hidden = false, paused_by_hide = false;
    float speed = 1.0f;          // the speed the decoder and clock run at now
    bool blank = false;
    int width = 0, height = 0;
    int given_width = 0, given_height = 0;  // what the file or device gives, before it is fitted
    double duration = 0;         // 0 = unknown
    bool has_video = true;
    bool parked = false;         // asleep
    std::string status = "no file";
    bool faulted = false;
    bool replaying = false;
    uint64_t decoded = 0, passes = 0, picture_bytes = 0;

    std::atomic<float> shrink{1.0f};
    std::atomic<int> picture_format{0};              // sc_media_set_picture
    std::atomic<bool> grow{false};
    std::atomic<bool> hardware{false};             // sc_media_set_decode: decode on the graphics card when it can
    bool on_card = false;                          // the pictures now are decoded there
    std::atomic<double> engine_took{-1e9};         // when the engine last asked for a picture (media_due)
    std::atomic<double> python_took{-1e9};         // when Python last did (sc_media_video_frame)

    // A picture decoded on the engine's own device, kept there for the engine (Picture::card), when only the
    // engine is taking this file's pictures. Null: copy it to memory as before.
    PicturePtr card_picture(const AVFrame *f) const;
    std::atomic<int> max_w{MAX_W}, max_h{MAX_H};
    std::atomic<int> fault{0};

    // threads: `thread` is the serial of the decoder thread that counts (0 = none). An older one that is still
    // finishing sees it isn't the one any more and ends by itself: nothing ever waits for it.
    uint64_t thread = 0, next_thread = 0;
    int alive = 0;
    std::condition_variable ended;

    // ---- the clock (lock held) ----
    double position() const { return running ? base + (now_s() - anchor) * speed : base; }
    void run_clock() {  // starts once the first picture or sound is decoded, so nothing at the start is lost
        if (state == SC_MEDIA_PLAYING && !running && (!frames.empty() || !audio.empty())) {
            running = true;
            anchor = now_s();
        }
    }
    void clear_queues() {
        frames.clear();
        audio.clear();
        audio_s = 0;
    }
    void jump(double seconds, int to) {
        ++generation;
        start_at = base = seconds;
        running = false;
        state = to;
        paused_by_hide = false;
        speed = clamp_speed(speed_setting);
        finished = false;
        clear_queues();
        has_shown = false;
        if (to == SC_MEDIA_STOPPED && has_video) blank = true;
        wake.notify_all();
    }
    void check_end() {
        if (state == SC_MEDIA_PLAYING && finished && position() >= finished_at && frames.empty() && audio.empty()) {
            base = finished_at;
            running = false;
            state = SC_MEDIA_ENDED;
            if (clear_on_end && has_video) blank = true;
        }
    }
    bool current(uint64_t gen, uint64_t mine) const { return thread == mine && generation == gen; }
    // Moves on to the picture due now on the file's clock (sources/media.py video_frame).
    void advance() {
        if (parked) return;  // asleep: nothing is due (the engine may still ask for a frame or two)
        if (blank) {
            blank = false;
            on_screen = nullptr;
            on_screen_blank = true;
            ++screen_serial;
            return;
        }
        if (live) {  // a camera: the newest picture, as soon as it's decoded
            if (!frames.empty()) {
                on_screen = frames.back().second;
                on_screen_blank = false;
                ++screen_serial;
                has_shown = true;
                frames.clear();
                wake.notify_all();
            }
            return;
        }
        if (state != SC_MEDIA_PLAYING && has_shown) return;
        run_clock();
        const double now = position();
        while (frames.size() >= 2 && frames[1].first <= now) {
            frames.pop_front();
            wake.notify_all();
        }
        if (!frames.empty() && (frames[0].first <= now + 0.005 || !has_shown)) {
            const double at = frames[0].first;
            const bool fresh = !has_shown || at != shown;
            if (fresh) {
                on_screen = frames[0].second;
                on_screen_blank = false;
                ++screen_serial;
                has_shown = true;
                shown = at;
            }
            if (frames.size() == 1) {  // the last one decoded: hand it over and make room
                frames.pop_front();
                wake.notify_all();
            }
            if (fresh) return;
        }
        check_end();
    }

    // ---- controls (any thread) ----
    void start(bool ensure);
    void close();
    void control(int what, double value);

    // ---- the decoder thread ----
    void run(uint64_t serial);
    bool play_once(Held &held, uint64_t serial);
    bool decode(const std::string &file, uint64_t gen, uint64_t serial, double from, float at_speed, Held &held);
    void open(Held &held, const std::string &file);
    bool rewind(Held &held);
    void probe(const std::string &file);
    void wait_for_change(uint64_t gen, uint64_t serial, double timeout);
    bool push_frame(double t, PicturePtr picture, uint64_t gen, uint64_t serial);
    bool push_audio(double t, std::shared_ptr<std::vector<float>> samples, uint64_t gen, uint64_t serial);
    void check_fault();
};

void Core::start(bool ensure) {
    std::unique_lock<std::mutex> hold(lock);
    if (ensure && parked) return;  // wake starts one
    if (thread != 0 || faulted) return;
    const uint64_t serial = ++next_thread;
    thread = serial;
    ++alive;
    wake.notify_all();  // an old thread still waiting sees it isn't the one any more
    auto self = shared_from_this();
    // Named for what it reads: every player was "SprigCast media player", so a camera's decoding (16% of a core
    // on the PC, 2026-11-21) read as a clip playing when none was.
    const wchar_t *name = live ? L"SprigCast camera reader" : has_video ? L"SprigCast video player"
                                                                         : L"SprigCast sound file player";
    hold.unlock();
    try {
        std::thread([self, serial, name] {
            sc::name_thread(name);
            self->run(serial);
        }).detach();
    } catch (...) {
        std::lock_guard<std::mutex> again(lock);
        if (thread == serial) thread = 0;
        --alive;
        status = "can't play: no thread";
        throw;
    }
}

void Core::close() {
    std::unique_lock<std::mutex> hold(lock);
    parked = false;
    thread = 0;
    wake.notify_all();
    ended.wait_for(hold, std::chrono::seconds(2), [this] { return alive == 0; });
    clear_queues();
}

void Core::control(int what, double value) {
    bool begin = false;
    {
        std::lock_guard<std::mutex> hold(lock);
        switch (what) {
        case SC_MEDIA_PLAY:
            paused_by_hide = false;
            if (state == SC_MEDIA_ENDED || state == SC_MEDIA_STOPPED) {
                jump(0, SC_MEDIA_PLAYING);
            } else if (state == SC_MEDIA_PAUSED) {
                state = SC_MEDIA_PLAYING;
                wake.notify_all();
            }
            break;
        case SC_MEDIA_PAUSE:
            paused_by_hide = false;
            if (state == SC_MEDIA_PLAYING) {
                base = position();
                running = false;
                state = SC_MEDIA_PAUSED;
            }
            break;
        case SC_MEDIA_RESTART:
            jump(0, SC_MEDIA_PLAYING);
            break;
        case SC_MEDIA_STOP:
            jump(0, SC_MEDIA_STOPPED);
            break;
        case SC_MEDIA_SEEK: {
            const int to = state == SC_MEDIA_PAUSED ? SC_MEDIA_PAUSED : SC_MEDIA_PLAYING;
            const double limit = duration > 0 ? duration : value;
            const bool by_hide = paused_by_hide;
            jump(std::min(std::max(0.0, value), std::max(0.0, limit - 0.05)), to);
            paused_by_hide = by_hide;
            break;
        }
        case SC_MEDIA_SLEEP: {
            const double at = position();
            running = false;
            base = at;
            parked = true;
            ++generation;  // the decoder stops where it is
            clear_queues();
            has_shown = false;
            on_screen = nullptr;  // off air nothing is kept: the canvas has its last picture as a texture
            wake.notify_all();
            break;
        }
        case SC_MEDIA_WAKE: {
            const bool resume = paused_by_hide;  // its eye was off when it went to sleep: it plays when shown
            hidden = paused_by_hide = false;     // the renderer says again if its eye is off
            parked = false;
            begin = true;
            if (restart_on_show) {
                jump(0, SC_MEDIA_PLAYING);
            } else if (state == SC_MEDIA_ENDED) {
                if (clear_on_end || duration <= 0) {
                    blank = true;  // nothing, as when it ended
                    begin = false;
                } else {
                    jump(std::max(0.0, duration - 0.1), SC_MEDIA_PLAYING);  // its last picture, then ended again
                }
            } else {
                jump(duration > 0 ? std::fmod(base, duration) : base, resume ? SC_MEDIA_PLAYING : state);
            }
            break;
        }
        case SC_MEDIA_HIDE:
            if (!has_video || hidden) break;
            hidden = true;
            if (state == SC_MEDIA_PLAYING) {
                base = position();
                running = false;
                state = SC_MEDIA_PAUSED;
                paused_by_hide = true;
            }
            break;
        case SC_MEDIA_SHOW:
            if (!has_video || !hidden) break;
            hidden = false;
            if (restart_on_show) {
                jump(0, SC_MEDIA_PLAYING);
            } else if (paused_by_hide) {
                paused_by_hide = false;
                state = SC_MEDIA_PLAYING;
                wake.notify_all();
            }
            break;
        case SC_MEDIA_RESPEED:  // decode again from here: its sound is resampled for the new speed
            if (state == SC_MEDIA_PLAYING || state == SC_MEDIA_PAUSED) {
                const bool by_hide = paused_by_hide;
                jump(position(), state);
                paused_by_hide = by_hide;
            } else {
                speed = clamp_speed(speed_setting);
            }
            break;
        default:
            break;
        }
    }
    if (what == SC_MEDIA_START) start(value != 0);
    if (what == SC_MEDIA_CLOSE) close();
    if (begin) start(false);
}

// ---- the decoder thread ---------------------------------------------------------------------------------------

void Core::check_fault() {
    const int kind = fault.exchange(0);
    if (kind == 1) throw std::logic_error("deliberate fault");
#if defined(_MSC_VER)
    if (kind == 2) {
        volatile int *nowhere = nullptr;
        *nowhere = 1;  // an access violation: /EHa turns it into something catch (...) stops
    }
#else
    if (kind == 2) throw std::logic_error("deliberate fault");
#endif
}

void Core::run(uint64_t serial) {
    Held held;
    try {
        for (;;) {
            uint64_t gen;
            {
                std::lock_guard<std::mutex> hold(lock);
                if (thread != serial) break;
                if (parked) {  // asleep: this thread is done, and wake starts another
                    thread = 0;
                    break;
                }
                gen = generation;
            }
            if (play_once(held, serial)) {
                // Played to the end (or waiting for play): let everything go, unless a control just asked for more.
                std::lock_guard<std::mutex> hold(lock);
                if (generation == gen && thread == serial) {
                    thread = 0;
                    break;
                }
            }
        }
        held.release();  // on this thread, after its last frame: nothing else ever touches them
    } catch (...) {
        // Crash gate: a fault in FFmpeg or in our own code ends this file, never the app. Nothing of FFmpeg's is
        // touched again (it may be what broke): the open file and decoders are left behind.
        held.abandon();
        sc::log_line(SC_LOG_ERROR, "media: the decoder faulted; this file is given up");
        std::lock_guard<std::mutex> hold(lock);
        faulted = true;
        status = "can't play: the decoder faulted";
        finished = true;
        finished_at = position();
        clear_queues();
    }
    std::lock_guard<std::mutex> hold(lock);
    if (thread == serial) thread = 0;
    replaying = false;
    --alive;
    ended.notify_all();
}

void Core::wait_for_change(uint64_t gen, uint64_t serial, double timeout) {
    std::unique_lock<std::mutex> hold(lock);
    auto changed = [&] { return thread != serial || generation != gen || parked; };
    if (timeout > 0) {
        wake.wait_for(hold, std::chrono::duration<double>(timeout), changed);
    } else {
        wake.wait(hold, changed);
    }
}

bool Core::play_once(Held &held, uint64_t serial) {
    std::string file;
    uint64_t gen;
    double from;
    float at_speed;
    {
        std::lock_guard<std::mutex> hold(lock);
        file = path;
        gen = generation;
        from = start_at;
        at_speed = speed;
        if (parked) return false;
        if (file.empty()) status = "no file";
    }
    if (file.empty()) {
        held.release();
        wait_for_change(gen, serial, 0);
        return false;
    }
    try {
        return decode(file, gen, serial, from, at_speed, held);
    } catch (const Error &e) {
        held.release();  // broken partway: open it fresh next time
        bool camera;
        {
            std::lock_guard<std::mutex> hold(lock);
            camera = live;
            if (camera) {
                status = (std::string(status == "ok" ? "camera disconnected, retrying: " : "camera not available, "
                                                                                          "retrying: ") + e.what())
                             .substr(0, 80);
            }
        }
        if (camera) {  // unplugged or busy: opened again in a moment, as Python's feed did
            sc::log_line(SC_LOG_INFO, std::string("media: a camera couldn't be read: ") + e.what());
            wait_for_change(gen, serial, 2.0);
            return false;
        }
        sc::log_line(SC_LOG_WARNING, "media: " + file + " could not be played: " + e.what());
        bool started;
        {
            std::lock_guard<std::mutex> hold(lock);
            started = status == "ok";
            status = (std::string("can't play: ") + e.what()).substr(0, 80);
            replaying = false;
            if (started && generation == gen) {  // it broke partway: end there, never start over by itself
                finished = true;
                finished_at = position();
            }
        }
        // Couldn't open it: try again in a bit (a file still copying, a drive waking up).
        wait_for_change(gen, serial, started ? 0 : 5.0);
        return false;
    }
}

void Core::probe(const std::string &file) {
    AVFormatContext *fmt = nullptr;
    int r = ff.avformat_open_input(&fmt, file.c_str(), nullptr, nullptr);
    if (r < 0) throw Error(av_text(r));
    r = ff.avformat_find_stream_info(fmt, nullptr);
    bool video = false;
    for (unsigned i = 0; r >= 0 && i < fmt->nb_streams; ++i) {
        const AVStream *s = fmt->streams[i];
        video = video || (s->codecpar->codec_type == AVMEDIA_TYPE_VIDEO &&
                          !(s->disposition & AV_DISPOSITION_ATTACHED_PIC));
    }
    const int64_t length = fmt->duration;
    ff.avformat_close_input(&fmt);
    if (r < 0) throw Error(av_text(r));
    std::lock_guard<std::mutex> hold(lock);
    has_video = video;
    if (length != AV_NOPTS_VALUE) duration = std::max(0.04, static_cast<double>(length) / 1e6);
}

void Core::open(Held &held, const std::string &file) {
    int r = 0;
    bool camera;
    std::string refused;  // a camera: the modes it refused and why
    std::string format;
    std::vector<std::pair<std::string, std::string>> choices;
    {
        std::lock_guard<std::mutex> hold(lock);
        camera = live;
        format = device_format;
        choices = device_choices;
    }
    if (camera) {  // each name and mode in turn, best first (sources/camera.py chooses them)
        const AVInputFormat *input = ff.av_find_input_format(format.c_str());
        if (!input) throw Error("this FFmpeg can't open " + format + " devices");
        r = AVERROR(ENODEV);
        // Said before the first try: a card with no signal can hold an open for a long time, and the log had
        // nothing at all about a camera whose open never came back (PC, 2026-11-21).
        bool first_try;
        {
            std::lock_guard<std::mutex> hold(lock);
            first_try = !opening_said;
            opening_said = true;
        }
        if (first_try)
            sc::log_line(SC_LOG_INFO, "media: opening a camera, " + std::to_string(choices.size()) + " modes to try");
        for (const auto &[name, options] : choices) {
            AVDictionary *opts = nullptr;
            if (!options.empty()) ff.av_dict_parse_string(&opts, options.c_str(), "=", ":", 0);
            ff.av_dict_set(&opts, "rtbufsize", "16M", 0);
            r = ff.avformat_open_input(&held.fmt, name.c_str(), input, &opts);
            ff.av_dict_free(&opts);
            if (r < 0 && refused.size() < 240)  // (the mode only: a device's name can hold its path)
                refused += (refused.empty() ? "" : "; ") + (options.empty() ? std::string("its own mode") : options) +
                           ": " + av_text(r);
            if (r >= 0) {
                sc::log_line(SC_LOG_INFO, "media: a camera opened with " + (options.empty() ? std::string("its own mode")
                                                                                            : options));
                // Its own mode after the ones asked were refused: say which and why (the PC's HD60 X with no signal
                // came up in its own, far bigger than asked, with nothing in the log about the modes it refused).
                if (!refused.empty()) sc::log_line(SC_LOG_INFO, "media: the camera refused " + refused);
                std::lock_guard<std::mutex> hold(lock);
                status = "ok";
                opening_said = false;
                refusal_said.clear();
                break;
            }
            held.fmt = nullptr;
        }
    } else {
        r = ff.avformat_open_input(&held.fmt, file.c_str(), nullptr, nullptr);
    }
    if (r < 0) {
        held.fmt = nullptr;
        if (camera) {  // none opened: say which modes it refused and why, once (it is tried again every 2 s)
            const std::string refused_all = refused.empty() ? av_text(r) : refused;
            bool news;
            {
                std::lock_guard<std::mutex> hold(lock);
                news = refusal_said != refused_all;
                refusal_said = refused_all;
            }
            if (news) sc::log_line(SC_LOG_WARNING, "media: the camera opened in no mode: " + refused_all);
        }
        throw Error(av_text(r));
    }
    if (camera) sc::log_line(SC_LOG_INFO, "media: reading what the camera gives");
    r = ff.avformat_find_stream_info(held.fmt, nullptr);
    if (r < 0) throw Error(av_text(r));
    held.vi = held.ai = -1;
    for (unsigned i = 0; i < held.fmt->nb_streams; ++i) {
        AVStream *s = held.fmt->streams[i];
        const AVMediaType type = s->codecpar->codec_type;
        // A sound file's cover picture is not a video.
        if (type == AVMEDIA_TYPE_VIDEO && held.vi < 0 && !(s->disposition & AV_DISPOSITION_ATTACHED_PIC)) {
            held.vi = static_cast<int>(i);
        } else if (type == AVMEDIA_TYPE_AUDIO && held.ai < 0 && !camera) {
            held.ai = static_cast<int>(i);
        } else {
            s->discard = AVDISCARD_ALL;
        }
    }
    if (held.vi < 0 && held.ai < 0) throw Error("no picture or sound in the file");
    if (camera && held.vi >= 0) {  // what it gives, on every open: size, format and rate
        const AVStream *v = held.fmt->streams[held.vi];
        const AVCodec *codec = ff.avcodec_find_decoder(v->codecpar->codec_id);
        std::string gives = codec && codec->name ? codec->name : "an unknown format";
        if (v->codecpar->codec_id == AV_CODEC_ID_RAWVIDEO) {
            const int f = v->codecpar->format;
            gives = f == AV_PIX_FMT_NV12 ? "nv12" : f == AV_PIX_FMT_YUYV422 ? "yuyv422" : f == AV_PIX_FMT_UYVY422 ? "uyvy422"
                     : f == AV_PIX_FMT_YUV420P ? "yuv420p" : f == AV_PIX_FMT_BGR24 ? "bgr24" : f == AV_PIX_FMT_BGRA ? "bgra"
                     : f == AV_PIX_FMT_BGR0 ? "bgr0" : "raw format " + std::to_string(f);
        }
        const double rate = v->avg_frame_rate.den > 0 ? av_q2d(v->avg_frame_rate) : 0.0;
        // A card that mirrors its input (the PC's HD60 X fed 1440p lists nothing smaller) gives more than was asked:
        // the picture is shrunk to the size asked as it is read, said here so the line isn't read as "shown at 1440p".
        const int given_w = v->codecpar->width, given_h = v->codecpar->height;
        const double fit_k = grow.load() ? 1.0
                                         : std::min({1.0, static_cast<double>(max_w.load()) / std::max(1, given_w),
                                                     static_cast<double>(max_h.load()) / std::max(1, given_h)});
        const int shown_w = std::max(2, static_cast<int>(given_w * fit_k) / 2 * 2);
        const int shown_h = std::max(2, static_cast<int>(given_h * fit_k) / 2 * 2);
        char text[200];
        if (fit_k < 1.0)
            std::snprintf(text, sizeof text, "media: the camera gives %dx%d %s at %.4g fps, shrunk to %dx%d as it is read",
                          given_w, given_h, gives.c_str(), rate, shown_w, shown_h);
        else
            std::snprintf(text, sizeof text, "media: the camera gives %dx%d %s at %.4g fps", given_w, given_h,
                          gives.c_str(), rate);
        sc::log_line(SC_LOG_INFO, text);
    }
    auto decoder = [&](int index, bool video) -> AVCodecContext * {
        const AVStream *s = held.fmt->streams[index];
        const AVCodec *codec = nullptr;
        // FFmpeg's own VP8 and VP9 decoders drop the see-through part of a WebM; libvpx keeps it. Only for a WebM
        // that has one (Matroska's AlphaMode): libvpx decodes on the CPU alone, and a plain VP9 clip (the in-app
        // MP4 to WebM converter's) cost the PC a dropped camera frame and half of YouTube's bitrate as it started
        // (2026-11-21). FFmpeg's own decoder can decode it on the graphics card.
        const AVDictionaryEntry *alpha = ff.av_dict_get(s->metadata, "alpha_mode", nullptr, 0);
        const bool see_through = video && alpha && alpha->value && std::strcmp(alpha->value, "0") != 0;
        if (see_through && s->codecpar->codec_id == AV_CODEC_ID_VP9) codec = ff.avcodec_find_decoder_by_name("libvpx-vp9");
        if (see_through && s->codecpar->codec_id == AV_CODEC_ID_VP8) codec = ff.avcodec_find_decoder_by_name("libvpx");
        if (!codec) codec = ff.avcodec_find_decoder(s->codecpar->codec_id);
        if (!codec) return nullptr;
        AVCodecContext *ctx = ff.avcodec_alloc_context3(codec);
        if (!ctx) throw std::bad_alloc();
        int e = ff.avcodec_parameters_to_context(ctx, s->codecpar);
        if (e >= 0) {
            ctx->pkt_timebase = s->time_base;
            if (video) {
                // A camera's MJPEG: one thread decodes 1080p60 in a few ms, and each more holds its own frames.
                ctx->thread_count = camera ? 1 : decode_threads(s->codecpar->width, s->codecpar->height);
                if (see_through) {  // libvpx (a stinger's see-through clip): picture and alpha, all on the CPU
                    const int cores = static_cast<int>(std::thread::hardware_concurrency());
                    ctx->thread_count = std::max(ctx->thread_count, std::min(4, std::max(1, cores)));
                }
                ctx->thread_type = FF_THREAD_FRAME | FF_THREAD_SLICE;
                AVHWDeviceType type = AV_HWDEVICE_TYPE_NONE;
                AVBufferRef *device = hardware.load() && !camera ? hw_device_ref(type) : nullptr;
                AVPixelFormat hw_format = AV_PIX_FMT_NONE;
                for (int i = 0; device; ++i) {  // libvpx (see-through WebM) has none: it stays on the CPU
                    const AVCodecHWConfig *config = ff.avcodec_get_hw_config(codec, i);
                    if (!config) break;
                    if ((config->methods & AV_CODEC_HW_CONFIG_METHOD_HW_DEVICE_CTX) && config->device_type == type) {
                        hw_format = config->pix_fmt;
                        break;
                    }
                }
                if (hw_format != AV_PIX_FMT_NONE) {
                    ctx->hw_device_ctx = device;  // the decoder keeps this reference
                    ctx->opaque = reinterpret_cast<void *>(static_cast<intptr_t>(hw_format));
                    ctx->get_format = pick_format;
                    ctx->thread_count = 1;  // the card decodes; FFmpeg's frame threads would only add copies
                    // Pictures left on the card hold their surface until shown and taken: room for those
                    // decoded ahead, the one on screen and the one being taken.
                    ctx->extra_hw_frames = static_cast<int>(AHEAD_FRAMES) + 3;
                    held.hardware = true;
                } else if (device) {
                    ff.av_buffer_unref(&device);
                }
            } else {
                ctx->thread_count = 1;
            }
            e = ff.avcodec_open2(ctx, codec, nullptr);
        }
        if (e < 0) {
            ff.avcodec_free_context(&ctx);
            return nullptr;
        }
        return ctx;
    };
    if (held.vi >= 0) {
        held.vdec = decoder(held.vi, true);
        if (!held.vdec) throw Error("no decoder for this video");
    }
    if (held.ai >= 0) {
        held.adec = decoder(held.ai, false);
        if (!held.adec) {
            if (held.vi < 0) throw Error("no decoder for this sound");
            sc::log_line(SC_LOG_WARNING, "media: " + file + ": no decoder for its sound; it plays silent");
            held.fmt->streams[held.ai]->discard = AVDISCARD_ALL;
            held.ai = -1;
        }
    }
    if (!held.pkt) held.pkt = ff.av_packet_alloc();
    if (!held.frame) held.frame = ff.av_frame_alloc();
    if (!held.pkt || !held.frame) throw std::bad_alloc();
}

// Back to the start of an open file for the next pass of a loop, with its decoders reset. False if the file
// can't seek (then it's opened again). Opening the file on every pass started FFmpeg's threads again each time.
bool Core::rewind(Held &held) {
    if (ff.av_seek_frame(held.fmt, -1, 0, AVSEEK_FLAG_BACKWARD) < 0) return false;
    if (held.vdec) ff.avcodec_flush_buffers(held.vdec);
    if (held.adec) ff.avcodec_flush_buffers(held.adec);
    return true;
}

bool Core::push_frame(double t, PicturePtr picture, uint64_t gen, uint64_t serial) {
    std::unique_lock<std::mutex> hold(lock);
    if (live) {  // a camera never waits for the screen: an older picture nobody took is dropped
        while (frames.size() >= 2) frames.pop_front();
    } else {
        wake.wait(hold, [&] { return !current(gen, serial) || frames.size() < AHEAD_FRAMES; });
    }
    if (!current(gen, serial)) return false;
    picture_bytes = std::max<uint64_t>(picture_bytes, picture->data.size());
    frames.emplace_back(t, std::move(picture));
    return true;
}

bool Core::push_audio(double t, std::shared_ptr<std::vector<float>> samples, uint64_t gen, uint64_t serial) {
    std::unique_lock<std::mutex> hold(lock);
    wake.wait(hold, [&] {
        return !current(gen, serial) || audio_s < AHEAD_AUDIO_S || (has_video && frames.size() < 2);
    });
    if (!current(gen, serial)) return false;
    audio_s += static_cast<double>(samples->size() / 2) / RATE;
    audio.push_back(Chunk{t, std::move(samples), 0});
    return true;
}

// The video's size shrunk (never grown) to fit inside the limit, kept even (render/stinger.py fit_size).
void fit_size(int w, int h, int max_w, int max_h, bool grow, int &out_w, int &out_h) {
    if (grow) {  // fitted to the box whichever way (store/instant_replay.py fit_into)
        const double k = std::min(static_cast<double>(max_w) / std::max(1, w),
                                  static_cast<double>(max_h) / std::max(1, h));
        out_w = std::max(2, static_cast<int>(std::lround(w * k / 2)) * 2);
        out_h = std::max(2, static_cast<int>(std::lround(h * k / 2)) * 2);
        return;
    }
    const double scale = std::min({1.0, static_cast<double>(max_w) / std::max(1, w),
                                   static_cast<double>(max_h) / std::max(1, h)});
    out_w = std::max(2, static_cast<int>(w * scale) / 2 * 2);
    out_h = std::max(2, static_cast<int>(h * scale) / 2 * 2);
}

// The picture size for a video shown at `shrink` of its size (1, 1/2 or 1/4), even on both sides.
void shrunk(int w, int h, float shrink, int &out_w, int &out_h) {
    out_w = w;
    out_h = h;
    if (shrink >= 1.0f) return;
    out_w = std::max(2, static_cast<int>(std::ceil(w * static_cast<double>(shrink) / 2)) * 2);
    out_h = std::max(2, static_cast<int>(std::ceil(h * static_cast<double>(shrink) / 2)) * 2);
}

void copy_rows(uint8_t *to, int to_stride, const uint8_t *from, int from_stride, int bytes, int rows) {
    for (int y = 0; y < rows; ++y) std::memcpy(to + static_cast<size_t>(y) * to_stride, from + y * from_stride, bytes);
}

// Scales or converts a decoded frame into held.planes (padded rows: the scaler may write past a row's end).
void scale(Held &held, const AVFrame *f, int ow, int oh, AVPixelFormat to, bool smaller, int strides[4]) {
    const int flags = smaller ? SWS_AREA : SWS_BILINEAR;
    const int key[7] = {f->width, f->height, f->format, ow, oh, to, flags};
    if (!held.sws || std::memcmp(key, held.sws_key, sizeof key) != 0) {
        if (held.sws) ff.sws_freeContext(held.sws);
        held.sws = ff.sws_getContext(f->width, f->height, static_cast<AVPixelFormat>(f->format), ow, oh, to, flags,
                                     nullptr, nullptr, nullptr);
        if (!held.sws) throw Error("its pictures can't be converted");
        std::memcpy(held.sws_key, key, sizeof key);
    }
    const bool rgba = to == AV_PIX_FMT_RGBA || to == AV_PIX_FMT_BGRA;
    const int planes = rgba ? 1 : (to == AV_PIX_FMT_YUVA420P ? 4 : 3);
    uint8_t *data[4] = {nullptr, nullptr, nullptr, nullptr};
    for (int i = 0; i < 4; ++i) strides[i] = 0;
    for (int i = 0; i < planes; ++i) {
        const bool chroma = !rgba && (i == 1 || i == 2);
        const int w = rgba ? ow * 4 : (chroma ? ow / 2 : ow);
        const int rows = chroma ? oh / 2 : oh;
        strides[i] = (w + 63) / 64 * 64;
        held.planes[i].resize(static_cast<size_t>(strides[i]) * rows + 64);
        data[i] = held.planes[i].data();
    }
    if (ff.sws_scale(held.sws, f->data, f->linesize, 0, f->height, data, strides) <= 0) {
        throw Error("its pictures can't be converted");
    }
}

// The picture to show for a decoded frame, ow x oh (even). 4:2:0 video stays as it is, packed into one
// 1-channel picture for the card to convert (sources/media.py pack_i420): Y rows, then rows of U | V side by
// side, then the alpha rows. Anything else becomes RGBA.
// as: 1 RGBA, 2 BGRA whatever the file holds (straight alpha), for whoever can't draw planar pictures; 0 as the
// file has them.
// How the compositor reads a picture's colours: untagged video is BT.601 TV range, as FFmpeg's own conversion.
void colours_of(const AVFrame *f, Picture &pic) {
    switch (f->colorspace) {
    case AVCOL_SPC_BT709: pic.matrix = 709; break;
    case AVCOL_SPC_BT2020_NCL:
    case AVCOL_SPC_BT2020_CL: pic.matrix = 2020; break;
    default: pic.matrix = 601; break;
    }
    pic.full = f->color_range == AVCOL_RANGE_JPEG || f->format == AV_PIX_FMT_YUVJ420P ||
               f->format == AV_PIX_FMT_YUVJ422P || f->format == AV_PIX_FMT_YUVJ444P;
}

PicturePtr Core::card_picture(const AVFrame *f) const {
#ifdef _WIN32
    constexpr double TAKER_S = 1.0;  // a taker that asked this recently is still taking
    if (f->format != AV_PIX_FMT_D3D11 || !f->hw_frames_ctx || card_refused.load()) return nullptr;
    const double now = now_s();
    if (now - engine_took.load() > TAKER_S || now - python_took.load() < TAKER_S) return nullptr;
    if (f->width < 2 || f->height < 2 || f->width % 2 || f->height % 2) return nullptr;
    auto *texture = reinterpret_cast<ID3D11Texture2D *>(f->data[0]);
    if (!texture) return nullptr;
    ID3D11Device *on = nullptr;
    texture->GetDevice(&on);
    if (on) on->Release();
    if (!on || on != card_now.load()) return nullptr;  // decoded on a device the engine no longer has
    AVFrame *held_frame = ff.av_frame_clone(f);
    if (!held_frame) return nullptr;
    auto pic = new_picture();
    pic->card = std::shared_ptr<AVFrame>(held_frame, [](AVFrame *g) { ff.av_frame_free(&g); });
    pic->format = SC_FMT_NV12;
    pic->width = pic->stride = f->width;
    pic->height = f->height;
    colours_of(f, *pic);
    return pic;
#else
    (void)f;
    return nullptr;
#endif
}

// A picture left on the card, copied to memory in the I420 layout make_picture gives NV12 (for Python's reads).
// Null if the card won't give it.
PicturePtr card_to_memory(const Picture &on_card) {
    AVFrame *cpu = ff.av_frame_alloc();
    if (!cpu) return nullptr;
    std::shared_ptr<AVFrame> hold_cpu(cpu, [](AVFrame *g) { ff.av_frame_free(&g); });
    if (ff.av_hwframe_transfer_data(cpu, on_card.card.get(), 0) < 0 || cpu->format != AV_PIX_FMT_NV12) return nullptr;
    const int w = on_card.width, h = on_card.height, cw = w / 2, ch = h / 2;
    auto pic = new_picture();
    pic->format = SC_FMT_I420;
    pic->width = pic->stride = w;
    pic->height = h + ch;
    pic->matrix = on_card.matrix;
    pic->full = on_card.full;
    size_data(*pic, static_cast<size_t>(w) * pic->height);
    uint8_t *y = pic->data.data();
    uint8_t *uv = y + static_cast<size_t>(w) * h;
    copy_rows(y, w, cpu->data[0], cpu->linesize[0], w, h);
    for (int row = 0; row < ch; ++row) {
        const uint8_t *from = cpu->data[1] + row * cpu->linesize[1];
        uint8_t *to_row = uv + static_cast<size_t>(row) * w;
        for (int x = 0; x < cw; ++x) {
            to_row[x] = from[2 * x];
            to_row[cw + x] = from[2 * x + 1];
        }
    }
    return pic;
}

// Left to right, in place: a mirrored camera (RGBA or BGRA pixels, or packed I420 planes).
void mirror_rows(Picture &pic) {
    auto flip = [](uint8_t *row, int count, int size) {
        for (int a = 0, b = count - 1; a < b; ++a, --b)
            for (int i = 0; i < size; ++i) std::swap(row[a * size + i], row[b * size + i]);
    };
    uint8_t *data = pic.data.data();
    if (pic.format != SC_FMT_I420 && pic.format != SC_FMT_I420A) {
        for (int y = 0; y < pic.height; ++y) flip(data + static_cast<size_t>(y) * pic.stride, pic.width, 4);
        return;
    }
    const int w = pic.width, cw = w / 2;
    const int h = pic.format == SC_FMT_I420A ? pic.height * 2 / 5 : pic.height * 2 / 3;  // height = h + h/2 (+ h)
    for (int y = 0; y < h; ++y) flip(data + static_cast<size_t>(y) * w, w, 1);
    for (int y = h; y < h + h / 2; ++y) {
        flip(data + static_cast<size_t>(y) * w, cw, 1);
        flip(data + static_cast<size_t>(y) * w + cw, cw, 1);
    }
    for (int y = h + h / 2; y < pic.height; ++y) flip(data + static_cast<size_t>(y) * w, w, 1);
}

PicturePtr make_picture(Held &held, const AVFrame *f, int ow, int oh, bool smaller, int as, bool camera = false,
                        bool mirrored = false) {
    auto pic = new_picture();
    int kind = 0;
    AVPixelFormat to = AV_PIX_FMT_RGBA;
    switch (f->format) {
    case AV_PIX_FMT_YUV420P: kind = 1; to = AV_PIX_FMT_YUV420P; break;
    case AV_PIX_FMT_YUVJ420P: kind = 1; to = AV_PIX_FMT_YUVJ420P; break;
    case AV_PIX_FMT_NV12: kind = 1; to = AV_PIX_FMT_YUV420P; break;
    case AV_PIX_FMT_YUVA420P: kind = 2; to = AV_PIX_FMT_YUVA420P; break;
    default: break;
    }
    // A camera's 4:2:2 (MJPEG's yuvj422p, raw YUY2) goes to the card as 4:2:0 planes, as Python's feed sent it:
    // half the bytes of RGBA, and the card makes the colour.
    if (camera && !kind) {
        switch (f->format) {
        case AV_PIX_FMT_YUVJ422P: case AV_PIX_FMT_YUVJ444P: kind = 1; to = AV_PIX_FMT_YUVJ420P; break;
        case AV_PIX_FMT_YUV422P: case AV_PIX_FMT_YUYV422: case AV_PIX_FMT_UYVY422: case AV_PIX_FMT_YUV444P:
            kind = 1;
            to = AV_PIX_FMT_YUV420P;
            break;
        default: break;
        }
    }
    if (as) kind = 0;
    if (kind && (ow % 2 || oh % 2)) kind = 0;  // 4:2:0 needs even sizes
    const bool direct = f->format == AV_PIX_FMT_YUV420P || f->format == AV_PIX_FMT_YUVJ420P ||
                        f->format == AV_PIX_FMT_NV12 || f->format == AV_PIX_FMT_YUVA420P;
    if (!kind) {
        int strides[4];
        scale(held, f, ow, oh, as == 2 ? AV_PIX_FMT_BGRA : AV_PIX_FMT_RGBA, smaller, strides);
        pic->format = as == 2 ? SC_FMT_BGRA : SC_FMT_RGBA;
        pic->width = ow;
        pic->height = oh;
        pic->stride = ow * 4;
        size_data(*pic, static_cast<size_t>(ow) * 4 * oh);
        copy_rows(pic->data.data(), ow * 4, held.planes[0].data(), strides[0], ow * 4, oh);
        if (mirrored) mirror_rows(*pic);
        return pic;
    }
    const int cw = ow / 2, ch = oh / 2;
    pic->format = kind == 2 ? SC_FMT_I420A : SC_FMT_I420;
    pic->width = pic->stride = ow;
    pic->height = oh + ch + (kind == 2 ? oh : 0);
    size_data(*pic, static_cast<size_t>(ow) * pic->height);
    uint8_t *y = pic->data.data();
    uint8_t *uv = y + static_cast<size_t>(ow) * oh;
    uint8_t *a = uv + static_cast<size_t>(ow) * ch;
    if (f->width == ow && f->height == oh && direct) {
        copy_rows(y, ow, f->data[0], f->linesize[0], ow, oh);
        if (f->format == AV_PIX_FMT_NV12) {  // U and V share one plane, UVUV...
            for (int row = 0; row < ch; ++row) {
                const uint8_t *from = f->data[1] + row * f->linesize[1];
                uint8_t *to_row = uv + static_cast<size_t>(row) * ow;
                for (int x = 0; x < cw; ++x) {
                    to_row[x] = from[2 * x];
                    to_row[cw + x] = from[2 * x + 1];
                }
            }
        } else {
            copy_rows(uv, ow, f->data[1], f->linesize[1], cw, ch);
            copy_rows(uv + cw, ow, f->data[2], f->linesize[2], cw, ch);
        }
        if (kind == 2) copy_rows(a, ow, f->data[3], f->linesize[3], ow, oh);
    } else {
        int strides[4];
        scale(held, f, ow, oh, to, smaller, strides);
        copy_rows(y, ow, held.planes[0].data(), strides[0], ow, oh);
        copy_rows(uv, ow, held.planes[1].data(), strides[1], cw, ch);
        copy_rows(uv + cw, ow, held.planes[2].data(), strides[2], cw, ch);
        if (kind == 2) copy_rows(a, ow, held.planes[3].data(), strides[3], ow, oh);
    }
    colours_of(f, *pic);
    if (mirrored) mirror_rows(*pic);
    return pic;
}

// A decoded sound frame as stereo floats at the resampler's rate.
std::shared_ptr<std::vector<float>> resample(Resampler &rs, const AVFrame *f) {
    const int channels = f->ch_layout.nb_channels;
    if (channels <= 0 || f->sample_rate <= 0 || f->nb_samples <= 0) return nullptr;
    if (!rs.ctx || rs.rate != f->sample_rate || rs.format != f->format || rs.channels != channels) {
        if (rs.ctx) ff.swr_free(&rs.ctx);
        AVChannelLayout stereo, in = f->ch_layout;
        ff.av_channel_layout_default(&stereo, 2);
        if (in.order == AV_CHANNEL_ORDER_UNSPEC) ff.av_channel_layout_default(&in, channels);
        int r = ff.swr_alloc_set_opts2(&rs.ctx, &stereo, AV_SAMPLE_FMT_FLT, rs.out_rate, &in,
                                       static_cast<AVSampleFormat>(f->format), f->sample_rate, 0, nullptr);
        if (r >= 0) r = ff.swr_init(rs.ctx);
        if (r < 0) {
            if (rs.ctx) ff.swr_free(&rs.ctx);
            throw Error("its sound can't be converted (" + av_text(r) + ")");
        }
        rs.rate = f->sample_rate;
        rs.format = f->format;
        rs.channels = channels;
    }
    const int room = std::max(0, ff.swr_get_out_samples(rs.ctx, f->nb_samples));
    auto out = std::make_shared<std::vector<float>>(static_cast<size_t>(room) * 2);
    if (!room) return nullptr;
    uint8_t *to[1] = {reinterpret_cast<uint8_t *>(out->data())};
    const int got = ff.swr_convert(rs.ctx, to, room, const_cast<const uint8_t *const *>(f->extended_data),
                                   f->nb_samples);
    if (got <= 0) return nullptr;
    out->resize(static_cast<size_t>(got) * 2);
    return out;
}

// Decode until the end, a jump or sleep. True when it played out (or is a file waiting for play).
bool Core::decode(const std::string &file, uint64_t gen, uint64_t serial, double from, float at_speed, Held &held) {
    bool camera;
    double gap;
    {
        std::lock_guard<std::mutex> hold(lock);
        camera = live;
        gap = live_gap;
    }
    if (!camera) {
        std::error_code ec;
        const std::u8string text(reinterpret_cast<const char8_t *>(file.data()), file.size());
        if (!std::filesystem::exists(std::filesystem::path(text), ec)) throw Error("file not found");
    }
    std::string key = file;  // asked anew, or the engine's device changed: reopened
    if (hardware.load()) {
        std::lock_guard<std::mutex> hold(hw_lock);
        key += "\n(on the graphics card " + std::to_string(card_serial) + ")";
    }
    if (held.key != key) held.release();
    held.key = key;
    if (camera) key = "camera\n" + key;
    double offset = 0;  // added to each pass's times when looping, so the timeline keeps running
    Resampler rs;       // one per play: a loop's next pass carries on with it
    rs.out_rate = std::max(8000, static_cast<int>(std::lround(RATE / static_cast<double>(at_speed))));
    // A silent looping clip small enough (LOOP_KEEP_BYTES): its first whole pass is kept and later passes play
    // from memory, with the file and decoder closed.
    std::vector<std::pair<double, PicturePtr>> kept, replay;
    bool keeping = false;
    size_t kept_bytes = 0;
    float kept_shrink = shrink.load();
    const double step = static_cast<double>(at_speed) / RATE;  // timeline seconds per resampled sample

    for (;;) {
        bool want_loop, stopped, known;
        double length;
        {
            std::lock_guard<std::mutex> hold(lock);
            if (!current(gen, serial)) return false;
            check_end();
            stopped = state == SC_MEDIA_STOPPED;
            known = duration > 0;
            want_loop = loop;
            length = duration;
            replaying = !stopped && !replay.empty() && kept_shrink == shrink.load() && want_loop;
        }
        if (stopped) {  // waiting for play: just learn what the file is
            if (!known) probe(file);
            std::lock_guard<std::mutex> hold(lock);
            status = "ok";
            return true;  // play starts a thread again
        }
        if (!replay.empty() && kept_shrink == shrink.load() && want_loop) {
            for (const auto &entry : replay) {  // from memory: nothing opened, nothing decoded
                if (!push_frame(offset + entry.first, entry.second, gen, serial)) return false;
            }
            offset += length;
            std::lock_guard<std::mutex> hold(lock);
            ++passes;
            continue;
        }
        replay.clear();  // shown at another size now (or loop turned off): decode as before
        if (!held.fmt) {
            open(held, file);
        } else if (!rewind(held)) {  // the next pass: from the top of the open file
            held.release();
            held.key = key;
            open(held, file);
        }
        const AVStream *video = held.vi >= 0 ? held.fmt->streams[held.vi] : nullptr;
        const AVStream *sound = held.ai >= 0 ? held.fmt->streams[held.ai] : nullptr;
        int size_w = 0, size_h = 0;
        const int as = picture_format.load();
        if (video) {
            fit_size(held.vdec->width, held.vdec->height, max_w.load(), max_h.load(), grow.load(), size_w, size_h);
        }
        {
            std::lock_guard<std::mutex> hold(lock);
            has_video = video != nullptr;
            if (held.fmt->duration != AV_NOPTS_VALUE) {
                duration = std::max(0.04, static_cast<double>(held.fmt->duration) / 1e6);
            } else if (video && video->duration != AV_NOPTS_VALUE && video->time_base.den) {
                duration = std::max(0.04, static_cast<double>(video->duration) * av_q2d(video->time_base));
            }
            length = duration;
            width = size_w;
            height = size_h;
            given_width = video ? held.vdec->width : 0;
            given_height = video ? held.vdec->height : 0;
            status = "ok";
        }
        // Times in the file can start above 0 (MPEG-TS, some MP4s): count from the file's start.
        const double origin = held.fmt->start_time != AV_NOPTS_VALUE ? static_cast<double>(held.fmt->start_time) / 1e6
                                                                     : 0.0;
        const double skip_to = offset == 0.0 ? from : 0.0;
        kept.clear();
        kept_bytes = 0;
        kept_shrink = shrink.load();
        keeping = !camera && !sound && video && skip_to <= 0.0 && want_loop && length > 0.05;
        if (skip_to > 0.5) {
            const AVStream *target = video ? video : sound;
            const int64_t ts = static_cast<int64_t>((skip_to + origin) / av_q2d(target->time_base));
            // If it can't seek it reads on from the start, and what comes before skip_to is thrown away below.
            ff.av_seek_frame(held.fmt, target->index, ts, AVSEEK_FLAG_BACKWARD);
            if (held.vdec) ff.avcodec_flush_buffers(held.vdec);
            if (held.adec) ff.avcodec_flush_buffers(held.adec);
        }
        int bad = 0;
        bool have_sound_time = false;
        double sound_time = 0;

        // One packet (or the end of the file, pkt = nullptr) through a decoder. False: stop decoding (a jump).
        auto pictures = [&](const AVPacket *pkt) -> bool {
            int r = ff.avcodec_send_packet(held.vdec, pkt);
            if (r < 0 && r != AVERROR(EAGAIN) && r != AVERROR_EOF) {
                if (++bad > BAD_PACKETS) throw Error(av_text(r));
                return true;
            }
            for (;;) {
                r = ff.avcodec_receive_frame(held.vdec, held.frame);
                if (r == AVERROR(EAGAIN) || r == AVERROR_EOF) return true;
                if (r < 0) {
                    if (++bad > BAD_PACKETS) throw Error(av_text(r));
                    return true;
                }
                bad = 0;
                AVFrame *f = held.frame;
                int64_t pts = f->pts != AV_NOPTS_VALUE ? f->pts : f->best_effort_timestamp;
                if (pts == AV_NOPTS_VALUE && pkt) pts = pkt->pts;
                if (PicturePtr on_card_picture = card_picture(f)) {  // left on the engine's card, as it is
                    const double at_card =
                        (pts != AV_NOPTS_VALUE ? static_cast<double>(pts) * av_q2d(video->time_base) : 0.0) - origin;
                    ff.av_frame_unref(f);
                    if (at_card < skip_to - 0.001) continue;
                    {
                        std::lock_guard<std::mutex> hold(lock);
                        on_card = true;
                        ++decoded;
                    }
                    keeping = false;  // a loop of these is decoded each pass: they belong to the decoder
                    kept.clear();
                    if (!push_frame(offset + at_card, std::move(on_card_picture), gen, serial)) return false;
                    continue;
                }
                if (f->hw_frames_ctx) {  // decoded on the graphics card: copied to memory (as NV12) to be packed
                    if (!held.cpu) held.cpu = ff.av_frame_alloc();
                    if (!held.cpu) throw std::bad_alloc();
                    ff.av_frame_unref(held.cpu);
                    const int got = ff.av_hwframe_transfer_data(held.cpu, f, 0);
                    held.cpu->colorspace = f->colorspace;
                    held.cpu->color_range = f->color_range;
                    ff.av_frame_unref(f);
                    if (got < 0) {
                        if (++bad > BAD_PACKETS) throw Error(av_text(got));
                        continue;
                    }
                    f = held.cpu;
                }
                const double at = (pts != AV_NOPTS_VALUE ? static_cast<double>(pts) * av_q2d(video->time_base) : 0.0)
                                  - origin;
                if (at < skip_to - 0.001) {
                    ff.av_frame_unref(f);
                    continue;
                }
                const float shrink_now = shrink.load();  // a new size shows from the next picture on
                int ow, oh;
                shrunk(size_w, size_h, shrink_now, ow, oh);
                PicturePtr picture = make_picture(held, f, ow, oh, ow < f->width || oh < f->height, as, camera,
                                                  camera && mirror.load());
                ff.av_frame_unref(f);
                {
                    std::lock_guard<std::mutex> hold(lock);
                    on_card = held.hardware;
                }
                if (keeping && shrink_now != kept_shrink) keeping = false;  // pictures of two sizes: not kept
                if (keeping) {
                    kept_bytes += picture->data.size();
                    if (kept_bytes > LOOP_KEEP_BYTES) {
                        keeping = false;  // too big: decoded each pass
                    } else {
                        kept.emplace_back(at, picture);
                    }
                }
                if (!keeping) kept.clear();
                {
                    std::lock_guard<std::mutex> hold(lock);
                    ++decoded;
                }
                if (!push_frame(offset + at, std::move(picture), gen, serial)) return false;
            }
        };
        auto sounds = [&](const AVPacket *pkt) -> bool {
            int r = ff.avcodec_send_packet(held.adec, pkt);
            if (r < 0 && r != AVERROR(EAGAIN) && r != AVERROR_EOF) {
                if (++bad > BAD_PACKETS) throw Error(av_text(r));
                return true;
            }
            for (;;) {
                r = ff.avcodec_receive_frame(held.adec, held.frame);
                if (r == AVERROR(EAGAIN) || r == AVERROR_EOF) return true;
                if (r < 0) {
                    if (++bad > BAD_PACKETS) throw Error(av_text(r));
                    return true;
                }
                bad = 0;
                AVFrame *f = held.frame;
                const int64_t pts = f->pts != AV_NOPTS_VALUE ? f->pts : f->best_effort_timestamp;
                auto samples = resample(rs, f);
                ff.av_frame_unref(f);
                if (pts != AV_NOPTS_VALUE) {
                    sound_time = static_cast<double>(pts) * av_q2d(sound->time_base) - origin;
                    have_sound_time = true;
                }
                if (!samples || !have_sound_time) continue;
                const double at = sound_time;
                const double span = static_cast<double>(samples->size() / 2) * step;
                sound_time += span;  // a frame without a time of its own follows the one before
                if (at + span < skip_to) continue;
                if (!push_audio(offset + at, std::move(samples), gen, serial)) return false;
            }
        };

        // A camera faster than the rate asked for (a Facecam's 120 for 60): MJPEG and raw pictures stand alone, so
        // the extra ones are skipped before they're decoded.
        const bool skippable = camera && gap > 0 && held.vdec &&
                               (held.vdec->codec_id == AV_CODEC_ID_MJPEG || held.vdec->codec_id == AV_CODEC_ID_RAWVIDEO ||
                                held.vdec->codec_id == AV_CODEC_ID_WRAPPED_AVFRAME);
        double last_packet = 0;
        for (;;) {
            {
                std::lock_guard<std::mutex> hold(lock);
                if (!current(gen, serial)) return false;
            }
            check_fault();
            const int r = ff.av_read_frame(held.fmt, held.pkt);
            if (r == AVERROR(EAGAIN)) {
                std::this_thread::sleep_for(std::chrono::milliseconds(2));
                continue;
            }
            if (r == AVERROR_EOF && camera) throw Error("the camera stopped sending pictures");
            if (r == AVERROR_EOF) break;
            if (r < 0) throw Error(av_text(r));
            if (skippable && held.pkt->stream_index == held.vi && held.pkt->size) {
                const double now = now_s();
                if (now - last_packet < gap) {
                    ff.av_packet_unref(held.pkt);
                    continue;
                }
                last_packet = now;
            }
            bool go = true;
            if (held.pkt->stream_index == held.vi) {
                go = pictures(held.pkt);
            } else if (held.pkt->stream_index == held.ai) {
                go = sounds(held.pkt);
            }
            ff.av_packet_unref(held.pkt);
            if (!go) return false;
        }
        // The end of the file: the last pictures and sound the decoders were holding back.
        if (held.vdec && !pictures(nullptr)) return false;
        if (held.adec && !sounds(nullptr)) return false;

        bool again;
        {
            std::lock_guard<std::mutex> hold(lock);
            ++passes;
            again = loop && length > 0.05;
            if (!again && generation == gen) {
                finished = true;
                finished_at = offset + length;
            }
        }
        if (!again) return true;  // played out: the thread closes the file and ends (run)
        if (keeping && !kept.empty()) {  // the whole pass was kept: later passes play from memory
            replay.swap(kept);
            kept.clear();
            held.release();
            held.key = key;
        }
        offset += length;
    }
}

int need_ffmpeg() {
    if (ff_ready.load()) return SC_OK;
    return sc::fail(SC_ERR_DEVICE, "media: FFmpeg isn't loaded");
}

}  // namespace

struct sc_media {
    std::shared_ptr<Core> core;
};

namespace sc {

std::shared_ptr<void> media_hold(sc_media *media) { return media ? media->core : nullptr; }

void media_card(void *device) {
    std::lock_guard<std::mutex> hold(hw_lock);
    if (device == card_device) return;
#ifdef _WIN32
    if (card_ref) ff.av_buffer_unref(&card_ref);  // decoders and pictures on it keep it until they end
    if (card_device) static_cast<ID3D11Device *>(card_device)->Release();
    if (device) static_cast<ID3D11Device *>(device)->AddRef();
#endif
    card_device = device;
    card_now.store(device);
    card_failed = false;
    card_refused.store(false);
    ++card_serial;
}

void media_card_gone(void *device) {
    {
        std::lock_guard<std::mutex> hold(hw_lock);
        if (device != card_device) return;
    }
    media_card(nullptr);
}

void media_card_refused() {
    if (!card_refused.exchange(true))
        log_line(SC_LOG_WARNING, "media: the engine can't take videos' pictures on the card; they come in memory");
}

void media_due(const std::shared_ptr<void> &player, uint64_t *seen, MediaDue *out) {
    out->what = SC_MEDIA_NO_PICTURE;
    out->keep.reset();
    out->texture = nullptr;
    out->slice = 0;
    Core &c = *static_cast<Core *>(player.get());
    c.engine_took.store(now_s());
    std::lock_guard<std::mutex> hold(c.lock);
    c.advance();
    if (*seen == c.screen_serial) return;
    *seen = c.screen_serial;
    if (c.on_screen_blank || !c.on_screen) {
        if (c.on_screen_blank) out->what = SC_MEDIA_BLANK;
        return;
    }
    const Picture &pic = *c.on_screen;
    out->what = SC_MEDIA_PICTURE;
    out->keep = c.on_screen;
    out->data = pic.data.data();
    out->how = sc_upload{};
    out->how.format = pic.format;
    out->how.width = pic.width;
    out->how.height = pic.height;
    out->how.stride = pic.stride;
    out->how.layout_width = c.width;
    out->how.layout_height = c.height;
    if (pic.card) {  // NV12 in one slice of the decoder's texture: rows of Y, then half as many of UV
        out->texture = pic.card->data[0];
        out->slice = static_cast<unsigned>(reinterpret_cast<intptr_t>(pic.card->data[1]));
        out->how.height = pic.height * 3 / 2;
        out->data = nullptr;
    }
    // render/compositor.py YUV_MATRICES: Kr, Kb, then 1 for full range.
    out->how.yuv[0] = pic.matrix == 709 ? 0.2126f : pic.matrix == 2020 ? 0.2627f : 0.299f;
    out->how.yuv[1] = pic.matrix == 709 ? 0.0722f : pic.matrix == 2020 ? 0.0593f : 0.114f;
    out->how.yuv[2] = pic.full ? 1.0f : 0.0f;
}

}  // namespace sc

extern "C" {

SC_API int sc_media_ffmpeg_load(const char *avutil, const char *avcodec, const char *avformat, const char *swscale,
                                const char *swresample) {
    return sc::guarded("sc_media_ffmpeg_load", [&]() -> int {
        std::lock_guard<std::mutex> hold(ff_lock);
        if (ff_ready.load()) return SC_OK;
        const char *paths[5] = {avutil, avcodec, avformat, swscale, swresample};
        void *libraries[5];
        for (int i = 0; i < 5; ++i) {
            if (!paths[i] || !*paths[i]) return sc::fail(SC_ERR_ARGUMENT, "media: an FFmpeg library path is missing");
            libraries[i] = open_library(paths[i]);
            if (!libraries[i]) return sc::fail(SC_ERR_DEVICE, std::string("media: couldn't load ") + paths[i]);
        }
        FFmpeg table;
#define X(lib, name)                                                                                               \
    table.name = reinterpret_cast<decltype(table.name)>(find_symbol(libraries[lib], #name));                       \
    if (!table.name) return sc::fail(SC_ERR_DEVICE, "media: FFmpeg has no " #name);
        SC_FF_FUNCTIONS(X)
#undef X
        const unsigned have[5] = {table.avutil_version() >> 16, table.avcodec_version() >> 16,
                                  table.avformat_version() >> 16, table.swscale_version() >> 16,
                                  table.swresample_version() >> 16};
        const unsigned want[5] = {LIBAVUTIL_VERSION_MAJOR, LIBAVCODEC_VERSION_MAJOR, LIBAVFORMAT_VERSION_MAJOR,
                                  LIBSWSCALE_VERSION_MAJOR, LIBSWRESAMPLE_VERSION_MAJOR};
        for (int i = 0; i < 5; ++i) {
            if (have[i] != want[i]) {
                return sc::fail(SC_ERR_DEVICE, std::string("media: ") + paths[i] + " is version " +
                                                   std::to_string(have[i]) + ", the engine was built for " +
                                                   std::to_string(want[i]));
            }
        }
        ff = table;
        ff_ready.store(true);
        sc::log_line(SC_LOG_INFO, "media: FFmpeg loaded (libavcodec " + std::to_string(have[1]) + ")");
        return SC_OK;
    });
}

SC_API int sc_media_ffmpeg_ready(void) { return ff_ready.load() ? 1 : 0; }

SC_API int sc_media_ffmpeg_load_devices(const char *avdevice) {
    return sc::guarded("sc_media_ffmpeg_load_devices", [&]() -> int {
        std::lock_guard<std::mutex> hold(ff_lock);
        if (!ff_ready.load()) return sc::fail(SC_ERR_ARGUMENT, "media: FFmpeg isn't loaded yet");
        if (devices_ready.load()) return SC_OK;
        if (!avdevice || !*avdevice) return sc::fail(SC_ERR_ARGUMENT, "media: no libavdevice path");
        void *library = open_library(avdevice);
        if (!library) return sc::fail(SC_ERR_DEVICE, std::string("media: couldn't load ") + avdevice);
        using Version = unsigned (*)(void);
        using Register = void (*)(void);
        auto version = reinterpret_cast<Version>(find_symbol(library, "avdevice_version"));
        auto register_all = reinterpret_cast<Register>(find_symbol(library, "avdevice_register_all"));
        if (!version || !register_all) return sc::fail(SC_ERR_DEVICE, "media: libavdevice has no avdevice_register_all");
        register_all();
        devices_ready.store(true);
        sc::log_line(SC_LOG_INFO, "media: FFmpeg's devices loaded (libavdevice " + std::to_string(version() >> 16) +
                                      ")");
        return SC_OK;
    });
}

SC_API int sc_media_set_device(sc_media *media, const char *format, const char *choices, int fps, int mirror) {
    return sc::guarded("sc_media_set_device", [&]() -> int {
        if (!media || !format || !*format || !choices || !*choices || fps < 0 || fps > 1000)
            return sc::fail(SC_ERR_ARGUMENT, "sc_media_set_device: no device format or choices");
        if (!devices_ready.load()) return sc::fail(SC_ERR_DEVICE, "sc_media_set_device: FFmpeg's devices aren't loaded");
        std::vector<std::pair<std::string, std::string>> parsed;
        const std::string text(choices);
        for (size_t at = 0; at <= text.size();) {
            size_t end = text.find('\n', at);
            if (end == std::string::npos) end = text.size();
            const std::string line = text.substr(at, end - at);
            const size_t tab = line.find('\t');
            if (!line.empty()) parsed.emplace_back(line.substr(0, tab), tab == std::string::npos ? "" : line.substr(tab + 1));
            at = end + 1;
        }
        if (parsed.empty()) return sc::fail(SC_ERR_ARGUMENT, "sc_media_set_device: no choices");
        Core &c = *media->core;
        c.mirror.store(mirror != 0);
        {
            std::lock_guard<std::mutex> hold(c.lock);
            c.live = true;
            c.device_format = format;
            c.device_choices = std::move(parsed);
            c.live_gap = fps > 0 ? 0.95 / fps : 0.0;
            c.path = c.device_choices.front().first;
            c.has_video = true;
            c.duration = 0;
            c.width = c.height = 0;
            c.loop = false;
            c.status = "starting";
            c.jump(0, SC_MEDIA_PLAYING);
        }
        c.start(false);  // a camera is read from now on (a file waits for play)
        return SC_OK;
    });
}

SC_API int sc_media_set_mirror(sc_media *media, int mirror) {
    if (!media) return SC_ERR_ARGUMENT;
    media->core->mirror.store(mirror != 0);
    return SC_OK;
}

SC_API int sc_media_create(int playing, sc_media **out) {
    return sc::guarded("sc_media_create", [&]() -> int {
        if (!out) return sc::fail(SC_ERR_ARGUMENT, "sc_media_create: no place for the player");
        *out = nullptr;
        if (int r = need_ffmpeg()) return r;
        auto media = std::make_unique<sc_media>();
        media->core = std::make_shared<Core>();
        media->core->state = playing ? SC_MEDIA_PLAYING : SC_MEDIA_STOPPED;
        *out = media.release();
        return SC_OK;
    });
}

SC_API void sc_media_destroy(sc_media *media) {
    if (!media) return;
    sc::guarded("sc_media_destroy", [&]() -> int {
        media->core->close();
        return SC_OK;
    });
    delete media;
}

SC_API int sc_media_set_options(sc_media *media, const sc_media_options *options) {
    return sc::guarded("sc_media_set_options", [&]() -> int {
        if (!media || !options) return sc::fail(SC_ERR_ARGUMENT, "sc_media_set_options: nothing given");
        Core &c = *media->core;
        std::lock_guard<std::mutex> hold(c.lock);
        c.loop = options->loop != 0;
        c.clear_on_end = options->clear_on_end != 0;
        c.restart_on_show = options->restart_on_show != 0;
        c.speed_setting = clamp_speed(options->speed);
        return SC_OK;
    });
}

SC_API int sc_media_set_decode(sc_media *media, int hardware) {
    return sc::guarded("sc_media_set_decode", [&]() -> int {
        if (!media) return sc::fail(SC_ERR_ARGUMENT, "sc_media_set_decode: no player");
        media->core->hardware = hardware != 0;
        return SC_OK;
    });
}

SC_API int sc_media_set_picture(sc_media *media, int format, int max_width, int max_height, int grow) {
    return sc::guarded("sc_media_set_picture", [&]() -> int {
        if (!media) return sc::fail(SC_ERR_ARGUMENT, "sc_media_set_picture: no player");
        Core &c = *media->core;
        if (format < 0 || format > 2) return sc::fail(SC_ERR_ARGUMENT, "sc_media_set_picture: unknown format");
        c.picture_format = format;
        c.grow = grow != 0 && max_width > 0 && max_height > 0;
        c.max_w = max_width > 0 ? std::max(2, max_width) : MAX_W;
        c.max_h = max_height > 0 ? std::max(2, max_height) : MAX_H;
        return SC_OK;
    });
}

SC_API int sc_media_set_file(sc_media *media, const char *path, int video, int playing) {
    return sc::guarded("sc_media_set_file", [&]() -> int {
        if (!media) return sc::fail(SC_ERR_ARGUMENT, "sc_media_set_file: no player");
        Core &c = *media->core;
        std::lock_guard<std::mutex> hold(c.lock);
        c.path = path ? path : "";
        c.has_video = video != 0;
        c.duration = 0;
        c.width = c.height = 0;
        c.status = c.path.empty() ? "no file" : "starting";
        c.jump(0, playing ? SC_MEDIA_PLAYING : SC_MEDIA_STOPPED);
        return SC_OK;
    });
}

SC_API int sc_media_control(sc_media *media, int what, double value) {
    return sc::guarded("sc_media_control", [&]() -> int {
        if (!media) return sc::fail(SC_ERR_ARGUMENT, "sc_media_control: no player");
        if (what < SC_MEDIA_PLAY || what > SC_MEDIA_CLOSE) {
            return sc::fail(SC_ERR_ARGUMENT, "sc_media_control: no such control");
        }
        media->core->control(what, value);
        return SC_OK;
    });
}

SC_API int sc_media_set_shrink(sc_media *media, float shrink) {
    if (!media) return SC_ERR_ARGUMENT;
    media->core->shrink.store(shrink > 0 ? shrink : 1.0f);
    return SC_OK;
}

SC_API int sc_media_info_get(sc_media *media, sc_media_info *out) {
    return sc::guarded("sc_media_info_get", [&]() -> int {
        if (!media || !out) return sc::fail(SC_ERR_ARGUMENT, "sc_media_info_get: nothing given");
        Core &c = *media->core;
        std::lock_guard<std::mutex> hold(c.lock);
        c.check_end();
        std::memset(out, 0, sizeof *out);
        out->state = c.state;
        out->has_video = c.has_video;
        out->width = c.width;
        out->height = c.height;
        out->hidden = c.hidden;
        out->thread = c.thread != 0;
        out->faulted = c.faulted;
        out->replaying = c.replaying;
        const double p = c.position();
        out->position = c.duration > 0 && c.loop ? std::fmod(p, c.duration) : p;
        out->duration = c.duration;
        out->speed = c.speed;
        out->decoded = c.decoded;
        out->passes = c.passes;
        out->picture_bytes = c.picture_bytes;
        out->hardware = c.on_card;
        out->given_width = c.given_width;
        out->given_height = c.given_height;
        std::snprintf(out->status, sizeof out->status, "%s", c.status.c_str());
        return SC_OK;
    });
}

SC_API int sc_media_video_frame(sc_media *media, uint64_t *seen, sc_media_picture *picture, uint8_t *out,
                                size_t capacity) {
    return sc::guarded("sc_media_video_frame", [&]() -> int {
        if (!media || !picture || !seen) return sc::fail(SC_ERR_ARGUMENT, "sc_media_video_frame: nothing given");
        Core &c = *media->core;
        PicturePtr found;
        double at = 0;
        PicturePtr on_card;
        {
            std::lock_guard<std::mutex> hold(c.lock);
            c.python_took.store(now_s());  // from now on the decoder copies its pictures to memory again
            c.advance();
            if (*seen == c.screen_serial) return SC_MEDIA_NO_PICTURE;
            if (c.on_screen && c.on_screen->card) on_card = c.on_screen;
        }
        if (on_card) {  // the one on screen was left on the card for the engine: copied here, once, for Python
            PicturePtr copied = card_to_memory(*on_card);
            std::lock_guard<std::mutex> hold(c.lock);
            if (c.on_screen == on_card) {
                if (!copied) return SC_MEDIA_NO_PICTURE;  // the next one comes in memory
                c.on_screen = copied;
            }
        }
        {
            std::lock_guard<std::mutex> hold(c.lock);
            c.advance();
            if (*seen == c.screen_serial) return SC_MEDIA_NO_PICTURE;
            if (c.on_screen && c.on_screen->card) return SC_MEDIA_NO_PICTURE;
            if (c.on_screen_blank || !c.on_screen) {
                *seen = c.screen_serial;
                return c.on_screen_blank ? SC_MEDIA_BLANK : SC_MEDIA_NO_PICTURE;
            }
            if (!out || capacity < c.on_screen->data.size()) {
                picture->bytes = c.on_screen->data.size();
                return SC_MEDIA_TOO_SMALL;
            }
            *seen = c.screen_serial;
            found = c.on_screen;
            at = c.shown;
        }
        // Copied with the lock let go: the decoder and the mixer never wait on a picture being copied.
        std::memcpy(out, found->data.data(), found->data.size());
        picture->format = found->format;
        picture->width = found->width;
        picture->height = found->height;
        picture->stride = found->stride;
        picture->matrix = found->matrix;
        picture->full_range = found->full;
        picture->bytes = found->data.size();
        picture->time = at;
        return SC_MEDIA_PICTURE;
    });
}

SC_API int sc_media_read_audio(sc_media *media, float *out, int count, int asleep) {
    return sc::guarded("sc_media_read_audio", [&]() -> int {
        if (!media || !out || count < 0) return sc::fail(SC_ERR_ARGUMENT, "sc_media_read_audio: nothing given");
        std::memset(out, 0, static_cast<size_t>(count) * 2 * sizeof(float));
        Core &c = *media->core;
        std::lock_guard<std::mutex> hold(c.lock);
        if (c.state != SC_MEDIA_PLAYING || asleep) return SC_OK;
        c.run_clock();
        const double now = c.position();
        const double step = static_cast<double>(c.speed) / RATE;  // timeline seconds per sample
        size_t filled = 0;
        const size_t want = static_cast<size_t>(count);
        auto drop = [&] {
            c.audio_s -= static_cast<double>(c.audio.front().frames()) / RATE;
            c.audio.pop_front();
            c.wake.notify_all();
        };
        while (!c.audio.empty() && filled < want) {
            Chunk &chunk = c.audio.front();
            const double cursor = now + static_cast<double>(filled) * step;
            const size_t have = chunk.frames();
            if (chunk.start + static_cast<double>(have) * step < cursor - SYNC_S) {  // fell behind the clock: skip it
                drop();
                continue;
            }
            if (chunk.start > cursor + SYNC_S) break;  // ahead of the clock: silence until it's due
            const size_t take = std::min(want - filled, have);
            std::memcpy(out + filled * 2, chunk.samples->data() + chunk.at * 2, take * 2 * sizeof(float));
            filled += take;
            if (take == have) {
                drop();
            } else {
                chunk.at += take;
                chunk.start += static_cast<double>(take) * step;
                c.audio_s -= static_cast<double>(take) / RATE;
            }
        }
        if (!c.has_video) c.check_end();
        return SC_OK;
    });
}

SC_API int sc_media_debug_fault(sc_media *media, int kind) {
    if (!media) return SC_ERR_ARGUMENT;
    media->core->fault.store(kind);
    return SC_OK;
}

}  // extern "C"
