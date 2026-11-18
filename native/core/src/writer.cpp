// Recording and streaming output (phase 4, docs/native-engine.md): a file or stream written by a thread of the
// engine's own. Pictures (NV12) and sound (stereo floats) are handed over and queued; the writer thread encodes
// them with FFmpeg (SprigCast's own copy, ffmpeg.h), muxes and writes, so nothing of it needs Python's lock.
//
// Like media.cpp this needs no graphics card and builds on Linux, so its checks run there too
// (tests/test_native_writer.py).
#include "base.h"
#include "ffmpeg.h"

#include <algorithm>
#include <chrono>
#include <condition_variable>
#include <cstring>
#include <deque>
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

struct Error : std::runtime_error {
    using std::runtime_error::runtime_error;
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

    std::mutex lock;
    std::condition_variable wake, done_cv;
    std::deque<std::pair<int64_t, FramePtr>> pictures;          // (pts in frames, NV12 picture)
    std::vector<std::deque<std::vector<float>>> sounds;          // per track, blocks not yet taken
    std::vector<FramePtr> spare;                                 // NV12 frames to fill again
    bool finishing = false, done = false;
    std::atomic<bool> abort{false};
    int state = SC_WRITER_OPENING;
    std::string error;
    uint64_t frames_in = 0, frames_written = 0, frames_dropped = 0, bytes = 0;
    double write_seconds = 0, longest_write = 0;
    bool yuv420 = false;  // the encoder took no NV12: pictures are repacked as I420 (writer thread only)
    FramePtr i420;

    AVFormatContext *out = nullptr;
    AVCodecContext *venc = nullptr;
    AVStream *vstream = nullptr;
    std::vector<Track> audio;
    AVPacket *pkt = nullptr;

    void run();
    void open();
    void write_packets(AVCodecContext *enc, AVStream *stream);
    void encode_video(AVFrame *frame);
    void encode_sound(Track &t, bool flush);
    void close_all(bool trailer);
    void set_failed(const std::string &why) {
        std::lock_guard<std::mutex> hold(lock);
        if (error.empty()) error = why;
        state = SC_WRITER_FAILED;
    }
};

int interrupted(void *opaque) { return static_cast<Core *>(opaque)->abort.load() ? 1 : 0; }

void Core::open() {
    const char *fmt = format.empty() ? nullptr : format.c_str();
    must(ff.avformat_alloc_output_context2(&out, nullptr, fmt, target.c_str()), "the output");
    out->interrupt_callback.callback = interrupted;
    out->interrupt_callback.opaque = this;
    const bool global = (out->oformat->flags & AVFMT_GLOBALHEADER) != 0;

    const AVCodec *codec = ff.avcodec_find_encoder_by_name(video_codec.c_str());
    if (!codec) throw Error("no video encoder named " + video_codec);
    for (int attempt = 0; attempt < 2; ++attempt) {  // NV12 first, then I420 for an encoder without it
        venc = ff.avcodec_alloc_context3(codec);
        if (!venc) throw std::bad_alloc();
        venc->width = width;
        venc->height = height;
        venc->pix_fmt = attempt == 0 ? AV_PIX_FMT_NV12 : AV_PIX_FMT_YUV420P;
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
        if (attempt == 1) must(r, ("the video encoder " + video_codec).c_str());
    }
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
    pkt = ff.av_packet_alloc();
    if (!pkt) throw std::bad_alloc();
}

void Core::write_packets(AVCodecContext *enc, AVStream *stream) {
    for (;;) {
        const int r = ff.avcodec_receive_packet(enc, pkt);
        if (r == AVERROR(EAGAIN) || r == AVERROR_EOF) return;
        must(r, "encoding");
        ff.av_packet_rescale_ts(pkt, enc->time_base, stream->time_base);
        pkt->stream_index = stream->index;
        const int size = pkt->size;
        const double began = now_s();
        const int w = ff.av_interleaved_write_frame(out, pkt);  // takes the packet's data, leaves it blank
        const double spent = now_s() - began;
        must(w, "writing");
        std::lock_guard<std::mutex> hold(lock);
        bytes += static_cast<uint64_t>(size);
        write_seconds += spent;
        longest_write = std::max(longest_write, spent);
    }
}

void Core::encode_video(AVFrame *frame) {
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
    must(ff.avcodec_send_frame(venc, frame), "encoding a picture");
    write_packets(venc, vstream);
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
    if (out && trailer) {
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
            std::pair<int64_t, FramePtr> picture{0, nullptr};
            std::vector<std::vector<float>> sound(static_cast<size_t>(tracks));
            bool last;
            {
                std::unique_lock<std::mutex> hold(lock);
                wake.wait(hold, [&] {
                    if (finishing || abort.load() || !pictures.empty()) return true;
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
                }
                last = finishing && pictures.empty() && !picture.second;
            }
            for (size_t i = 0; i < audio.size(); ++i) {
                audio[i].waiting.insert(audio[i].waiting.end(), sound[i].begin(), sound[i].end());
                encode_sound(audio[i], false);
            }
            if (picture.second) {
                picture.second->pts = picture.first;
                encode_video(picture.second.get());
                std::lock_guard<std::mutex> hold(lock);
                ++frames_written;
                if (spare.size() < 4) spare.push_back(std::move(picture.second));
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

}  // namespace

struct sc_writer {
    std::shared_ptr<Core> core;
    std::thread thread;
};

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
        Core &c = *w->core;
        FramePtr f;
        {
            std::lock_guard<std::mutex> hold(c.lock);
            ++c.frames_in;
            if (c.state == SC_WRITER_FAILED || c.state == SC_WRITER_FINISHED || c.finishing) return SC_WRITER_REFUSED;
            if (c.pictures.size() >= c.queue_limit) {
                ++c.frames_dropped;
                return SC_WRITER_DROPPED;
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
            std::memcpy(f->data[0] + static_cast<ptrdiff_t>(y) * f->linesize[0],
                        nv12 + static_cast<ptrdiff_t>(y) * stride, static_cast<size_t>(width));
        const uint8_t *uv = nv12 + static_cast<ptrdiff_t>(height) * stride;
        for (int y = 0; y < height / 2; ++y)
            std::memcpy(f->data[1] + static_cast<ptrdiff_t>(y) * f->linesize[1], uv + static_cast<ptrdiff_t>(y) * stride,
                        static_cast<size_t>(width));
        f->color_range = AVCOL_RANGE_MPEG;
        f->colorspace = AVCOL_SPC_BT709;
        {
            std::lock_guard<std::mutex> hold(c.lock);
            c.pictures.emplace_back(pts, std::move(f));
        }
        c.wake.notify_one();
        return SC_OK;
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
        std::snprintf(out->error, sizeof out->error, "%s", c.error.c_str());
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
