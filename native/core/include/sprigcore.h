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
#define SC_INTERFACE_VERSION 6

enum {
    SC_OK = 0,
    SC_ERR_ARGUMENT = -1,    /* a bad value was passed in */
    SC_ERR_DEVICE = -2,      /* the graphics card or its driver refused */
    SC_ERR_SHADER = -3,      /* a shader didn't compile */
    SC_ERR_OUT_OF_MEMORY = -4,
    SC_ERR_INTERNAL = -5,    /* a bug in the engine; the log says more */
    SC_ERR_FAULTED = -6,     /* this device faulted earlier and is shut off: make a new one (crash gate) */
    SC_ERR_DEVICE_LOST = -7, /* the graphics card was reset or removed: make a new device */
    SC_ERR_TIMEOUT = -8,     /* the engine's frame thread didn't answer in time (it is busy or stuck) */
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

/* A transition between two snapshot scenes (core/scene.py Transition; the stinger and move kinds stay in
 * Python for now). Both scenes are drawn to pictures of their own and mixed the way render/compositor.py's
 * transition shader mixes them. */
enum {
    SC_TRANSITION_FADE = 1,
    SC_TRANSITION_SLIDE = 2,
    SC_TRANSITION_SWIPE = 3,
    SC_TRANSITION_WIPE = 4,
    SC_TRANSITION_LEAF = 5,
    SC_TRANSITION_BLOOM = 6,
};

typedef struct sc_transition {
    int mode;              /* SC_TRANSITION_* */
    float seconds;         /* how long it runs (the engine's own transitions) */
    float direction[2];    /* where slide, swipe and wipe move to: left (-1, 0), right (1, 0), up (0, -1), down (0, 1) */
    float softness;        /* wipe, leaf and bloom edge, 0 .. 1 */
    int reserved;
    uint64_t wipe_map;     /* wipe: a picture set with sc_mask_set, dark parts change first; 0 = a straight edge */
} sc_transition;

/* Draws the change from scene `from` to scene `to` onto the canvas at `progress` (0 .. 1). */
SC_API int sc_canvas_render_transition(sc_canvas *canvas, uint64_t from, uint64_t to, double time,
                                       const sc_transition *how, float progress);

/* How an output smaller than its canvas is scaled (Settings → Video): render/compositor.py SCALE_MODES. */
enum {
    SC_SCALE_BILINEAR = 0,
    SC_SCALE_AREA = 1,
    SC_SCALE_BICUBIC = 2,
    SC_SCALE_LANCZOS = 3
};

/* The canvas scaled to width x height (both even) as NV12 for an encoder, BT.709 limited range like
 * render/compositor.py NV12_FRAGMENT: width x height bytes of Y, then width x height / 2 bytes of CbCr pairs, top
 * row first, into `out` (width * height * 3 / 2 bytes). Waits for the card: tests only. */
SC_API int sc_canvas_read_nv12(sc_canvas *canvas, uint8_t *out, int width, int height, int scale_mode);

/* Checks of the crash gate (tests only): faults inside a device call. kind 1 throws a C++ exception, kind 2
 * writes through a null pointer (an access violation). Either way the call answers SC_ERR_INTERNAL and the
 * device is shut off (SC_ERR_FAULTED from then on); the app goes on. */
SC_API int sc_debug_fault(sc_device *device, int kind);

/* ---- The engine (phase 2) ------------------------------------------------------------------------------------
 *
 * The engine draws on its own: one frame thread, paced by a high-resolution timer, draws every canvas each frame
 * and shows them in preview windows. Python only hands it work. Every call below copies what it is given and
 * returns at once: the frame thread picks the work up at the start of its next frame, so Python never waits on
 * the graphics card and the frame thread never waits on Python.
 *
 * Crash gates: a fault or a lost graphics card while drawing doesn't end the engine. It builds a new device and
 * hands it everything again from its own copies (pictures uploaded with SC_UPLOAD_KEEP, looks, shapes and the
 * snapshot; video sources send their next frame anyway). Five rebuilds within a minute and it stops trying and
 * reports `faulted`, so Python can switch to its own engine. A watchdog thread reports `hung` when one frame
 * has taken more than two seconds. */

typedef struct sc_engine sc_engine;

typedef struct sc_engine_stats {
    uint64_t ticks;          /* frames drawn */
    uint64_t late;           /* frame times missed because drawing ran long (skipped, never queued up) */
    uint64_t presented;      /* frames shown in preview windows */
    uint64_t preview_skips;  /* preview frames skipped because a window wasn't ready (the engine never waits) */
    double time;             /* the engine's clock: seconds since it started */
    double render_ms_last;   /* drawing every canvas and preview, one frame */
    double render_ms_avg;    /* the same, smoothed over about a second */
    double render_ms_max;    /* the longest since the engine started */
    int32_t running;         /* the frame thread is going */
    int32_t faulted;         /* gave up after repeated faults: Python should use its own engine */
    int32_t hung;            /* the frame being drawn has taken more than 2 s (watchdog) */
    int32_t rebuilds;        /* times the device was rebuilt after a fault or a lost graphics card */
    int32_t canvases;
    int32_t previews;
    int32_t sources;
    int32_t reserved;
} sc_engine_stats;

/* sc_engine_source_upload flags. */
enum {
    SC_UPLOAD_KEEP = 1,  /* keep a copy so the picture comes back after a rebuild: for pictures that don't change */
};

#define SC_ENGINE_CANVASES 4  /* canvas slots: the horizontal and vertical canvases and room for more */
#define SC_ENGINE_OUTPUTS 4  /* NV12 outputs per canvas: a recording and streams of different sizes */
#define SC_ENGINE_OUTPUT_QUEUE 4  /* finished output frames kept waiting for Python */
#define SC_ENGINE_SHARE_POOL 5    /* NV12 textures of an output shared on the card: being encoded, waiting, spares */
#define SC_ENGINE_SHARE_QUEUE 2   /* shared frames kept waiting for Python (each holds a texture) */
#define SC_OUTPUT_SHARE 1         /* sc_engine_output_set flag: frames stay on the card as NV12 textures */

/* An engine on adapter `adapter` (-1 = Windows' default) or the software renderer (SC_DEVICE_WARP). It starts
 * drawing at 60 frames a second with no canvases. */
SC_API int sc_engine_create(int adapter, int flags, sc_engine **out);
/* Stops the threads and frees everything. If the frame thread is stuck it is left behind (and its memory with
 * it) rather than waited on forever. */
SC_API void sc_engine_destroy(sc_engine *engine);
SC_API int sc_engine_set_rate(sc_engine *engine, int fps_num, int fps_den);
/* Canvas slot `index` (0 .. SC_ENGINE_CANVASES - 1) at width x height; 0 x 0 removes it. */
SC_API int sc_engine_canvas_set(sc_engine *engine, int index, int width, int height);
/* Which snapshot scene the canvas draws (0 = nothing: see-through). */
SC_API int sc_engine_canvas_show(sc_engine *engine, int index, uint64_t scene);
/* As sc_source_upload, sc_source_forget, sc_look_set, sc_mask_set and sc_snapshot_set, for the engine: the data
 * is copied and drawn from the next frame on. A source uploaded again before the next frame only sends the
 * newest picture. */
SC_API int sc_engine_source_upload(sc_engine *engine, uint64_t id, const uint8_t *data, const sc_upload *how,
                                   int flags);
SC_API int sc_engine_source_forget(sc_engine *engine, uint64_t id);
SC_API int sc_engine_look_set(sc_engine *engine, uint64_t id, const float *table, int n);
SC_API int sc_engine_mask_set(sc_engine *engine, uint64_t id, const uint8_t *rgba, int width, int height, int luma);
SC_API int sc_engine_snapshot_set(sc_engine *engine, const sc_snapshot *snapshot);
/* Shows canvas `canvas` in the window `hwnd` (an HWND), fitted inside it with bars of `background` (0xRRGGBB),
 * at most `max_fps` times a second (0 = every frame). The window must stay open until the preview is detached.
 * Gives the preview's id in `out_id`. */
SC_API int sc_engine_preview_attach(sc_engine *engine, int canvas, void *hwnd, int max_fps, uint32_t background,
                                    int *out_id);
/* Stops showing the preview and waits (up to 2 s) until the engine has let go of the window. */
SC_API int sc_engine_preview_detach(sc_engine *engine, int id);
SC_API int sc_engine_stats_get(sc_engine *engine, sc_engine_stats *out);
/* Waits for a frame drawn after this call (up to 2 s) and copies that canvas into `out` as BGRA, width * 4 bytes a row. The
 * width and height must be the canvas's. Tests and screenshots only: the frame path never copies frames back. */
SC_API int sc_engine_canvas_read(sc_engine *engine, int index, uint8_t *out, int width, int height);
/* Starts a transition on canvas `index` from scene `from` to the scene it shows (sc_engine_canvas_show), timed
 * by the engine from its next frame. from = 0 (or how = NULL) stops one that is running. A transition whose
 * `from` scene is no longer in the snapshot ends at once. */
SC_API int sc_engine_canvas_transition(sc_engine *engine, int index, uint64_t from, const sc_transition *how);
/* Pictures of a canvas for Python's screens: each frame (at most `max_fps` a second, 0 = every frame) the engine
 * draws canvas `index` shrunk to width x height as premultiplied RGBA, the way SprigCast's previews take it, and
 * copies it back without waiting on the graphics card, so it arrives a frame or two later. 0 x 0 stops. */
SC_API int sc_engine_canvas_watch(sc_engine *engine, int index, int width, int height, int max_fps);
/* The newest watched picture of canvas `index`, if it is newer than *seq and width x height: copies it into `out`
 * (width * 4 bytes a row), updates *seq, sets *fresh = 1 and *drawn to the engine time it was drawn. Otherwise
 * *fresh = 0. Never waits. */
SC_API int sc_engine_canvas_take(sc_engine *engine, int index, uint8_t *out, int width, int height, uint64_t *seq,
                                 int *fresh, double *drawn);
/* An output of canvas `index` (a recording or stream), in output slot `slot` (0 .. SC_ENGINE_OUTPUTS - 1): every
 * frame the engine draws the canvas scaled to width x height (both even) as NV12 (sc_canvas_read_nv12) and copies
 * it back without waiting on the graphics card. Finished frames queue up (at most SC_ENGINE_OUTPUT_QUEUE; past
 * that the oldest is dropped and counted) until taken. 0 x 0 stops it and empties its queue.
 * With SC_OUTPUT_SHARE in `flags` nothing is copied back: each frame is drawn into one of SC_ENGINE_SHARE_POOL
 * NV12 textures for an encoder on the same device (sc_engine_output_take_texture). */
SC_API int sc_engine_output_set(sc_engine *engine, int index, int slot, int width, int height, int scale_mode,
                                int flags);
/* The oldest finished frame of an output, if there is one of width x height: copies it into `out` (`bytes` must
 * be width * height * 3 / 2), sets *fresh = 1 and *drawn to when it was drawn in seconds of the performance
 * counter (Python's time.perf_counter() on Windows). Otherwise *fresh = 0. *dropped (optional) gets how many
 * frames this output has dropped so far. Never waits. */
SC_API int sc_engine_output_take(sc_engine *engine, int index, int slot, uint8_t *out, size_t bytes, int *fresh,
                                 double *drawn, uint64_t *dropped);
/* The engine's Direct3D 11 device (an ID3D11Device* with a reference of the caller's, let go with
 * sc_com_release), for an encoder session that takes shared output frames, and how many times the engine has
 * rebuilt its device (a texture from before a rebuild is no use to a session opened after it, nor the other way
 * round). SC_ERR_DEVICE while the engine has no device. The device is safe to use from several threads. */
SC_API int sc_engine_device(sc_engine *engine, void **device, int *rebuilds);
/* The oldest finished frame of a shared output (SC_OUTPUT_SHARE). *state: 1 = a frame: *texture is an NV12
 * ID3D11Texture2D* with a reference of the caller's (sc_com_release), *pool and *entry name it (the same for the
 * same texture as long as the output keeps its size: an encoder registers each once) and *drawn is when it was
 * drawn (performance counter seconds). 0 = none waiting. -1 = this device can't draw NV12 textures: use a plain
 * output. The texture is the caller's until sc_engine_output_give_back. *dropped (optional) as
 * sc_engine_output_take. Never waits. */
SC_API int sc_engine_output_take_texture(sc_engine *engine, int index, int slot, void **texture, uint64_t *pool,
                                         int *entry, double *drawn, int *state, uint64_t *dropped);
/* Gives a shared frame's texture back for the engine to draw into again. A texture of a pool that is gone (the
 * output changed or the device was rebuilt) is simply forgotten. Works on a stopped engine too. */
SC_API int sc_engine_output_give_back(sc_engine *engine, int index, int slot, uint64_t pool, int entry);
/* IUnknown::Release on a pointer one of the calls above gave. */
SC_API void sc_com_release(void *unknown);
/* Checks of the crash gates (tests only), done by the frame thread on its next frame: kind 1 throws a C++
 * exception, 2 writes through a null pointer, 3 acts as if the graphics card was lost (the engine rebuilds),
 * 4 stalls the frame for 3 s (the watchdog reports it hung, then it goes on). */
SC_API int sc_engine_debug_fault(sc_engine *engine, int kind);

#ifdef __cplusplus
}
#endif

#endif
