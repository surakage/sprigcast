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
 *  - Never throws: C++ exceptions, and Windows faults such as an access violation, stop at this boundary
 *    (crash gate). A device that faulted is shut off and answers SC_ERR_FAULTED from then on, so a bug ends
 *    that device, never the app; Python makes a new one.
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
#define SC_INTERFACE_VERSION 2

enum {
    SC_OK = 0,
    SC_ERR_ARGUMENT = -1,    /* a bad value was passed in */
    SC_ERR_DEVICE = -2,      /* the graphics card or its driver refused */
    SC_ERR_SHADER = -3,      /* a shader didn't compile */
    SC_ERR_OUT_OF_MEMORY = -4,
    SC_ERR_INTERNAL = -5,    /* a bug in the engine; the log says more */
    SC_ERR_FAULTED = -6,     /* this device faulted earlier and is shut off: make a new one (crash gate) */
    SC_ERR_DEVICE_LOST = -7, /* the graphics card was reset or removed: make a new device */
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

/* ---- The compositor (phase 2) -------------------------------------------------------------------------------
 *
 * Python hands the engine pictures (sc_source_upload, later the engine's own sources), colour looks and shape
 * pictures, and a frozen snapshot of what to draw (sc_snapshot_set). The engine keeps its own copy of the
 * snapshot, so drawing never reads Python's objects. It draws the same way render/compositor.py does, shader
 * for shader, so a bloom looks the same on either engine.
 *
 * Ids are 64-bit numbers Python makes from its string ids (sprigcore.key). */

/* How a source's bytes are laid out (render/compositor.py SWIZZLES and planar_layout). */
enum {
    SC_FMT_RGBA = 0,
    SC_FMT_BGRA = 1,
    SC_FMT_RGB = 2,    /* 3 bytes a pixel, opaque */
    SC_FMT_BGR = 3,    /* 3 bytes a pixel, opaque */
    SC_FMT_BGRX = 4,   /* 4 bytes a pixel, the 4th ignored */
    SC_FMT_PBGRA = 5,  /* BGRA with colours already multiplied by alpha */
    SC_FMT_I420 = 6,   /* 1 byte a pixel: Y rows, then half as many rows of U | V side by side */
    SC_FMT_I420A = 7,  /* as I420, then as many alpha rows as Y rows */
};

typedef struct sc_upload {
    int format;            /* SC_FMT_* */
    int width, height;     /* of the data: for I420 the packed rows (picture height * 3 / 2) */
    int stride;            /* bytes per row of the data */
    int layout_width;      /* the source's size in the layout (0 = the picture's own size) */
    int layout_height;
    float yuv[3];          /* I420: Kr, Kb, 1 for full range */
} sc_upload;

typedef struct sc_halo {
    int mode;              /* 1 soft (shadow, glow), 2 hard edge (stroke) */
    float color[3];
    float alpha;
    float radius;          /* canvas pixels */
    float offset[2];       /* canvas pixels */
    float boost;
} sc_halo;

#define SC_MAX_HALOS 4

/* core/filters.py VideoEffects, field for field. */
typedef struct sc_effects {
    float scroll[2];
    float sharpen;
    int key_mode;
    float key_color[3];
    float similarity, smoothness, spill;
    float luma[3];
    float gamma, contrast, brightness, saturation, hue, opacity;
    float tint[3];
    uint64_t lut;          /* a look set with sc_look_set; 0 = none */
    float lut_amount;
    int shape;
    float roundness;
    int stretch;
    float feather, border;
    float border_color[3];
    uint64_t mask;         /* a picture set with sc_mask_set (shape 7) */
    float blur, pixelate, blackout;
    float spin_turns, spin_seconds;
    int spin_run;
    int halo_count;
    sc_halo halos[SC_MAX_HALOS];
} sc_effects;

/* core/scene.py SceneItem, as drawn: a source (or a sprig's picture) placed on a canvas. */
typedef struct sc_item {
    uint64_t source;       /* the picture: a source, or with `part` set, the sprig's own drawing */
    uint64_t part;         /* nonzero: draw the snapshot scene with this id (a sprig) instead of a source */
    float x, y, scale, scale_y, rotation;  /* scale_y < 0: the same as scale */
    int align;             /* index in core/scene.py ALIGNS */
    int crop[4];           /* left, top, right, bottom */
    int bounds;            /* index in BOUNDS: 0 off, 1 fit, 2 fill, 3 stretch */
    float bounds_size[2];
    int bounds_align;
    float fade;            /* 0..1, items fading in or out */
    int effects;           /* index into the snapshot's effects, -1 = none */
} sc_item;

typedef struct sc_scene {
    uint64_t id;
    int width, height;     /* what it is drawn at: the canvas, or a sprig's layout size */
    int first_item;        /* its items in the snapshot's item list, bottom first */
    int item_count;
} sc_scene;

typedef struct sc_snapshot {
    const sc_scene *scenes;
    int scene_count;
    const sc_item *items;
    int item_count;
    const sc_effects *effects;
    int effects_count;
} sc_snapshot;

SC_API int sc_source_upload(sc_device *device, uint64_t id, const uint8_t *data, const sc_upload *how);
SC_API int sc_source_forget(sc_device *device, uint64_t id);
/* A colour look: n x n x n RGB floats, red fastest (core/looks.py). */
SC_API int sc_look_set(sc_device *device, uint64_t id, const float *table, int n);
/* A shape picture: RGBA. luma = 1 when it has no transparency (white keeps, black cuts). */
SC_API int sc_mask_set(sc_device *device, uint64_t id, const uint8_t *rgba, int width, int height, int luma);
/* Replaces what the device draws. The engine copies it: the caller's arrays can go right away. */
SC_API int sc_snapshot_set(sc_device *device, const sc_snapshot *snapshot);
/* Draws the snapshot's scene `scene` onto the canvas (see-through where nothing is), sprigs first. `time` is
 * seconds, for scrolling and barrel rolls. */
SC_API int sc_canvas_render(sc_canvas *canvas, uint64_t scene, double time);

/* Checks of the crash gate (tests only): faults inside a device call. kind 1 throws a C++ exception, kind 2
 * writes through a null pointer (an access violation). Either way the call answers SC_ERR_INTERNAL and the
 * device is shut off (SC_ERR_FAULTED from then on); the app goes on. */
SC_API int sc_debug_fault(sc_device *device, int kind);

#ifdef __cplusplus
}
#endif

#endif
