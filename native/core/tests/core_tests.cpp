// Engine checks that run without Python, on Windows' software renderer (WARP), so CI and every PC can run them.
// Exit code 0 = all passed. Pixel checks against Python's reference drawing live in tests/test_sprigcore.py.
#include "sprigcore.h"

#ifndef NOMINMAX
#  define NOMINMAX
#endif
#include <windows.h>
#include <d3d11.h>

#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <thread>
#include <vector>

static int failures = 0;

#define CHECK(cond)                                                                      \
    do {                                                                                 \
        if (!(cond)) {                                                                   \
            std::printf("FAILED %s:%d: %s (%s)\n", __FILE__, __LINE__, #cond, sc_last_error()); \
            ++failures;                                                                  \
        }                                                                                \
    } while (0)

static bool close_to(int a, int b, int tolerance = 2) { return std::abs(a - b) <= tolerance; }

struct Pixel { int b, g, r, a; };

static Pixel at(const std::vector<uint8_t> &img, int width, int x, int y) {
    const uint8_t *p = &img[(static_cast<size_t>(y) * width + x) * 4];
    return {p[0], p[1], p[2], p[3]};
}

// Polls the engine's numbers until `ok` says yes or `ms` pass.
template <typename F>
static bool wait_until(sc_engine *engine, F ok, int ms, sc_engine_stats &s) {
    for (int waited = 0;; waited += 10) {
        if (sc_engine_stats_get(engine, &s) == SC_OK && ok(s)) return true;
        if (waited >= ms) return false;
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
}

static void print_stats(const char *when, const sc_engine_stats &s) {
    std::printf("engine %s: ticks %llu late %llu presented %llu skips %llu render %.2f/%.2f/%.2f ms running %d "
                "faulted %d hung %d rebuilds %d canvases %d previews %d sources %d\n",
                when, static_cast<unsigned long long>(s.ticks), static_cast<unsigned long long>(s.late),
                static_cast<unsigned long long>(s.presented), static_cast<unsigned long long>(s.preview_skips),
                s.render_ms_last, s.render_ms_avg, s.render_ms_max, s.running, s.faulted, s.hung, s.rebuilds,
                s.canvases, s.previews, s.sources);
}

int main() {
    CHECK(sc_interface_version() == SC_INTERFACE_VERSION);
    std::printf("%s\n", sc_build_info());

    int cards = sc_adapter_count();
    CHECK(cards >= 1);  // WARP's "Microsoft Basic Render Driver" is always listed
    for (int i = 0; i < cards; ++i) {
        sc_adapter_info info;
        CHECK(sc_adapter_info_get(i, &info) == SC_OK);
        std::printf("card %d: %ls vendor 0x%04X, %llu MB dedicated\n", i, info.name, info.vendor_id,
                    static_cast<unsigned long long>(info.dedicated_video_memory >> 20));
    }
    sc_adapter_info info;
    CHECK(sc_adapter_info_get(9999, &info) == SC_ERR_ARGUMENT);
    CHECK(std::string(sc_last_error()).size() > 0);

    {  // The sound mixer: two sounds into the stream track, one at half volume.
        sc_mixer *mixer = nullptr;
        CHECK(sc_mixer_create(0, &mixer) == SC_OK);
        sc_mixer_source_settings one{1.0f, 0, 0.0f, 0, 1u << SC_TRACK_STREAM, 0, 0, 0}, half = one;
        half.gain = 0.5f;
        int a = 0, b = 0, tap = 0, track = SC_TRACK_STREAM;
        CHECK(sc_mixer_source_add(mixer, &one, &a) == SC_OK && sc_mixer_source_add(mixer, &half, &b) == SC_OK);
        CHECK(sc_mixer_tap_add(mixer, &track, 1, &tap) == SC_OK);
        std::vector<float> block(SC_MIXER_BLOCK * 2, 0.25f), out(SC_MIXER_BLOCK * 2);
        CHECK(sc_mixer_source_push(mixer, a, block.data(), SC_MIXER_BLOCK) == SC_OK);
        CHECK(sc_mixer_source_push(mixer, b, block.data(), SC_MIXER_BLOCK) == SC_OK);
        CHECK(sc_mixer_tick(mixer, 1) == SC_OK);
        int64_t index = -1;
        CHECK(sc_mixer_tap_take(mixer, tap, out.data(), static_cast<int>(out.size()), &index, nullptr) == 1);
        CHECK(index == 0 && std::abs(out[0] - 0.375f) < 1e-5f && std::abs(out.back() - 0.375f) < 1e-5f);
        CHECK(sc_mixer_tap_take(mixer, tap, out.data(), static_cast<int>(out.size()), &index, nullptr) == 0);
        CHECK(sc_mixer_source_push(mixer, 999, block.data(), SC_MIXER_BLOCK) == SC_ERR_ARGUMENT);
        sc_mixer_destroy(mixer);
        sc_mixer *clocked = nullptr;  // its own thread mixes, and stops cleanly
        CHECK(sc_mixer_create(1, &clocked) == SC_OK);
        int64_t now = 0;
        CHECK(sc_mixer_wait(clocked, 2, 1000, &now) == 1 && now > 2);
        sc_mixer_destroy(clocked);
    }
    {  // Sound capture: the test tone reaches the mix; a missing device and the default open and close cleanly
       // (CI has no sound card: they only say why they aren't capturing).
        sc_mixer *mixer = nullptr;
        CHECK(sc_mixer_create(0, &mixer) == SC_OK);
        sc_mixer_source_settings one{1.0f, 0, 0.0f, 0, 1u << SC_TRACK_STREAM, 0, 0, 0};
        int sound = 0, tap = 0, track = SC_TRACK_STREAM;
        CHECK(sc_mixer_source_add(mixer, &one, &sound) == SC_OK && sc_mixer_tap_add(mixer, &track, 1, &tap) == SC_OK);
        sc_capture *tone = nullptr, *missing = nullptr, *usual = nullptr;
        CHECK(sc_capture_open(mixer, sound, L"sprigcast-test-tone", 0, &tone) == SC_OK);
        CHECK(sc_capture_open(mixer, sound, L"No such microphone", 0, &missing) == SC_OK);
        CHECK(sc_capture_open(mixer, sound, nullptr, 1, &usual) == SC_OK);
        sc_capture_stats st{};
        for (int i = 0; i < 200 && (sc_capture_stats_get(tone, &st) == SC_OK && st.frames < 2 * SC_MIXER_BLOCK); ++i)
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
        CHECK(st.state == SC_CAPTURE_CAPTURING && st.frames >= 2 * SC_MIXER_BLOCK);
        CHECK(sc_mixer_tick(mixer, 1) == SC_OK);
        std::vector<float> out(SC_MIXER_BLOCK * 2);
        int64_t index = 0;
        CHECK(sc_mixer_tap_take(mixer, tap, out.data(), static_cast<int>(out.size()), &index, nullptr) == 1);
        float peak = 0.0f;
        for (float v : out) peak = std::fmax(peak, std::fabs(v));
        CHECK(peak > 0.2f && peak < 0.26f);
        std::this_thread::sleep_for(std::chrono::milliseconds(300));
        CHECK(sc_capture_stats_get(missing, &st) == SC_OK && st.state != SC_CAPTURE_CAPTURING && st.frames == 0);
        std::printf("capture: a missing device says \"%s\"\n", st.error);
        CHECK(sc_capture_stats_get(usual, &st) == SC_OK);
        std::printf("capture: the default desktop sound: state %d, \"%s\"\n", st.state, st.error);
        sc_capture_close(tone);
        sc_capture_close(missing);
        sc_capture_close(usual);
        sc_mixer_destroy(mixer);
    }

    sc_device *dev = nullptr;
    CHECK(sc_device_create(-1, SC_DEVICE_WARP, &dev) == SC_OK);
    if (!dev) return 1;
    int level = 0;
    CHECK(sc_device_info(dev, &info, &level) == SC_OK);
    CHECK(info.software == 1);
    CHECK(level >= 0xa000);

    sc_canvas *canvas = nullptr;
    CHECK(sc_canvas_create(dev, 0, 10, &canvas) == SC_ERR_ARGUMENT);
    CHECK(canvas == nullptr);
    const int W = 64, H = 36;
    CHECK(sc_canvas_create(dev, W, H, &canvas) == SC_OK);
    if (!canvas) return 1;
    std::vector<uint8_t> img(W * H * 4);

    // Clear: every pixel the colour asked for.
    CHECK(sc_canvas_clear(canvas, 0.0f, 0.5f, 1.0f, 1.0f) == SC_OK);
    CHECK(sc_canvas_read_bgra(canvas, img.data(), W * 4) == SC_OK);
    Pixel p = at(img, W, 10, 10);
    CHECK(p.b == 255 && close_to(p.g, 128) && p.r == 0 && p.a == 255);
    CHECK(sc_canvas_read_bgra(canvas, img.data(), W * 4 - 1) == SC_ERR_ARGUMENT);

    // A solid red 4x4 picture drawn into 16,8 .. 48,24 covers exactly that rectangle.
    std::vector<uint8_t> red(4 * 4 * 4);
    for (size_t i = 0; i < red.size(); i += 4) { red[i] = 0; red[i + 1] = 0; red[i + 2] = 255; red[i + 3] = 255; }
    CHECK(sc_canvas_clear(canvas, 0, 0, 0, 1) == SC_OK);
    CHECK(sc_canvas_draw_bgra(canvas, red.data(), 4, 4, 16, 16, 8, 32, 16, 1.0f) == SC_OK);
    CHECK(sc_canvas_read_bgra(canvas, img.data(), W * 4) == SC_OK);
    p = at(img, W, 30, 15);
    CHECK(p.r == 255 && p.g == 0 && p.b == 0);
    p = at(img, W, 15, 15);
    CHECK(p.r == 0);
    p = at(img, W, 48, 15);
    CHECK(p.r == 0);
    p = at(img, W, 16, 8);
    CHECK(p.r == 255);
    p = at(img, W, 47, 23);
    CHECK(p.r == 255);

    // Half opacity over black: half red.
    CHECK(sc_canvas_clear(canvas, 0, 0, 0, 1) == SC_OK);
    CHECK(sc_canvas_draw_bgra(canvas, red.data(), 4, 4, 16, 0, 0, W, H, 0.5f) == SC_OK);
    CHECK(sc_canvas_read_bgra(canvas, img.data(), W * 4) == SC_OK);
    p = at(img, W, 20, 20);
    CHECK(close_to(p.r, 128) && p.g == 0 && p.a == 255);

    CHECK(sc_canvas_draw_bgra(canvas, red.data(), 4, 4, 8, 0, 0, W, H, 1.0f) == SC_ERR_ARGUMENT);  // stride too small

    // The log has the device line, and draining empties it.
    char buf[4096];
    int n = sc_log_drain(buf, sizeof buf);
    CHECK(n > 0);
    CHECK(std::string(buf, n).find("device:") != std::string::npos);
    while (sc_log_drain(buf, sizeof buf) > 0) {}
    CHECK(sc_log_drain(buf, sizeof buf) == 0);

    sc_canvas_destroy(canvas);
    sc_canvas_destroy(nullptr);
    sc_device_destroy(dev);

    // The crash gate: a fault inside a call shuts that device off and the program carries on.
    for (int kind = 1; kind <= 2; ++kind) {
#ifdef SPRIGCORE_ASAN
        if (kind == 2) break;  // AddressSanitizer stops the program on an access violation, by design
#endif
        sc_device *broken = nullptr;
        CHECK(sc_device_create(-1, SC_DEVICE_WARP, &broken) == SC_OK);
        CHECK(sc_debug_fault(broken, 0) == SC_OK);
        CHECK(sc_debug_fault(broken, kind) == SC_ERR_INTERNAL);
        sc_canvas *after = nullptr;
        CHECK(sc_canvas_create(broken, 8, 8, &after) == SC_ERR_FAULTED);
        CHECK(after == nullptr);
        sc_device_destroy(broken);
    }

    // The compositor: an 8x8 picture of four colours laid out at its own size (texel centres land on pixel
    // centres, so the colours are exact), then the same drawn inside a sprig placed beside it.
    sc_device *d2 = nullptr;
    CHECK(sc_device_create(-1, SC_DEVICE_WARP, &d2) == SC_OK);
    if (!d2) return 1;
    std::vector<uint8_t> quad(8 * 8 * 4);
    for (int y = 0; y < 8; ++y) {
        for (int x = 0; x < 8; ++x) {
            uint8_t *px = &quad[(static_cast<size_t>(y) * 8 + x) * 4];  // RGBA
            const bool right = x >= 4, low = y >= 4;
            px[0] = (!right && !low) || (right && low) ? 255 : 0;
            px[1] = (right != low) && right ? 255 : (right && low ? 255 : 0);
            px[2] = (low && !right) || (right && low) ? 255 : 0;
            px[3] = 255;
        }
    }
    sc_upload how = {};
    how.format = SC_FMT_RGBA;
    how.width = how.height = 8;
    how.stride = 32;
    CHECK(sc_source_upload(d2, 11, quad.data(), &how) == SC_OK);
    sc_item items[2] = {};
    items[0].source = 11;
    items[0].scale = 1;
    items[0].scale_y = -1;
    items[0].fade = 1;
    items[0].effects = -1;
    items[1] = items[0];
    items[1].source = 99;
    items[1].part = 500;  // the sprig below
    items[1].x = 16;
    items[0].align = items[1].align = 0;  // top left
    sc_scene scenes[2] = {{500, 8, 8, 0, 1}, {1, 32, 16, 0, 2}};
    sc_snapshot snap = {scenes, 2, items, 2, nullptr, 0};
    CHECK(sc_snapshot_set(d2, &snap) == SC_OK);
    sc_canvas *c2 = nullptr;
    CHECK(sc_canvas_create(d2, 32, 16, &c2) == SC_OK);
    if (!c2) return 1;
    CHECK(sc_canvas_render(c2, 1, 0.0) == SC_OK);
    std::vector<uint8_t> out(32 * 16 * 4);
    CHECK(sc_canvas_read_bgra(c2, out.data(), 32 * 4) == SC_OK);
    Pixel tl = at(out, 32, 1, 1), br = at(out, 32, 6, 6), sprig = at(out, 32, 17, 1), empty = at(out, 32, 30, 14);
    std::printf("compositor pixels: tl %d,%d,%d,%d br %d,%d,%d,%d sprig %d,%d,%d,%d\n", tl.r, tl.g, tl.b, tl.a,
                br.r, br.g, br.b, br.a, sprig.r, sprig.g, sprig.b, sprig.a);
    CHECK(tl.r == 255 && tl.g == 0 && tl.a == 255);
    CHECK(br.r == 255 && br.g == 255 && br.b == 255);
    CHECK(sprig.r == 255 && sprig.g == 0 && sprig.a == 255);
    CHECK(empty.a == 0);
    sc_scene bad = {2, 8, 8, 1, 5};
    sc_snapshot bad_snap = {&bad, 1, items, 2, nullptr, 0};
    CHECK(sc_snapshot_set(d2, &bad_snap) == SC_ERR_ARGUMENT);
    CHECK(sc_canvas_render(c2, 12345, 0.0) == SC_OK);  // an unknown scene: see-through
    // Transitions: at the start a fade shows the outgoing scene exactly; at the end, the incoming one.
    sc_transition fade = {};
    fade.mode = SC_TRANSITION_FADE;
    fade.seconds = 0.3f;
    std::vector<uint8_t> mixed(32 * 16 * 4);
    CHECK(sc_canvas_render_transition(c2, 1, 12345, 0.0, &fade, 0.0f) == SC_OK);
    CHECK(sc_canvas_read_bgra(c2, mixed.data(), 32 * 4) == SC_OK);
    CHECK(mixed == out);
    CHECK(sc_canvas_render_transition(c2, 1, 12345, 0.0, &fade, 1.0f) == SC_OK);
    CHECK(sc_canvas_read_bgra(c2, mixed.data(), 32 * 4) == SC_OK);
    CHECK(at(mixed, 32, 1, 1).a == 0);
    for (int mode = SC_TRANSITION_FADE; mode <= SC_TRANSITION_BLOOM; ++mode) {
        sc_transition t = fade;
        t.mode = mode;
        t.softness = 0.2f;
        CHECK(sc_canvas_render_transition(c2, 1, 12345, 0.0, &t, 0.5f) == SC_OK);
    }
    fade.mode = 99;
    CHECK(sc_canvas_render_transition(c2, 1, 12345, 0.0, &fade, 0.5f) == SC_ERR_ARGUMENT);

    // NV12 for encoders, BT.709 limited range: red is Y 63, Cb 102, Cr 240; white Y 235; see-through is black.
    CHECK(sc_canvas_render(c2, 1, 0.0) == SC_OK);
    std::vector<uint8_t> nv12(32 * 16 * 3 / 2);
    CHECK(sc_canvas_read_nv12(c2, nv12.data(), 32, 16, SC_SCALE_BILINEAR) == SC_OK);
    const uint8_t *cbcr = nv12.data() + 32 * 16;
    std::printf("nv12: red Y %d Cb %d Cr %d, white Y %d, empty Y %d Cb %d Cr %d\n", nv12[1 * 32 + 1], cbcr[0], cbcr[1],
                nv12[6 * 32 + 6], nv12[14 * 32 + 30], cbcr[7 * 32 + 30], cbcr[7 * 32 + 31]);
    CHECK(close_to(nv12[1 * 32 + 1], 63, 1) && close_to(cbcr[0], 102, 1) && close_to(cbcr[1], 240, 1));
    CHECK(close_to(nv12[6 * 32 + 6], 235, 1));
    CHECK(nv12[14 * 32 + 30] == 16 && cbcr[7 * 32 + 30] == 128 && cbcr[7 * 32 + 31] == 128);
    for (int mode = SC_SCALE_BILINEAR; mode <= SC_SCALE_LANCZOS; ++mode) {  // half size: the corners stay
        std::vector<uint8_t> half(16 * 8 * 3 / 2);
        CHECK(sc_canvas_read_nv12(c2, half.data(), 16, 8, mode) == SC_OK);
        if (!close_to(half[0], 63, 1) || half[7 * 16 + 15] != 16) {
            std::printf("FAILED NV12 scaled with mode %d: Y %d and %d\n", mode, half[0], half[7 * 16 + 15]);
            ++failures;
        }
    }
    CHECK(sc_canvas_read_nv12(c2, nv12.data(), 31, 16, SC_SCALE_BILINEAR) == SC_ERR_ARGUMENT);  // odd
    sc_canvas_destroy(c2);
    sc_device_destroy(d2);
    sc_device_destroy(nullptr);

    // ---- The engine: its own frame thread draws the same picture, shows it in a window, and survives faults.
    sc_engine *eng = nullptr;
    CHECK(sc_engine_create(-1, SC_DEVICE_WARP, &eng) == SC_OK);
    if (!eng) return 1;
    sc_engine_stats st;
    CHECK(sc_engine_set_rate(eng, 30, 1) == SC_OK);
    CHECK(sc_engine_set_rate(eng, 0, 1) == SC_ERR_ARGUMENT);
    CHECK(sc_engine_canvas_set(eng, 9, 32, 16) == SC_ERR_ARGUMENT);
    CHECK(sc_engine_canvas_set(eng, 0, 32, 16) == SC_OK);
    CHECK(sc_engine_source_upload(eng, 11, quad.data(), &how, SC_UPLOAD_KEEP) == SC_OK);
    CHECK(sc_engine_snapshot_set(eng, &snap) == SC_OK);
    CHECK(sc_engine_snapshot_set(eng, &bad_snap) == SC_ERR_ARGUMENT);
    CHECK(sc_engine_canvas_show(eng, 0, 1) == SC_OK);
    std::vector<uint8_t> got(32 * 16 * 4);
    CHECK(sc_engine_canvas_read(eng, 0, got.data(), 16, 32) == SC_ERR_ARGUMENT);  // not its size
    auto engine_draws_it = [&](const char *when) {
        std::fill(got.begin(), got.end(), uint8_t(7));
        int code = sc_engine_canvas_read(eng, 0, got.data(), 32, 16);
        CHECK(code == SC_OK);
        if (code == SC_OK && got != out) {
            std::printf("FAILED the engine's drawing %s differs from the device's\n", when);
            ++failures;
        }
    };
    engine_draws_it("at first");
    {   // A transition run by the engine's own clock: from an unknown scene it ends at once (the canvas as is).
        sc_transition t = {};
        t.mode = SC_TRANSITION_FADE;
        t.seconds = 0.2f;
        CHECK(sc_engine_canvas_transition(eng, 0, 777, &t) == SC_OK);
        engine_draws_it("with a transition from a scene that isn't there");
        t.seconds = 0;
        CHECK(sc_engine_canvas_transition(eng, 0, 1, &t) == SC_ERR_ARGUMENT);
        CHECK(sc_engine_canvas_transition(eng, 0, 0, nullptr) == SC_OK);
    }
    // Watched pictures come back without waiting: RGBA at the size asked, the same picture at full size.
    CHECK(sc_engine_canvas_watch(eng, 0, 32, 16, 0) == SC_OK);
    {
        std::vector<uint8_t> rgba(32 * 16 * 4);
        uint64_t seq = 0;
        int fresh = 0;
        double drawn = -1;
        for (int waited = 0; waited < 2000 && !fresh; waited += 10) {
            CHECK(sc_engine_canvas_take(eng, 0, rgba.data(), 32, 16, &seq, &fresh, &drawn) == SC_OK);
            if (!fresh) std::this_thread::sleep_for(std::chrono::milliseconds(10));
        }
        CHECK(fresh == 1 && seq >= 1 && drawn >= 0);
        bool same = fresh == 1;
        for (size_t i = 0; same && i < rgba.size(); i += 4)
            same = rgba[i] == out[i + 2] && rgba[i + 1] == out[i + 1] && rgba[i + 2] == out[i] && rgba[i + 3] == out[i + 3];
        if (!same) {
            std::printf("FAILED the watched picture differs from the canvas\n");
            ++failures;
        }
        int again = 1;
        const uint64_t had = seq;
        CHECK(sc_engine_canvas_take(eng, 0, rgba.data(), 8, 8, &seq, &again, &drawn) == SC_OK);
        CHECK(again == 0 && seq == had);  // not that size
        CHECK(sc_engine_canvas_watch(eng, 0, 8, 4, 30) == SC_OK);
        fresh = 0;
        for (int waited = 0; waited < 2000 && !fresh; waited += 10) {
            CHECK(sc_engine_canvas_take(eng, 0, rgba.data(), 8, 4, &seq, &fresh, &drawn) == SC_OK);
            if (!fresh) std::this_thread::sleep_for(std::chrono::milliseconds(10));
        }
        CHECK(fresh == 1);
        CHECK(sc_engine_canvas_watch(eng, 0, 0, 0, 0) == SC_OK);
    }
    // Outputs: NV12 frames queue up without waiting on the card, the same as the device's, timed by the
    // performance counter; past the queue's length the oldest are dropped and counted.
    {
        CHECK(sc_engine_output_set(eng, 0, 0, 31, 16, SC_SCALE_BILINEAR, 0) == SC_ERR_ARGUMENT);
        CHECK(sc_engine_output_set(eng, 0, SC_ENGINE_OUTPUTS, 32, 16, SC_SCALE_BILINEAR, 0) == SC_ERR_ARGUMENT);
        CHECK(sc_engine_output_set(eng, 0, 0, 32, 16, 9, 0) == SC_ERR_ARGUMENT);
        CHECK(sc_engine_output_set(eng, 0, 1, 32, 16, SC_SCALE_BILINEAR, 0) == SC_OK);
        std::vector<uint8_t> frame(32 * 16 * 3 / 2);
        int fresh = 0;
        double drawn = -1;
        uint64_t dropped = 99;
        CHECK(sc_engine_output_take(eng, 0, 1, frame.data(), frame.size() - 1, &fresh, &drawn, &dropped) ==
              SC_ERR_ARGUMENT);
        for (int waited = 0; waited < 2000 && !fresh; waited += 10) {
            CHECK(sc_engine_output_take(eng, 0, 1, frame.data(), frame.size(), &fresh, &drawn, &dropped) == SC_OK);
            if (!fresh) std::this_thread::sleep_for(std::chrono::milliseconds(10));
        }
        LARGE_INTEGER now, freq;
        QueryPerformanceCounter(&now);
        QueryPerformanceFrequency(&freq);
        const double clock = static_cast<double>(now.QuadPart) / static_cast<double>(freq.QuadPart);
        CHECK(fresh == 1 && drawn > clock - 5 && drawn <= clock);
        if (fresh && frame != nv12) {
            std::printf("FAILED the engine's NV12 frame differs from the device's\n");
            ++failures;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(500));  // 15 frames at 30 a second, none taken
        int queued = 0;
        do {
            CHECK(sc_engine_output_take(eng, 0, 1, frame.data(), frame.size(), &fresh, &drawn, &dropped) == SC_OK);
            queued += fresh;
        } while (fresh && queued < 100);
        std::printf("output queue: %d waiting, %llu dropped\n", queued, static_cast<unsigned long long>(dropped));
        CHECK(queued >= 1 && queued <= SC_ENGINE_OUTPUT_QUEUE && dropped >= 1);
        CHECK(sc_engine_output_set(eng, 0, 1, 0, 0, SC_SCALE_BILINEAR, 0) == SC_OK);
        CHECK(sc_engine_output_take(eng, 0, 1, frame.data(), 0, &fresh, &drawn, &dropped) == SC_OK);
        CHECK(fresh == 0 && dropped == 0);
    }
    // Shared outputs: frames stay on the card as NV12 textures of a small pool, for an encoder on the engine's
    // device. A texture is the taker's until given back; with all of them out, frames are missed, not overwritten.
    {
        void *device = nullptr;
        int rebuilds = -1;
        CHECK(sc_engine_device(eng, &device, &rebuilds) == SC_OK && device != nullptr && rebuilds == 0);
        CHECK(sc_engine_output_set(eng, 0, 2, 32, 16, SC_SCALE_BILINEAR, SC_OUTPUT_SHARE) == SC_OK);
        struct Held {
            void *texture;
            uint64_t pool;
            int entry;
        };
        std::vector<Held> held;
        int state = 0;
        double drawn = 0;
        uint64_t dropped = 0;
        auto take = [&](int ms) {
            Held h = {nullptr, 0, -1};
            state = 0;
            for (int waited = 0; waited <= ms && state == 0; waited += 10) {
                CHECK(sc_engine_output_take_texture(eng, 0, 2, &h.texture, &h.pool, &h.entry, &drawn, &state,
                                                    &dropped) == SC_OK);
                if (state == 0) std::this_thread::sleep_for(std::chrono::milliseconds(10));
            }
            if (state == 1) held.push_back(h);
            return state == 1;
        };
        take(2000);
        if (state == -1) {
            std::printf("shared outputs: this device can't draw NV12 textures (not checked here)\n");
        } else {
            CHECK(state == 1 && !held.empty() && held[0].texture && held[0].pool >= 1 && drawn > 0);
            if (!held.empty()) {  // the texture holds the same NV12 as the read-back path gives
                auto *d3d = static_cast<ID3D11Device *>(device);
                auto *texture = static_cast<ID3D11Texture2D *>(held[0].texture);
                ID3D11DeviceContext *context = nullptr;
                d3d->GetImmediateContext(&context);
                D3D11_TEXTURE2D_DESC td;
                texture->GetDesc(&td);
                CHECK(td.Format == DXGI_FORMAT_NV12 && td.Width == 32 && td.Height == 16);
                td.Usage = D3D11_USAGE_STAGING;
                td.BindFlags = 0;
                td.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
                ID3D11Texture2D *copy = nullptr;
                D3D11_MAPPED_SUBRESOURCE m;
                if (SUCCEEDED(d3d->CreateTexture2D(&td, nullptr, &copy))) {
                    context->CopyResource(copy, texture);
                    if (SUCCEEDED(context->Map(copy, 0, D3D11_MAP_READ, 0, &m))) {
                        const auto *rows = static_cast<const uint8_t *>(m.pData);
                        bool same = true;
                        for (int row = 0; row < 24 && same; ++row)  // 16 rows of Y, then 8 of CbCr
                            same = std::memcmp(rows + static_cast<size_t>(row) * m.RowPitch, nv12.data() + row * 32, 32) == 0;
                        if (!same) {
                            std::printf("FAILED the shared NV12 texture differs from the read-back frame\n");
                            ++failures;
                        }
                        context->Unmap(copy, 0);
                    } else {
                        std::printf("shared outputs: the NV12 copy can't be read here (pixels not checked)\n");
                    }
                    copy->Release();
                } else {
                    std::printf("shared outputs: no NV12 copy here (pixels not checked)\n");
                }
                context->Release();
            }
            while (take(300)) {}  // keep every texture: the pool runs dry
            std::printf("shared outputs: %d textures out, %llu frames missed\n", static_cast<int>(held.size()),
                        static_cast<unsigned long long>(dropped));
            CHECK(static_cast<int>(held.size()) == SC_ENGINE_SHARE_POOL);
            for (size_t a = 0; a < held.size(); ++a)
                for (size_t b = a + 1; b < held.size(); ++b) CHECK(held[a].entry != held[b].entry);
            CHECK(!take(200) && dropped >= 1);
            const Held back = held[0];
            CHECK(sc_engine_output_give_back(eng, 0, 2, back.pool, back.entry) == SC_OK);
            CHECK(take(2000) && held.back().entry == back.entry && held.back().texture == back.texture);
            CHECK(sc_engine_output_give_back(eng, 0, 2, back.pool, 99) == SC_ERR_ARGUMENT);
        }
        CHECK(sc_engine_output_set(eng, 0, 2, 0, 0, SC_SCALE_BILINEAR, 0) == SC_OK);
        for (const Held &h : held) {
            CHECK(sc_engine_output_give_back(eng, 0, 2, h.pool, h.entry) == SC_OK);  // a pool that's gone: forgotten
            sc_com_release(h.texture);
        }
        sc_com_release(device);
    }
    CHECK(wait_until(eng, [](const sc_engine_stats &s) { return s.ticks >= 3; }, 2000, st));
    print_stats("running", st);
    CHECK(st.running == 1 && st.faulted == 0 && st.canvases == 1 && st.sources == 1);

    // A preview window (hidden; Windows still takes the frames).
    HWND window = CreateWindowExW(0, L"STATIC", L"sprigcore preview check", WS_OVERLAPPEDWINDOW, 0, 0, 200, 120,
                                  nullptr, nullptr, GetModuleHandleW(nullptr), nullptr);
    CHECK(window != nullptr);
    int preview = 0;
    CHECK(sc_engine_preview_attach(eng, 0, nullptr, 0, 0x101820, &preview) == SC_ERR_ARGUMENT);
    if (window) {
        CHECK(sc_engine_preview_attach(eng, 0, window, 0, 0x101820, &preview) == SC_OK);
        int again = 0;
        CHECK(sc_engine_preview_attach(eng, 0, window, 0, 0, &again) == SC_ERR_ARGUMENT);  // one per window
        CHECK(wait_until(eng, [](const sc_engine_stats &s) { return s.presented + s.preview_skips >= 3; }, 3000, st));
        print_stats("previewing", st);
        CHECK(st.previews == 1 && st.faulted == 0);
        CHECK(sc_engine_preview_detach(eng, preview) == SC_OK);
        CHECK(sc_engine_preview_detach(eng, preview) == SC_ERR_ARGUMENT);
        DestroyWindow(window);
    }

    // Crash gates: a C++ exception, a lost card and an access violation inside a frame each rebuild the device,
    // and the kept picture and snapshot come back by themselves.
    int expected = 0;
    for (int kind : {1, 3, 2}) {
#ifdef SPRIGCORE_ASAN
        if (kind == 2) continue;
#endif
#ifndef _MSC_VER
        if (kind == 2) continue;  // only MSVC's /EHa turns the fault into an exception
#endif
        ++expected;
        CHECK(sc_engine_debug_fault(eng, kind) == SC_OK);
        const bool rebuilt = wait_until(eng, [&](const sc_engine_stats &s) { return s.rebuilds >= expected; }, 3000, st);
        CHECK(rebuilt);
        print_stats("after a fault", st);
        CHECK(st.faulted == 0 && st.running == 1);
        engine_draws_it("after a rebuild");
    }
    // A stuck frame: the watchdog says so, and the engine goes on afterwards.
    CHECK(sc_engine_debug_fault(eng, 4) == SC_OK);
    CHECK(wait_until(eng, [](const sc_engine_stats &s) { return s.hung == 1; }, 3000, st));
    CHECK(wait_until(eng, [](const sc_engine_stats &s) { return s.hung == 0; }, 5000, st));
    print_stats("after a stuck frame", st);
    CHECK(st.late > 0 && st.faulted == 0);
    engine_draws_it("after a stuck frame");
    // Faulting over and over: the engine gives up (Python's engine takes over) and says so.
    for (int i = 0; i < 10 && !st.faulted; ++i) {
        const int before = st.rebuilds;
        sc_engine_debug_fault(eng, 3);
        wait_until(eng, [&](const sc_engine_stats &s) { return s.rebuilds > before || s.faulted; }, 3000, st);
    }
    print_stats("after many faults", st);
    CHECK(st.faulted == 1);
    CHECK(sc_engine_source_upload(eng, 11, quad.data(), &how, 0) == SC_ERR_FAULTED);
    CHECK(sc_engine_canvas_read(eng, 0, got.data(), 32, 16) == SC_ERR_FAULTED);
    sc_engine_destroy(eng);
    sc_engine_destroy(nullptr);
    for (int lines; (lines = sc_log_drain(buf, sizeof buf)) > 0;) std::printf("%.*s", lines, buf);

    if (failures) {
        std::printf("%d check(s) failed\n", failures);
        return 1;
    }
    std::printf("all engine checks passed\n");
    return 0;
}
