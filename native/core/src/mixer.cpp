// The sound mixer (sc_mixer_*, docs/native-engine.md "Sound"): one 48 kHz stereo mix of every sound, a block of
// SC_MIXER_BLOCK samples (about 21 ms) at a time, on a thread of its own paced by the clock. It never waits on
// Python: sounds Python still makes are pushed into a short queue each, sounds the engine plays (sc_media) are read
// straight from their player, and outputs take finished blocks from queues of their own.
//
// The same mix as Python's audio/mixer.py: per sound mono, balance, delay (sync offset), the fade of a sound per
// bloom, volume; ducking of everything else while a voice talks; named tracks (stream, recording, VOD-safe, voice,
// clips, track 4, track 5, and Source record's clean track); and the monitor mix for your speakers.
#include "base.h"
#include "ffmpeg.h"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstdint>
#include <cstring>
#include <deque>
#include <map>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

namespace {

constexpr int BLOCK = SC_MIXER_BLOCK;
constexpr int RATE = 48000;
constexpr float FLOOR_DB = -60.0f;
constexpr double BLOOM_FADE_S = 0.25;
constexpr int MAX_DELAY_MS = 2000;
// libavfilter's major version the declarations in ffmpeg.h follow (FFmpeg 7 and 8: 10 and 11; the calls used have
// stayed the same): the one PyAV's libavcodec goes with.
constexpr unsigned FILTERS_MAJOR = LIBAVCODEC_VERSION_MAJOR - 51;
constexpr size_t MAX_QUEUED = RATE / 5;                 // a pushed sound keeps at most 200 ms queued
constexpr size_t LEAD = BLOCK;                            // ... and plays once it has a block ahead
// A sound that delivers in uneven bursts (a capture card's sound while it also sends 1440p60 video, a busy Python)
// ran dry about once a second (PC 2026-11-21: the Elgato HD60 X, 30 gaps every 30 s). Each time it runs dry it keeps
// half a block more ahead, up to four blocks (85 ms); after a calm minute it gives a quarter block back. One that
// stays well ahead of that for two seconds (a capture whose clock runs fast) drops the extra, so the delay can't build.
constexpr size_t CUSHION_STEP = BLOCK / 2;
constexpr size_t MAX_CUSHION = BLOCK * 4;
constexpr int CUSHION_WINDOW = 94;  // blocks: about 2 s
constexpr int CALM_WINDOWS = 30;    // windows without running dry: about a minute
constexpr size_t TAP_QUEUE = 64;                          // blocks an output keeps until taken (about 1.4 s)
constexpr int MONITOR = SC_MIXER_MONITOR;                 // a tap entry: the monitor mix
constexpr int SOURCE_TAP = SC_MIXER_SOURCE_TAP;           // a tap entry: one sound after its volume
constexpr int RAW_TAP = SC_MIXER_SOURCE_RAW_TAP;          // a tap entry: one sound before its filters

using Block = std::vector<float>;  // BLOCK * 2 interleaved floats

float to_db(float value) {
    if (value <= std::pow(10.0f, FLOOR_DB / 20.0f)) return FLOOR_DB;
    return std::max(FLOOR_DB, 20.0f * std::log10(value));
}

void measure(const float *b, float gain, float &peak_db, float &rms_db) {
    float peak = 0.0f;
    double sum = 0.0;
    for (int i = 0; i < BLOCK * 2; ++i) {
        peak = std::max(peak, std::fabs(b[i]));
        sum += static_cast<double>(b[i]) * b[i];
    }
    peak_db = to_db(peak * gain);
    rms_db = to_db(static_cast<float>(std::sqrt(sum / (BLOCK * 2))) * gain);
}

// A sound's built-in filters (core/filters.py, the same numbers): gain, noise gate, compressor, limiter, invert
// polarity and ducking under another sound. Each keeps its state between blocks; new settings of the same filters
// keep it (as AudioChain.update does).
// The numbers are worked in double, as Python does, and each gain is rounded to float only when applied.
double db_gain(double db) { return std::pow(10.0, db / 20.0); }

double smoothing(double ms) { return std::exp(-1.0 / std::max(1.0, ms / 1000.0 * RATE)); }  // filters.py _coeff

double chunk_peak(const float *b, int from, int count) {
    float peak = 0.0f;
    for (int i = from * 2; i < (from + count) * 2; ++i) peak = std::max(peak, std::fabs(b[i]));
    return peak;
}

void scale(float *b, int from, int count, double gain) {
    const float g = static_cast<float>(gain);
    for (int i = from * 2; i < (from + count) * 2; ++i) b[i] *= g;
}

void ramp(float *b, int from, int count, double start, double end) {  // numpy linspace(start, end, count)
    for (int i = 0; i < count; ++i) {
        const double step = count > 1 ? (end - start) / (count - 1) : 0.0;
        const float g = static_cast<float>(i == count - 1 && count > 1 ? end : start + step * i);
        b[2 * (from + i)] *= g;
        b[2 * (from + i) + 1] *= g;
    }
}

// One of FFmpeg's audio filter chains (core/filters.py FFmpegAudioFilter: equalizer, RNNoise, low cut, ...), run
// here on the mixer's thread instead of in Python a block at a time: a mic with one of these was pulled through
// Python every 21 ms, and on a busy PC its sound ran dry (PC 2026-11-21). The same chain as Python's: abuffer, the
// filters, aformat back to 48 kHz stereo float, abuffersink; a filter that holds sound back is padded with silence
// at the start, so each block comes out the size it went in.
struct Graph {
    AVFilterGraph *graph = nullptr;
    AVFilterContext *src = nullptr, *sink = nullptr;
    AVFrame *in = nullptr, *out = nullptr;
    int64_t pts = 0;
    std::deque<float> pending;  // filtered, interleaved, not handed out yet

    Graph() = default;
    Graph(const Graph &) = delete;
    Graph &operator=(const Graph &) = delete;
    ~Graph() {
        if (graph) sc::ffm::fg.avfilter_graph_free(&graph);
        if (in) sc::ffm::ff.av_frame_free(&in);
        if (out) sc::ffm::ff.av_frame_free(&out);
    }

