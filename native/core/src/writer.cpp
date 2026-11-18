// Recording and streaming output (phase 4, docs/native-engine.md): a file or stream written by a thread of the
// engine's own. Pictures (NV12) and sound (stereo floats) are handed over and queued; the writer thread encodes
// them with FFmpeg (SprigCast's own copy, ffmpeg.h), muxes and writes, so nothing of it needs Python's lock.
//
// Like media.cpp this needs no graphics card and builds on Linux, so its checks run there too
// (tests/test_native_writer.py).
#include "base.h"
#include "ffmpeg.h"
#include "writer.h"

#if defined(_WIN32)
#  include <d3d11.h>
#  if defined(_MSC_VER)
#    pragma warning(push, 0)
#  endif
extern "C" {
#  include <libavutil/hwcontext_d3d11va.h>
}
#  if defined(_MSC_VER)
#    pragma warning(pop)
#  endif
#endif

#include <algorithm>
#include <chrono>
#include <condition_variable>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <functional>
#include <memory>
#include <thread>
#include <vector>

namespace {

using sc::ffm::av_text;
using sc::ffm::ff;

constexpr int RATE = 48000;
constexpr int MAX_TRACKS = 6;

double now_s() {
    return std::chrono::duration<double>(std::chrono::steady_clock::now().time_since_epoch()).count();
}

struct FrameFree {
    void operator()(AVFrame *f) const { ff.av_frame_free(&f); }
};
using FramePtr = std::unique_ptr<AVFrame, FrameFree>;

// An environment variable's text ("" when unset). On Windows the process's own block: Python sets variables there
// (os.environ), which a C runtime's copy (getenv, _dupenv_s) may not see.
std::string env_text(const char *name) {
#if defined(_WIN32)
    char value[256];
    const DWORD n = GetEnvironmentVariableA(name, value, sizeof value);
    return n > 0 && n < sizeof value ? std::string(value, n) : std::string();
#else
    const char *value = std::getenv(name);
    return value ? std::string(value) : std::string();
#endif
}

struct Error : std::runtime_error {
    using std::runtime_error::runtime_error;
};
// The file or connection refused what was written (disk full, drive gone): not the encoder's doing.
struct WriteError : Error {
    using Error::Error;
};

void must(int code, const char *what) {
    if (code < 0) throw Error(std::string(what) + ": " + av_text(code));
}

struct Track {
    AVCodecContext *enc = nullptr;
    AVStream *stream = nullptr;
    std::vector<float> waiting;  // stereo, interleaved: sound not yet a whole encoder frame
    int64_t next_pts = 0;        // in samples
};

struct Core {
    // Set once, before the thread starts.
    std::string target, format, video_codec, video_options, format_options;
    std::vector<std::string> titles;  // each sound track's title (more than one track)
    int b_frames = 0;
    int width = 0, height = 0, fps_num = 30, fps_den = 1, video_kbps = 6000, gop = 60, audio_kbps = 160;
    int tracks = 1;
    size_t queue_limit = 8;
    // Frames on the card (cfg->d3d11_device): the encoder takes NV12 textures of that device straight from the
    // engine (writer_feed_texture), and pictures from memory are copied up to the card first.
    void *device = nullptr;          // ID3D11Device*, a reference held
    AVBufferRef *hw_device = nullptr, *hw_frames = nullptr;
    std::vector<void *> seen;        // every texture handed over, a reference held until the encoder is closed:
                                     // NVENC remembers registered textures by pointer and holds none itself
    std::atomic<bool> on_card{false};  // takes textures now: false once the encoder moved to memory (to_memory)
    int recoveries = 0;              // times the encoder on the card failed and went on from memory
    std::mutex lock;
    std::condition_variable wake, done_cv;
    struct Picture {
        int64_t pts = 0, count = 1;  // written as frames pts .. pts + count - 1 (more than one: a hole filled)
        FramePtr frame;
        bool key = false;            // sc_writer_keyframe was asked before it came: it starts a keyframe
    };
    std::deque<Picture> pictures;
    std::vector<std::deque<std::vector<float>>> sounds;          // per track, blocks not yet taken
    std::vector<FramePtr> spare;                                 // NV12 frames to fill again
    bool finishing = false, done = false;
    std::atomic<bool> abort{false};
    int state = SC_WRITER_OPENING;
    std::string error;
    uint64_t frames_in = 0, frames_written = 0, frames_dropped = 0, bytes = 0, frames_late = 0, frames_repeated = 0;
    int64_t last_pts = -1;  // the newest frame number taken: pictures never go back in time
    // When the writer thread last took a picture. A full queue while it hasn't for STALLED (its encoder opening
    // again after a graphics reset, about a second) holds the newest picture longer instead of losing frames.
    std::chrono::steady_clock::time_point taken_at = std::chrono::steady_clock::now();
    uint64_t frames_held = 0;  // frames written as a repeat of the newest picture while the thread was busy
    // While the encoder opens again (to_memory), pictures from memory queue past queue_limit, up to REOPEN_BYTES of
    // them, so the file keeps moving pictures instead of a still (PC 2026-11-18: up to 0.4 s still).
    bool reopening = false;
    int64_t hole_from = -1;    // frames of textures lost with the encoder on the card start here (writer thread)
    double write_seconds = 0, longest_write = 0;
    bool yuv420 = false;  // the encoder took no NV12: pictures are repacked as I420 (writer thread only)
    std::deque<int64_t> faults;  // SPRIGCAST_WRITER_FAULT (tests): pictures whose encoding fails ("10" or "10,20")
    int reopen_ms = 0;  // SPRIGCAST_WRITER_REOPEN_MS (tests): opening the encoder again takes this long
    int64_t encoded = 0;
    // Packets mode: a keyframe starts every `gop` frames of the stream's own clock (the first picture at or past
    // each multiple), so writers fed the same frames at their own rates key at the same instants (Enhanced
    // Broadcasting's tracks, a stream joined later).
    int64_t key_group = -1;
    // Packets mode (cfg->packets): encoded packets wait here for sc_writer_packet_take instead of being written.
    bool packets = false;
    struct Out {
        int track = 0;
        bool key = false;
        int64_t pts = 0, dts = 0;
        AVRational tb{1, 1};
        std::vector<uint8_t> data;
    };
    std::deque<Out> outs;
    std::atomic<int> want_kbps{0}, want_max{0}, want_buffer{0};  // sc_writer_set_bitrate, applied before a picture
    bool key_next = false;  // sc_writer_keyframe: the next picture queued starts a keyframe (under `lock`)
    FramePtr i420;

    AVFormatContext *out = nullptr;
    AVCodecContext *venc = nullptr;
    AVStream *vstream = nullptr;
    std::vector<Track> audio;
    AVPacket *pkt = nullptr;

