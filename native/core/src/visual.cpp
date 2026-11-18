// Audio visualizer (sc_visual_*, docs/native-engine.md "Plugins drawn in the engine"): store/audio_visualizer.py's
// Analyser and Painter in C++, number for number, so the bars, wave, circle and line look the same as Python drew
// them. Python drew them on a worker holding its lock dozens of times a frame (the encoders and the sound queued
// behind it); here a thread of its own hears a mixer tap, draws at most 30 times a second and hands the newest
// drawing to the engine's frame thread, never through Python.
//
// The arithmetic follows NumPy's types step by step (float32 where the Python keeps float32, double where NumPy
// widens), which is why some lines cast where plain C++ wouldn't.

#include "base.h"
#include "visual.h"

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <cmath>
#include <complex>
#include <condition_variable>
#include <cstring>
#include <memory>
#include <mutex>
#include <thread>
#include <vector>

namespace {

constexpr int W = SC_VISUAL_WIDTH, H = SC_VISUAL_HEIGHT;
constexpr int RATE = 48000;
constexpr int N = 2048;          // FFT size: two mixer blocks
constexpr int BINS = N / 2 + 1;
constexpr int WAVE = 256;
constexpr double LOW = 40.0, HIGH = 16000.0;
constexpr int FPS = 30;
constexpr float STILL = 0.004f;
constexpr double PAD = 12.0;
constexpr double PI = 3.14159265358979323846;
constexpr int MOST_FRAMES = 4;   // drawings kept: one shown, one waiting, one being drawn, one spare

using Frame = std::vector<uint8_t>;
using Rgba = std::array<double, 4>;
using Premul = std::array<float, 4>;

// NumPy's float32 floor division and remainder (npy_divmodf).
float floordiv_f(float a, float b) {
    float mod = std::fmod(a, b);
    float div = (a - mod) / b;
    if (mod != 0.0f) {
        if ((b < 0) != (mod < 0)) div -= 1.0f;
    }
    if (div != 0.0f) {
        float f = std::floor(div);
        if (div - f > 0.5f) f += 1.0f;
        return f;
    }
    return std::copysign(0.0f, a / b);
}

float remainder_f(float a, float b) {
    float mod = std::fmod(a, b);
    if (mod != 0.0f) {
        if ((b < 0) != (mod < 0)) mod += b;
    } else {
        mod = std::copysign(0.0f, b);
    }
    return mod;
}

// np.interp for one x.
double interp(double x, const double *xp, const double *fp, int n) {
    if (x <= xp[0]) return fp[0];
    if (x >= xp[n - 1]) return fp[n - 1];
    int j = static_cast<int>(std::upper_bound(xp, xp + n, x) - xp) - 1;
    if (x == xp[j + 1]) return fp[j + 1];
    const double slope = (fp[j + 1] - fp[j]) / (xp[j + 1] - xp[j]);
    return slope * (x - xp[j]) + fp[j];
}

// np.linspace(start, stop, num)
std::vector<double> linspace(double start, double stop, int num) {
    std::vector<double> y(static_cast<size_t>(num));
    if (num == 1) {
        y[0] = start;
        return y;
    }
    const double step = (stop - start) / (num - 1);
    for (int i = 0; i < num; ++i) y[static_cast<size_t>(i)] = i * step + start;
    y.back() = stop;
    return y;
}

// premul(): B, G, R, A premultiplied, 0..255, in float32.
Premul premul(const Rgba &rgba, double alpha = 1.0) {
    const double a = rgba[3] * alpha;
    return {static_cast<float>(rgba[2] * a) * 255.0f, static_cast<float>(rgba[1] * a) * 255.0f,
            static_cast<float>(rgba[0] * a) * 255.0f, static_cast<float>(a) * 255.0f};
}

uint8_t to_byte(double v) {  // NumPy's astype(uint8) of a value already in range: truncation
    if (!(v > 0.0)) return 0;
    return v >= 255.0 ? 255 : static_cast<uint8_t>(v);
}

// gradient(): a W x H picture whose colour changes from row to row through the stops.
Frame gradient(const std::vector<std::pair<double, Premul>> &stops) {
    std::vector<double> at;
    for (auto &s : stops) at.push_back(s.first);
    Frame picture(static_cast<size_t>(W) * H * 4);
    for (int y = 0; y < H; ++y) {
        const double ys = static_cast<double>(static_cast<float>(y) + 0.5f);
        uint8_t px[4];
        for (int k = 0; k < 4; ++k) {
            std::vector<double> fp;
            for (auto &s : stops) fp.push_back(static_cast<double>(s.second[static_cast<size_t>(k)]));
            px[k] = to_byte(std::nearbyint(interp(ys, at.data(), fp.data(), static_cast<int>(at.size()))));
        }
        uint8_t *row = picture.data() + static_cast<size_t>(y) * W * 4;
        for (int x = 0; x < W; ++x) std::memcpy(row + x * 4, px, 4);
    }
    return picture;
}

// band_edges(): count + 1 rising bin indexes, log-spaced from LOW to HIGH.
std::vector<int> band_edges(int count) {
    const double hz = static_cast<double>(RATE) / ((BINS - 1) * 2);
    const double ls = std::log10(LOW), le = std::log10(HIGH);
    const double step = (le - ls) / count;
    std::vector<int> idx(static_cast<size_t>(count) + 1);
    for (int i = 0; i <= count; ++i) {
        double f = i == 0 ? LOW : i == count ? HIGH : std::pow(10.0, i * step + ls);
        idx[static_cast<size_t>(i)] = static_cast<int>(std::nearbyint(f / hz));
    }
    for (size_t i = 1; i < idx.size(); ++i) idx[i] = std::max(idx[i], idx[i - 1] + 1);
    for (int &v : idx) v = std::min(v, BINS - 1);
    return idx;
}

// mirrored(): bass in the middle, the same both ways.
std::vector<float> mirrored(const std::vector<float> &values) {
    const int n = static_cast<int>(values.size());
    const int m = (n + 1) / 2;
    std::vector<float> half;
    for (double v : linspace(0.0, n - 1, m)) half.push_back(values[static_cast<size_t>(std::nearbyint(v))]);
    std::vector<float> out(half.rbegin(), half.rend());
    out.insert(out.end(), half.begin() + (n % 2), half.end());
    return out;
}

// A real FFT of N samples (radix 2, in double).
class Fft {
public:
    Fft() : twiddle(N / 2), order(N) {
        for (int i = 0; i < N / 2; ++i) twiddle[static_cast<size_t>(i)] = std::polar(1.0, -2.0 * PI * i / N);
        int bits = 0;
        while ((1 << bits) < N) ++bits;
        for (int i = 0; i < N; ++i) {
            int r = 0;
            for (int b = 0; b < bits; ++b) r |= ((i >> b) & 1) << (bits - 1 - b);
            order[static_cast<size_t>(i)] = r;
        }
    }