    // spec: a filter a line, "name\targs". Empty `why` when it was made.
    void build(const std::string &spec, std::string &why) {
        using sc::ffm::fg;
        using sc::ffm::ff;
        if (!sc::ffm::fg_ready.load() || !sc::ffm::ff_ready.load()) {
            why = "FFmpeg's filters aren't loaded";
            return;
        }
        graph = fg.avfilter_graph_alloc();
        in = ff.av_frame_alloc();
        out = ff.av_frame_alloc();
        if (!graph || !in || !out) {
            why = "out of memory";
            return;
        }
        // One thread: left to choose, FFmpeg starts a pool per graph (as filters.py found: 15 on the user's PC).
        fg.av_opt_set_int(graph, "threads", 1, 0);
        auto add = [&](const char *name, const std::string &args, AVFilterContext **made) -> bool {
            const AVFilter *filter = fg.avfilter_get_by_name(name);
            if (!filter) {
                why = std::string("FFmpeg has no ") + name + " filter";
                return false;
            }
            const int e = fg.avfilter_graph_create_filter(made, filter, nullptr, args.empty() ? nullptr : args.c_str(),
                                                          nullptr, graph);
            if (e < 0) {
                why = std::string(name) + ": " + sc::ffm::av_text(e);
                return false;
            }
            return true;
        };
        if (!add("abuffer", "time_base=1/48000:sample_rate=48000:sample_fmt=fltp:channel_layout=stereo", &src)) return;
        AVFilterContext *last = src;
        size_t at = 0;
        while (at < spec.size()) {
            size_t end = spec.find('\n', at);
            if (end == std::string::npos) end = spec.size();
            const std::string line = spec.substr(at, end - at);
            at = end + 1;
            if (line.empty()) continue;
            const size_t tab = line.find('\t');
            const std::string name = line.substr(0, tab);
            AVFilterContext *made = nullptr;
            if (!add(name.c_str(), tab == std::string::npos ? std::string() : line.substr(tab + 1), &made)) return;
            if (fg.avfilter_link(last, 0, made, 0) < 0) {
                why = "couldn't link " + name;
                return;
            }
            last = made;
        }
        AVFilterContext *shape = nullptr;
        if (!add("aformat", "sample_fmts=fltp:channel_layouts=stereo:sample_rates=48000", &shape)) return;
        if (!add("abuffersink", "", &sink)) return;
        if (fg.avfilter_link(last, 0, shape, 0) < 0 || fg.avfilter_link(shape, 0, sink, 0) < 0) {
            why = "couldn't link the chain";
            return;
        }
        const int e = fg.avfilter_graph_config(graph, nullptr);
        if (e < 0) why = "the chain couldn't be set up: " + sc::ffm::av_text(e);
    }

    // One block through the chain, in place. False if FFmpeg failed (the block is left as it came).
    bool process(float *b) {
        using sc::ffm::fg;
        using sc::ffm::ff;
        in->format = AV_SAMPLE_FMT_FLTP;
        in->sample_rate = RATE;
        in->nb_samples = BLOCK;
        ff.av_channel_layout_default(&in->ch_layout, 2);
        if (ff.av_frame_get_buffer(in, 0) < 0) return false;
        auto *l = reinterpret_cast<float *>(in->data[0]);
        auto *r = reinterpret_cast<float *>(in->data[1]);
        for (int i = 0; i < BLOCK; ++i) {
            l[i] = b[2 * i];
            r[i] = b[2 * i + 1];
        }
        in->pts = pts;
        pts += BLOCK;
        if (fg.av_buffersrc_add_frame_flags(src, in, 0) < 0) {  // takes the frame's buffers either way
            ff.av_frame_unref(in);
            return false;
        }
        while (fg.av_buffersink_get_frame(sink, out) >= 0) {
            const auto *ol = reinterpret_cast<const float *>(out->data[0]);
            const auto *orr = reinterpret_cast<const float *>(out->ch_layout.nb_channels > 1 ? out->data[1]
                                                                                              : out->data[0]);
            for (int i = 0; i < out->nb_samples; ++i) {
                pending.push_back(ol[i]);
                pending.push_back(orr[i]);
            }
            ff.av_frame_unref(out);
        }
        const size_t want = static_cast<size_t>(BLOCK) * 2;
        if (pending.size() < want) pending.insert(pending.begin(), want - pending.size(), 0.0f);  // held back
        std::copy(pending.begin(), pending.begin() + static_cast<std::ptrdiff_t>(want), b);
        pending.erase(pending.begin(), pending.begin() + static_cast<std::ptrdiff_t>(want));
        return true;
    }
};

struct Filter {
    sc_mixer_filter settings{};
    std::string text;              // SC_FILTER_GRAPH: its chain (Graph::build)
    std::shared_ptr<Graph> graph;  // SC_FILTER_GRAPH: the chain, made by sc_mixer_source_filters_text
    bool open = false;
    double held = 0.0, level = 0.0, env = 0.0, gain = 1.0;