    void run();
    void open();
    void open_video(const AVCodec *codec, bool global);
    void open_card();
    void to_memory(const std::string &why, bool failed = true);
    std::atomic<bool> leave{false};  // writers_leave_device: the engine let go of this encoder's device
    void upload(FramePtr &picture);
    void write_packets(AVCodecContext *enc, AVStream *stream, AVPacket *use = nullptr);
    void encode_video(AVFrame *frame, bool key = false);
    void encode_sound(Track &t, bool flush);
    void close_all(bool trailer);
    ~Core() {
#if defined(_WIN32)
        for (void *t : seen) static_cast<IUnknown *>(t)->Release();
        if (device) static_cast<IUnknown *>(device)->Release();
#endif
    }
    void set_failed(const std::string &why) {
        std::lock_guard<std::mutex> hold(lock);
        if (error.empty()) error = why;
        state = SC_WRITER_FAILED;
    }
};

int interrupted(void *opaque) { return static_cast<Core *>(opaque)->abort.load() ? 1 : 0; }

void Core::open() {
    const char *fmt = packets ? "nut" : format.empty() ? nullptr : format.c_str();
    must(ff.avformat_alloc_output_context2(&out, nullptr, fmt, target.c_str()), "the output");
    out->interrupt_callback.callback = interrupted;
    out->interrupt_callback.opaque = this;
    // Packets go to muxers of the caller's (FLV, Matroska, MP4): all want the codec headers apart.
    const bool global = packets || (out->oformat->flags & AVFMT_GLOBALHEADER) != 0;

    const AVCodec *codec = ff.avcodec_find_encoder_by_name(video_codec.c_str());
    if (!codec) throw Error("no video encoder named " + video_codec);
    if (device) open_card();
    open_video(codec, global);
    vstream = ff.avformat_new_stream(out, nullptr);
    if (!vstream) throw std::bad_alloc();
    vstream->time_base = venc->time_base;
    must(ff.avcodec_parameters_from_context(vstream->codecpar, venc), "the video stream");

    const AVCodec *aac = ff.avcodec_find_encoder(AV_CODEC_ID_AAC);
    if (tracks > 0 && !aac) throw Error("no AAC encoder");
    audio.resize(static_cast<size_t>(tracks));
    for (size_t i = 0; i < audio.size(); ++i) {
        Track &t = audio[i];
        t.enc = ff.avcodec_alloc_context3(aac);
        if (!t.enc) throw std::bad_alloc();
        t.enc->sample_fmt = AV_SAMPLE_FMT_FLTP;
        t.enc->sample_rate = RATE;
        ff.av_channel_layout_default(&t.enc->ch_layout, 2);
        t.enc->bit_rate = static_cast<int64_t>(audio_kbps) * 1000;
        t.enc->time_base = AVRational{1, RATE};
        if (global) t.enc->flags |= AV_CODEC_FLAG_GLOBAL_HEADER;
        must(ff.avcodec_open2(t.enc, aac, nullptr), "the sound encoder");
        t.stream = ff.avformat_new_stream(out, nullptr);
        if (!t.stream) throw std::bad_alloc();
        t.stream->time_base = t.enc->time_base;
        must(ff.avcodec_parameters_from_context(t.stream->codecpar, t.enc), "the sound stream");
        if (tracks > 1 && i < titles.size() && !titles[i].empty())
            ff.av_dict_set(&t.stream->metadata, "title", titles[i].c_str(), 0);
    }
    if (!(out->oformat->flags & AVFMT_NOFILE)) {
        AVIOInterruptCB stop{interrupted, this};
        must(ff.avio_open2(&out->pb, target.c_str(), AVIO_FLAG_WRITE, &stop, nullptr), "opening it");
    }
    AVDictionary *muxing = nullptr;
    if (!format_options.empty()) ff.av_dict_parse_string(&muxing, format_options.c_str(), "=", "\n", 0);
    const int header = ff.avformat_write_header(out, &muxing);
    ff.av_dict_free(&muxing);
    must(header, "the header");
    if (packets) {  // the streams' file: one placeholder byte a stream, so a demuxer opens it; then it's done
        uint8_t zero[1 + AV_INPUT_BUFFER_PADDING_SIZE] = {};
        for (unsigned i = 0; i < out->nb_streams; ++i) {
            AVPacket *mark = ff.av_packet_alloc();
            if (!mark) throw std::bad_alloc();
            mark->data = zero;  // not reference-counted: the muxer copies it
            mark->size = 1;
            mark->pts = mark->dts = 0;
            mark->stream_index = static_cast<int>(i);
            mark->flags |= AV_PKT_FLAG_KEY;
            const int w = ff.av_interleaved_write_frame(out, mark);
            ff.av_packet_free(&mark);
            must(w, "the streams' file");
        }
        must(ff.av_write_trailer(out), "the streams' file");
        if (out->pb) ff.avio_closep(&out->pb);
    }
    pkt = ff.av_packet_alloc();
    if (!pkt) throw std::bad_alloc();
}

// The video encoder: on the card (hw_frames) or taking pictures from memory, NV12 or else I420.
void Core::open_video(const AVCodec *codec, bool global) {
    for (int attempt = 0; attempt < 2; ++attempt) {  // NV12 first, then I420 for an encoder without it
        venc = ff.avcodec_alloc_context3(codec);
        if (!venc) throw std::bad_alloc();
        venc->width = width;
        venc->height = height;
        venc->pix_fmt = attempt == 0 ? AV_PIX_FMT_NV12 : AV_PIX_FMT_YUV420P;
        if (hw_frames) {  // frames on the card: one way only
            venc->pix_fmt = AV_PIX_FMT_D3D11;
            venc->sw_pix_fmt = AV_PIX_FMT_NV12;
            venc->hw_frames_ctx = ff.av_buffer_ref(hw_frames);
            if (!venc->hw_frames_ctx) throw std::bad_alloc();
        }
        venc->time_base = AVRational{fps_den, fps_num};
        venc->framerate = AVRational{fps_num, fps_den};
        venc->bit_rate = static_cast<int64_t>(video_kbps) * 1000;
        venc->gop_size = gop;
        venc->max_b_frames = b_frames;
        venc->color_range = AVCOL_RANGE_MPEG;
        venc->colorspace = AVCOL_SPC_BT709;
        venc->color_primaries = AVCOL_PRI_BT709;
        venc->color_trc = AVCOL_TRC_BT709;
        if (global) venc->flags |= AV_CODEC_FLAG_GLOBAL_HEADER;
        AVDictionary *opts = nullptr;
        if (!video_options.empty()) ff.av_dict_parse_string(&opts, video_options.c_str(), "=", "\n", 0);
        const int r = ff.avcodec_open2(venc, codec, &opts);
        ff.av_dict_free(&opts);
        if (r >= 0) {
            yuv420 = attempt == 1;
            break;
        }
        ff.avcodec_free_context(&venc);
        if (attempt == 1 || hw_frames)
            must(r, ("the video encoder " + video_codec + (hw_frames ? " (frames on the card)" : "")).c_str());
    }
}

void Core::open_card() {
#if defined(_WIN32)
    hw_device = ff.av_hwdevice_ctx_alloc(AV_HWDEVICE_TYPE_D3D11VA);
    if (!hw_device) throw std::bad_alloc();
    auto *dctx = reinterpret_cast<AVD3D11VADeviceContext *>(reinterpret_cast<AVHWDeviceContext *>(hw_device->data)->hwctx);
    auto *d3d = static_cast<ID3D11Device *>(device);
    d3d->AddRef();  // the device context lets go of it when freed
    dctx->device = d3d;
    must(ff.av_hwdevice_ctx_init(hw_device), "the graphics card for the encoder");
    hw_frames = ff.av_hwframe_ctx_alloc(hw_device);
    if (!hw_frames) throw std::bad_alloc();
    auto *frames = reinterpret_cast<AVHWFramesContext *>(hw_frames->data);
    frames->format = AV_PIX_FMT_D3D11;
    frames->sw_format = AV_PIX_FMT_NV12;
    frames->width = width;
    frames->height = height;
    frames->initial_pool_size = 0;  // textures for pictures from memory are made as needed
    must(ff.av_hwframe_ctx_init(hw_frames), "frames on the card for the encoder");
#else
    throw Error("frames on the card need Windows");
#endif
}

// A picture from memory, for an encoder that takes frames on the card: copied up into a texture of its own.
void Core::upload(FramePtr &picture) {
    if (!hw_frames || picture->format == AV_PIX_FMT_D3D11) return;
    FramePtr card(ff.av_frame_alloc());
    if (!card) throw std::bad_alloc();
    must(ff.av_hwframe_get_buffer(hw_frames, card.get(), 0), "a texture for a picture");
    must(ff.av_hwframe_transfer_data(card.get(), picture.get(), 0), "copying a picture to the card");
    card->color_range = picture->color_range;
    card->colorspace = picture->colorspace;
    picture = std::move(card);
}

void Core::write_packets(AVCodecContext *enc, AVStream *stream, AVPacket *use) {
    AVPacket *p = use ? use : pkt;  // the helper finishing an old encoder brings its own
    for (;;) {
        const int r = ff.avcodec_receive_packet(enc, p);
        if (r == AVERROR(EAGAIN) || r == AVERROR_EOF) return;
        must(r, "encoding");
        if (packets) {  // for the caller to take: copied out, in the encoder's own time base
            Out o;
            o.track = stream->index;
            o.key = (p->flags & AV_PKT_FLAG_KEY) != 0;
            o.pts = p->pts;
            o.dts = p->dts;
            o.tb = enc->time_base;
            o.data.assign(p->data, p->data + p->size);
            ff.av_packet_unref(p);
            std::lock_guard<std::mutex> hold(lock);
            if (outs.size() >= 4096) throw WriteError("the encoded packets weren't taken for a minute");
            bytes += o.data.size();
            outs.push_back(std::move(o));
            continue;
        }
        ff.av_packet_rescale_ts(p, enc->time_base, stream->time_base);
        p->stream_index = stream->index;
        const int size = p->size;
        const double began = now_s();
        const int w = ff.av_interleaved_write_frame(out, p);  // takes the packet's data, leaves it blank
        const double spent = now_s() - began;
        if (w < 0) throw WriteError("writing: " + av_text(w));
        std::lock_guard<std::mutex> hold(lock);
        bytes += static_cast<uint64_t>(size);
        write_seconds += spent;
        longest_write = std::max(longest_write, spent);
    }
}

void Core::encode_video(AVFrame *frame, bool key) {
    if (frame && yuv420) {  // an encoder without NV12: the planes apart, U then V
        if (!i420) {
            i420.reset(ff.av_frame_alloc());
            if (!i420) throw std::bad_alloc();
            i420->format = AV_PIX_FMT_YUV420P;
            i420->width = width;
            i420->height = height;
            must(ff.av_frame_get_buffer(i420.get(), 0), "a picture");
        }
        must(ff.av_frame_make_writable(i420.get()), "a picture");
        for (int y = 0; y < height; ++y)
            std::memcpy(i420->data[0] + static_cast<ptrdiff_t>(y) * i420->linesize[0],
                        frame->data[0] + static_cast<ptrdiff_t>(y) * frame->linesize[0], static_cast<size_t>(width));
        for (int y = 0; y < height / 2; ++y) {
            const uint8_t *row = frame->data[1] + static_cast<ptrdiff_t>(y) * frame->linesize[1];
            uint8_t *u = i420->data[1] + static_cast<ptrdiff_t>(y) * i420->linesize[1];
            uint8_t *v = i420->data[2] + static_cast<ptrdiff_t>(y) * i420->linesize[2];
            for (int x = 0; x < width / 2; ++x) {
                u[x] = row[2 * x];
                v[x] = row[2 * x + 1];
            }
        }
        i420->pts = frame->pts;
        i420->color_range = frame->color_range;
        i420->colorspace = frame->colorspace;
        frame = i420.get();
    }
    if (frame) {
        const int kbps = want_kbps.exchange(0);
        if (kbps > 0) {  // a stream steering its bitrate: x264 and NVENC take it from the next picture
            venc->bit_rate = static_cast<int64_t>(kbps) * 1000;
            const int peak = want_max.exchange(0), buffer = want_buffer.exchange(0);
            if (peak > 0) venc->rc_max_rate = static_cast<int64_t>(peak) * 1000;
            if (buffer > 0) venc->rc_buffer_size = buffer * 1000;
        }
        frame->pict_type = key ? AV_PICTURE_TYPE_I : AV_PICTURE_TYPE_NONE;
    }
    if (frame && !faults.empty() && encoded++ == faults.front()) {  // SPRIGCAST_WRITER_FAULT: fails here
        faults.pop_front();
        throw Error("encoding a picture: a deliberate fault (SPRIGCAST_WRITER_FAULT)");
    }
    must(ff.avcodec_send_frame(venc, frame), "encoding a picture");
    write_packets(venc, vstream);
}

// The video encoder failed (a graphics driver reset, a lost card): the old one is closed and let go of entirely,
// with its frames on the card and their textures, and a new one goes on from memory into the same file. Once a
// recording: a second failure ends it cleanly (run's catch), never a loop.
void Core::to_memory(const std::string &why, bool failed) {
    sc::log_line(SC_LOG_WARNING, "writer: " + target + (failed ? ": the video encoder failed (" : ": ") + why +
                                     (failed ? "); it starts again from memory and the file goes on"
                                             : "; the video encoder starts again from memory and the file goes on"));
    const auto began = std::chrono::steady_clock::now();
    uint64_t held_before = 0;
    AVBufferRef *frames_ctx = nullptr, *device_ctx = nullptr;
    std::vector<void *> gone;
    void *old_device = nullptr;
    {
        std::lock_guard<std::mutex> hold(lock);  // feed_texture takes hw_frames under it
        held_before = frames_held;
        reopening = true;
        std::swap(frames_ctx, hw_frames);
        std::swap(device_ctx, hw_device);
        std::swap(old_device, device);  // nothing of the old device is held any more: it can go (PC 2026-11-18)
        gone.swap(seen);
        on_card = false;
        if (failed) ++recoveries;
    }
    // The old encoder finishes (its last frames go in the file) on a helper thread while the new one opens here:
    // the wait is the longer of the two, not both (PC 2026-11-18: up to 912 ms, the picture still meanwhile). The new
    // encoder writes nothing until the old one is done, so the file's order holds.
    AVCodecContext *old = venc;
    venc = nullptr;
    std::exception_ptr old_failed;
    double flush_ms = 0.0;
    std::thread finishing_old;
    if (old) {
        finishing_old = std::thread([&, old]() mutable {
            sc::name_thread(L"sprigcast-writer-finish");
            const auto from = std::chrono::steady_clock::now();
            AVPacket *own = ff.av_packet_alloc();
            try {
                if (own) {
                    ff.avcodec_send_frame(old, nullptr);
                    write_packets(old, vstream, own);
                }
            } catch (const WriteError &) {
                old_failed = std::current_exception();
            } catch (...) {  // a broken encoder may not finish: its last frames are lost
            }
            if (own) ff.av_packet_free(&own);
            ff.avcodec_free_context(&old);
            flush_ms = std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - from).count();
        });
    }
    std::exception_ptr open_failed;
    try {
        i420.reset();
        if (reopen_ms > 0) std::this_thread::sleep_for(std::chrono::milliseconds(reopen_ms));  // tests: slow reopen
        const AVCodec *codec = ff.avcodec_find_encoder_by_name(video_codec.c_str());
        if (!codec) throw Error("no video encoder named " + video_codec);
        // Packets mode: the caller's muxers hold the first encoder's headers, which a new encoder may not match. Its
        // headers go in the keyframes themselves instead (decoders take headers that come with the pictures).
        open_video(codec, !packets && (out->oformat->flags & AVFMT_GLOBALHEADER) != 0);
    } catch (...) {
        open_failed = std::current_exception();
    }
    const double open_ms =
        std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - began).count();
    if (finishing_old.joinable()) finishing_old.join();
    // The old device's last references go once its encoder is gone.
    if (frames_ctx) ff.av_buffer_unref(&frames_ctx);
    if (device_ctx) ff.av_buffer_unref(&device_ctx);