    // |X[k]| for k = 0 .. N/2.
    void magnitudes(const float *in, double *out) {
        std::vector<std::complex<double>> &a = work;
        a.resize(N);
        for (int i = 0; i < N; ++i) a[static_cast<size_t>(order[static_cast<size_t>(i)])] = in[i];
        for (int len = 2; len <= N; len <<= 1) {
            const int half = len / 2, stride = N / len;
            for (int i = 0; i < N; i += len) {
                for (int j = 0; j < half; ++j) {
                    const auto w = twiddle[static_cast<size_t>(j * stride)];
                    const auto u = a[static_cast<size_t>(i + j)];
                    const auto v = a[static_cast<size_t>(i + j + half)] * w;
                    a[static_cast<size_t>(i + j)] = u + v;
                    a[static_cast<size_t>(i + j + half)] = u - v;
                }
            }
        }
        for (int k = 0; k < BINS; ++k) out[k] = std::abs(a[static_cast<size_t>(k)]);
    }

private:
    std::vector<std::complex<double>> twiddle;
    std::vector<int> order;
    std::vector<std::complex<double>> work;
};

// Analyser: the blocks it hears into band heights (0..1) and a wave (-1..1).
struct Analyser {
    std::vector<float> chunks[2] = {std::vector<float>(N / 2), std::vector<float>(N / 2)};  // mono
    uint64_t fed = 0, used = 0;
    double unheard = 0.0;
    std::vector<float> window = std::vector<float>(N);
    struct Edges {
        std::vector<int> edges;
        std::vector<float> tilt;
    };
    std::vector<std::pair<int, Edges>> edges;
    std::vector<float> bands, wave;
    Fft fft;
    std::vector<double> mags = std::vector<double>(BINS);

    Analyser() {
        for (int n = 0; n < N; ++n)
            window[static_cast<size_t>(n)] = static_cast<float>(0.5 - 0.5 * std::cos(2.0 * PI * n / (N - 1)));
    }

    void feed(const float *stereo, int frames) {
        std::vector<float> mono(N / 2, 0.0f);
        const int n = std::min(frames, N / 2);
        for (int i = 0; i < n; ++i) mono[static_cast<size_t>(N / 2 - n + i)] = (stereo[2 * i] + stereo[2 * i + 1]) / 2.0f;
        chunks[0].swap(chunks[1]);
        chunks[1].swap(mono);
        ++fed;
    }