    // other_level: the followed sound's last peak (ducking)
    void process(float *b, double other_level) {
        double p[6];
        for (int i = 0; i < 6; ++i) p[i] = settings.p[i];
        constexpr int STEP = 32;
        switch (settings.kind) {
        case SC_FILTER_GAIN:
            scale(b, 0, BLOCK, db_gain(p[0]));
            break;
        case SC_FILTER_NOISE_GATE: {  // p: close_db, open_db, attack_ms, hold_ms, release_ms
            const double open_at = db_gain(p[1]), close_at = db_gain(p[0]);
            const double step_s = static_cast<double>(STEP) / RATE;
            const double attack = step_s / std::max(0.001, p[2] / 1000.0);
            const double release = step_s / std::max(0.001, p[4] / 1000.0);
            const double hold = p[3] / 1000.0;
            for (int i = 0; i < BLOCK; i += STEP) {
                const int count = std::min(STEP, BLOCK - i);
                const double peak = chunk_peak(b, i, count);
                if (peak >= open_at) {
                    open = true;
                    held = hold;
                } else if (open && peak < close_at) {
                    held -= step_s;
                    if (held <= 0.0) open = false;
                }
                const double start = level;
                level = open ? std::min(1.0, level + attack) : std::max(0.0, level - release);
                ramp(b, i, count, start, level);
            }
            break;
        }
        case SC_FILTER_COMPRESSOR:  // p: ratio, threshold_db, attack_ms, release_ms, output_db
        case SC_FILTER_LIMITER: {   // p: threshold_db, release_ms
            const bool limiter = settings.kind == SC_FILTER_LIMITER;
            const double ratio = limiter ? 1000.0 : std::max(1.0, p[0]);
            const double threshold = limiter ? p[0] : p[1];
            const double attack = smoothing((limiter ? 0.0 : p[2]) / STEP);
            const double release = smoothing((limiter ? p[1] : p[3]) / STEP);
            const double makeup = db_gain(limiter ? 0.0 : p[4]);
            for (int i = 0; i < BLOCK; i += STEP) {
                const int count = std::min(STEP, BLOCK - i);
                const double x = chunk_peak(b, i, count);
                const double c = x > env ? attack : release;
                env = c * env + (1.0 - c) * x;
                // Python works the gain out in float32 (numpy, from _envelope's float32 out array)
                const float env_db = 20.0f * std::log10(std::max(static_cast<float>(env), 1e-6f));
                const float over = std::max(0.0f, env_db - static_cast<float>(threshold));
                const float g = std::pow(10.0f, -over * static_cast<float>(1.0 - 1.0 / ratio) / 20.0f) *
                                static_cast<float>(makeup);
                scale(b, i, count, g);
            }
            if (limiter) {
                const float ceiling = static_cast<float>(db_gain(threshold));
                for (int i = 0; i < BLOCK * 2; ++i) b[i] = std::clamp(b[i], -ceiling, ceiling);
            }
            break;
        }
        case SC_FILTER_INVERT:
            for (int i = 0; i < BLOCK * 2; ++i) b[i] = -b[i];
            break;
        case SC_FILTER_GRAPH:
            if (graph) graph->process(b);
            break;
        case SC_FILTER_DUCKING: {  // follow: the sound; p: amount_db, threshold_db, attack_ms, hold_ms, release_ms
            const double block_s = static_cast<double>(BLOCK) / RATE;
            if (settings.follow > 0 && other_level > db_gain(p[1]))
                held = p[3] / 1000.0;
            else
                held = std::max(0.0, held - block_s);
            const double target = held > 0.0 ? db_gain(p[0]) : 1.0;
            const double start = gain;
            if (std::fabs(target - start) < 1e-4) {
                gain = target;
                if (target != 1.0) scale(b, 0, BLOCK, target);
                break;
            }
            const double tau = (target < start ? p[2] : p[4]) / 1000.0;
            gain = start + (target - start) * (1.0 - std::exp(-block_s / std::max(tau, 1e-3)));
            ramp(b, 0, BLOCK, start, gain);
            break;
        }
        default:
            break;
        }
    }
};

struct Source {
    sc_mixer_source_settings settings{};
    std::deque<float> queued;  // pushed samples, interleaved
    bool primed = false;       // a block ahead was queued: plays from it
    sc_media *media = nullptr; // read straight from the engine's player instead
    int media_asleep = 0;
    std::vector<float> held;   // the delay's samples between blocks
    float gate = 1.0f;         // sound per bloom: its fade now
    float talk = 1.0f;         // push to talk: its gain now (1 heard, 0 not)
    float peak_db = FLOOR_DB, rms_db = FLOOR_DB, level = 0.0f;
    bool measured = false;
    uint64_t underruns = 0;
    size_t cushion = 0;                  // frames kept ahead beyond the block being played (grows when it runs dry)
    size_t floor_cushion = 0;            // ... never less than this (sc_mixer_source_cushion)
    size_t lowest = SIZE_MAX;            // the fewest frames left after a read in this window
    int window = 0, calm = 0;
    Block block = Block(BLOCK * 2);  // this tick's, after shaping, the fade and ducking (volume not applied)
    std::vector<Filter> filters;     // its built-in filters, in order (before mono, balance and delay)
    bool peeked = false;             // a tap wants it before its filters (the Filters window)
    Block raw = Block(BLOCK * 2);    // this tick's before its filters, when peeked
};

struct Tap {
    std::vector<int> what;  // tracks (0..), MONITOR, or SOURCE_TAP + id
    std::deque<std::pair<int64_t, std::vector<float>>> blocks;
    uint64_t dropped = 0;
    // Straight into a writer (sc_mixer_tap_writer), with no thread of Python's between: `held` until the caller
    // has written what it took before (sc_mixer_tap_go), so no block changes places.
    sc::WriterSound sink;
    int sink_tracks = 0;
    bool held = false, paused = false, over = false;

    void send(const std::vector<float> &out) {
        if (paused || over) return;
        const int entries = static_cast<int>(what.size());
        for (int t = 0; t < sink_tracks; ++t) {
            const int from = entries == 1 ? 0 : t;  // one mix for every track of the writer, or one each
            if (from >= entries) break;
            if (sink(t, out.data() + static_cast<size_t>(from) * BLOCK * 2, BLOCK) != SC_OK) {
                over = true;  // the writer is finishing or failed: nothing more goes to it
                return;
            }
        }
    }
};

}  // namespace

struct sc_mixer {
    std::mutex lock;
    std::condition_variable ticked;  // a block was mixed (sc_mixer_wait)
    std::condition_variable wake;    // the thread: stop
    std::map<int, Source> sources;
    std::map<int, Tap> taps;
    int next_source = 1, next_tap = 1;
    sc_mixer_ducking ducking{};
    int push_source = 0, push_mode = SC_PUSH_OFF;  // push to talk: the sound it holds, and how
    int push_release_blocks = 0;                   // blocks a let-go waits before it counts
    bool push_down = false;                        // the key is held
    int push_hold = 0;                             // blocks left before a let-go counts
    float duck_gain = 1.0f, hold_left = 0.0f;
    float master_peak = FLOOR_DB, master_rms = FLOOR_DB;
    bool master_measured = false;
    int64_t tick = 0;  // blocks mixed so far
    bool stop = false;
    bool faulted = false;
    std::string last_problem;
    std::chrono::steady_clock::time_point said_at{};
    std::thread thread;
    std::vector<float> scratch = std::vector<float>(BLOCK * 2);

    void problem(const std::string &what) {  // logged once, again only if it changes or a minute passed
        const auto now = std::chrono::steady_clock::now();
        if (what != last_problem || now - said_at > std::chrono::seconds(60)) {
            last_problem = what;
            said_at = now;
            sc::log_line(SC_LOG_ERROR, "sound mixer: " + what);
        }
    }

    // A pushed sound that plays on: drop what has stayed well ahead for two seconds (the cushion and a block more
    // stay), and give back some of the cushion after a calm minute.
    static void keep_even(Source &s) {
        s.lowest = std::min(s.lowest, s.queued.size() / 2);
        if (++s.window < CUSHION_WINDOW) return;
        const size_t keep = s.cushion + LEAD;
        if (s.lowest != SIZE_MAX && s.lowest > keep + LEAD / 2) {
            const size_t extra = s.lowest - keep;
            s.queued.erase(s.queued.begin(), s.queued.begin() + static_cast<std::ptrdiff_t>(extra * 2));
        }
        s.window = 0;
        s.lowest = SIZE_MAX;
        if (++s.calm >= CALM_WINDOWS) {
            s.calm = 0;
            s.cushion = std::max(s.floor_cushion, s.cushion - std::min(s.cushion, CUSHION_STEP / 2));
        }
    }