#if defined(_WIN32)
    for (void *t : gone) static_cast<IUnknown *>(t)->Release();
    if (old_device) static_cast<IUnknown *>(old_device)->Release();
#else
    (void)old_device;
#endif
    {
        std::lock_guard<std::mutex> hold(lock);
        reopening = false;
    }
    if (old_failed) std::rethrow_exception(old_failed);
    if (open_failed) std::rethrow_exception(open_failed);
    const auto took = std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::steady_clock::now() - began);
    uint64_t held = 0;
    {
        std::lock_guard<std::mutex> hold(lock);
        held = frames_held - held_before;
        taken_at = std::chrono::steady_clock::now();
    }
    sc::log_line(SC_LOG_INFO, "writer: " + target + ": the video encoder goes on from memory (opened again in " +
                                  std::to_string(took.count()) + " ms: the new one opened in " +
                                  std::to_string(static_cast<int>(open_ms)) + " ms while the old one finished in " +
                                  std::to_string(static_cast<int>(flush_ms)) + " ms; " + std::to_string(held) +
                                  " frames held the newest picture meanwhile, none lost)");
}

void Core::encode_sound(Track &t, bool flush) {
    const int n = t.enc->frame_size > 0 ? t.enc->frame_size : 1024;
    size_t at = 0;
    while (t.waiting.size() - at >= static_cast<size_t>(n) * 2) {
        FramePtr f(ff.av_frame_alloc());
        if (!f) throw std::bad_alloc();
        f->format = AV_SAMPLE_FMT_FLTP;
        f->sample_rate = RATE;
        ff.av_channel_layout_default(&f->ch_layout, 2);
        f->nb_samples = n;
        must(ff.av_frame_get_buffer(f.get(), 0), "a sound frame");
        auto *left = reinterpret_cast<float *>(f->data[0]);
        auto *right = reinterpret_cast<float *>(f->data[1]);
        for (int i = 0; i < n; ++i) {
            left[i] = t.waiting[at + 2 * static_cast<size_t>(i)];
            right[i] = t.waiting[at + 2 * static_cast<size_t>(i) + 1];
        }
        at += static_cast<size_t>(n) * 2;
        f->pts = t.next_pts;
        t.next_pts += n;
        must(ff.avcodec_send_frame(t.enc, f.get()), "encoding sound");
        write_packets(t.enc, t.stream);
    }
    t.waiting.erase(t.waiting.begin(), t.waiting.begin() + static_cast<std::ptrdiff_t>(at));
    if (flush) {
        must(ff.avcodec_send_frame(t.enc, nullptr), "finishing the sound");
        write_packets(t.enc, t.stream);
    }
}