    const Edges &edges_for(int count) {
        for (auto &e : edges)
            if (e.first == count) return e.second;
        Edges made;
        made.edges = band_edges(count);
        for (int i = 0; i < count; ++i) {
            const double a = std::max(made.edges[static_cast<size_t>(i)], 1);
            const double b = std::max(made.edges[static_cast<size_t>(i) + 1], 1);
            const double centre = std::sqrt(static_cast<double>(static_cast<int64_t>(a) * static_cast<int64_t>(b))) *
                                  (static_cast<double>(RATE) / N);
            made.tilt.push_back(static_cast<float>(std::clamp(3.0 * std::log2(centre / 500.0), 0.0, 18.0)));
        }
        edges.emplace_back(count, std::move(made));
        return edges.back().second;
    }

    void step(int count, double smoothing, double sensitivity, double dt) {
        float samples[N];
        std::memcpy(samples, chunks[0].data(), sizeof(float) * (N / 2));
        std::memcpy(samples + N / 2, chunks[1].data(), sizeof(float) * (N / 2));
        if (fed != used) {
            used = fed;
            unheard = 0.0;
        } else {
            unheard += dt;
            if (unheard > 0.25) std::fill(samples, samples + N, 0.0f);  // the sound went: fall away
        }
        const float gain = static_cast<float>(std::max(0.05, sensitivity));
        const Edges &e = edges_for(count);
        float windowed[N];
        for (int i = 0; i < N; ++i) windowed[i] = samples[i] * window[static_cast<size_t>(i)];
        fft.magnitudes(windowed, mags.data());
        const float scale = static_cast<float>(4.0 / N);
        std::vector<float> target(static_cast<size_t>(count));
        for (int b = 0; b < count; ++b) {
            const int lo = e.edges[static_cast<size_t>(b)];
            const int hi = b + 1 < count ? e.edges[static_cast<size_t>(b) + 1] : BINS;  // reduceat: the last runs on
            float peak = 0.0f;
            for (int k = lo; k < std::max(hi, lo + 1); ++k)
                peak = std::max(peak, static_cast<float>(mags[static_cast<size_t>(k)]) * scale * gain);
            const float db = 20.0f * std::log10(peak + 1e-9f) + e.tilt[static_cast<size_t>(b)];
            target[static_cast<size_t>(b)] = std::clamp((db + 60.0f) / 60.0f, 0.0f, 1.0f);
        }
        float now_wave[WAVE];
        for (int i = 0; i < WAVE; ++i) {
            const float *s = samples + (N - 1024) + i * 4;
            const float mean = (((s[0] + s[1]) + s[2]) + s[3]) / 4.0f;
            now_wave[i] = std::clamp(mean * gain * 2.0f, -1.0f, 1.0f);
        }
        if (static_cast<int>(bands.size()) != count) bands.assign(static_cast<size_t>(count), 0.0f);
        if (wave.size() != WAVE) wave.assign(WAVE, 0.0f);
        const float hold = static_cast<float>(std::pow(std::clamp(smoothing, 0.0, 0.95), std::max(dt * FPS, 0.0)));
        for (int b = 0; b < count; ++b) {
            float &v = bands[static_cast<size_t>(b)];
            const float t = target[static_cast<size_t>(b)];
            v = t > v ? v + (t - v) * 0.7f : t + (v - t) * hold;
        }
        for (int i = 0; i < WAVE; ++i) {
            float &v = wave[static_cast<size_t>(i)];
            v = now_wave[i] + (v - now_wave[i]) * hold * 0.6f;
        }
    }
};

// Painter: the looks, drawn straight into premultiplied BGRA pictures.
struct Painter {
    std::vector<float> cols = std::vector<float>(W), rows = std::vector<float>(H);
    std::vector<bool> outside = std::vector<bool>(W);

    // The look's pictures made once for its colours (as Painter._sprite), dropped when the look changes.
    std::vector<int64_t> key;
    Frame sprite_a, sprite_b;
    // The circle's: per pixel of its square, the radius, which bar, whether past the ring, and three colourings.
    int circle_size = 0;
    std::vector<float> radius;
    std::vector<int> bar_of;
    std::vector<uint8_t> beyond;
    std::vector<uint32_t> bars_px, soft_px, ring_px;

    Painter() {
        for (int x = 0; x < W; ++x) {
            cols[static_cast<size_t>(x)] = static_cast<float>(x) + 0.5f;
            outside[static_cast<size_t>(x)] = cols[static_cast<size_t>(x)] < PAD || cols[static_cast<size_t>(x)] > W - PAD;
        }
        for (int y = 0; y < H; ++y) rows[static_cast<size_t>(y)] = static_cast<float>(y) + 0.5f;
    }

    bool same_key(const std::vector<int64_t> &k) {
        if (k == key) return true;
        key = k;
        sprite_a.clear();
        sprite_b.clear();
        radius.clear();
        return false;
    }