    // One sound's next block into s.block: pushed samples or the player's, then mono, balance and delay.
    void read(Source &s) {
        float *b = s.block.data();
        std::fill(s.block.begin(), s.block.end(), 0.0f);
        if (s.media) {
            if (sc_media_read_audio(s.media, b, BLOCK, s.media_asleep) != SC_OK) {
                problem("a sound file's player failed; it is silent this block");
                std::fill(s.block.begin(), s.block.end(), 0.0f);
            }
        } else {
            if (!s.primed && s.queued.size() >= (LEAD + s.cushion) * 2) s.primed = true;
            if (s.primed) {
                const size_t have = std::min(s.queued.size(), static_cast<size_t>(BLOCK * 2));
                std::copy(s.queued.begin(), s.queued.begin() + static_cast<std::ptrdiff_t>(have), b);
                s.queued.erase(s.queued.begin(), s.queued.begin() + static_cast<std::ptrdiff_t>(have));
                if (have < static_cast<size_t>(BLOCK * 2)) {  // ran dry: waits for a block (and its cushion) again
                    s.primed = false;
                    ++s.underruns;
                    s.cushion = std::min(MAX_CUSHION, s.cushion + CUSHION_STEP);
                    s.calm = 0;
                } else {
                    keep_even(s);
                }
            }
        }
        if (s.peeked) std::copy(s.block.begin(), s.block.end(), s.raw.begin());
        for (Filter &f : s.filters) {
            double other = 0.0;
            if (f.settings.kind == SC_FILTER_DUCKING) {
                auto found = sources.find(f.settings.follow);
                if (found != sources.end()) other = found->second.level;
            }
            f.process(b, other);
        }
        const sc_mixer_source_settings &o = s.settings;
        if (o.mono) {
            for (int i = 0; i < BLOCK; ++i) b[2 * i] = b[2 * i + 1] = 0.5f * (b[2 * i] + b[2 * i + 1]);
        }
        const float balance = std::clamp(o.balance, -1.0f, 1.0f);
        if (balance != 0.0f) {
            const float l = std::min(1.0f, 1.0f - balance), r = std::min(1.0f, 1.0f + balance);
            for (int i = 0; i < BLOCK; ++i) {
                b[2 * i] *= l;
                b[2 * i + 1] *= r;
            }
        }
        const size_t delay = static_cast<size_t>(std::clamp(o.delay_ms, 0, MAX_DELAY_MS)) * RATE / 1000 * 2;
        if (delay) {
            if (s.held.size() != delay) s.held.assign(delay, 0.0f);  // a new delay starts with silence
            std::vector<float> joined(s.held);
            joined.insert(joined.end(), b, b + BLOCK * 2);
            std::copy(joined.begin(), joined.begin() + BLOCK * 2, b);
            s.held.assign(joined.begin() + BLOCK * 2, joined.end());
        } else if (!s.held.empty()) {
            s.held.clear();
        }
    }

    // Sound per bloom: a fade toward quiet while its blooms are off air, back up when one goes on.
    void fade(Source &s) {
        const float target = s.settings.bloom_out ? 0.0f : 1.0f;
        const float start = s.gate;
        if (start == target) {
            if (target == 0.0f) std::fill(s.block.begin(), s.block.end(), 0.0f);
            return;
        }
        const float step = static_cast<float>(BLOCK / static_cast<double>(RATE) / BLOOM_FADE_S);
        const float end = target > start ? std::min(target, start + step) : std::max(target, start - step);
        s.gate = end;
        for (int i = 0; i < BLOCK; ++i) {
            const float g = start + (end - start) * static_cast<float>(i) / static_cast<float>(BLOCK - 1);
            s.block[2 * i] *= g;
            s.block[2 * i + 1] *= g;
        }
    }

    // Push to talk: whether the held sound is heard now (the key, or a let-go still waiting out its delay).
    float push_target() const {
        const bool held = push_down || push_hold > 0;
        return (push_mode == SC_PUSH_TALK) == held ? 1.0f : 0.0f;
    }

    // ... and its gain on the sound this block, gliding over the block when it changes.
    void push_gate(int id, Source &s) {
        if (id != push_source || push_mode == SC_PUSH_OFF) return;
        const float target = push_target();
        const float start = s.talk;
        s.talk = target;
        if (start == target) {
            if (target == 0.0f) std::fill(s.block.begin(), s.block.end(), 0.0f);
            return;
        }
        for (int i = 0; i < BLOCK; ++i) {
            const float g = start + (target - start) * static_cast<float>(i) / static_cast<float>(BLOCK - 1);
            s.block[2 * i] *= g;
            s.block[2 * i + 1] *= g;
        }
    }

    // Ducking: the gain on every sound that isn't a voice this block, gliding from the last one (start, end).
    std::pair<float, float> duck(float voice_db) {
        const sc_mixer_ducking &d = ducking;
        const float block_s = static_cast<float>(BLOCK) / RATE;
        if (d.enabled && voice_db > d.threshold_db) {
            hold_left = d.hold_ms / 1000.0f;
        } else {
            hold_left = std::max(0.0f, hold_left - block_s);
        }
        const bool talking = d.enabled && hold_left > 0.0f;
        const float target = talking ? std::pow(10.0f, d.amount_db / 20.0f) : 1.0f;
        const float start = duck_gain;
        if (std::fabs(target - start) < 1e-4f) {
            duck_gain = target;
            return {target, target};
        }
        const float tau = std::max((target < start ? d.attack_ms : d.release_ms) / 1000.0f, 1e-3f);
        duck_gain = start + (target - start) * (1.0f - std::exp(-block_s / tau));
        return {start, duck_gain};
    }