void Core::close_all(bool trailer) {
    if (out && trailer && !packets) {  // packets mode: the streams' file was finished when it opened
        const int r = ff.av_write_trailer(out);
        if (r < 0) set_failed("writing the file's ending: " + av_text(r));
    }
    if (out && !(out->oformat->flags & AVFMT_NOFILE) && out->pb) ff.avio_closep(&out->pb);
    if (out) ff.avformat_free_context(out);
    out = nullptr;
    if (venc) ff.avcodec_free_context(&venc);
    for (Track &t : audio)
        if (t.enc) ff.avcodec_free_context(&t.enc);
    audio.clear();
    if (pkt) ff.av_packet_free(&pkt);
    i420.reset();
    AVBufferRef *frames_ctx = nullptr, *device_ctx = nullptr;
    std::vector<void *> gone;
    {
        std::lock_guard<std::mutex> hold(lock);  // feed_texture takes hw_frames under it
        std::swap(frames_ctx, hw_frames);
        std::swap(device_ctx, hw_device);
        gone.swap(seen);
    }
    if (frames_ctx) ff.av_buffer_unref(&frames_ctx);
    if (device_ctx) ff.av_buffer_unref(&device_ctx);
#if defined(_WIN32)
    for (void *t : gone) static_cast<IUnknown *>(t)->Release();  // the encoder is gone: nothing remembers them
#endif
}