    // Painter.paste: the sprite copied into the frame between top and bottom (one each per column), soft at both ends.
    void paste(uint8_t *frame, const Frame &sprite, const std::vector<double> &top_in,
               const std::vector<double> &bottom_in) {
        std::vector<float> top(W), bottom(W);
        float top_min = H, bottom_max = 0;
        for (int x = 0; x < W; ++x) {
            top[static_cast<size_t>(x)] = static_cast<float>(std::clamp(top_in[static_cast<size_t>(x)], 0.0, double(H)));
            bottom[static_cast<size_t>(x)] =
                static_cast<float>(std::clamp(bottom_in[static_cast<size_t>(x)], 0.0, double(H)));
            if (outside[static_cast<size_t>(x)]) bottom[static_cast<size_t>(x)] = top[static_cast<size_t>(x)];
            top_min = std::min(top_min, top[static_cast<size_t>(x)]);
            bottom_max = std::max(bottom_max, bottom[static_cast<size_t>(x)]);
        }
        const int r0 = static_cast<int>(top_min), r1 = std::min(H, static_cast<int>(std::ceil(bottom_max)));
        if (r1 <= r0) return;
        auto *out = reinterpret_cast<uint32_t *>(frame);
        auto *in = reinterpret_cast<const uint32_t *>(sprite.data());
        for (int y = r0; y < r1; ++y) {
            const float ry = rows[static_cast<size_t>(y)];
            for (int x = 0; x < W; ++x) {
                if (ry >= top[static_cast<size_t>(x)] && ry < bottom[static_cast<size_t>(x)])
                    out[static_cast<size_t>(y) * W + x] = in[static_cast<size_t>(y) * W + x];
            }
        }
        for (int pass = 0; pass < 2; ++pass) {
            for (int x = 0; x < W; ++x) {
                const float t = top[static_cast<size_t>(x)], b = bottom[static_cast<size_t>(x)];
                const int first = static_cast<int>(std::ceil(t - 0.5f));
                const int last = static_cast<int>(std::ceil(b - 0.5f)) - 1;
                if (last < first) continue;
                const int row = pass == 0 ? first - 1 : last + 1;
                const double cover = pass == 0 ? first - static_cast<double>(t) : static_cast<double>(b) - (last + 1);
                if (row < 0 || row >= H || !(cover > 0.02)) continue;
                const double c = std::min(cover, 1.0);
                const uint8_t *s = sprite.data() + (static_cast<size_t>(row) * W + x) * 4;
                uint8_t *d = frame + (static_cast<size_t>(row) * W + x) * 4;
                double src[4];
                for (int k = 0; k < 4; ++k) src[k] = static_cast<double>(static_cast<float>(s[k])) * c;
                const double keep = 1.0 - src[3] / 255.0;
                for (int k = 0; k < 4; ++k) d[k] = to_byte(src[k] + static_cast<double>(static_cast<float>(d[k])) * keep + 0.5);
            }
        }
    }

    // Painter._stroke: the span a line through (column, ys) covers in each column, width pixels thick.
    static void stroke(const std::vector<double> &ys, double width, std::vector<double> &lo, std::vector<double> &hi) {
        const size_t n = ys.size();
        lo.resize(n);
        hi.resize(n);
        for (size_t i = 0; i < n; ++i) {
            const double before = i == 0 ? ys[0] : (ys[i] + ys[i - 1]) / 2;
            const double after = i + 1 == n ? ys[n - 1] : (ys[i + 1] + ys[i]) / 2;
            lo[i] = std::min(ys[i], std::min(before, after)) - width / 2;
            hi[i] = std::max(ys[i], std::max(before, after)) + width / 2;
        }
    }

