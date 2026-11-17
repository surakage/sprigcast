/* SprigCast's native engine (sprigcore.dll): a plain C interface.
 *
 * The engine is moving out of Python one part at a time (docs/native-engine.md). Python reaches it through
 * ctypes, which lets go of Python's lock (the GIL) for the whole of every call here, so engine work never
 * waits on the screens or plugins and they never wait on it.
 *
 * Rules every function here keeps:
 *  - Returns SC_OK (0) or a negative SC_ERR_* code; sc_last_error() then says what went wrong, in words,
 *    for the calling thread.
 *  - Never calls back into Python. Messages for the log wait in a queue that Python empties with
 *    sc_log_drain(), so no engine thread ever has to take the GIL.
 *  - Never throws: C++ exceptions stop at this boundary and become SC_ERR_INTERNAL.
 *  - Handles are opaque. Each create has a destroy; destroying NULL does nothing.
 */
#ifndef SPRIGCORE_H
#define SPRIGCORE_H

#include <stdint.h>
#include <wchar.h>

#ifdef _WIN32
#  ifdef SPRIGCORE_BUILD
#    define SC_API __declspec(dllexport)
#  else
#    define SC_API __declspec(dllimport)
#  endif
#else
#  define SC_API
#endif

#ifdef __cplusplus
extern "C" {
#endif

/* Bumped whenever this interface changes in a way Python must know about. */
#define SC_INTERFACE_VERSION 1

enum {
    SC_OK = 0,
    SC_ERR_ARGUMENT = -1,    /* a bad value was passed in */
    SC_ERR_DEVICE = -2,      /* the graphics card or its driver refused */
    SC_ERR_SHADER = -3,      /* a shader didn't compile */
    SC_ERR_OUT_OF_MEMORY = -4,
    SC_ERR_INTERNAL = -5,    /* a bug in the engine; the log says more */
};

enum { SC_LOG_DEBUG = 10, SC_LOG_INFO = 20, SC_LOG_WARNING = 30, SC_LOG_ERROR = 40 };

/* Device flags. */
enum {
    SC_DEVICE_WARP = 1,  /* Windows' software renderer: every PC has it, tests use it */
};

typedef struct sc_adapter_info {
    wchar_t name[128];
    uint32_t vendor_id;     /* 0x10DE NVIDIA, 0x1002 AMD, 0x8086 Intel, 0x1414 Microsoft (software) */
    uint32_t device_id;
    uint64_t dedicated_video_memory;
    uint64_t shared_system_memory;
    int32_t software;       /* 1 for the software renderer */
    int32_t reserved;
} sc_adapter_info;

typedef struct sc_device sc_device;
typedef struct sc_canvas sc_canvas;

SC_API int sc_interface_version(void);
SC_API const char *sc_build_info(void);
SC_API const char *sc_last_error(void);

/* Moves up to `size` bytes of queued log lines into `buffer`, oldest first, each "<level> <message>\n".
 * Returns the bytes written; lines that don't fit stay queued. The queue keeps the newest 512 lines. */
SC_API int sc_log_drain(char *buffer, int size);

/* Graphics cards, in Windows' order (0 = the one Windows prefers). Returns how many there are. */
SC_API int sc_adapter_count(void);
SC_API int sc_adapter_info_get(int index, sc_adapter_info *out);

/* A Direct3D 11 device on adapter `index` (-1 = Windows' default), or the software renderer with
 * SC_DEVICE_WARP. */
SC_API int sc_device_create(int index, int flags, sc_device **out);
SC_API void sc_device_destroy(sc_device *device);
SC_API int sc_device_info(sc_device *device, sc_adapter_info *out, int *feature_level);

/* A canvas: a BGRA picture on the graphics card that pictures are drawn onto. The first steps of the
 * compositor; phase 2 builds blooms on it. */
SC_API int sc_canvas_create(sc_device *device, int width, int height, sc_canvas **out);
SC_API void sc_canvas_destroy(sc_canvas *canvas);
/* Fills the canvas with one colour, each part 0..1, not premultiplied. */
SC_API int sc_canvas_clear(sc_canvas *canvas, float r, float g, float b, float a);
/* Draws a BGRA picture (straight alpha, `stride` bytes per row) into the rectangle x, y, w, h of the canvas,
 * scaled with bilinear filtering and blended "over" what is there at `opacity` (0..1). */
SC_API int sc_canvas_draw_bgra(sc_canvas *canvas, const uint8_t *pixels, int width, int height, int stride,
                               float x, float y, float w, float h, float opacity);
/* Copies the canvas into `out` (BGRA, `stride` bytes per row, at least width * 4). For tests and screenshots:
 * the frame path never copies frames back. */
SC_API int sc_canvas_read_bgra(sc_canvas *canvas, uint8_t *out, int stride);

#ifdef __cplusplus
}
#endif

#endif