void Core::run() {
    sc::name_thread(L"SprigCast writer");
    bool header = false;
    try {
        open();
        header = true;
        {
            std::lock_guard<std::mutex> hold(lock);
            if (state == SC_WRITER_OPENING) state = SC_WRITER_RUNNING;
        }
        for (;;) {
            Picture picture;
            std::vector<std::vector<float>> sound(static_cast<size_t>(tracks));
            bool last, moving = false;
            {
                std::unique_lock<std::mutex> hold(lock);
                wake.wait(hold, [&] {
                    if (finishing || abort.load() || !pictures.empty() || (leave.load() && hw_frames)) return true;
                    return std::any_of(sounds.begin(), sounds.end(), [](const auto &q) { return !q.empty(); });
                });
                if (abort.load()) break;
                for (size_t i = 0; i < sounds.size(); ++i) {
                    for (auto &block : sounds[i]) sound[i].insert(sound[i].end(), block.begin(), block.end());
                    sounds[i].clear();
                }
                if (!pictures.empty()) {
                    picture = std::move(pictures.front());
                    pictures.pop_front();
                    taken_at = std::chrono::steady_clock::now();
                }
                moving = leave.exchange(false) && hw_frames != nullptr;
                last = finishing && pictures.empty() && !picture.frame;
            }
            if (moving) to_memory("the engine let go of its graphics device (rebuilt)", false);
            for (size_t i = 0; i < audio.size(); ++i) {
                audio[i].waiting.insert(audio[i].waiting.end(), sound[i].begin(), sound[i].end());
                encode_sound(audio[i], false);
            }
            if (picture.frame && picture.frame->format == AV_PIX_FMT_D3D11 && !hw_frames) {
                std::lock_guard<std::mutex> hold(lock);  // a texture queued before the encoder left the card
                if (hole_from < 0) hole_from = picture.pts;  // the next picture from memory stands for it
                picture.frame.reset();  // its texture goes back to the engine
            }
            if (picture.frame && hole_from >= 0 && picture.frame->format != AV_PIX_FMT_D3D11) {
                std::lock_guard<std::mutex> hold(lock);
                const int64_t more = picture.pts - hole_from;
                if (more > 0) {
                    picture.count += more;
                    picture.pts = hole_from;
                    frames_repeated += static_cast<uint64_t>(more);
                    frames_held += static_cast<uint64_t>(more);
                }
                hole_from = -1;
            }
            if (picture.frame) {
                const bool mine = picture.frame->format == AV_PIX_FMT_NV12;  // an NV12 buffer to fill again
                FramePtr own;
                int64_t k = 0;
                try {
                    if (hw_frames && mine) {  // from memory, for frames on the card: copied up first
                        own = std::move(picture.frame);
                        picture.frame.reset(ff.av_frame_clone(own.get()));
                        if (!picture.frame) throw std::bad_alloc();
                        upload(picture.frame);
                    }
                    for (; k < picture.count && !abort.load(); ++k) {  // the encoder keeps its own reference
                        picture.frame->pts = picture.pts + k;
                        bool aligned = false;
                        if (packets && picture.frame->pts / gop > key_group) {
                            key_group = picture.frame->pts / gop;
                            aligned = true;
                        }
                        encode_video(picture.frame.get(), (picture.key && k == 0) || aligned);
                        std::lock_guard<std::mutex> hold(lock);
                        ++frames_written;
                    }
                } catch (const WriteError &) {
                    throw;
                } catch (const Error &e) {
                    if (recoveries > 0 || abort.load()) throw;
                    to_memory(e.what());
                    if (own) picture.frame = std::move(own);  // the same picture again, from memory
                    if (picture.frame && picture.frame->format != AV_PIX_FMT_D3D11) {
                        for (; k < picture.count && !abort.load(); ++k) {
                            picture.frame->pts = picture.pts + k;
                            encode_video(picture.frame.get(), true);  // a new encoder starts on a keyframe
                            std::lock_guard<std::mutex> hold(lock);
                            ++frames_written;
                        }
                    } else {
                        std::lock_guard<std::mutex> hold(lock);  // a texture: lost with the encoder on the card,
                        if (hole_from < 0) hole_from = picture.pts + k;  // so the next picture stands for it
                        picture.frame.reset();
                    }
                }
                std::lock_guard<std::mutex> hold(lock);
                if (own) {
                    if (spare.size() < 4) spare.push_back(std::move(own));
                } else if (mine && picture.frame && spare.size() < 4) {
                    spare.push_back(std::move(picture.frame));
                }  // a texture's frame: let go here, its texture goes back to the engine when the encoder is done
            }
            if (last) break;
        }
        if (!abort.load()) {
            encode_video(nullptr);
            for (Track &t : audio) encode_sound(t, true);
        }
        close_all(!abort.load());
    } catch (const std::exception &e) {
        set_failed(e.what());
        sc::log_line(SC_LOG_ERROR, std::string("writer: ") + target + ": " + e.what());
        try {
            close_all(header);
        } catch (...) {
        }
    } catch (...) {
        // Crash gate: a fault inside FFmpeg ends this output, never the app; nothing of it is touched again.
        set_failed("the encoder faulted");
        sc::log_line(SC_LOG_ERROR, "writer: " + target + ": the encoder faulted; this output is given up");
    }
    std::lock_guard<std::mutex> hold(lock);
    if (state != SC_WRITER_FAILED) state = SC_WRITER_FINISHED;
    pictures.clear();
    for (auto &q : sounds) q.clear();
    done = true;
    done_cv.notify_all();
}

// The queue is full while the writer thread hasn't taken a picture for a while (its encoder opening again after a
// graphics reset): the newest queued picture from memory also stands for frames up to `last`, so the file keeps
// every frame (a short still) instead of a hole. An encoder that's merely slow still loses frames as before: it
// would only fall further behind on repeats. `c.lock` held.
constexpr auto STALLED = std::chrono::milliseconds(150);
// Returns 1 when held, 2 when this picture should still be queued past the limit (the newest queued is a texture
// from before the reset, which can't stand for later frames: this one will), 0 to drop it as before.
int hold_newest(Core &c, int64_t last) {
    if (c.pictures.empty() || std::chrono::steady_clock::now() - c.taken_at < STALLED || last <= c.last_pts) return 0;
    Core::Picture &newest = c.pictures.back();
    if (!newest.frame || newest.frame->format != AV_PIX_FMT_NV12) return c.pictures.size() <= c.queue_limit ? 2 : 0;
    const int64_t more = last - c.last_pts;
    newest.count = last - newest.pts + 1;
    c.last_pts = last;
    c.frames_repeated += static_cast<uint64_t>(more);
    c.frames_held += static_cast<uint64_t>(more);
    return 1;
}