    void paint(uint8_t *frame, const sc_visual_look &look, const std::vector<float> &bands,
               const std::vector<float> &wave) {
        std::memset(frame, 0, static_cast<size_t>(W) * H * 4);
        const Rgba base{look.color[0], look.color[1], look.color[2], look.color[3]};
        const Rgba peak{look.peak[0], look.peak[1], look.peak[2], look.peak[3]};
        const double floor_y = H - PAD;
        auto bits = [](double v) {
            int64_t b;
            std::memcpy(&b, &v, sizeof b);
            return b;
        };
        std::vector<int64_t> k{look.style, look.style == SC_VISUAL_BARS || look.style == SC_VISUAL_CIRCLE
                                               ? static_cast<int64_t>(bands.size()) : 0};
        for (int i = 0; i < 4; ++i) {
            k.push_back(bits(look.color[i]));
            k.push_back(bits(look.peak[i]));
        }
        const bool have = same_key(k);
        std::vector<double> top, bottom;
        if (look.style == SC_VISUAL_CIRCLE) {
            circle(frame, bands, base, peak, have);
        } else if (look.style == SC_VISUAL_WAVE) {
            if (!have) sprite_a = gradient({{PAD, premul(peak)}, {H / 2.0, premul(base)}, {floor_y, premul(peak)}});
            const auto xp = linspace(PAD, W - PAD, static_cast<int>(wave.size()));
            std::vector<double> fp(wave.size());
            for (size_t i = 0; i < wave.size(); ++i)
                fp[i] = static_cast<double>(200.0f - wave[i] * static_cast<float>(H / 2 - PAD * 2));
            std::vector<double> ys(W);
            for (int x = 0; x < W; ++x)
                ys[static_cast<size_t>(x)] = interp(cols[static_cast<size_t>(x)], xp.data(), fp.data(),
                                                    static_cast<int>(fp.size()));
            stroke(ys, 5.0, top, bottom);
            paste(frame, sprite_a, top, bottom);
        } else if (look.style == SC_VISUAL_LINE) {
            if (!have) {
                sprite_a = gradient({{PAD, premul(peak, 0.5)}, {floor_y, premul(base, 0.0)}});
                sprite_b = gradient({{PAD, premul(peak)}, {floor_y, premul(base)}});
            }
            const int n = static_cast<int>(bands.size());
            std::vector<double> ys(W);
            for (int x = 0; x < W; ++x) {
                float at = (cols[static_cast<size_t>(x)] - static_cast<float>(PAD)) / static_cast<float>(W - 2 * PAD) *
                           static_cast<float>(n - 1);
                at = std::clamp(at, 0.0f, static_cast<float>(n - 1));
                const int i = std::min(static_cast<int>(at), n - 2);
                double t = static_cast<double>(at) - i;
                t = t * t * (3 - 2 * t);
                const double v = bands[static_cast<size_t>(i)] * (1 - t) + bands[static_cast<size_t>(i) + 1] * t;
                ys[static_cast<size_t>(x)] = floor_y - 3 - v * (floor_y - PAD - 6);
            }
            paste(frame, sprite_a, ys, std::vector<double>(W, static_cast<double>(static_cast<float>(floor_y))));
            stroke(ys, 5.0, top, bottom);
            paste(frame, sprite_b, top, bottom);
        } else {  // bars: pills from the floor up, the tips lighting up toward the peak colour
            const int count = static_cast<int>(bands.size());
            const double slot = (W - PAD * 2) / count;
            const double r = std::max(1.0, slot * 0.34);
            std::vector<int> index(W);
            std::vector<double> dx(W);
            for (int x = 0; x < W; ++x) {
                const float q = floordiv_f(cols[static_cast<size_t>(x)] - static_cast<float>(PAD), static_cast<float>(slot));
                index[static_cast<size_t>(x)] = std::clamp(static_cast<int>(q), 0, count - 1);
                dx[static_cast<size_t>(x)] = std::abs(static_cast<double>(cols[static_cast<size_t>(x)]) -
                                                      (PAD + (index[static_cast<size_t>(x)] + 0.5) * slot));
            }
            if (!have) {
                sprite_a = gradient({{PAD, premul(peak)}, {floor_y, premul(base)}});
                for (int x = 0; x < W; ++x) {
                    const double sides = std::clamp(r + 0.5 - dx[static_cast<size_t>(x)], 0.0, 1.0);
                    if (sides >= 1.0) continue;
                    const float f = static_cast<float>(sides);
                    for (int y = 0; y < H; ++y) {
                        uint8_t *p = sprite_a.data() + (static_cast<size_t>(y) * W + x) * 4;
                        for (int c = 0; c < 4; ++c)
                            p[c] = sides <= 0.0 ? 0 : to_byte(static_cast<float>(p[c]) * f + 0.5f);
                    }
                }
            }
            top.resize(W);
            bottom.resize(W);
            const float most = static_cast<float>(floor_y - PAD);
            for (int x = 0; x < W; ++x) {
                const float height = std::max(static_cast<float>(2 * r), bands[static_cast<size_t>(index[static_cast<size_t>(x)])] * most);
                const double d = std::min(dx[static_cast<size_t>(x)], r);
                const double cap = r - std::sqrt(std::max(0.0, r * r - d * d));
                top[static_cast<size_t>(x)] = static_cast<double>(static_cast<float>(floor_y) - height) + cap;
                bottom[static_cast<size_t>(x)] = floor_y - cap;
            }
            paste(frame, sprite_a, top, bottom);
        }
    }

