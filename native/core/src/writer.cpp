// Recording and streaming output (phase 4, docs/native-engine.md): a file or stream written by a thread of the
// engine's own. Pictures (NV12) and sound (stereo floats) are handed over and queued; the writer thread encodes
// them with FFmpeg (SprigCast's own copy, ffmpeg.h), muxes and writes, so nothing of it needs Python's lock.
//
// Like media.cpp this needs no graphics card and builds on Linux, so its checks run there too
// (tests/test_native_writer.py).
#include "base.h"
#include "packet_times.h"
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
#include <cmath>
#include <condition_variable>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <filesystem>
#include <fstream>
#include <limits>
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

struct Link;
struct File;

// Clips: a fixed-size ring of packet bytes in a file (output/replay.py Spool). Positions only ever grow; the byte
// in the file is at position % capacity. A packet never wraps around the end, so it may skip ahead to the next
// lap. Packets more than one lap behind the write position have been overwritten.
struct Ring {
    std::mutex lock;
    std::fstream file;
    std::filesystem::path path;
    uint64_t capacity = 0, pos = 0;
    static std::shared_ptr<Ring> make(const std::string &utf8, uint64_t capacity) {
        auto r = std::make_shared<Ring>();
        r->path = std::filesystem::path(std::u8string(utf8.begin(), utf8.end()));
        r->capacity = capacity;
        r->file.open(r->path, std::ios::in | std::ios::out | std::ios::binary | std::ios::trunc);
        return r->file ? r : nullptr;
    }
    ~Ring() {
        file.close();
        std::error_code ignored;
        std::filesystem::remove(path, ignored);
    }
    uint64_t place(size_t size) {  // `lock` held
        if (pos % capacity + size > capacity) pos += capacity - pos % capacity;
        const uint64_t at = pos;
        pos += size;
        return at;
    }
    bool write(uint64_t at, const uint8_t *data, size_t size) {  // `lock` held
        file.clear();
        file.seekp(static_cast<std::streamoff>(at % capacity));
        file.write(reinterpret_cast<const char *>(data), static_cast<std::streamsize>(size));
        return static_cast<bool>(file);
    }
    bool read(uint64_t at, uint8_t *data, size_t size) {
        std::lock_guard<std::mutex> hold(lock);
        if (pos > capacity && at < pos - capacity) return false;  // written over since
        file.clear();
        file.flush();
        file.seekg(static_cast<std::streamoff>(at % capacity));
        file.read(reinterpret_cast<char *>(data), static_cast<std::streamsize>(size));
        return static_cast<bool>(file);
    }
};
// One packet kept for clips: its bytes in memory, or in a ring once moved there.
struct Kept {
    int track = 0;
    bool key = false;
    int64_t pts = 0, dts = 0;
    AVRational tb{1, 1};
    size_t size = 0;
    double seconds = 0;
    std::shared_ptr<const std::vector<uint8_t>> mem;
    std::shared_ptr<Ring> ring;
    uint64_t at = 0, seq = 0;
};
// One encoded packet on its way to the engine's own senders and files (sc_link, sc_file), shared between them.
struct Encoded {
    int track = 0;  // 0 the video, 1.. the sound tracks
    bool key = false;
    int64_t pts = 0, dts = 0;
    AVRational tb{1, 1};
    std::vector<uint8_t> data;  // with FFmpeg's padding after `size` bytes
    size_t size = 0;
    double seconds = 0;
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
    // After it opens, the pictures that waited are written while new ones keep coming: the extra room stays until
    // the queue is back to queue_limit (PC 2026-11-18: 25 frames dropped right after a reopen without it).
    bool catching_up = false;
    int64_t hole_from = -1;    // frames of textures lost with the encoder on the card start here (writer thread)
    double write_seconds = 0, longest_write = 0;
    double encode_seconds = 0;  // time handing pictures to the encoder and taking its packets, writing them not counted
    bool yuv420 = false;  // the encoder took no NV12: pictures are repacked as I420 (writer thread only)
    std::deque<int64_t> faults;  // SPRIGCAST_WRITER_FAULT (tests): pictures whose encoding fails ("10" or "10,20")
    int reopen_ms = 0;  // SPRIGCAST_WRITER_REOPEN_MS (tests): opening the encoder again takes this long
    int slow_ms = 0;    // SPRIGCAST_WRITER_SLOW_MS (tests): each picture after a reopen takes this long to encode
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
    std::atomic<bool> hand_out{true};  // sc_writer_hand_out: 0 = nobody takes them, none are queued
    // Clips (sc_writer_keep): the last keep_seconds of packets, from a video keyframe (ReplayBuffer._keep, _trim).
    std::mutex keep_lock;
    double keep_seconds = 0, newest = -std::numeric_limits<double>::infinity();
    std::string ring_path;
    int rings_made = 0;
    bool ring_failed = false;
    std::shared_ptr<Ring> ring;
    std::deque<Kept> kept;
    std::deque<std::pair<uint64_t, double>> keys;  // the video keyframes among `kept` (seq, seconds), oldest first
    uint64_t kept_seq = 0;
    void keep_packet(const Out &o);
    void pop_oldest() {  // keep_lock held
        if (!keys.empty() && keys.front().first == kept.front().seq) keys.pop_front();
        kept.pop_front();
    }
    // Streams the engine sends itself (sc_link): each is offered every packet as it is encoded (writer thread).
    std::mutex links_lock;
    std::vector<std::shared_ptr<Link>> links;
    std::vector<std::shared_ptr<File>> files;  // recordings the engine writes itself (sc_file), under links_lock
    bool streams_ready = false;  // under links_lock: `out`'s streams hold the codec headers (open() .. close_all)
    void offer_links(const Out &o);
    void end_links(const std::string &why);
    void offer_files(const std::vector<std::shared_ptr<File>> &to, const std::shared_ptr<const Encoded> &p);
    void end_files(const std::vector<std::shared_ptr<File>> &gone);
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
    bool dts_told = false;  // logged once that the encoder's decode times ran ahead
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
    std::lock_guard<std::mutex> hold(links_lock);
    streams_ready = true;
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
        // FFmpeg's AMD encoder takes decode times from a list of the frames handed in, apart from the
        // pictures it gives back; on an older Radeon (tester PC 2026-11-22, Radeon R2) that list ran a
        // frame ahead, so every stream ended at its second frame with "pts < dts" -> "Invalid argument".
        if (sc::even_out_dts(p->pts, p->dts) && !dts_told) {
            dts_told = true;
            sc::log_line(SC_LOG_INFO, std::string("writer: ") + enc->codec->name
                         + " gave decode times after the picture times; using the picture times");
        }
        if (packets) {  // for the caller to take: copied out, in the encoder's own time base
            Out o;
            o.track = stream->index;
            o.key = (p->flags & AV_PKT_FLAG_KEY) != 0;
            o.pts = p->pts;
            o.dts = p->dts;
            o.tb = enc->time_base;
            o.data.assign(p->data, p->data + p->size);
            ff.av_packet_unref(p);
            keep_packet(o);
            offer_links(o);
            std::lock_guard<std::mutex> hold(lock);
            bytes += o.data.size();
            if (!hand_out.load()) continue;
            if (outs.size() >= 4096) throw WriteError("the encoded packets weren't taken for a minute");
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
    // Timed for the stream report's "encode ms" (PC soak 2026-11-20: 0.0 ms on every line with the engine encoding).
    const double began = now_s(), written = write_seconds;  // write_seconds only changes on this thread
    struct Timed {
        Core &c;
        double began, written;
        ~Timed() {
            const double spent = now_s() - began - (c.write_seconds - written);
            std::lock_guard<std::mutex> hold(c.lock);
            c.encode_seconds += spent > 0 ? spent : 0;
        }
    } timed{*this, began, written};
    if (slow_ms > 0 && frame && recoveries > 0) {  // tests: an encoder that only just keeps up
        // To the millisecond: Windows' sleep rounds 20 ms up to the next 15.6 ms tick (31 ms), and an encoder at 32
        // a second never caught up with 30 coming, so the queue filled and frames were repeated (Windows CI on #756:
        // 7 repeated, passing on a re-run).
        const auto until = std::chrono::steady_clock::now() + std::chrono::milliseconds(slow_ms);
        while (std::chrono::steady_clock::now() < until) std::this_thread::yield();
    }
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
        catching_up = pictures.size() > queue_limit;
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



// ---- Clips kept by the engine (sc_writer_keep) -----------------------------------------------------------------
constexpr double KEY_SLACK_S = 10.0;  // sound kept past the seconds while waiting for a keyframe

void Core::keep_packet(const Out &o) {
    std::lock_guard<std::mutex> hold(keep_lock);
    if (keep_seconds <= 0) {
        if (!kept.empty()) {
            kept.clear();
            keys.clear();
            newest = -std::numeric_limits<double>::infinity();
        }
        ring.reset();  // packets a clip being written still needs keep their ring alive
        return;
    }
    Kept k;
    k.track = o.track;
    k.key = o.key;
    k.pts = o.pts;
    k.dts = o.dts;
    k.tb = o.tb;
    k.size = o.data.size();
    k.seconds = static_cast<double>(o.pts) * o.tb.num / std::max(1, o.tb.den);
    k.seq = kept_seq++;
    bool spooled = false;
    if (!ring_failed && !ring_path.empty() && k.size > 0) {
        // Room for the seconds plus a keyframe interval, twice over for peaks and sound; at least 16 MB.
        const uint64_t wanted =
            std::max<uint64_t>(16ull << 20, static_cast<uint64_t>(video_kbps * 125.0 * (keep_seconds + 10) * 2));
        if (!ring || ring->capacity < wanted) {  // a longer time asked for: a new ring (older packets keep theirs)
            auto made = Ring::make(ring_path + (rings_made ? "." + std::to_string(rings_made) : std::string()), wanted);
            ++rings_made;
            if (made) {
                ring = made;
            } else {
                ring_failed = true;
                sc::log_line(SC_LOG_WARNING, "writer: the clips' ring file can't be made; clips are kept in memory");
            }
        }
        if (!ring_failed && k.size <= ring->capacity) {
            std::lock_guard<std::mutex> ring_hold(ring->lock);
            const uint64_t at = ring->place(k.size);
            const int64_t limit = static_cast<int64_t>(at + k.size) - static_cast<int64_t>(ring->capacity);
            while (!kept.empty() && kept.front().ring == ring && static_cast<int64_t>(kept.front().at) < limit)
                pop_oldest();  // a burst bigger than the ring: the oldest moments are given up
            if (ring->write(at, o.data.data(), k.size)) {
                k.ring = ring;
                k.at = at;
                spooled = true;
            } else {
                ring_failed = true;
                sc::log_line(SC_LOG_WARNING, "writer: the clips' ring file can't be written; clips are kept in memory");
            }
        }
    }
    if (!spooled) {
        auto bytes_kept = std::make_shared<std::vector<uint8_t>>(o.data.size() + AV_INPUT_BUFFER_PADDING_SIZE);
        if (!o.data.empty()) std::memcpy(bytes_kept->data(), o.data.data(), o.data.size());
        k.mem = std::move(bytes_kept);
    }
    if (k.track == 0) {
        newest = std::max(newest, k.seconds);
        if (k.key) keys.emplace_back(k.seq, k.seconds);
    }
    const double last = k.seconds;
    kept.push_back(std::move(k));
    if (keys.empty()) {
        // No picture keyframe yet: a clip can't start anywhere, so the sound mustn't pile up behind it.
        while (!kept.empty() && kept.front().seconds < last - std::max(keep_seconds, 1.0) - KEY_SLACK_S) pop_oldest();
        return;
    }
    const double cutoff = newest - keep_seconds;
    while (keys.size() > 1 && keys[1].second <= cutoff) keys.pop_front();
    while (!kept.empty() && kept.front().seq != keys.front().first) kept.pop_front();
}

// ---- A stream sent by the engine (sc_link) -------------------------------------------------------------------
// One connection of a packets-mode writer: output/shared_stream.py's StreamLink (offer, _send, _session and the
// DelayLine), on a thread of the engine's own. Python only looks at its numbers and decides when to try again.
struct Link {
    std::shared_ptr<Core> core;
    std::string url, format;
    double bytes_per_s = 750000, delay_s = 0, max_backlog = 4, resume_backlog = 2, behind_s = 0.25;
    double connect_s = 10, send_s = 20;
    std::atomic<bool> writing{false};    // inside av_interleaved_write_frame (link_interrupted)
    std::atomic<double> stop_seen{0};    // when a stop first met a packet halfway out
    int slow_ms = 0;  // SPRIGCAST_LINK_SLOW_MS (tests): each packet takes this long to send (a slow upload)

    using Packet = Encoded;
    struct Held {
        std::shared_ptr<const Packet> packet;
        double due = 0;
    };
    std::mutex lock;
    std::condition_variable wake, done_cv;
    std::deque<Held> delayed;                           // not yet due (a stream delay)
    std::deque<std::shared_ptr<const Packet>> waiting;  // to be sent
    int state = SC_LINK_CONNECTING;
    bool started = false, congested = false, finishing = false, done = false;
    std::string error, ended;  // ended: the writer closed (says why)
    double zero = 0;           // the writer's seconds of the keyframe this connection started at
    uint64_t frames = 0, dropped = 0, bytes = 0, backlog_bytes = 0;
    double send_seconds = 0, longest_send = 0, behind_seconds = 0, sent_at = -1;
    std::atomic<bool> stop{false};
    std::atomic<bool> closing{false};  // saying goodbye: only the deadline ends a network call now
    std::atomic<double> deadline{0};   // the network call running now is given up after this (now_s)
    bool header = false, broken = false, trailer = false;  // link thread only
    std::thread thread;

    // `lock` held: packets whose delay has passed may be sent.
    void ripen(double now) {
        while (!delayed.empty() && delayed.front().due <= now) {
            backlog_bytes += delayed.front().packet->size;
            waiting.push_back(std::move(delayed.front().packet));
            delayed.pop_front();
        }
    }
    // Writer thread. True: this link fell behind just now and, alone on its writer, wants a keyframe to rejoin at.
    bool offer(const std::shared_ptr<const Packet> &p) {
        std::lock_guard<std::mutex> hold(lock);
        if ((state != SC_LINK_READY && state != SC_LINK_LIVE) || finishing) return false;
        const bool picture = p->track == 0;
        if (!started) {
            if (!(picture && p->key)) return false;  // a connection starts at a keyframe
            started = true;
            zero = p->seconds;
        }
        const double now = now_s();
        ripen(now);
        const double backlog = static_cast<double>(backlog_bytes) / bytes_per_s;
        if (congested) {
            if (backlog < resume_backlog && picture && p->key) {
                congested = false;  // caught up: carry on from this keyframe
            } else {
                if (picture) ++dropped;
                return false;
            }
        } else if (backlog > max_backlog) {
            congested = true;
            if (picture) ++dropped;
            return true;
        }
        if (delay_s > 0) {
            delayed.push_back(Held{p, now + delay_s});
        } else {
            backlog_bytes += p->size;
            waiting.push_back(p);
        }
        wake.notify_one();
        return false;
    }
    void run();
    void session(AVFormatContext *&ctx);
};

int link_interrupted(void *opaque) {
    auto *l = static_cast<Link *>(opaque);
    const double now = now_s();
    if (now > l->deadline.load()) return 1;
    if (!l->stop.load() || l->closing.load()) return 0;
    // Stopped halfway through a packet: let it finish (briefly) so the goodbye that follows starts on a whole
    // message. Cut mid-message, the server reads FCUnpublish and deleteStream as part of the video and keeps the
    // stream live: Twitch showed a tester live after Stop, and a start 85 s later never connected (2026-11-22).
    if (l->writing.load()) {
        double expected = 0;
        l->stop_seen.compare_exchange_strong(expected, now);
        return now - l->stop_seen.load() > 2.0 ? 1 : 0;
    }
    return 1;
}

void Core::offer_links(const Out &o) {
    std::vector<std::shared_ptr<Link>> to;
    std::vector<std::shared_ptr<File>> into;
    {
        std::lock_guard<std::mutex> hold(links_lock);
        if (links.empty() && files.empty()) return;
        if (o.track <= 1) to = links;  // a stream carries the video and the first sound track
        into = files;
    }
    if (to.empty() && into.empty()) return;
    auto p = std::make_shared<Link::Packet>();
    p->track = o.track;
    p->key = o.key;
    p->pts = o.pts;
    p->dts = o.dts;
    p->tb = o.tb;
    p->size = o.data.size();
    p->data.resize(o.data.size() + AV_INPUT_BUFFER_PADDING_SIZE);
    if (!o.data.empty()) std::memcpy(p->data.data(), o.data.data(), o.data.size());
    p->seconds = static_cast<double>(o.pts) * o.tb.num / std::max(1, o.tb.den);
    offer_files(into, p);
    bool key = false;
    for (auto &l : to) key = l->offer(p) || key;
    if (key && to.size() < 2) {
        // With others on the same encoder it waits for the next regular keyframe: one forced each time a slow
        // place fell behind gave every place bursts of bitrate (output/shared_stream.py StreamLink.offer).
        std::lock_guard<std::mutex> hold(lock);
        key_next = true;
    }
}

void Core::end_links(const std::string &why) {
    std::vector<std::shared_ptr<Link>> gone;
    std::vector<std::shared_ptr<File>> written;
    {
        std::lock_guard<std::mutex> hold(links_lock);
        streams_ready = false;
        gone.swap(links);
        written.swap(files);
    }
    end_files(written);
    for (auto &l : gone) {
        std::lock_guard<std::mutex> hold(l->lock);
        if (l->ended.empty()) l->ended = why;
        l->wake.notify_all();
    }
}

void Link::session(AVFormatContext *&ctx) {
    // The writer's encoders may still be opening (a link is made as soon as the stream starts).
    const double until = now_s() + 10;
    for (;;) {
        {
            std::lock_guard<std::mutex> hold(core->links_lock);
            if (core->streams_ready) break;
        }
        {
            std::lock_guard<std::mutex> hold(lock);
            if (!ended.empty()) throw Error(ended);
        }
        if (stop.load()) return;
        if (now_s() > until) throw Error("the shared encoder did not start");
        std::this_thread::sleep_for(std::chrono::milliseconds(20));
    }
    must(ff.avformat_alloc_output_context2(&ctx, nullptr, format.c_str(), url.c_str()), "the connection");
    ctx->interrupt_callback.callback = link_interrupted;
    ctx->interrupt_callback.opaque = this;
    {
        std::lock_guard<std::mutex> hold(core->links_lock);
        if (!core->streams_ready || !core->out) throw Error("the encoder stopped");
        const unsigned count = std::min(2u, core->out->nb_streams);
        for (unsigned i = 0; i < count; ++i) {
            AVStream *st = ff.avformat_new_stream(ctx, nullptr);
            if (!st) throw std::bad_alloc();
            must(ff.avcodec_parameters_copy(st->codecpar, core->out->streams[i]->codecpar), "the stream's headers");
            st->codecpar->codec_tag = 0;  // the tag is the other muxer's
        }
    }
    deadline = now_s() + connect_s;
    if (!(ctx->oformat->flags & AVFMT_NOFILE)) {
        AVIOInterruptCB cb{link_interrupted, this};
        const int r = ff.avio_open2(&ctx->pb, url.c_str(), AVIO_FLAG_WRITE, &cb, nullptr);
        if (r < 0) throw Error("couldn't connect: " + av_text(r));
    }
    const int head = ff.avformat_write_header(ctx, nullptr);
    if (head < 0) throw Error("couldn't connect: " + av_text(head));
    this->header = true;
    {
        std::lock_guard<std::mutex> hold(lock);
        state = SC_LINK_READY;
    }
    {
        std::lock_guard<std::mutex> hold(core->lock);
        core->key_next = true;  // a connection starts at a keyframe: the next picture is one
    }
    AVPacket *pkt = ff.av_packet_alloc();
    if (!pkt) throw std::bad_alloc();
    struct Free {
        AVPacket *p;
        ~Free() { ff.av_packet_free(&p); }
    } free_pkt{pkt};
    for (;;) {
        std::shared_ptr<const Packet> p;
        double first = 0;
        {
            std::unique_lock<std::mutex> hold(lock);
            for (;;) {
                ripen(now_s());
                if (stop.load()) return;
                if (!waiting.empty()) break;
                if (!ended.empty()) throw Error(ended);
                if (finishing && delayed.empty()) {
                    hold.unlock();
                    closing = true;
                    deadline = now_s() + 5;
                    trailer = true;
                    ff.av_write_trailer(ctx);
                    hold.lock();
                    state = SC_LINK_FINISHED;
                    return;
                }
                if (delayed.empty()) {
                    wake.wait_for(hold, std::chrono::milliseconds(200));
                } else {
                    const double left = std::clamp(delayed.front().due - now_s(), 0.001, 0.2);
                    wake.wait_for(hold, std::chrono::duration<double>(left));
                }
            }
            p = std::move(waiting.front());
            waiting.pop_front();
            first = zero;
        }
        // Times count from the keyframe the connection started at; sound from just before it is left out.
        const int64_t offset = std::llround(first * p->tb.den / std::max(1, p->tb.num));
        int w = 0;
        bool sent = false;
        double spent = 0;
        if (p->pts >= offset && p->track < static_cast<int>(ctx->nb_streams)) {
            AVStream *st = ctx->streams[p->track];
            pkt->data = const_cast<uint8_t *>(p->data.data());  // not reference-counted: the muxer copies it
            pkt->size = static_cast<int>(p->size);
            pkt->pts = p->pts - offset;
            pkt->dts = p->dts - offset;
            pkt->flags = p->key ? AV_PKT_FLAG_KEY : 0;
            pkt->stream_index = st->index;
            ff.av_packet_rescale_ts(pkt, p->tb, st->time_base);
            const double began = now_s();
            deadline = began + send_s;
            if (slow_ms > 0) std::this_thread::sleep_for(std::chrono::milliseconds(slow_ms));
            writing = true;
            w = ff.av_interleaved_write_frame(ctx, pkt);
            writing = false;
            spent = now_s() - began;
            ff.av_packet_unref(pkt);
            sent = true;
        }
        std::lock_guard<std::mutex> hold(lock);
        backlog_bytes -= std::min<uint64_t>(backlog_bytes, p->size);
        if (!sent) continue;
        if (w < 0) {
            broken = true;
            throw Error(stop.load() ? "stopped" : "sending: " + av_text(w));
        }
        const double done_at = now_s();
        send_seconds += spent;
        longest_send = std::max(longest_send, spent);
        if (sent_at >= 0 && static_cast<double>(backlog_bytes) / bytes_per_s >= behind_s)
            behind_seconds += done_at - sent_at;
        sent_at = done_at;
        bytes += p->size;
        if (p->track == 0) ++frames;
        state = SC_LINK_LIVE;
    }
}

void Link::run() {
    sc::name_thread(L"SprigCast stream link");
    sc::sender_thread_priority();
    AVFormatContext *ctx = nullptr;
    std::string why;
    bool faulted = false;
    try {
        session(ctx);
    } catch (const std::exception &e) {
        why = e.what();
    } catch (...) {
        // Crash gate: a fault inside FFmpeg ends this connection, never the app; nothing of it is touched again.
        faulted = true;
        why = "the connection faulted";
    }
    if (!faulted) {
        try {
            closing = true;
            deadline = now_s() + 2;  // closing says goodbye to the server: not for long
            if (ctx && header && !broken && !trailer) ff.av_write_trailer(ctx);  // what the muxer still holds goes out
            if (ctx && !(ctx->oformat->flags & AVFMT_NOFILE) && ctx->pb) ff.avio_closep(&ctx->pb);
            if (ctx) ff.avformat_free_context(ctx);
        } catch (...) {
        }
    }
    {
        std::lock_guard<std::mutex> hold(core->links_lock);
        auto &all = core->links;
        all.erase(std::remove_if(all.begin(), all.end(), [&](const auto &l) { return l.get() == this; }), all.end());
    }
    std::lock_guard<std::mutex> hold(lock);
    if (state != SC_LINK_FINISHED) {
        state = SC_LINK_FAILED;
        if (error.empty()) error = why.empty() ? "stopped" : why;
    }
    delayed.clear();
    waiting.clear();
    backlog_bytes = 0;
    done = true;
    done_cv.notify_all();
}


// ---- Twitch Enhanced Broadcasting sent by the engine (sc_esend) -------------------------------------------------
// output/eflv.py's EnhancedIngest on a thread of the engine's own: FFmpeg's FLV muxer writes every quality and the
// sound into memory, each classic H.264 video tag of the main quality is rewritten as its Enhanced RTMP
// single-track form (Twitch drops the connection otherwise), and the bytes go to the server through FFmpeg's
// RTMP protocol, which sends each FLV tag as a message. Python still chooses and tags the packets
// (output/enhanced_stream.py _from_track); packing and sending them held Python's lock on a thread above normal
// priority, 230 packets a second, and the window stopped answering for 371 ms beside it (PC, 2026-11-21).
struct ESend {
    std::string url;
    double connect_s = 10, send_s = 20;
    std::vector<AVCodecParameters *> pars;  // each stream's headers: the qualities, then the sound

    std::mutex lock;
    std::condition_variable wake, done_cv;
    std::deque<std::shared_ptr<const Encoded>> waiting;
    int state = SC_LINK_CONNECTING;
    bool finishing = false, done = false;
    std::string error;
    uint64_t packets = 0, bytes = 0, backlog_bytes = 0;
    double send_seconds = 0, longest_send = 0;
    std::atomic<bool> stop{false};
    std::atomic<bool> closing{false};
    std::atomic<double> deadline{0};
    std::thread thread;

    // Sender thread only.
    std::vector<uint8_t> packed;   // what the FLV muxer wrote since it was last taken
    std::vector<uint8_t> pending;  // FLV bytes not yet a whole tag
    bool flv_header = false;
    AVIOContext *out = nullptr;
    bool broken = false;

    ~ESend() {
        for (auto *p : pars) ff.avcodec_parameters_free(&p);
    }
    void run();
    void session(AVFormatContext *&flv);
    void forward();
    void rewrite(std::vector<uint8_t> &to);
};

int esend_interrupted(void *opaque) {
    auto *e = static_cast<ESend *>(opaque);
    if (now_s() > e->deadline.load()) return 1;
    return (e->stop.load() && !e->closing.load()) ? 1 : 0;
}

#if LIBAVFORMAT_VERSION_MAJOR >= 61
int esend_packed(void *opaque, const uint8_t *data, int size) {
#else
int esend_packed(void *opaque, uint8_t *data, int size) {
#endif
    auto *e = static_cast<ESend *>(opaque);
    e->packed.insert(e->packed.end(), data, data + size);
    return size;
}

// A classic FLV H.264 video tag's body in its Enhanced RTMP single-track form (eflv.py enhanced_video_body).
void enhanced_video_body(const uint8_t *body, size_t size, std::vector<uint8_t> &to) {
    constexpr uint8_t EX = 0x80, AVC = 7;
    static const uint8_t FOURCC[4] = {'a', 'v', 'c', '1'};
    if (size < 5 || (body[0] & EX) || (body[0] & 0x0F) != AVC) {
        to.insert(to.end(), body, body + size);
        return;
    }
    const uint8_t frame = body[0] & 0x70, kind = body[1];
    const uint8_t *data = body + 5;
    const size_t left = size - 5;
    const bool no_offset = body[2] == 0 && body[3] == 0 && body[4] == 0;
    const uint8_t type = kind == 0 ? 0 : kind == 2 ? 2 : no_offset ? 3 : 1;  // start, end, frames X, frames
    to.push_back(static_cast<uint8_t>(EX | frame | type));
    to.insert(to.end(), FOURCC, FOURCC + 4);
    if (kind == 2) return;
    if (type == 1) to.insert(to.end(), body + 2, body + 5);
    to.insert(to.end(), data, data + left);
}

// What the muxer wrote, whole tags only, with classic H.264 tags made Enhanced (eflv.py Rewriter.feed).
void ESend::rewrite(std::vector<uint8_t> &to) {
    constexpr size_t HEADER = 13, TAG = 11;
    pending.insert(pending.end(), packed.begin(), packed.end());
    packed.clear();
    size_t at = 0;
    if (!flv_header) {
        if (pending.size() < HEADER) return;
        to.insert(to.end(), pending.begin(), pending.begin() + HEADER);
        at = HEADER;
        flv_header = true;
    }
    std::vector<uint8_t> body;
    while (pending.size() - at >= TAG) {
        const uint8_t *tag = pending.data() + at;
        const size_t size = (static_cast<size_t>(tag[1]) << 16) | (static_cast<size_t>(tag[2]) << 8) | tag[3];
        const size_t whole = TAG + size + 4;
        if (pending.size() - at < whole) break;
        if ((tag[0] & 0x1F) == 9) {
            body.clear();
            enhanced_video_body(tag + TAG, size, body);
            const uint32_t n = static_cast<uint32_t>(body.size()), back = n + static_cast<uint32_t>(TAG);
            to.push_back(tag[0]);
            to.push_back(static_cast<uint8_t>(n >> 16));
            to.push_back(static_cast<uint8_t>(n >> 8));
            to.push_back(static_cast<uint8_t>(n));
            to.insert(to.end(), tag + 4, tag + TAG);
            to.insert(to.end(), body.begin(), body.end());
            for (int shift = 24; shift >= 0; shift -= 8) to.push_back(static_cast<uint8_t>(back >> shift));
        } else {
            to.insert(to.end(), tag, tag + whole);
        }
        at += whole;
    }
    pending.erase(pending.begin(), pending.begin() + static_cast<std::ptrdiff_t>(at));
}

// The muxer's new bytes go to the server.
void ESend::forward() {
    std::vector<uint8_t> bytes_out;
    rewrite(bytes_out);
    if (bytes_out.empty()) return;
    ff.avio_write(out, bytes_out.data(), static_cast<int>(bytes_out.size()));
    ff.avio_flush(out);
    if (out->error < 0) {
        broken = true;
        throw Error(stop.load() ? "stopped" : "sending: " + av_text(out->error));
    }
}

void ESend::session(AVFormatContext *&flv) {
    deadline = now_s() + connect_s;
    AVIOInterruptCB cb{esend_interrupted, this};
    AVDictionary *how = nullptr;
    // The codecs announced to the server: H.264, plus HEVC when a quality is HEVC (Twitch asks HEVC for a vertical
    // canvas's quality). FFmpeg's FLV muxer writes HEVC in the Enhanced form ("hvc1") itself.
    const bool hevc = std::any_of(pars.begin(), pars.end(),
                                  [](const AVCodecParameters *p) { return p->codec_id == AV_CODEC_ID_HEVC; });
    if (url.rfind("rtmp", 0) == 0) ff.av_dict_set(&how, "rtmp_enhanced_codecs", hevc ? "avc1,hvc1" : "avc1", 0);
    const int opened = ff.avio_open2(&out, url.c_str(), AVIO_FLAG_WRITE, &cb, &how);
    ff.av_dict_free(&how);
    if (opened < 0) throw Error("couldn't connect: " + av_text(opened));
    must(ff.avformat_alloc_output_context2(&flv, nullptr, "flv", nullptr), "the stream's packing");
    constexpr int ROOM = 32768;
    auto *room = static_cast<unsigned char *>(ff.av_malloc(ROOM));
    if (!room) throw std::bad_alloc();
    flv->pb = ff.avio_alloc_context(room, ROOM, 1, this, nullptr, esend_packed, nullptr);
    if (!flv->pb) {
        ff.av_free(room);
        throw std::bad_alloc();
    }
    flv->flags |= AVFMT_FLAG_CUSTOM_IO;
    for (auto *par : pars) {
        AVStream *st = ff.avformat_new_stream(flv, nullptr);
        if (!st) throw std::bad_alloc();
        must(ff.avcodec_parameters_copy(st->codecpar, par), "the stream's headers");
        st->codecpar->codec_tag = 0;
    }
    AVDictionary *options = nullptr;
    ff.av_dict_set(&options, "flvflags", "no_duration_filesize", 0);
    ff.av_dict_set(&options, "flush_packets", "1", 0);
    const int head = ff.avformat_write_header(flv, &options);
    ff.av_dict_free(&options);
    if (head < 0) throw Error("couldn't start the stream: " + av_text(head));
    ff.avio_flush(flv->pb);
    forward();
    {
        std::lock_guard<std::mutex> hold(lock);
        state = SC_LINK_READY;
    }
    AVPacket *pkt = ff.av_packet_alloc();
    if (!pkt) throw std::bad_alloc();
    struct Free {
        AVPacket *p;
        ~Free() { ff.av_packet_free(&p); }
    } free_pkt{pkt};
    for (;;) {
        std::shared_ptr<const Encoded> p;
        {
            std::unique_lock<std::mutex> hold(lock);
            for (;;) {
                if (stop.load()) return;
                if (!waiting.empty()) break;
                if (finishing) {
                    hold.unlock();
                    closing = true;
                    deadline = now_s() + 5;
                    ff.av_write_trailer(flv);
                    ff.avio_flush(flv->pb);
                    forward();
                    hold.lock();
                    state = SC_LINK_FINISHED;
                    return;
                }
                wake.wait_for(hold, std::chrono::milliseconds(200));
            }
            p = std::move(waiting.front());
            waiting.pop_front();
        }
        AVStream *st = flv->streams[p->track];
        pkt->data = const_cast<uint8_t *>(p->data.data());  // not reference-counted: the muxer copies it
        pkt->size = static_cast<int>(p->size);
        pkt->pts = p->pts;
        pkt->dts = p->dts;
        pkt->flags = p->key ? AV_PKT_FLAG_KEY : 0;
        pkt->stream_index = st->index;
        ff.av_packet_rescale_ts(pkt, p->tb, st->time_base);
        const double began = now_s();
        deadline = began + send_s;
        const int w = ff.av_interleaved_write_frame(flv, pkt);
        ff.av_packet_unref(pkt);
        if (w < 0) {
            broken = true;
            throw Error("packing: " + av_text(w));
        }
        forward();
        const double spent = now_s() - began;
        std::lock_guard<std::mutex> hold(lock);
        backlog_bytes -= std::min<uint64_t>(backlog_bytes, p->size);
        send_seconds += spent;
        longest_send = std::max(longest_send, spent);
        bytes += p->size;
        ++packets;
        state = SC_LINK_LIVE;
    }
}

void ESend::run() {
    sc::name_thread(L"SprigCast Enhanced sender");
    sc::sender_thread_priority();
    AVFormatContext *flv = nullptr;
    std::string why;
    bool faulted = false;
    try {
        session(flv);
    } catch (const std::exception &e) {
        why = e.what();
    } catch (...) {
        faulted = true;  // crash gate: nothing of this connection is touched again
        why = "the connection faulted";
    }
    if (!faulted) {
        try {
            closing = true;
            deadline = now_s() + 2;
            if (flv) {
                if (flv->pb) {
                    ff.av_free(flv->pb->buffer);
                    ff.avio_context_free(&flv->pb);
                }
                ff.avformat_free_context(flv);
            }
            if (out) ff.avio_closep(&out);
        } catch (...) {
        }
    }
    std::lock_guard<std::mutex> hold(lock);
    if (state != SC_LINK_FINISHED) {
        state = SC_LINK_FAILED;
        if (error.empty()) error = why.empty() ? "stopped" : why;
    }
    waiting.clear();
    backlog_bytes = 0;
    done = true;
    done_cv.notify_all();
}


// ---- A recording written by the engine (sc_file) ---------------------------------------------------------------
// output/shared.py's FileTap and output/replay.py's PacketFile on a thread of the engine's own: the file starts at
// a keyframe, pauses and resumes as if no time passed, splits into parts and ends just before a keyframe.
struct File {
    std::shared_ptr<Core> core;
    std::string path, format, options;
    std::vector<int> kept;  // sound tracks kept (0 = the first), in order
    double split_seconds = 0, frame_s = 1.0 / 30;
    uint64_t split_bytes = 0;

    struct Chapter {
        int64_t start_ms = 0, end_ms = 0;
        std::string title;
    };
    struct Part {
        std::string path;
        double seconds = 0;
        std::vector<Chapter> chapters;
    };
    std::mutex lock;
    std::condition_variable wake, done_cv;
    std::deque<std::shared_ptr<const Link::Packet>> waiting;
    uint64_t queued_bytes = 0, bytes = 0;
    int state = SC_FILE_WAITING;
    bool paused = false, resuming = false, closing = false, ended = false, done = false;
    std::string error;
    std::vector<Part> parts;
    std::vector<std::pair<double, std::string>> marks;  // chapter marks of the part being written
    double last_rel = 0, write_seconds = 0, longest_write = 0;
    std::atomic<bool> stop{false};
    std::thread thread;

    // File thread only.
    AVFormatContext *streams = nullptr;  // the writer's streams (codec headers), kept for every part
    AVFormatContext *out = nullptr;
    AVPacket *pkt = nullptr;
    double zero = 0, part_zero = 0;
    uint64_t part_bytes = 0;
    std::vector<int64_t> last_dts;

    static constexpr uint64_t QUEUE_BYTES = 512ull << 20;  // waiting for the disk: past this the file is given up

    void offer(const std::shared_ptr<const Link::Packet> &p) {
        std::lock_guard<std::mutex> hold(lock);
        if (state == SC_FILE_FINISHED || state == SC_FILE_FAILED || ended) return;
        if (queued_bytes > QUEUE_BYTES) {
            if (error.empty()) error = "the disk can't keep up";
            wake.notify_one();
            return;
        }
        queued_bytes += p->size;
        waiting.push_back(p);
        wake.notify_one();
    }
    std::string part_path(size_t number) const {  // "<name> part 2<.ext>" (output/shared.py part_path)
        if (number <= 1) return path;
        const size_t slash = path.find_last_of("/\\");
        const size_t dot = path.find_last_of('.');
        const bool ext = dot != std::string::npos && (slash == std::string::npos || dot > slash + 1);
        const std::string stem = ext ? path.substr(0, dot) : path;
        return stem + " part " + std::to_string(number) + (ext ? path.substr(dot) : std::string());
    }
    void take_streams();
    void open_part(double seconds);
    void finish_part();
    void write(const Link::Packet &p);
    bool step(const Link::Packet &p);
    void run();
};

void Core::offer_files(const std::vector<std::shared_ptr<File>> &to, const std::shared_ptr<const Link::Packet> &p) {
    for (auto &f : to) f->offer(p);
}

void Core::end_files(const std::vector<std::shared_ptr<File>> &gone) {
    for (auto &f : gone) {
        std::lock_guard<std::mutex> hold(f->lock);
        f->ended = true;  // what waits is still written, then the file is closed properly
        f->wake.notify_all();
    }
}

// The writer's streams, copied once: a part started after the writer closed (its last packets still waiting)
// still gets its headers.
void File::take_streams() {
    const double until = now_s() + 10;
    for (;;) {
        {
            std::lock_guard<std::mutex> hold(core->links_lock);
            if (core->streams_ready && core->out) {
                must(ff.avformat_alloc_output_context2(&streams, nullptr, "nut", nullptr), "the file's streams");
                for (unsigned i = 0; i < core->out->nb_streams; ++i) {
                    AVStream *st = ff.avformat_new_stream(streams, nullptr);
                    if (!st) throw std::bad_alloc();
                    must(ff.avcodec_parameters_copy(st->codecpar, core->out->streams[i]->codecpar), "the headers");
                    const AVDictionaryEntry *title =
                        ff.av_dict_get(core->out->streams[i]->metadata, "title", nullptr, 0);
                    if (title) ff.av_dict_set(&st->metadata, "title", title->value, 0);
                }
                return;
            }
        }
        {
            std::lock_guard<std::mutex> hold(lock);
            if (ended) throw Error("the encoder stopped");
        }
        if (stop.load()) throw Error("stopped");
        if (now_s() > until) throw Error("the encoder did not start");
        std::this_thread::sleep_for(std::chrono::milliseconds(20));
    }
}

void File::open_part(double seconds) {
    size_t number;
    {
        std::lock_guard<std::mutex> hold(lock);
        number = parts.size() + 1;
    }
    const std::string target = part_path(number);
    must(ff.avformat_alloc_output_context2(&out, nullptr, format.empty() ? nullptr : format.c_str(), target.c_str()),
         "the file");
    std::vector<unsigned> from{0};
    for (int t : kept)
        if (static_cast<unsigned>(t) + 1 < streams->nb_streams) from.push_back(static_cast<unsigned>(t) + 1);
    for (unsigned i : from) {
        AVStream *st = ff.avformat_new_stream(out, nullptr);
        if (!st) throw std::bad_alloc();
        must(ff.avcodec_parameters_copy(st->codecpar, streams->streams[i]->codecpar), "the file's streams");
        st->codecpar->codec_tag = 0;
        const AVDictionaryEntry *title = ff.av_dict_get(streams->streams[i]->metadata, "title", nullptr, 0);
        if (title && from.size() > 2) ff.av_dict_set(&st->metadata, "title", title->value, 0);
    }
    if (!(out->oformat->flags & AVFMT_NOFILE))
        must(ff.avio_open2(&out->pb, target.c_str(), AVIO_FLAG_WRITE, nullptr, nullptr), "opening the file");
    AVDictionary *muxing = nullptr;
    if (!options.empty()) ff.av_dict_parse_string(&muxing, options.c_str(), "=", "\n", 0);
    const int header = ff.avformat_write_header(out, &muxing);
    ff.av_dict_free(&muxing);
    must(header, "the file's header");
    zero = part_zero = seconds;
    part_bytes = 0;
    last_dts.assign(out->nb_streams, INT64_MIN);
    std::lock_guard<std::mutex> hold(lock);
    parts.push_back(Part{target, 0, {}});
    marks.clear();
    last_rel = 0;
    if (state == SC_FILE_WAITING) state = SC_FILE_WRITING;
}

// Chapters from the marks (output/replay.py chapter_list): each runs to the next or the end, and "Start" covers
// the time before the first.
void File::finish_part() {
    if (!out) return;
    std::vector<Chapter> chapters;
    {
        std::lock_guard<std::mutex> hold(lock);
        const double end = last_rel + frame_s;
        std::vector<std::pair<double, std::string>> use;
        for (auto &m : marks)
            if (m.first <= end) use.emplace_back(std::max(0.0, m.first), m.second);
        std::stable_sort(use.begin(), use.end(), [](const auto &a, const auto &b) { return a.first < b.first; });
        if (!use.empty() && use.front().first > 0.5) use.insert(use.begin(), {0.0, "Start"});
        for (size_t i = 0; i < use.size(); ++i) {
            const double until = i + 1 < use.size() ? use[i + 1].first : std::max(end, use[i].first + 0.001);
            Chapter c;
            c.start_ms = std::llround(use[i].first * 1000);
            c.end_ms = std::max<int64_t>(std::llround(until * 1000), c.start_ms + 1);
            c.title = use[i].second;
            chapters.push_back(std::move(c));
        }
        marks.clear();
        if (!parts.empty()) {
            parts.back().seconds = end;
            parts.back().chapters = chapters;
        }
    }
    if (!chapters.empty() && !out->chapters) {  // a container without chapters leaves them out: the file is fine
        auto **list = static_cast<AVChapter **>(ff.av_mallocz(chapters.size() * sizeof(AVChapter *)));
        if (list) {
            unsigned made = 0;
            for (const Chapter &c : chapters) {
                auto *ch = static_cast<AVChapter *>(ff.av_mallocz(sizeof(AVChapter)));
                if (!ch) break;
                ch->id = static_cast<int64_t>(made) + 1;
                ch->time_base = AVRational{1, 1000};
                ch->start = c.start_ms;
                ch->end = c.end_ms;
                ff.av_dict_set(&ch->metadata, "title", c.title.c_str(), 0);
                list[made++] = ch;
            }
            out->chapters = list;
            out->nb_chapters = made;
        }
    }
    AVFormatContext *ctx = out;
    out = nullptr;
    const int r = ff.av_write_trailer(ctx);
    if (!(ctx->oformat->flags & AVFMT_NOFILE) && ctx->pb) ff.avio_closep(&ctx->pb);
    ff.avformat_free_context(ctx);
    if (r < 0) throw WriteError("writing the file's ending: " + av_text(r));
}

void File::write(const Link::Packet &p) {
    int index = 0;
    if (p.track > 0) {
        const auto at = std::find(kept.begin(), kept.end(), p.track - 1);
        if (at == kept.end()) return;  // a track this file leaves out
        index = static_cast<int>(at - kept.begin()) + 1;
    }
    if (index >= static_cast<int>(out->nb_streams)) return;
    const int64_t offset = std::llround(zero * p.tb.den / std::max(1, p.tb.num));
    if (p.pts < offset) return;  // sound from just before the first keyframe
    const int64_t stamp = p.dts - offset;
    if (stamp <= last_dts[static_cast<size_t>(index)]) return;  // sound from just before a resumed keyframe
    last_dts[static_cast<size_t>(index)] = stamp;
    AVStream *st = out->streams[index];
    pkt->data = const_cast<uint8_t *>(p.data.data());  // not reference-counted: the muxer copies it
    pkt->size = static_cast<int>(p.size);
    pkt->pts = p.pts - offset;
    pkt->dts = stamp;
    pkt->flags = p.key ? AV_PKT_FLAG_KEY : 0;
    pkt->stream_index = index;
    ff.av_packet_rescale_ts(pkt, p.tb, st->time_base);
    const double began = now_s();
    const int w = ff.av_interleaved_write_frame(out, pkt);
    const double spent = now_s() - began;
    ff.av_packet_unref(pkt);
    if (w < 0) throw WriteError("writing: " + av_text(w));
    part_bytes += p.size;
    std::lock_guard<std::mutex> hold(lock);
    bytes += p.size;
    write_seconds += spent;
    longest_write = std::max(longest_write, spent);
    if (p.track == 0) last_rel = p.seconds - zero;
}

// One packet, as FileTap._to_file takes it. False: the file has ended (finish asked, and its keyframe came).
bool File::step(const Link::Packet &p) {
    const bool key = p.track == 0 && p.key;
    bool hold_off, carry_on;
    double rel;
    {
        std::lock_guard<std::mutex> hold(lock);
        if (closing && key) return false;
        hold_off = paused;
        carry_on = resuming;
        rel = last_rel;
    }
    if (hold_off) return true;
    if (carry_on && out) {
        if (!key) return true;  // the file carries on at a keyframe
        // This file's clock moves on by the pause, so this picture follows the last one written.
        const double shift = (p.seconds - zero) - (rel + frame_s);
        zero += shift;
        part_zero += shift;
        std::lock_guard<std::mutex> hold(lock);
        resuming = false;
    }
    if (out && key &&
        ((split_seconds > 0 && p.seconds - part_zero >= split_seconds) || (split_bytes && part_bytes >= split_bytes)))
        finish_part();  // the next part starts with this keyframe
    if (!out) {
        if (!key) return true;  // a file starts at a keyframe
        open_part(p.seconds);
        std::lock_guard<std::mutex> hold(lock);
        resuming = false;
    }
    write(p);
    return true;
}

void File::run() {
    sc::name_thread(L"SprigCast recording file");
    std::string why;
    bool faulted = false;
    try {
        take_streams();
        pkt = ff.av_packet_alloc();
        if (!pkt) throw std::bad_alloc();
        for (;;) {
            std::shared_ptr<const Link::Packet> p;
            {
                std::unique_lock<std::mutex> hold(lock);
                wake.wait(hold, [&] {
                    return stop.load() || !waiting.empty() || ended || !error.empty() || (closing && !out);
                });
                if (!error.empty()) throw WriteError(error);
                if (stop.load() || (closing && !out)) break;  // nothing written yet, or closed as it is
                if (waiting.empty()) break;                   // the writer ended and everything is written
                p = std::move(waiting.front());
                waiting.pop_front();
                queued_bytes -= std::min<uint64_t>(queued_bytes, p->size);
            }
            if (!step(*p)) break;
        }
        finish_part();
    } catch (const std::exception &e) {
        why = e.what();
    } catch (...) {
        faulted = true;  // crash gate: this file is given up, never the app; nothing of it is touched again
        why = "writing the file faulted";
    }
    if (!faulted) {
        try {
            if (out) {  // it failed part way: what's on the disk stays as it is
                if (!(out->oformat->flags & AVFMT_NOFILE) && out->pb) ff.avio_closep(&out->pb);
                ff.avformat_free_context(out);
                out = nullptr;
            }
            if (streams) ff.avformat_free_context(streams);
            if (pkt) ff.av_packet_free(&pkt);
        } catch (...) {
        }
    }
    {
        std::lock_guard<std::mutex> hold(core->links_lock);
        auto &all = core->files;
        all.erase(std::remove_if(all.begin(), all.end(), [&](const auto &f) { return f.get() == this; }), all.end());
    }
    if (!why.empty()) sc::log_line(SC_LOG_ERROR, "recording file: " + why);
    std::lock_guard<std::mutex> hold(lock);
    state = why.empty() ? SC_FILE_FINISHED : SC_FILE_FAILED;
    if (!why.empty()) error = why;
    waiting.clear();
    queued_bytes = 0;
    done = true;
    done_cv.notify_all();
}

void Core::close_all(bool trailer) {
    end_links("the encoder stopped");
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
                    if (catching_up && pictures.size() <= queue_limit / 2) catching_up = false;  // room again
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
int hold_newest(Core &c, int64_t last, bool busy) {
    if (c.pictures.empty() || last <= c.last_pts) return 0;
    if (!busy && std::chrono::steady_clock::now() - c.taken_at < STALLED) return 0;
    Core::Picture &newest = c.pictures.back();
    if (!newest.frame || newest.frame->format != AV_PIX_FMT_NV12) return c.pictures.size() <= c.queue_limit ? 2 : 0;
    const int64_t more = last - c.last_pts;
    newest.count = last - newest.pts + 1;
    c.last_pts = last;
    c.frames_repeated += static_cast<uint64_t>(more);
    c.frames_held += static_cast<uint64_t>(more);
    return 1;
}

// While the encoder opens again, and until the pictures that waited meanwhile are written, more pictures from memory
// may wait than queue_limit: up to a second of them, at most REOPEN_BYTES (about 1 s at 1080p60, 0.6 s at 1440p60),
// so the file goes on moving rather than holding a still. Opening NVENC again took up to 0.9 s on the PC (writers
// open one after another: FFmpeg opens that encoder under its global lock). The extra frames are freed as they're
// written. `c.lock` held.
constexpr size_t REOPEN_BYTES = size_t{192} << 20;
constexpr size_t REOPEN_MOST = 120;
size_t reopen_room(const Core &c) {
    const size_t frame = static_cast<size_t>(c.width) * static_cast<size_t>(c.height) * 3 / 2;
    const size_t second = static_cast<size_t>(std::ceil(static_cast<double>(c.fps_num) / std::max(1, c.fps_den)));
    const size_t most = std::min(REOPEN_MOST, std::max<size_t>(second, 1));
    return std::clamp(frame ? REOPEN_BYTES / frame : c.queue_limit, c.queue_limit, std::max(c.queue_limit, most));
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
        const bool busy = c.reopening || c.catching_up;  // the encoder opening again, or writing what waited
        if (c.pictures.size() >= (busy ? reopen_room(c) : c.queue_limit)) {
            const int held = hold_newest(c, last, busy);
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

namespace {
int core_audio(Core &c, int track, const float *stereo, int frames) {
    if (track < 0 || track >= c.tracks) return sc::fail(SC_ERR_ARGUMENT, "sc_writer_audio: no such track");
    std::vector<float> block(stereo, stereo + static_cast<size_t>(frames) * 2);
    {
        std::lock_guard<std::mutex> hold(c.lock);
        if (c.state == SC_WRITER_FAILED || c.state == SC_WRITER_FINISHED || c.finishing) return SC_WRITER_REFUSED;
        c.sounds[static_cast<size_t>(track)].push_back(std::move(block));
    }
    c.wake.notify_one();
    return SC_OK;
}
}  // namespace

sc::WriterSound sc::writer_sound(sc_writer *writer, int *tracks) {
    std::shared_ptr<Core> core = writer->core;
    if (tracks) *tracks = core->tracks;
    return [core](int track, const float *stereo, int frames) { return core_audio(*core, track, stereo, frames); };
}

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
        core->slow_ms = std::clamp(std::atoi(env_text("SPRIGCAST_WRITER_SLOW_MS").c_str()), 0, 1000);
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
        return core_audio(*w->core, track, stereo, frames);
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
        out->encode_seconds = c.encode_seconds;
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

struct sc_link {
    std::shared_ptr<Link> link;
};

extern "C" {

SC_API int sc_link_open(sc_writer *w, const sc_link_config *cfg, sc_link **out) {
    return sc::guarded("sc_link_open", [&]() -> int {
        if (!w || !cfg || !out || !cfg->url || !*cfg->url)
            return sc::fail(SC_ERR_ARGUMENT, "sc_link_open: no writer or address");
        *out = nullptr;
        if (!w->core->packets) return sc::fail(SC_ERR_ARGUMENT, "sc_link_open: the writer doesn't hand out packets");
        auto l = std::make_shared<Link>();
        l->core = w->core;
        l->url = cfg->url;
        l->format = cfg->format && *cfg->format ? cfg->format : "flv";
        l->bytes_per_s = std::max(1, cfg->kbps) * 125.0;
        l->delay_s = std::clamp(cfg->delay_s, 0.0, 3600.0);
        if (cfg->max_backlog_s > 0) l->max_backlog = cfg->max_backlog_s;
        if (cfg->resume_backlog_s > 0) l->resume_backlog = cfg->resume_backlog_s;
        if (cfg->behind_s > 0) l->behind_s = cfg->behind_s;
        if (cfg->connect_timeout_ms > 0) l->connect_s = cfg->connect_timeout_ms / 1000.0;
        if (cfg->send_timeout_ms > 0) l->send_s = cfg->send_timeout_ms / 1000.0;
        l->deadline = now_s() + 3600;
        l->slow_ms = std::clamp(std::atoi(env_text("SPRIGCAST_LINK_SLOW_MS").c_str()), 0, 1000);
        {
            std::lock_guard<std::mutex> hold(w->core->lock);
            if (w->core->done) return sc::fail(SC_ERR_ARGUMENT, "sc_link_open: the writer has ended");
        }
        auto handle = std::make_unique<sc_link>();
        handle->link = l;
        {
            std::lock_guard<std::mutex> hold(w->core->links_lock);
            w->core->links.push_back(l);
        }
        l->thread = std::thread([l] { l->run(); });
        *out = handle.release();
        return SC_OK;
    });
}

SC_API int sc_link_stats_get(sc_link *h, sc_link_stats *out) {
    return sc::guarded("sc_link_stats_get", [&]() -> int {
        if (!h || !out) return sc::fail(SC_ERR_ARGUMENT, "sc_link_stats_get: nothing given");
        Link &l = *h->link;
        std::lock_guard<std::mutex> hold(l.lock);
        std::memset(out, 0, sizeof *out);
        l.ripen(now_s());
        out->state = l.state;
        out->congested = l.congested ? 1 : 0;
        out->frames = l.frames;
        out->dropped = l.dropped;
        out->bytes = l.bytes;
        out->backlog_bytes = l.backlog_bytes;
        out->delayed = static_cast<int>(l.delayed.size());
        out->send_seconds = l.send_seconds;
        out->longest_send = l.longest_send;
        out->behind_seconds = l.behind_seconds;
        out->first_seconds = l.zero;
        std::snprintf(out->error, sizeof out->error, "%s", l.error.c_str());
        return SC_OK;
    });
}

SC_API int sc_link_finish(sc_link *h) {
    return sc::guarded("sc_link_finish", [&]() -> int {
        if (!h) return sc::fail(SC_ERR_ARGUMENT, "sc_link_finish: no link");
        std::lock_guard<std::mutex> hold(h->link->lock);
        h->link->finishing = true;
        h->link->wake.notify_all();
        return SC_OK;
    });
}

SC_API void sc_link_destroy(sc_link *h) {
    if (!h) return;
    try {
        Link &l = *h->link;
        l.stop.store(true);
        {
            std::lock_guard<std::mutex> hold(l.lock);
            l.wake.notify_all();
        }
        if (l.thread.joinable()) {
            std::unique_lock<std::mutex> hold(l.lock);
            const bool ended = l.done_cv.wait_for(hold, std::chrono::seconds(5), [&] { return l.done; });
            hold.unlock();
            if (ended) {
                l.thread.join();
            } else {
                l.thread.detach();  // still stuck in the network: it keeps its Link and ends by itself
            }
        }
    } catch (...) {
    }
    delete h;
}

}  // extern "C"

struct sc_esend {
    std::shared_ptr<ESend> send;
};

extern "C" {

SC_API int sc_esend_open(const sc_esend_config *cfg, sc_esend **out) {
    return sc::guarded("sc_esend_open", [&]() -> int {
        if (!cfg || !out || !cfg->url || !*cfg->url || !cfg->videos || cfg->video_count < 1 || cfg->video_count > 8 ||
            !cfg->audio_header || cfg->audio_header_size < 2 || cfg->audio_header_size > 64)
            return sc::fail(SC_ERR_ARGUMENT, "sc_esend_open: no address, qualities or sound header");
        *out = nullptr;
        auto e = std::make_shared<ESend>();
        e->url = cfg->url;
        if (cfg->connect_timeout_ms > 0) e->connect_s = cfg->connect_timeout_ms / 1000.0;
        if (cfg->send_timeout_ms > 0) e->send_s = cfg->send_timeout_ms / 1000.0;
        e->deadline = now_s() + 3600;
        for (int i = 0; i < cfg->video_count; ++i) {
            sc_writer *w = cfg->videos[i];
            if (!w || !w->core->packets)
                return sc::fail(SC_ERR_ARGUMENT, "sc_esend_open: a quality's writer doesn't hand out packets");
            AVCodecParameters *par = ff.avcodec_parameters_alloc();
            if (!par) throw std::bad_alloc();
            e->pars.push_back(par);
            std::lock_guard<std::mutex> hold(w->core->links_lock);
            if (!w->core->streams_ready || !w->core->out || w->core->out->nb_streams < 1)
                return sc::fail(SC_ERR_ARGUMENT, "sc_esend_open: a quality's encoder isn't open yet");
            must(ff.avcodec_parameters_copy(par, w->core->out->streams[0]->codecpar), "a quality's headers");
        }
        AVCodecParameters *sound = ff.avcodec_parameters_alloc();
        if (!sound) throw std::bad_alloc();
        e->pars.push_back(sound);
        sound->codec_type = AVMEDIA_TYPE_AUDIO;
        sound->codec_id = AV_CODEC_ID_AAC;
        sound->format = AV_SAMPLE_FMT_FLTP;
        sound->sample_rate = cfg->audio_rate > 0 ? cfg->audio_rate : 48000;
        sound->bit_rate = static_cast<int64_t>(std::max(1, cfg->audio_kbps)) * 1000;
        sound->frame_size = 1024;
        ff.av_channel_layout_default(&sound->ch_layout, cfg->audio_channels > 0 ? cfg->audio_channels : 2);
        sound->extradata = static_cast<uint8_t *>(
            ff.av_mallocz(static_cast<size_t>(cfg->audio_header_size) + AV_INPUT_BUFFER_PADDING_SIZE));
        if (!sound->extradata) throw std::bad_alloc();
        std::memcpy(sound->extradata, cfg->audio_header, static_cast<size_t>(cfg->audio_header_size));
        sound->extradata_size = cfg->audio_header_size;
        auto handle = std::make_unique<sc_esend>();
        handle->send = e;
        e->thread = std::thread([e] { e->run(); });
        *out = handle.release();
        return SC_OK;
    });
}

SC_API int sc_esend_packet(sc_esend *h, int stream, const uint8_t *data, int size, int64_t pts, int64_t dts,
                           int tb_num, int tb_den, int key) {
    return sc::guarded("sc_esend_packet", [&]() -> int {
        if (!h || !data || size < 0 || tb_num < 1 || tb_den < 1)
            return sc::fail(SC_ERR_ARGUMENT, "sc_esend_packet: no packet");
        ESend &e = *h->send;
        if (stream < 0 || stream >= static_cast<int>(e.pars.size()))
            return sc::fail(SC_ERR_ARGUMENT, "sc_esend_packet: no such stream");
        auto p = std::make_shared<Encoded>();
        p->track = stream;
        p->key = key != 0;
        p->pts = pts;
        p->dts = dts;
        p->tb = AVRational{tb_num, tb_den};
        p->size = static_cast<size_t>(size);
        p->data.resize(p->size + AV_INPUT_BUFFER_PADDING_SIZE);
        if (size) std::memcpy(p->data.data(), data, p->size);
        std::lock_guard<std::mutex> hold(e.lock);
        if (e.state == SC_LINK_FAILED || e.state == SC_LINK_FINISHED || e.finishing) return SC_WRITER_REFUSED;
        e.backlog_bytes += p->size;
        e.waiting.push_back(std::move(p));
        e.wake.notify_one();
        return SC_OK;
    });
}

SC_API int sc_esend_stats_get(sc_esend *h, sc_esend_stats *out) {
    return sc::guarded("sc_esend_stats_get", [&]() -> int {
        if (!h || !out) return sc::fail(SC_ERR_ARGUMENT, "sc_esend_stats_get: nothing given");
        ESend &e = *h->send;
        std::lock_guard<std::mutex> hold(e.lock);
        std::memset(out, 0, sizeof *out);
        out->state = e.state;
        out->packets = e.packets;
        out->bytes = e.bytes;
        out->backlog_bytes = e.backlog_bytes;
        out->send_seconds = e.send_seconds;
        out->longest_send = e.longest_send;
        std::snprintf(out->error, sizeof out->error, "%s", e.error.c_str());
        return SC_OK;
    });
}

SC_API int sc_esend_finish(sc_esend *h) {
    return sc::guarded("sc_esend_finish", [&]() -> int {
        if (!h) return sc::fail(SC_ERR_ARGUMENT, "sc_esend_finish: no sender");
        ESend &e = *h->send;
        std::lock_guard<std::mutex> hold(e.lock);
        e.finishing = true;
        e.wake.notify_all();
        return SC_OK;
    });
}

SC_API void sc_esend_destroy(sc_esend *h) {
    if (!h) return;
    try {
        ESend &e = *h->send;
        e.stop.store(true);
        {
            std::lock_guard<std::mutex> hold(e.lock);
            e.wake.notify_all();
        }
        if (e.thread.joinable()) {
            std::unique_lock<std::mutex> hold(e.lock);
            const bool ended = e.done_cv.wait_for(hold, std::chrono::seconds(5), [&] { return e.done; });
            hold.unlock();
            if (ended) {
                e.thread.join();
            } else {
                e.thread.detach();  // still stuck in the network: it keeps its ESend and ends by itself
            }
        }
    } catch (...) {
    }
    delete h;
}

}  // extern "C"

struct sc_file {
    std::shared_ptr<File> file;
};

extern "C" {

SC_API int sc_file_open(sc_writer *w, const sc_file_config *cfg, sc_file **out) {
    return sc::guarded("sc_file_open", [&]() -> int {
        if (!w || !cfg || !out || !cfg->path || !*cfg->path)
            return sc::fail(SC_ERR_ARGUMENT, "sc_file_open: no writer or path");
        *out = nullptr;
        Core &c = *w->core;
        if (!c.packets) return sc::fail(SC_ERR_ARGUMENT, "sc_file_open: the writer doesn't hand out packets");
        auto f = std::make_shared<File>();
        f->core = w->core;
        f->path = cfg->path;
        f->format = cfg->format ? cfg->format : "";
        f->options = cfg->format_options ? cfg->format_options : "";
        for (int t = 0; t < MAX_TRACKS; ++t)
            if (cfg->tracks & (1u << t)) f->kept.push_back(t);
        if (f->kept.empty()) f->kept.push_back(0);
        f->split_seconds = std::max(0.0, cfg->split_seconds);
        f->split_bytes = cfg->split_bytes;
        f->frame_s = static_cast<double>(c.fps_den) / std::max(1, c.fps_num);
        {
            std::lock_guard<std::mutex> hold(c.lock);
            if (c.done) return sc::fail(SC_ERR_ARGUMENT, "sc_file_open: the writer has ended");
            c.key_next = true;  // the file starts at a keyframe: the next picture is one
        }
        auto handle = std::make_unique<sc_file>();
        handle->file = f;
        {
            std::lock_guard<std::mutex> hold(c.links_lock);
            c.files.push_back(f);
        }
        f->thread = std::thread([f] { f->run(); });
        *out = handle.release();
        return SC_OK;
    });
}

SC_API int sc_file_pause(sc_file *h) {
    return sc::guarded("sc_file_pause", [&]() -> int {
        if (!h) return sc::fail(SC_ERR_ARGUMENT, "sc_file_pause: no file");
        File &f = *h->file;
        std::lock_guard<std::mutex> hold(f.lock);
        if (f.paused || f.done || f.closing) return 0;
        f.paused = true;
        f.resuming = false;
        if (f.state == SC_FILE_WRITING) f.state = SC_FILE_PAUSED;
        return 1;
    });
}

SC_API int sc_file_resume(sc_file *h) {
    return sc::guarded("sc_file_resume", [&]() -> int {
        if (!h) return sc::fail(SC_ERR_ARGUMENT, "sc_file_resume: no file");
        File &f = *h->file;
        {
            std::lock_guard<std::mutex> hold(f.lock);
            if (!f.paused || f.done) return 0;
            f.paused = false;
            f.resuming = true;
            if (f.state == SC_FILE_PAUSED) f.state = SC_FILE_WRITING;
        }
        std::lock_guard<std::mutex> hold(f.core->lock);
        f.core->key_next = true;
        return 1;
    });
}

SC_API int sc_file_chapter(sc_file *h, const char *name, double *seconds) {
    return sc::guarded("sc_file_chapter", [&]() -> int {
        if (!h || !name) return sc::fail(SC_ERR_ARGUMENT, "sc_file_chapter: no file or name");
        File &f = *h->file;
        std::lock_guard<std::mutex> hold(f.lock);
        if (f.done || f.parts.empty() || f.state == SC_FILE_WAITING) return 0;
        f.marks.emplace_back(f.last_rel, name);
        if (seconds) *seconds = f.last_rel;
        return 1;
    });
}

SC_API int sc_file_finish(sc_file *h) {
    return sc::guarded("sc_file_finish", [&]() -> int {
        if (!h) return sc::fail(SC_ERR_ARGUMENT, "sc_file_finish: no file");
        File &f = *h->file;
        {
            std::lock_guard<std::mutex> hold(f.lock);
            f.closing = true;
            f.wake.notify_all();
        }
        std::lock_guard<std::mutex> hold(f.core->lock);
        f.core->key_next = true;  // it ends just before this keyframe: everything shown before it has been encoded
        return SC_OK;
    });
}

SC_API int sc_file_stats_get(sc_file *h, sc_file_stats *out) {
    return sc::guarded("sc_file_stats_get", [&]() -> int {
        if (!h || !out) return sc::fail(SC_ERR_ARGUMENT, "sc_file_stats_get: nothing given");
        File &f = *h->file;
        std::lock_guard<std::mutex> hold(f.lock);
        std::memset(out, 0, sizeof *out);
        out->state = f.state;
        out->parts = static_cast<int>(f.parts.size());
        out->bytes = f.bytes;
        out->queued_bytes = f.queued_bytes;
        out->seconds = f.last_rel;
        out->write_seconds = f.write_seconds;
        out->longest_write = f.longest_write;
        std::snprintf(out->error, sizeof out->error, "%s", f.error.c_str());
        return SC_OK;
    });
}

SC_API int sc_file_part(sc_file *h, int index, char *path, int capacity, double *seconds, int *chapters) {
    return sc::guarded("sc_file_part", [&]() -> int {
        if (!h) return sc::fail(SC_ERR_ARGUMENT, "sc_file_part: no file");
        File &f = *h->file;
        std::lock_guard<std::mutex> hold(f.lock);
        if (index < 0 || index >= static_cast<int>(f.parts.size())) return SC_ERR_ARGUMENT;
        const File::Part &part = f.parts[static_cast<size_t>(index)];
        if (path && capacity > 0) std::snprintf(path, static_cast<size_t>(capacity), "%s", part.path.c_str());
        if (seconds) *seconds = part.seconds;
        if (chapters) *chapters = static_cast<int>(part.chapters.size());
        return SC_OK;
    });
}

SC_API int sc_file_part_chapter(sc_file *h, int index, int chapter, int64_t *start_ms, int64_t *end_ms, char *title,
                                int capacity) {
    return sc::guarded("sc_file_part_chapter", [&]() -> int {
        if (!h) return sc::fail(SC_ERR_ARGUMENT, "sc_file_part_chapter: no file");
        File &f = *h->file;
        std::lock_guard<std::mutex> hold(f.lock);
        if (index < 0 || index >= static_cast<int>(f.parts.size())) return SC_ERR_ARGUMENT;
        const File::Part &part = f.parts[static_cast<size_t>(index)];
        if (chapter < 0 || chapter >= static_cast<int>(part.chapters.size())) return SC_ERR_ARGUMENT;
        const File::Chapter &c = part.chapters[static_cast<size_t>(chapter)];
        if (start_ms) *start_ms = c.start_ms;
        if (end_ms) *end_ms = c.end_ms;
        if (title && capacity > 0) std::snprintf(title, static_cast<size_t>(capacity), "%s", c.title.c_str());
        return SC_OK;
    });
}

SC_API int sc_file_stop(sc_file *h) {
    return sc::guarded("sc_file_stop", [&]() -> int {
        if (!h) return sc::fail(SC_ERR_ARGUMENT, "sc_file_stop: no file");
        File &f = *h->file;
        f.stop.store(true);
        std::unique_lock<std::mutex> hold(f.lock);
        f.wake.notify_all();
        return f.done_cv.wait_for(hold, std::chrono::seconds(10), [&] { return f.done; }) ? 1 : 0;
    });
}

SC_API void sc_file_destroy(sc_file *h) {
    if (!h) return;
    try {
        File &f = *h->file;
        const bool ended = sc_file_stop(h) == 1;
        if (f.thread.joinable()) {
            if (ended) {
                f.thread.join();
            } else {
                f.thread.detach();  // still stuck on the disk: it keeps its File and ends by itself
            }
        }
    } catch (...) {
    }
    delete h;
}

}  // extern "C"

struct sc_clip {
    std::vector<Kept> packets;
    double zero = 0;
    AVFormatContext *streams = nullptr;  // the writer's streams (codec headers) as the clip was taken
};

extern "C" {

SC_API int sc_writer_keep(sc_writer *w, double seconds, const char *spool_path) {
    return sc::guarded("sc_writer_keep", [&]() -> int {
        if (!w) return sc::fail(SC_ERR_ARGUMENT, "sc_writer_keep: no writer");
        Core &c = *w->core;
        if (!c.packets) return sc::fail(SC_ERR_ARGUMENT, "sc_writer_keep: the writer doesn't hand out packets");
        std::lock_guard<std::mutex> hold(c.keep_lock);
        c.keep_seconds = std::clamp(seconds, 0.0, 3600.0);
        if (spool_path && *spool_path && c.ring_path.empty()) c.ring_path = spool_path;
        return SC_OK;
    });
}

SC_API int sc_writer_kept(sc_writer *w, double *seconds, int *packets) {
    return sc::guarded("sc_writer_kept", [&]() -> int {
        if (!w) return sc::fail(SC_ERR_ARGUMENT, "sc_writer_kept: no writer");
        Core &c = *w->core;
        std::lock_guard<std::mutex> hold(c.keep_lock);
        double first = 0;
        bool found = false;
        for (const Kept &k : c.kept) {
            if (k.track == 0) {
                first = k.seconds;
                found = true;
                break;
            }
        }
        if (seconds) *seconds = found ? std::max(0.0, c.newest - first) : 0.0;
        if (packets) *packets = static_cast<int>(c.kept.size());
        return SC_OK;
    });
}

SC_API int sc_writer_hand_out(sc_writer *w, int on) {
    return sc::guarded("sc_writer_hand_out", [&]() -> int {
        if (!w) return sc::fail(SC_ERR_ARGUMENT, "sc_writer_hand_out: no writer");
        Core &c = *w->core;
        std::lock_guard<std::mutex> hold(c.lock);
        c.hand_out = on != 0;
        if (!on) c.outs.clear();
        return SC_OK;
    });
}

SC_API int sc_clip_snapshot(sc_writer *w, sc_clip **out) {
    return sc::guarded("sc_clip_snapshot", [&]() -> int {
        if (!w || !out) return sc::fail(SC_ERR_ARGUMENT, "sc_clip_snapshot: nothing given");
        *out = nullptr;
        Core &c = *w->core;
        auto clip = std::make_unique<sc_clip>();
        {
            std::lock_guard<std::mutex> hold(c.keep_lock);
            auto start = std::find_if(c.kept.begin(), c.kept.end(),
                                      [](const Kept &k) { return k.track == 0 && k.key; });
            if (start == c.kept.end()) return sc::fail(SC_ERR_ARGUMENT, "nothing buffered yet");
            clip->zero = start->seconds;
            clip->packets.assign(start, c.kept.end());
        }
        std::lock_guard<std::mutex> hold(c.links_lock);
        if (!c.streams_ready || !c.out) return sc::fail(SC_ERR_ARGUMENT, "nothing buffered yet");
        must(ff.avformat_alloc_output_context2(&clip->streams, nullptr, "nut", nullptr), "the clip's streams");
        for (unsigned i = 0; i < c.out->nb_streams; ++i) {
            AVStream *st = ff.avformat_new_stream(clip->streams, nullptr);
            if (!st || ff.avcodec_parameters_copy(st->codecpar, c.out->streams[i]->codecpar) < 0) {
                ff.avformat_free_context(clip->streams);
                return sc::fail(SC_ERR_DEVICE, "sc_clip_snapshot: the streams' headers couldn't be copied");
            }
        }
        *out = clip.release();
        return SC_OK;
    });
}

SC_API int sc_clip_write(sc_clip *clip, const char *path, const char *format, const char *format_options,
                         unsigned tracks) {
    return sc::guarded("sc_clip_write", [&]() -> int {
        if (!clip || !path || !*path) return sc::fail(SC_ERR_ARGUMENT, "sc_clip_write: no clip or path");
        File f;  // the recording file's own writing, on this thread
        f.path = path;
        f.format = format ? format : "";
        f.options = format_options ? format_options : "";
        for (int t = 0; t < MAX_TRACKS; ++t)
            if (tracks & (1u << t)) f.kept.push_back(t);
        if (f.kept.empty()) f.kept.push_back(0);
        f.streams = clip->streams;
        std::string why;
        try {
            f.pkt = ff.av_packet_alloc();
            if (!f.pkt) throw std::bad_alloc();
            f.open_part(clip->zero);
            Encoded e;
            for (const Kept &k : clip->packets) {
                e.track = k.track;
                e.key = k.key;
                e.pts = k.pts;
                e.dts = k.dts;
                e.tb = k.tb;
                e.size = k.size;
                e.seconds = k.seconds;
                if (k.mem) {
                    e.data = *k.mem;
                } else {
                    e.data.assign(k.size + AV_INPUT_BUFFER_PADDING_SIZE, 0);
                    if (!k.ring || !k.ring->read(k.at, e.data.data(), k.size))
                        throw Error("the clip's start has been recorded over");
                }
                f.write(e);
            }
            f.finish_part();
        } catch (const std::exception &e) {
            why = e.what();
        }
        if (f.out) {
            if (!(f.out->oformat->flags & AVFMT_NOFILE) && f.out->pb) ff.avio_closep(&f.out->pb);
            ff.avformat_free_context(f.out);
            f.out = nullptr;
        }
        if (f.pkt) ff.av_packet_free(&f.pkt);
        f.streams = nullptr;  // the clip's
        return why.empty() ? SC_OK : sc::fail(SC_ERR_DEVICE, why);
    });
}

SC_API void sc_clip_destroy(sc_clip *clip) {
    if (!clip) return;
    try {
        if (clip->streams) ff.avformat_free_context(clip->streams);
    } catch (...) {
    }
    delete clip;
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