// While the encoder opens again, more pictures from memory may wait than queue_limit: up to REOPEN_BYTES of them
// (about half a second at 1080p60, more when smaller), so the file goes on moving rather than holding a still. Back
// to queue_limit as soon as it's open; the extra frames are freed as they're written. `c.lock` held.
constexpr size_t REOPEN_BYTES = size_t{96} << 20;
constexpr size_t REOPEN_MOST = 90;
bool reopen_room(const Core &c) {
    if (!c.reopening) return false;
    const size_t frame = static_cast<size_t>(c.width) * static_cast<size_t>(c.height) * 3 / 2;
    const size_t room = std::clamp(frame ? REOPEN_BYTES / frame : c.queue_limit, c.queue_limit, REOPEN_MOST);
    return c.pictures.size() < room;
}

// One picture for frames first .. last (one frame, or a hole filled with it). Never waits on the writer thread.
int feed(Core &c, const uint8_t *nv12, int stride, int64_t first, int64_t last) {
    FramePtr f;
    {
        std::lock_guard<std::mutex> hold(c.lock);
        ++c.frames_in;
        if (c.state == SC_WRITER_FAILED || c.state == SC_WRITER_FINISHED || c.finishing) return SC_WRITER_REFUSED;
        if (last <= c.last_pts) {  // already written past it (two feeds met): never back in time
            ++c.frames_late;
            return SC_WRITER_LATE;
        }
        if (c.pictures.size() >= c.queue_limit && !reopen_room(c)) {
            const int held = hold_newest(c, last);
            if (held == 1) return SC_OK;
            if (held == 0) {
                ++c.frames_dropped;
                return SC_WRITER_DROPPED;
            }
        }
        if (!c.spare.empty()) {
            f = std::move(c.spare.back());
            c.spare.pop_back();
        }
    }
    const int width = c.width, height = c.height;
    if (!f) {
        f.reset(ff.av_frame_alloc());
        if (!f) throw std::bad_alloc();
        f->format = AV_PIX_FMT_NV12;
        f->width = width;
        f->height = height;
        if (ff.av_frame_get_buffer(f.get(), 0) < 0) throw std::bad_alloc();
    } else if (ff.av_frame_make_writable(f.get()) < 0) {
        throw std::bad_alloc();
    }
    for (int y = 0; y < height; ++y)
        std::memcpy(f->data[0] + static_cast<ptrdiff_t>(y) * f->linesize[0], nv12 + static_cast<ptrdiff_t>(y) * stride,
                    static_cast<size_t>(width));
    const uint8_t *uv = nv12 + static_cast<ptrdiff_t>(height) * stride;
    for (int y = 0; y < height / 2; ++y)
        std::memcpy(f->data[1] + static_cast<ptrdiff_t>(y) * f->linesize[1], uv + static_cast<ptrdiff_t>(y) * stride,
                    static_cast<size_t>(width));
    f->color_range = AVCOL_RANGE_MPEG;
    f->colorspace = AVCOL_SPC_BT709;
    {
        std::lock_guard<std::mutex> hold(c.lock);
        if (last <= c.last_pts) {  // another feed got in while this one copied
            ++c.frames_late;
            return SC_WRITER_LATE;
        }
        first = std::max(first, c.last_pts + 1);
        c.last_pts = last;
        c.frames_repeated += static_cast<uint64_t>(last - first);
        c.pictures.push_back(Core::Picture{first, last - first + 1, std::move(f), c.key_next});
        c.key_next = false;
    }
    c.wake.notify_one();
    return SC_OK;
}

#if defined(_WIN32)
struct TextureHold {  // a texture handed over by the engine, until the encoder lets go of its frame
    ID3D11Texture2D *texture;
    std::function<void()> done;
};

void let_go_of_texture(void *opaque, uint8_t *) {
    auto *hold = static_cast<TextureHold *>(opaque);
    try {
        if (hold->done) hold->done();
    } catch (...) {
    }
    hold->texture->Release();
    delete hold;
}
#endif

// An NV12 texture of the writer's device and size for frames first .. last (frames on the card). SC_OK: the writer
// has it and calls `done` once the encoder is finished with it. Anything else: not taken (`done` isn't called).
int feed_texture(Core &c, void *texture, int64_t first, int64_t last, std::function<void()> done) {
#if defined(_WIN32)
    if (!texture) return SC_ERR_ARGUMENT;
    auto *tex = static_cast<ID3D11Texture2D *>(texture);
    D3D11_TEXTURE2D_DESC desc;
    tex->GetDesc(&desc);
    ID3D11Device *owner = nullptr;
    tex->GetDevice(&owner);
    bool same;
    {
        std::lock_guard<std::mutex> hold(c.lock);  // to_memory lets go of it under the lock
        same = owner != nullptr && owner == c.device && !c.leave.load();
    }
    if (owner) owner->Release();
    AVBufferRef *frames_ctx = nullptr;
    if (!same || desc.Format != DXGI_FORMAT_NV12 || static_cast<int>(desc.Width) != c.width ||
        static_cast<int>(desc.Height) != c.height)
        return SC_ERR_ARGUMENT;  // another device (the engine rebuilt its own) or another size
    {
        std::lock_guard<std::mutex> hold(c.lock);
        ++c.frames_in;
        if (c.state == SC_WRITER_FAILED || c.state == SC_WRITER_FINISHED || c.finishing) return SC_WRITER_REFUSED;
        if (c.state != SC_WRITER_RUNNING) {
            ++c.frames_dropped;
            return SC_WRITER_DROPPED;  // still opening: the encoder isn't there to take it yet
        }
        if (!c.hw_frames) return SC_ERR_ARGUMENT;  // its encoder left the card (to_memory): the link ends
        if (last <= c.last_pts) {
            ++c.frames_late;
            return SC_WRITER_LATE;
        }
        if (c.pictures.size() >= c.queue_limit) {
            ++c.frames_dropped;
            return SC_WRITER_DROPPED;
        }
        const bool known = std::find(c.seen.begin(), c.seen.end(), texture) != c.seen.end();
        if (!known && c.seen.size() >= 128) return SC_ERR_ARGUMENT;  // the engine made its pools again too often
        frames_ctx = ff.av_buffer_ref(c.hw_frames);  // the writer thread may close it any time after this lock
        if (!frames_ctx) throw std::bad_alloc();
        if (!known) {
            tex->AddRef();
            c.seen.push_back(texture);
        }
    }
    FramePtr f(ff.av_frame_alloc());
    if (!f) {
        ff.av_buffer_unref(&frames_ctx);
        throw std::bad_alloc();
    }
    f->format = AV_PIX_FMT_D3D11;
    f->width = c.width;
    f->height = c.height;
    f->data[0] = static_cast<uint8_t *>(texture);
    f->data[1] = nullptr;  // array slice 0
    f->color_range = AVCOL_RANGE_MPEG;
    f->colorspace = AVCOL_SPC_BT709;
    f->hw_frames_ctx = frames_ctx;  // f's now
    auto *hold = new TextureHold{tex, std::move(done)};
    tex->AddRef();
    f->buf[0] = ff.av_buffer_create(reinterpret_cast<uint8_t *>(hold), sizeof *hold, let_go_of_texture, hold, 0);
    if (!f->buf[0]) {
        hold->done = nullptr;  // not taken: the caller gives it back
        tex->Release();
        delete hold;
        throw std::bad_alloc();
    }
    {
        std::lock_guard<std::mutex> hold_lock(c.lock);
        if (last <= c.last_pts) {  // another feed got in meanwhile: let go of it here, as taken
            ++c.frames_late;
            return SC_OK;          // f's free gives the texture back
        }
        first = std::max(first, c.last_pts + 1);
        c.last_pts = last;
        c.frames_repeated += static_cast<uint64_t>(last - first);
        c.pictures.push_back(Core::Picture{first, last - first + 1, std::move(f), c.key_next});
        c.key_next = false;
    }
    c.wake.notify_one();
    return SC_OK;
#else
    (void)c, (void)texture, (void)first, (void)last, (void)done;
    return SC_ERR_ARGUMENT;
#endif
}

}  // namespace