    // Painter._circle: bars around a ring, growing outward from it, the ring drawn in.
    void circle(uint8_t *frame, const std::vector<float> &bands, const Rgba &base, const Rgba &peak, bool have) {
        const int count = static_cast<int>(bands.size());
        const double r0 = H * 0.26;
        const double most = H / 2.0 - PAD - r0;
        const int size = static_cast<int>(2 * (r0 + most) + 4);
        const int x0 = (W - size) / 2, y0 = (H - size) / 2;
        if (!have || radius.empty()) {
            circle_size = size;
            const size_t n = static_cast<size_t>(size) * size;
            radius.assign(n, 0.0f);
            bar_of.assign(n, 0);
            beyond.assign(n, 0);
            bars_px.assign(n, 0);
            soft_px.assign(n, 0);
            ring_px.assign(n, 0);
            const float off = static_cast<float>(0.5 - size / 2.0);
            const float two_pi = static_cast<float>(2 * PI);
            const float per_bar = static_cast<float>(2 * PI / count);
            const double half = std::max(1.5, std::min(7.0, PI * r0 / count * 0.55));
            const Premul low = premul(base), high = premul(peak);
            for (int y = 0; y < size; ++y) {
                const float dy = static_cast<float>(y) + off;
                for (int x = 0; x < size; ++x) {
                    const float dxp = static_cast<float>(x) + off;
                    const size_t at = static_cast<size_t>(y) * size + x;
                    const float rad = std::hypot(dxp, dy);
                    float turn = std::atan2(dxp, -dy);
                    turn /= two_pi;
                    turn = remainder_f(turn, 1.0f);
                    turn *= static_cast<float>(count);
                    const float nearest = std::nearbyint(turn);
                    bar_of[at] = ((static_cast<int>(nearest) % count) + count) % count;
                    turn -= nearest;
                    turn *= rad;
                    turn *= per_bar;
                    float cover = std::clamp(static_cast<float>(half + 0.5) - std::abs(turn), 0.0f, 1.0f);
                    cover *= std::clamp(rad - static_cast<float>(r0 - 0.5), 0.0f, 1.0f);
                    const float shade = std::clamp((rad - static_cast<float>(r0)) / static_cast<float>(most), 0.0f, 1.0f);
                    const float ringed = std::clamp(2.0f - std::abs(rad - static_cast<float>(r0 - 10)), 0.0f, 1.0f);
                    uint8_t b[4], s[4], g[4];
                    for (int k = 0; k < 4; ++k) {
                        float channel = shade * (high[static_cast<size_t>(k)] - low[static_cast<size_t>(k)]);
                        channel += low[static_cast<size_t>(k)];
                        channel *= cover;
                        b[k] = to_byte(channel + 0.5f);
                        s[k] = to_byte(channel * 0.5f + 0.5f);
                        g[k] = to_byte(ringed * low[static_cast<size_t>(k)] + 0.5f);
                    }
                    std::memcpy(&bars_px[at], b, 4);
                    std::memcpy(&soft_px[at], s, 4);
                    std::memcpy(&ring_px[at], g, 4);
                    radius[at] = rad;
                    beyond[at] = rad >= r0 - 0.5;
                }
            }
        }
        std::vector<float> ends(static_cast<size_t>(count));
        for (int b = 0; b < count; ++b)
            ends[static_cast<size_t>(b)] = static_cast<float>(r0 + 3.0) + bands[static_cast<size_t>(b)] * static_cast<float>(most);
        auto *out = reinterpret_cast<uint32_t *>(frame);
        for (int y = 0; y < size; ++y) {
            for (int x = 0; x < size; ++x) {
                const size_t at = static_cast<size_t>(y) * size + x;
                float end = ends[static_cast<size_t>(bar_of[at])];
                end -= 0.25f;
                const bool full = radius[at] < end && beyond[at];
                end += 0.5f;
                const bool edge = (radius[at] < end && beyond[at]) != full;
                out[static_cast<size_t>(y0 + y) * W + (x0 + x)] = full ? bars_px[at] : edge ? soft_px[at] : ring_px[at];
            }
        }
    }
};

// What the engine and Python take drawings from: outlives the visualizer while either holds it.
struct Drawings {
    std::mutex lock;
    std::shared_ptr<Frame> newest;
    uint64_t serial = 0;
};

}  // namespace

struct sc_visual {
    sc_mixer *mixer = nullptr;
    int tap = 0;           // the mixer tap heard now (0: none)
    int what = -1;
    std::mutex work;       // one step at a time; guards everything below but `drawings`
    sc_visual_look look{};
    Analyser analyser;
    Painter painter;
    bool still = false;    // the last drawing was of silence
    std::vector<std::shared_ptr<Frame>> frames;
    std::shared_ptr<Drawings> drawings = std::make_shared<Drawings>();
    std::vector<float> block = std::vector<float>(SC_MIXER_BLOCK * 2);
    std::atomic<bool> faulted{false};
    std::atomic<bool> stop{false};
    std::mutex wake_lock;
    std::condition_variable wake;
    std::thread thread;

    sc_visual() {
        look.style = SC_VISUAL_BARS;
        look.bars = 48;
        look.smoothing = 0.6;
        look.sensitivity = 1.0;
        const double c[4] = {0x4f / 255.0, 0xd8 / 255.0, 0x8f / 255.0, 1.0};
        const double p[4] = {0xbf / 255.0, 0xf3 / 255.0, 0xd6 / 255.0, 1.0};
        std::copy(c, c + 4, look.color);
        std::copy(p, p + 4, look.peak);
    }