    void mix_one() {
        float voice_db = FLOOR_DB;
        for (auto &[id, s] : sources) {  // kept before its filters only while a tap asks
            s.peeked = false;
            (void)id;
        }
        for (auto &[tap_id, tap] : taps) {
            for (int what : tap.what) {
                if (what < RAW_TAP) continue;
                auto found = sources.find(what - RAW_TAP);
                if (found != sources.end()) found->second.peeked = true;
            }
            (void)tap_id;
        }
        if (!push_down && push_hold > 0) --push_hold;  // a let-go waits out its delay block by block
        for (auto &[id, s] : sources) {
            read(s);
            fade(s);
            push_gate(id, s);
            const float gain = std::max(0.0f, s.settings.gain);
            float peak = FLOOR_DB, rms = FLOOR_DB;
            if (gain > 0.0f) measure(s.block.data(), gain, peak, rms);
            if (s.settings.voice && gain > 0.0f) voice_db = std::max(voice_db, rms);
            s.level = peak > FLOOR_DB ? std::pow(10.0f, peak / 20.0f) : 0.0f;  // after volume, before ducking
            (void)id;
        }
        const auto [d0, d1] = duck(voice_db);
        const bool ducked = !(d0 == 1.0f && d1 == 1.0f);
        std::vector<Block> tracks(SC_MIXER_TRACKS, Block(BLOCK * 2, 0.0f));
        Block monitor(BLOCK * 2, 0.0f);
        for (auto &[id, s] : sources) {
            if (!s.settings.voice && ducked) {
                for (int i = 0; i < BLOCK; ++i) {
                    const float g = d0 + (d1 - d0) * static_cast<float>(i) / static_cast<float>(BLOCK - 1);
                    s.block[2 * i] *= g;
                    s.block[2 * i + 1] *= g;
                }
            }
            const float gain = std::max(0.0f, s.settings.gain);
            float peak = FLOOR_DB, rms = FLOOR_DB;
            if (gain > 0.0f) measure(s.block.data(), gain, peak, rms);
            s.peak_db = s.measured ? std::max(s.peak_db, peak) : peak;  // the loudest since the levels were read
            s.rms_db = rms;
            s.measured = true;
            if (gain <= 0.0f) continue;
            for (int t = 0; t < SC_MIXER_TRACKS; ++t) {
                if (!(s.settings.tracks & (1u << t))) continue;
                float *out = tracks[t].data();
                for (int i = 0; i < BLOCK * 2; ++i) out[i] += s.block[i] * gain;
            }
            if (s.settings.monitor) {
                for (int i = 0; i < BLOCK * 2; ++i) monitor[i] += s.block[i] * gain;
            }
            (void)id;
        }
        for (auto &t : tracks) {
            for (float &v : t) v = std::clamp(v, -1.0f, 1.0f);
        }
        for (float &v : monitor) v = std::clamp(v, -1.0f, 1.0f);
        float peak = FLOOR_DB, rms = FLOOR_DB;
        measure(tracks[SC_TRACK_STREAM].data(), 1.0f, peak, rms);
        master_peak = master_measured ? std::max(master_peak, peak) : peak;
        master_rms = rms;
        master_measured = true;
        for (auto &[tap_id, tap] : taps) {
            std::vector<float> out;
            out.reserve(tap.what.size() * BLOCK * 2);
            for (int what : tap.what) {
                if (what >= 0 && what < SC_MIXER_TRACKS) {
                    out.insert(out.end(), tracks[static_cast<size_t>(what)].begin(),
                               tracks[static_cast<size_t>(what)].end());
                } else if (what == MONITOR) {
                    out.insert(out.end(), monitor.begin(), monitor.end());
                } else if (what >= RAW_TAP) {  // before its filters (the Filters window)
                    auto found = sources.find(what - RAW_TAP);
                    const size_t at = out.size();
                    out.resize(at + BLOCK * 2, 0.0f);
                    if (found != sources.end() && found->second.peeked) {
                        const Block &from = found->second.raw;
                        std::copy(from.begin(), from.end(), out.begin() + static_cast<std::ptrdiff_t>(at));
                    }
                } else {
                    auto found = sources.find(what - SOURCE_TAP);
                    const size_t at = out.size();
                    out.resize(at + BLOCK * 2, 0.0f);
                    if (found != sources.end()) {
                        const float gain = std::max(0.0f, found->second.settings.gain);
                        for (int i = 0; i < BLOCK * 2; ++i) out[at + i] = found->second.block[i] * gain;
                    }
                }
            }
            if (tap.sink && !tap.held) {
                tap.send(out);
                continue;
            }
            if (tap.blocks.size() >= TAP_QUEUE) {  // nobody took them: the oldest goes
                tap.blocks.pop_front();
                ++tap.dropped;
            }
            tap.blocks.emplace_back(tick, std::move(out));
            (void)tap_id;
        }
        ++tick;
    }

    void run() {
        sc::name_thread(L"sprigcore sound mixer");
        sc::sound_thread_priority();
        using clock = std::chrono::steady_clock;
        const auto period = std::chrono::duration_cast<clock::duration>(std::chrono::duration<double>(
            static_cast<double>(BLOCK) / RATE));
        auto next = clock::now() + period;
        std::unique_lock<std::mutex> hold(lock);
        while (!stop) {
            if (wake.wait_until(hold, next, [this] { return stop; })) break;
            try {
                mix_one();
            } catch (const std::exception &e) {
                problem(std::string("mixing failed: ") + e.what());
            } catch (...) {
                problem("mixing faulted");
            }
            ticked.notify_all();
            next += period;
            const auto now = clock::now();
            if (now - next > std::chrono::milliseconds(500)) next = now + period;  // the PC slept: catch up at once
        }
    }
};