struct sc_writer {
    std::shared_ptr<Core> core;
    std::thread thread;
};

namespace {
// Writers whose encoder is on a graphics device of the engine's (writers_leave_device).
std::mutex on_card_lock;
std::vector<std::weak_ptr<Core>> on_card_writers;
}  // namespace

namespace sc {
void writers_leave_device(void *device) {
    std::lock_guard<std::mutex> hold(on_card_lock);
    on_card_writers.erase(std::remove_if(on_card_writers.begin(), on_card_writers.end(),
                                         [](const std::weak_ptr<Core> &w) { return w.expired(); }),
                          on_card_writers.end());
    for (const auto &w : on_card_writers) {
        auto c = w.lock();
        if (!c) continue;
        bool mine;
        {
            std::lock_guard<std::mutex> hold_core(c->lock);
            mine = c->device == device;
        }
        if (mine) {
            c->leave = true;
            c->wake.notify_all();
        }
    }
}
}  // namespace sc

extern "C" {

SC_API int sc_writer_open(const sc_writer_config *cfg, sc_writer **out) {
    return sc::guarded("sc_writer_open", [&]() -> int {
        if (!cfg || !out || !cfg->target || !*cfg->target || !cfg->video_codec)
            return sc::fail(SC_ERR_ARGUMENT, "sc_writer_open: no target or encoder");
        *out = nullptr;
        if (!sc::ffm::ff_ready.load()) return sc::fail(SC_ERR_DEVICE, "sc_writer_open: FFmpeg isn't loaded");
        if (cfg->width < 2 || cfg->height < 2 || cfg->width % 2 || cfg->height % 2 || cfg->width > 8192 ||
            cfg->height > 8192 || cfg->fps_num < 1 || cfg->fps_den < 1 || cfg->audio_tracks < 0 ||
            cfg->audio_tracks > MAX_TRACKS)
            return sc::fail(SC_ERR_ARGUMENT, "sc_writer_open: a size, rate or track count it can't write");
        auto core = std::make_shared<Core>();
        core->target = cfg->target;
        core->format = cfg->format ? cfg->format : "";
        core->video_codec = cfg->video_codec;
        core->video_options = cfg->video_options ? cfg->video_options : "";
        core->format_options = cfg->format_options ? cfg->format_options : "";
        if (cfg->track_titles) {
            std::string all = cfg->track_titles;
            size_t at = 0;
            for (;;) {
                const size_t end = all.find('\n', at);
                core->titles.push_back(all.substr(at, end == std::string::npos ? std::string::npos : end - at));
                if (end == std::string::npos) break;
                at = end + 1;
            }
        }
        core->b_frames = std::clamp(cfg->b_frames, 0, 4);
        core->width = cfg->width;
        core->height = cfg->height;
        core->fps_num = cfg->fps_num;
        core->fps_den = cfg->fps_den;
        core->video_kbps = std::max(100, cfg->video_kbps);
        core->gop = std::max(1, cfg->keyframe_frames);
        core->tracks = cfg->audio_tracks;
        core->audio_kbps = std::max(32, cfg->audio_kbps);
        core->queue_limit = static_cast<size_t>(std::clamp(cfg->queue_frames, 2, 120));
        core->packets = cfg->packets != 0;
        core->reopen_ms = std::clamp(std::atoi(env_text("SPRIGCAST_WRITER_REOPEN_MS").c_str()), 0, 5000);
        const std::string fault = env_text("SPRIGCAST_WRITER_FAULT");  // tests: the encoder fails at these
        if (!fault.empty()) {
            for (const char *at = fault.c_str(); *at;) {
                char *end = nullptr;
                const long long n = std::strtoll(at, &end, 10);
                if (end == at) break;
                if (n >= 0 && (core->faults.empty() || n > core->faults.back())) core->faults.push_back(n);
                at = *end == ',' ? end + 1 : end;
            }
        }
        if (cfg->d3d11_device) {
#if defined(_WIN32)
            static_cast<IUnknown *>(cfg->d3d11_device)->AddRef();
            core->device = cfg->d3d11_device;
            core->on_card = true;
            {
                std::lock_guard<std::mutex> hold(on_card_lock);
                on_card_writers.push_back(core);
            }
#else
            return sc::fail(SC_ERR_ARGUMENT, "sc_writer_open: frames on the card need Windows");
#endif
        }
        core->sounds.resize(static_cast<size_t>(core->tracks));
        auto w = std::make_unique<sc_writer>();
        w->core = core;
        w->thread = std::thread([core] { core->run(); });
        *out = w.release();
        return SC_OK;
    });
}

SC_API int sc_writer_video(sc_writer *w, const uint8_t *nv12, int stride, int64_t pts) {
    return sc::guarded("sc_writer_video", [&]() -> int {
        if (!w || !nv12 || stride < w->core->width) return sc::fail(SC_ERR_ARGUMENT, "sc_writer_video: no picture");
        return feed(*w->core, nv12, stride, pts, pts);
    });
}

SC_API int sc_writer_video_span(sc_writer *w, const uint8_t *nv12, int stride, int64_t first, int64_t last) {
    return sc::guarded("sc_writer_video_span", [&]() -> int {
        if (!w || !nv12 || stride < w->core->width || last < first || first < 0)
            return sc::fail(SC_ERR_ARGUMENT, "sc_writer_video_span: no picture or no frames");
        return feed(*w->core, nv12, stride, first, last);
    });
}

SC_API int sc_writer_audio(sc_writer *w, int track, const float *stereo, int frames) {
    return sc::guarded("sc_writer_audio", [&]() -> int {
        if (!w || !stereo || frames < 0) return sc::fail(SC_ERR_ARGUMENT, "sc_writer_audio: no sound");
        Core &c = *w->core;
        if (track < 0 || track >= c.tracks) return sc::fail(SC_ERR_ARGUMENT, "sc_writer_audio: no such track");
        std::vector<float> block(stereo, stereo + static_cast<size_t>(frames) * 2);
        {
            std::lock_guard<std::mutex> hold(c.lock);
            if (c.state == SC_WRITER_FAILED || c.state == SC_WRITER_FINISHED || c.finishing) return SC_WRITER_REFUSED;
            c.sounds[static_cast<size_t>(track)].push_back(std::move(block));
        }
        c.wake.notify_one();
        return SC_OK;
    });
}

SC_API int sc_writer_finish(sc_writer *w) {
    return sc::guarded("sc_writer_finish", [&]() -> int {
        if (!w) return sc::fail(SC_ERR_ARGUMENT, "sc_writer_finish: no writer");
        {
            std::lock_guard<std::mutex> hold(w->core->lock);
            w->core->finishing = true;
        }
        w->core->wake.notify_one();
        return SC_OK;
    });
}

SC_API int sc_writer_wait(sc_writer *w, int timeout_ms) {
    return sc::guarded("sc_writer_wait", [&]() -> int {
        if (!w) return sc::fail(SC_ERR_ARGUMENT, "sc_writer_wait: no writer");
        Core &c = *w->core;
        std::unique_lock<std::mutex> hold(c.lock);
        c.done_cv.wait_for(hold, std::chrono::milliseconds(std::max(0, timeout_ms)), [&] { return c.done; });
        return c.done ? 1 : 0;
    });
}

SC_API int sc_writer_stats_get(sc_writer *w, sc_writer_stats *out) {
    return sc::guarded("sc_writer_stats_get", [&]() -> int {
        if (!w || !out) return sc::fail(SC_ERR_ARGUMENT, "sc_writer_stats_get: nothing given");
        Core &c = *w->core;
        std::lock_guard<std::mutex> hold(c.lock);
        std::memset(out, 0, sizeof *out);
        out->state = c.state;
        out->frames_in = c.frames_in;
        out->frames_written = c.frames_written;
        out->frames_dropped = c.frames_dropped;
        out->bytes = c.bytes;
        out->queued = static_cast<int>(c.pictures.size());
        out->write_seconds = c.write_seconds;
        out->longest_write = c.longest_write;
        out->last_pts = c.last_pts;
        out->frames_late = c.frames_late;
        out->frames_repeated = c.frames_repeated;
        out->on_card = c.on_card.load() ? 1 : 0;
        out->recoveries = c.recoveries;
        std::snprintf(out->error, sizeof out->error, "%s", c.error.c_str());
        return SC_OK;
    });
}

SC_API int sc_writer_packet_take(sc_writer *w, sc_writer_packet *info, uint8_t *data, int capacity) {
    return sc::guarded("sc_writer_packet_take", [&]() -> int {
        if (!w || !info) return sc::fail(SC_ERR_ARGUMENT, "sc_writer_packet_take: nothing given");
        Core &c = *w->core;
        std::lock_guard<std::mutex> hold(c.lock);
        if (c.outs.empty()) return 0;
        Core::Out &o = c.outs.front();
        info->track = o.track;
        info->keyframe = o.key ? 1 : 0;
        info->pts = o.pts;
        info->dts = o.dts;
        info->tb_num = o.tb.num;
        info->tb_den = o.tb.den;
        info->size = static_cast<int>(o.data.size());
        if (!data || capacity < info->size) return SC_ERR_ARGUMENT;  // asked again with room; no error noted
        if (!o.data.empty()) std::memcpy(data, o.data.data(), o.data.size());
        c.outs.pop_front();
        return 1;
    });
}

SC_API int sc_writer_set_bitrate(sc_writer *w, int kbps, int max_kbps, int buffer_kbits) {
    return sc::guarded("sc_writer_set_bitrate", [&]() -> int {
        if (!w || kbps < 50) return sc::fail(SC_ERR_ARGUMENT, "sc_writer_set_bitrate: no writer or bitrate");
        Core &c = *w->core;
        c.want_max = std::max(0, max_kbps);
        c.want_buffer = std::max(0, buffer_kbits);
        c.want_kbps = kbps;  // last: the writer thread reads it first
        return SC_OK;
    });
}

SC_API int sc_writer_keyframe(sc_writer *w) {
    return sc::guarded("sc_writer_keyframe", [&]() -> int {
        if (!w) return sc::fail(SC_ERR_ARGUMENT, "sc_writer_keyframe: no writer");
        std::lock_guard<std::mutex> hold(w->core->lock);
        w->core->key_next = true;
        return SC_OK;
    });
}

SC_API void sc_writer_destroy(sc_writer *w) {
    if (!w) return;
    try {
        bool done;
        {
            std::lock_guard<std::mutex> hold(w->core->lock);
            done = w->core->done;
        }
        if (!done) {  // closed without finishing: given up at once (a stream stuck connecting stops waiting too)
            w->core->abort.store(true);
            w->core->wake.notify_one();
        }
        if (w->thread.joinable()) {
            std::unique_lock<std::mutex> hold(w->core->lock);
            const bool ended = w->core->done_cv.wait_for(hold, std::chrono::seconds(5), [&] { return w->core->done; });
            hold.unlock();
            if (ended) {
                w->thread.join();
            } else {
                w->thread.detach();  // still stuck inside FFmpeg: it keeps its Core and ends by itself
            }
        }
    } catch (...) {
    }
    delete w;
}

}  // extern "C"