    void hear() {  // under `work`: every block the tap has waiting
        if (!mixer || !tap) return;
        int64_t index = 0;
        uint64_t lost = 0;
        while (sc_mixer_tap_take(mixer, tap, block.data(), static_cast<int>(block.size()), &index, &lost) == 1)
            analyser.feed(block.data(), SC_MIXER_BLOCK);
    }

    std::shared_ptr<Frame> free_frame() {  // one neither the engine nor Python holds now
        for (auto &f : frames)
            if (f.use_count() == 1) return f;
        if (static_cast<int>(frames.size()) >= MOST_FRAMES) return nullptr;
        frames.push_back(std::make_shared<Frame>(static_cast<size_t>(W) * H * 4));
        return frames.back();
    }

    // AudioVisualizerSource.draw: the next bands and their drawing. 1 drawn, 0 not (silence already drawn).
    int step(double dt) {
        hear();
        const int count = std::clamp(look.bars, 8, 128);
        analyser.step(count, std::clamp(look.smoothing, 0.0, 0.95), std::clamp(look.sensitivity, 0.05, 10.0), dt);
        float loudest = 0.0f, widest = 0.0f;
        for (float v : analyser.bands) loudest = std::max(loudest, v);
        for (float v : analyser.wave) widest = std::max(widest, std::abs(v));
        const bool now_still = loudest < STILL && widest < STILL;
        if (now_still && still) return 0;
        auto frame = free_frame();
        if (!frame) return 0;  // every picture is still being read: this one is skipped
        if (look.mirror)
            painter.paint(frame->data(), look, mirrored(analyser.bands), mirrored(analyser.wave));
        else
            painter.paint(frame->data(), look, analyser.bands, analyser.wave);
        still = now_still;
        std::lock_guard<std::mutex> hold(drawings->lock);
        drawings->newest = frame;
        ++drawings->serial;
        return 1;
    }

    void run() {
        sc::name_thread(L"sprigcore audio visualizer");
        auto last = std::chrono::steady_clock::now();
        const auto every = std::chrono::microseconds(1000000 / FPS);
        try {
            while (!stop) {
                {
                    std::unique_lock<std::mutex> hold(wake_lock);
                    wake.wait_until(hold, last + every, [&] { return stop.load(); });
                }
                if (stop) break;
                const auto now = std::chrono::steady_clock::now();
                const double dt = std::min(std::chrono::duration<double>(now - last).count(), 0.5);
                last = now;
                std::lock_guard<std::mutex> hold(work);
                step(dt);
            }
        } catch (...) {  // crash gate: this visualizer stops drawing; Python's draws again, the app goes on
            faulted = true;
            sc::log_line(SC_LOG_ERROR, "audio visualizer: its thread faulted; it stops drawing");
        }
    }
};

namespace sc {

std::shared_ptr<void> visual_hold(sc_visual *visual) {
    if (!visual) return nullptr;
    return visual->drawings;
}

void visual_due(const std::shared_ptr<void> &held, uint64_t *seen, MediaDue *out) {
    out->what = SC_MEDIA_NO_PICTURE;
    auto *d = static_cast<Drawings *>(held.get());
    if (!d) return;
    std::lock_guard<std::mutex> hold(d->lock);
    if (!d->newest || d->serial == *seen) return;
    *seen = d->serial;
    out->what = SC_MEDIA_PICTURE;
    out->how = sc_upload{};
    out->how.format = SC_FMT_PBGRA;
    out->how.width = W;
    out->how.height = H;
    out->how.stride = W * 4;
    out->data = d->newest->data();
    out->keep = d->newest;
}

}  // namespace sc

SC_API int sc_visual_open(sc_mixer *mixer, int what, int clocked, sc_visual **out) {
    return sc::guarded("sc_visual_open", [&]() -> int {
        if (!out) return sc::fail(SC_ERR_ARGUMENT, "sc_visual_open: nowhere to put it");
        *out = nullptr;
        auto v = std::make_unique<sc_visual>();
        v->mixer = mixer;
        if (mixer && what >= 0) {
            const int rc = sc_mixer_tap_add(mixer, &what, 1, &v->tap);
            if (rc != SC_OK) return rc;
            v->what = what;
        }
        if (clocked) v->thread = std::thread([p = v.get()] { p->run(); });
        *out = v.release();
        return SC_OK;
    });
}

