// Engine checks that run without Python, on Windows' software renderer (WARP), so CI and every PC can run them.
// Exit code 0 = all passed. Pixel checks against Python's reference drawing live in tests/test_sprigcore.py.
#include "sprigcore.h"

#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <string>
#include <vector>

static int failures = 0;

#define CHECK(cond)                                                                      \
    do {                                                                                 \
        if (!(cond)) {                                                                   \
            std::printf("FAILED %s:%d: %s (%s)\n", __FILE__, __LINE__, #cond, sc_last_error()); \
            ++failures;                                                                  \
        }                                                                                \
    } while (0)

static bool near(int a, int b, int tolerance = 2) { return std::abs(a - b) <= tolerance; }

struct Pixel { int b, g, r, a; };

static Pixel at(const std::vector<uint8_t> &img, int width, int x, int y) {
    const uint8_t *p = &img[(static_cast<size_t>(y) * width + x) * 4];
    return {p[0], p[1], p[2], p[3]};
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
    CHECK(p.b == 255 && near(p.g, 128) && p.r == 0 && p.a == 255);
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
    CHECK(near(p.r, 128) && p.g == 0 && p.a == 255);

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
    sc_device_destroy(nullptr);

    if (failures) {
        std::printf("%d check(s) failed\n", failures);
        return 1;
    }
    std::printf("all engine checks passed\n");
    return 0;
}