namespace sc {

std::shared_ptr<void> writer_share(sc_writer *w) { return w ? std::static_pointer_cast<void>(w->core) : nullptr; }

int writer_feed(const std::shared_ptr<void> &shared, const uint8_t *nv12, int width, int height, int64_t first,
                int64_t last) {
    auto *c = static_cast<Core *>(shared.get());
    if (!c || width != c->width || height != c->height || last < first || first < 0) return SC_ERR_ARGUMENT;
    return feed(*c, nv12, width, first, last);
}

int writer_feed_texture(const std::shared_ptr<void> &shared, void *texture, int64_t first, int64_t last,
                        std::function<void()> done) {
    auto *c = static_cast<Core *>(shared.get());
    if (!c || last < first || first < 0) return SC_ERR_ARGUMENT;
    return feed_texture(*c, texture, first, last, std::move(done));
}

bool writer_on_card(const std::shared_ptr<void> &shared) {
    auto *c = static_cast<Core *>(shared.get());
    return c && c->on_card.load();
}

int64_t writer_last(const std::shared_ptr<void> &shared) {
    auto *c = static_cast<Core *>(shared.get());
    if (!c) return -1;
    std::lock_guard<std::mutex> hold(c->lock);
    return c->last_pts;
}

bool writer_size(const std::shared_ptr<void> &shared, int *width, int *height) {
    auto *c = static_cast<Core *>(shared.get());
    if (!c) return false;
    *width = c->width;
    *height = c->height;
    return true;
}

}  // namespace sc