extern "C" {

SC_API int sc_mixer_create(int clocked, sc_mixer **out) {
    return sc::guarded("sc_mixer_create", [&]() -> int {
        if (!out) return sc::fail(SC_ERR_ARGUMENT, "sc_mixer_create: nowhere to put it");
        *out = nullptr;
        auto m = std::make_unique<sc_mixer>();
        m->ducking = sc_mixer_ducking{0, -14.0f, -38.0f, 250.0f, 900.0f, 400.0f};
        if (clocked) m->thread = std::thread([p = m.get()] { p->run(); });
        *out = m.release();
        return SC_OK;
    });
}

SC_API void sc_mixer_destroy(sc_mixer *mixer) {
    if (!mixer) return;
    sc::guarded("sc_mixer_destroy", [&]() -> int {
        {
            std::lock_guard<std::mutex> hold(mixer->lock);
            mixer->stop = true;
        }
        mixer->wake.notify_all();
        mixer->ticked.notify_all();
        if (mixer->thread.joinable()) mixer->thread.join();
        delete mixer;
        return SC_OK;
    });
}

SC_API int sc_mixer_source_add(sc_mixer *mixer, const sc_mixer_source_settings *settings, int *id) {
    return sc::guarded("sc_mixer_source_add", [&]() -> int {
        if (!mixer || !settings || !id) return sc::fail(SC_ERR_ARGUMENT, "sc_mixer_source_add: nothing given");
        std::lock_guard<std::mutex> hold(mixer->lock);
        *id = mixer->next_source++;
        Source &s = mixer->sources[*id];
        s.settings = *settings;
        s.gate = settings->bloom_out ? 0.0f : 1.0f;  // starts where it belongs, no fade
        return SC_OK;
    });
}

SC_API int sc_mixer_source_set(sc_mixer *mixer, int id, const sc_mixer_source_settings *settings) {
    return sc::guarded("sc_mixer_source_set", [&]() -> int {
        if (!mixer || !settings) return sc::fail(SC_ERR_ARGUMENT, "sc_mixer_source_set: nothing given");
        std::lock_guard<std::mutex> hold(mixer->lock);
        auto found = mixer->sources.find(id);
        if (found == mixer->sources.end()) return sc::fail(SC_ERR_ARGUMENT, "sc_mixer_source_set: no such sound");
        found->second.settings = *settings;
        return SC_OK;
    });
}

SC_API int sc_mixer_source_remove(sc_mixer *mixer, int id) {
    return sc::guarded("sc_mixer_source_remove", [&]() -> int {
        if (!mixer) return sc::fail(SC_ERR_ARGUMENT, "sc_mixer_source_remove: no mixer");
        std::lock_guard<std::mutex> hold(mixer->lock);
        mixer->sources.erase(id);
        return SC_OK;
    });
}

SC_API int sc_mixer_source_push(sc_mixer *mixer, int id, const float *stereo, int frames) {
    return sc::guarded("sc_mixer_source_push", [&]() -> int {
        if (!mixer || (!stereo && frames) || frames < 0)
            return sc::fail(SC_ERR_ARGUMENT, "sc_mixer_source_push: nothing given");
        std::lock_guard<std::mutex> hold(mixer->lock);
        auto found = mixer->sources.find(id);
        if (found == mixer->sources.end()) return sc::fail(SC_ERR_ARGUMENT, "sc_mixer_source_push: no such sound");
        Source &s = found->second;
        s.queued.insert(s.queued.end(), stereo, stereo + static_cast<size_t>(frames) * 2);
        if (s.queued.size() > MAX_QUEUED * 2) {  // never let the delay build up: the oldest goes
            s.queued.erase(s.queued.begin(),
                           s.queued.begin() + static_cast<std::ptrdiff_t>(s.queued.size() - MAX_QUEUED * 2));
        }
        return SC_OK;
    });
}

SC_API int sc_mixer_source_cushion(sc_mixer *mixer, int id, int frames) {
    return sc::guarded("sc_mixer_source_cushion", [&]() -> int {
        if (!mixer || frames < 0) return sc::fail(SC_ERR_ARGUMENT, "sc_mixer_source_cushion: nothing given");
        std::lock_guard<std::mutex> hold(mixer->lock);
        auto found = mixer->sources.find(id);
        if (found == mixer->sources.end()) return sc::fail(SC_ERR_ARGUMENT, "sc_mixer_source_cushion: no such sound");
        Source &s = found->second;
        s.floor_cushion = std::min(MAX_CUSHION, static_cast<size_t>(frames));
        s.cushion = std::max(s.cushion, s.floor_cushion);
        return SC_OK;
    });
}

SC_API int sc_mixer_source_media(sc_mixer *mixer, int id, sc_media *media, int asleep) {
    return sc::guarded("sc_mixer_source_media", [&]() -> int {
        if (!mixer) return sc::fail(SC_ERR_ARGUMENT, "sc_mixer_source_media: no mixer");
        std::lock_guard<std::mutex> hold(mixer->lock);  // once this returns, the mixer no longer reads an old player
        auto found = mixer->sources.find(id);
        if (found == mixer->sources.end()) return sc::fail(SC_ERR_ARGUMENT, "sc_mixer_source_media: no such sound");
        found->second.media = media;
        found->second.media_asleep = asleep;
        if (media) found->second.queued.clear();
        return SC_OK;
    });
}

SC_API int sc_mixer_source_filters(sc_mixer *mixer, int id, const sc_mixer_filter *filters, int count) {
    return sc_mixer_source_filters_text(mixer, id, filters, nullptr, count);
}

SC_API int sc_mixer_source_filters_text(sc_mixer *mixer, int id, const sc_mixer_filter *filters,
                                        const char *const *texts, int count) {
    return sc::guarded("sc_mixer_source_filters", [&]() -> int {
        if (!mixer || count < 0 || (count && !filters) || count > 32)
            return sc::fail(SC_ERR_ARGUMENT, "sc_mixer_source_filters: nothing given");
        std::vector<std::string> wanted(static_cast<size_t>(count));
        for (int i = 0; i < count; ++i) {
            if (filters[i].kind != SC_FILTER_GRAPH) continue;
            if (!texts || !texts[i] || !*texts[i])
                return sc::fail(SC_ERR_ARGUMENT, "sc_mixer_source_filters: an FFmpeg filter chain without its text");
            wanted[static_cast<size_t>(i)] = texts[i];
        }
        // The chains already running with the same text are kept (their sound held back with them); new ones are
        // made here, outside the mixer's lock, so the mix never waits for FFmpeg to set one up.
        std::vector<std::shared_ptr<Graph>> graphs(static_cast<size_t>(count));
        {
            std::lock_guard<std::mutex> hold(mixer->lock);
            auto found = mixer->sources.find(id);
            if (found == mixer->sources.end())
                return sc::fail(SC_ERR_ARGUMENT, "sc_mixer_source_filters: no such sound");
            const std::vector<Filter> &have = found->second.filters;
            for (size_t i = 0; i < graphs.size() && i < have.size(); ++i)
                if (filters[i].kind == SC_FILTER_GRAPH && have[i].settings.kind == SC_FILTER_GRAPH &&
                    have[i].text == wanted[i])
                    graphs[i] = have[i].graph;
        }
        for (size_t i = 0; i < graphs.size(); ++i) {
            if (filters[i].kind != SC_FILTER_GRAPH || graphs[i]) continue;
            auto made = std::make_shared<Graph>();
            std::string why;
            made->build(wanted[i], why);
            if (!why.empty()) return sc::fail(SC_ERR_ARGUMENT, "sound filter: " + why);
            graphs[i] = std::move(made);
        }
        std::lock_guard<std::mutex> hold(mixer->lock);
        auto found = mixer->sources.find(id);
        if (found == mixer->sources.end()) return sc::fail(SC_ERR_ARGUMENT, "sc_mixer_source_filters: no such sound");
        std::vector<Filter> &have = found->second.filters;
        bool same = static_cast<int>(have.size()) == count;
        for (int i = 0; same && i < count; ++i) same = have[static_cast<size_t>(i)].settings.kind == filters[i].kind;
        if (!same) {  // other filters: they start fresh
            have.assign(static_cast<size_t>(count), Filter{});
        }
        for (int i = 0; i < count; ++i) {
            Filter &f = have[static_cast<size_t>(i)];
            f.settings = filters[i];  // state kept
            f.text = wanted[static_cast<size_t>(i)];
            f.graph = graphs[static_cast<size_t>(i)];
        }
        return SC_OK;
    });
}

SC_API int sc_mixer_ffmpeg_load_filters(const char *avfilter, const char *avutil) {
    return sc::guarded("sc_mixer_ffmpeg_load_filters", [&]() -> int {
        using namespace sc::ffm;
        std::lock_guard<std::mutex> hold(ff_lock);
        if (!ff_ready.load()) return sc::fail(SC_ERR_ARGUMENT, "sound filters: FFmpeg isn't loaded yet");
        if (fg_ready.load()) return SC_OK;
        if (!avfilter || !*avfilter || !avutil || !*avutil)
            return sc::fail(SC_ERR_ARGUMENT, "sound filters: no libavfilter or libavutil path");
        void *library = open_library(avfilter), *util = open_library(avutil);
        if (!library || !util) return sc::fail(SC_ERR_DEVICE, std::string("sound filters: couldn't load ") + avfilter);
        FilterGraphApi api;
#define SC_FG(lib, name)                                                                                           \
    api.name = reinterpret_cast<decltype(api.name)>(find_symbol(lib, #name));                                       \
    if (!api.name) return sc::fail(SC_ERR_DEVICE, "sound filters: FFmpeg has no " #name);
        SC_FG(library, avfilter_version)
        SC_FG(library, avfilter_graph_alloc)
        SC_FG(library, avfilter_graph_free)
        SC_FG(library, avfilter_get_by_name)
        SC_FG(library, avfilter_graph_create_filter)
        SC_FG(library, avfilter_link)
        SC_FG(library, avfilter_graph_config)
        SC_FG(library, av_buffersrc_add_frame_flags)
        SC_FG(library, av_buffersink_get_frame)
        SC_FG(util, av_opt_set_int)
#undef SC_FG
        if (api.avfilter_version() >> 16 != FILTERS_MAJOR)
            return sc::fail(SC_ERR_DEVICE, "sound filters: libavfilter is version " +
                                               std::to_string(api.avfilter_version() >> 16) + ", the engine was "
                                               "built for " + std::to_string(FILTERS_MAJOR));
        fg = api;
        fg_ready.store(true);
        sc::log_line(SC_LOG_INFO, "sound filters: FFmpeg's filters loaded (libavfilter " +
                                      std::to_string(api.avfilter_version() >> 16) + ")");
        return SC_OK;
    });
}

SC_API int sc_mixer_ffmpeg_filters_ready(void) { return sc::ffm::fg_ready.load() ? 1 : 0; }

SC_API int sc_mixer_set_ducking(sc_mixer *mixer, const sc_mixer_ducking *ducking) {
    return sc::guarded("sc_mixer_set_ducking", [&]() -> int {
        if (!mixer || !ducking) return sc::fail(SC_ERR_ARGUMENT, "sc_mixer_set_ducking: nothing given");
        std::lock_guard<std::mutex> hold(mixer->lock);
        mixer->ducking = *ducking;
        return SC_OK;
    });
}

SC_API int sc_mixer_push_set(sc_mixer *mixer, int source, int mode, int release_ms) {
    return sc::guarded("sc_mixer_push_set", [&]() -> int {
        if (!mixer) return sc::fail(SC_ERR_ARGUMENT, "sc_mixer_push_set: no mixer");
        if (mode < SC_PUSH_OFF || mode > SC_PUSH_MUTE)
            return sc::fail(SC_ERR_ARGUMENT, "sc_mixer_push_set: mode is SC_PUSH_OFF, _TALK or _MUTE");
        std::lock_guard<std::mutex> hold(mixer->lock);
        const bool changed = mixer->push_source != source || mixer->push_mode != mode;
        mixer->push_source = mode == SC_PUSH_OFF ? 0 : source;
        mixer->push_mode = mixer->push_source ? mode : SC_PUSH_OFF;
        const int ms = std::clamp(release_ms, 0, 2000);
        mixer->push_release_blocks = (ms * RATE / 1000 + BLOCK - 1) / BLOCK;
        if (changed) {
            mixer->push_hold = 0;
            for (auto &[id, s] : mixer->sources)  // a newly held sound starts where it belongs, no glide
                s.talk = id == mixer->push_source ? mixer->push_target() : 1.0f;
        }
        return SC_OK;
    });
}

SC_API int sc_mixer_push_key(sc_mixer *mixer, int down) {
    return sc::guarded("sc_mixer_push_key", [&]() -> int {
        if (!mixer) return sc::fail(SC_ERR_ARGUMENT, "sc_mixer_push_key: no mixer");
        std::lock_guard<std::mutex> hold(mixer->lock);
        const bool was = mixer->push_down;
        mixer->push_down = down != 0;
        if (was && !mixer->push_down) mixer->push_hold = mixer->push_release_blocks;
        if (mixer->push_down) mixer->push_hold = 0;
        return SC_OK;
    });
}

SC_API int sc_mixer_tap_add(sc_mixer *mixer, const int *what, int count, int *id) {
    return sc::guarded("sc_mixer_tap_add", [&]() -> int {
        if (!mixer || !what || count < 1 || count > 16 || !id)
            return sc::fail(SC_ERR_ARGUMENT, "sc_mixer_tap_add: 1 to 16 entries");
        for (int i = 0; i < count; ++i) {
            const int w = what[i];
            if (!((w >= 0 && w < SC_MIXER_TRACKS) || w == MONITOR || w >= SOURCE_TAP))
                return sc::fail(SC_ERR_ARGUMENT, "sc_mixer_tap_add: no such track");
        }
        std::lock_guard<std::mutex> hold(mixer->lock);
        *id = mixer->next_tap++;
        mixer->taps[*id].what.assign(what, what + count);
        return SC_OK;
    });
}

SC_API int sc_mixer_tap_remove(sc_mixer *mixer, int id) {
    return sc::guarded("sc_mixer_tap_remove", [&]() -> int {
        if (!mixer) return sc::fail(SC_ERR_ARGUMENT, "sc_mixer_tap_remove: no mixer");
        std::lock_guard<std::mutex> hold(mixer->lock);
        mixer->taps.erase(id);
        return SC_OK;
    });
}

SC_API int sc_mixer_tap_take(sc_mixer *mixer, int id, float *out, int capacity, int64_t *index, uint64_t *dropped) {
    return sc::guarded("sc_mixer_tap_take", [&]() -> int {
        if (!mixer || !out) return sc::fail(SC_ERR_ARGUMENT, "sc_mixer_tap_take: nothing given");
        std::lock_guard<std::mutex> hold(mixer->lock);
        auto found = mixer->taps.find(id);
        if (found == mixer->taps.end()) return sc::fail(SC_ERR_ARGUMENT, "sc_mixer_tap_take: no such tap");
        Tap &tap = found->second;
        if (dropped) *dropped = tap.dropped;
        if (tap.blocks.empty() || tap.sink) return 0;  // into a writer: what waits is the writer's
        auto &front = tap.blocks.front();
        if (capacity < static_cast<int>(front.second.size()))
            return sc::fail(SC_ERR_ARGUMENT, "sc_mixer_tap_take: too little room");
        std::memcpy(out, front.second.data(), front.second.size() * sizeof(float));
        if (index) *index = front.first;
        tap.blocks.pop_front();
        return 1;
    });
}

SC_API int sc_mixer_tap_writer(sc_mixer *mixer, int id, sc_writer *writer) {
    return sc::guarded("sc_mixer_tap_writer", [&]() -> int {
        if (!mixer) return sc::fail(SC_ERR_ARGUMENT, "sc_mixer_tap_writer: no mixer");
        int tracks = 0;
        sc::WriterSound sink = writer ? sc::writer_sound(writer, &tracks) : sc::WriterSound();
        std::lock_guard<std::mutex> hold(mixer->lock);
        auto found = mixer->taps.find(id);
        if (found == mixer->taps.end()) return sc::fail(SC_ERR_ARGUMENT, "sc_mixer_tap_writer: no such tap");
        Tap &tap = found->second;
        tap.sink = std::move(sink);
        tap.sink_tracks = tracks;
        tap.held = writer != nullptr;
        tap.paused = tap.over = false;
        return SC_OK;
    });
}

SC_API int sc_mixer_tap_go(sc_mixer *mixer, int id) {
    return sc::guarded("sc_mixer_tap_go", [&]() -> int {
        if (!mixer) return sc::fail(SC_ERR_ARGUMENT, "sc_mixer_tap_go: no mixer");
        std::lock_guard<std::mutex> hold(mixer->lock);
        auto found = mixer->taps.find(id);
        if (found == mixer->taps.end() || !found->second.sink)
            return sc::fail(SC_ERR_ARGUMENT, "sc_mixer_tap_go: no such tap into a writer");
        Tap &tap = found->second;
        for (auto &block : tap.blocks) tap.send(block.second);
        tap.blocks.clear();
        tap.held = false;
        return SC_OK;
    });
}

SC_API int sc_mixer_tap_pause(sc_mixer *mixer, int id, int paused) {
    return sc::guarded("sc_mixer_tap_pause", [&]() -> int {
        if (!mixer) return sc::fail(SC_ERR_ARGUMENT, "sc_mixer_tap_pause: no mixer");
        std::lock_guard<std::mutex> hold(mixer->lock);
        auto found = mixer->taps.find(id);
        // (A tap already taken away has nothing left to leave out: an output stopping after its tap went, as
        // clips switched off do, is no mistake. It logged "no such tap" on the PC, 2026-11-21.)
        if (found != mixer->taps.end()) found->second.paused = paused != 0;
        return SC_OK;
    });
}

SC_API int sc_mixer_tick(sc_mixer *mixer, int count) {
    return sc::guarded("sc_mixer_tick", [&]() -> int {
        if (!mixer || count < 0) return sc::fail(SC_ERR_ARGUMENT, "sc_mixer_tick: nothing given");
        std::lock_guard<std::mutex> hold(mixer->lock);
        if (mixer->thread.joinable()) return sc::fail(SC_ERR_ARGUMENT, "sc_mixer_tick: this mixer keeps its own time");
        for (int i = 0; i < count; ++i) mixer->mix_one();
        mixer->ticked.notify_all();
        return SC_OK;
    });
}

SC_API int sc_mixer_wait(sc_mixer *mixer, int64_t after, int timeout_ms, int64_t *now) {
    return sc::guarded("sc_mixer_wait", [&]() -> int {
        if (!mixer || timeout_ms < 0) return sc::fail(SC_ERR_ARGUMENT, "sc_mixer_wait: nothing given");
        std::unique_lock<std::mutex> hold(mixer->lock);
        mixer->ticked.wait_for(hold, std::chrono::milliseconds(timeout_ms),
                               [&] { return mixer->tick > after || mixer->stop; });
        if (now) *now = mixer->tick;
        return mixer->tick > after ? 1 : 0;
    });
}

SC_API int sc_mixer_levels(sc_mixer *mixer, int id, sc_mixer_level *out) {
    return sc::guarded("sc_mixer_levels", [&]() -> int {
        if (!mixer || !out) return sc::fail(SC_ERR_ARGUMENT, "sc_mixer_levels: nothing given");
        std::lock_guard<std::mutex> hold(mixer->lock);
        *out = sc_mixer_level{FLOOR_DB, FLOOR_DB, 0.0f, 0};
        if (id == SC_MIXER_MASTER) {
            out->peak_db = mixer->master_peak;
            out->rms_db = mixer->master_rms;
            out->level = mixer->duck_gain;  // for the master: the ducking gain now
            mixer->master_measured = false;
            return SC_OK;
        }
        auto found = mixer->sources.find(id);
        if (found == mixer->sources.end()) return sc::fail(SC_ERR_ARGUMENT, "sc_mixer_levels: no such sound");
        Source &s = found->second;
        out->peak_db = s.peak_db;
        out->rms_db = s.rms_db;
        out->level = s.level;
        out->underruns = s.underruns;
        s.measured = false;
        return SC_OK;
    });
}

}  // extern "C"