SC_API int sc_visual_listen(sc_visual *visual, int what) {
    return sc::guarded("sc_visual_listen", [&]() -> int {
        if (!visual) return sc::fail(SC_ERR_ARGUMENT, "sc_visual_listen: no visualizer");
        std::lock_guard<std::mutex> hold(visual->work);
        if (what == visual->what || !visual->mixer) return SC_OK;
        if (visual->tap) sc_mixer_tap_remove(visual->mixer, visual->tap);
        visual->tap = 0;
        visual->what = -1;
        if (what >= 0) {
            const int rc = sc_mixer_tap_add(visual->mixer, &what, 1, &visual->tap);
            if (rc != SC_OK) return rc;
            visual->what = what;
        }
        return SC_OK;
    });
}

SC_API int sc_visual_set_look(sc_visual *visual, const sc_visual_look *look) {
    return sc::guarded("sc_visual_set_look", [&]() -> int {
        if (!visual || !look) return sc::fail(SC_ERR_ARGUMENT, "sc_visual_set_look: nothing given");
        if (look->style < SC_VISUAL_BARS || look->style > SC_VISUAL_LINE)
            return sc::fail(SC_ERR_ARGUMENT, "sc_visual_set_look: no such style");
        std::lock_guard<std::mutex> hold(visual->work);
        visual->look = *look;
        visual->look.bars = std::clamp(look->bars, 8, 128);
        visual->still = false;  // the new look is drawn straight away
        return SC_OK;
    });
}

SC_API int sc_visual_push(sc_visual *visual, const float *stereo, int frames) {
    return sc::guarded("sc_visual_push", [&]() -> int {
        if (!visual || !stereo || frames <= 0) return sc::fail(SC_ERR_ARGUMENT, "sc_visual_push: nothing given");
        std::lock_guard<std::mutex> hold(visual->work);
        visual->analyser.feed(stereo, frames);
        return SC_OK;
    });
}

SC_API int sc_visual_step(sc_visual *visual, double dt) {
    return sc::guarded("sc_visual_step", [&]() -> int {
        if (!visual) return sc::fail(SC_ERR_ARGUMENT, "sc_visual_step: no visualizer");
        std::lock_guard<std::mutex> hold(visual->work);
        return visual->step(dt);
    });
}

SC_API int sc_visual_values(sc_visual *visual, float *bands, int bands_room, float *wave, int wave_room) {
    return sc::guarded("sc_visual_values", [&]() -> int {
        if (!visual) return sc::fail(SC_ERR_ARGUMENT, "sc_visual_values: no visualizer");
        std::lock_guard<std::mutex> hold(visual->work);
        const auto &b = visual->analyser.bands;
        const auto &w = visual->analyser.wave;
        if (bands) std::copy_n(b.begin(), std::min<size_t>(b.size(), static_cast<size_t>(std::max(bands_room, 0))), bands);
        if (wave) std::copy_n(w.begin(), std::min<size_t>(w.size(), static_cast<size_t>(std::max(wave_room, 0))), wave);
        return static_cast<int>(b.size());
    });
}

SC_API int sc_visual_draw(sc_visual *visual, const float *bands, int count, const float *wave, int wave_count,
                          uint8_t *out) {
    return sc::guarded("sc_visual_draw", [&]() -> int {
        if (!visual || !bands || !wave || !out || count < 2 || wave_count < 2)
            return sc::fail(SC_ERR_ARGUMENT, "sc_visual_draw: nothing given");
        std::lock_guard<std::mutex> hold(visual->work);
        visual->painter.paint(out, visual->look, std::vector<float>(bands, bands + count),
                              std::vector<float>(wave, wave + wave_count));
        return SC_OK;
    });
}

SC_API int sc_visual_frame(sc_visual *visual, uint64_t *seen, uint8_t *out) {
    return sc::guarded("sc_visual_frame", [&]() -> int {
        if (!visual || !seen || !out) return sc::fail(SC_ERR_ARGUMENT, "sc_visual_frame: nothing given");
        if (visual->faulted) return sc::fail(SC_ERR_INTERNAL, "sc_visual_frame: the visualizer's thread faulted");
        std::shared_ptr<Frame> frame;
        {
            std::lock_guard<std::mutex> hold(visual->drawings->lock);
            if (!visual->drawings->newest || visual->drawings->serial == *seen) return 0;
            frame = visual->drawings->newest;
            *seen = visual->drawings->serial;
        }
        std::memcpy(out, frame->data(), frame->size());
        return 1;
    });
}

SC_API void sc_visual_close(sc_visual *visual) {
    if (!visual) return;
    sc::guarded("sc_visual_close", [&]() -> int {
        {
            std::lock_guard<std::mutex> hold(visual->wake_lock);
            visual->stop = true;
        }
        visual->wake.notify_all();
        if (visual->thread.joinable()) visual->thread.join();
        if (visual->mixer && visual->tap) sc_mixer_tap_remove(visual->mixer, visual->tap);
        delete visual;
        return SC_OK;
    });
}
