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

#include <stddef.h>
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
#define SC_INTERFACE_VERSION 42

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
    SC_FMT_NV12 = 8,   /* 1 byte a pixel: Y rows, then half as many rows of U and V interleaved (UVUV...) */
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
    int bg_mode;           /* the Background filter: SC_BACKGROUND_*, 0 = off */
    float bg_blur;         /* blur: 0..1 */
    float bg_edge;         /* how soft the person's edge is: 0..1 */
    uint64_t bg_picture;   /* picture: set with sc_mask_set; 0 = see-through behind the person */
} sc_effects;

/* The Background filter (segmenter.h): the room behind a person found by a small model on the graphics card. */
enum { SC_BACKGROUND_REMOVE = 1, SC_BACKGROUND_BLUR = 2, SC_BACKGROUND_PICTURE = 3 };

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

/* How the Background filter's person finder is doing, on whichever device drew one last (sc_background_info). */
enum {
    SC_BACKGROUND_IDLE = 0,      /* no picture used it in the last 3 seconds */
    SC_BACKGROUND_RUNNING = 1,
    SC_BACKGROUND_NO_CARD = 2,   /* the graphics card has no compute shaders (older than Direct3D 11) */
    SC_BACKGROUND_TOO_SLOW = 3,  /* it took over 40 ms a mask: pictures are drawn as they are */
};
typedef struct sc_background_status {
    int state;               /* SC_BACKGROUND_IDLE ... */
    float gpu_ms;            /* graphics card time a mask, smoothed; < 0 not measured yet */
    float masks_per_second;
} sc_background_status;
SC_API int sc_background_info(sc_background_status *out);

SC_API int sc_source_upload(sc_device *device, uint64_t id, const uint8_t *data, const sc_upload *how);
SC_API int sc_source_forget(sc_device *device, uint64_t id);
/* Source `id` becomes a still copy of what is drawn right now: of source `source`'s picture (scene = 0), or of
 * the snapshot scene `scene` (a sprig) drawn at its own size at `time` (source = 0). One item frozen while its
 * source goes on live everywhere else is drawn from such a copy. Returns 1, and makes nothing, while there is no
 * picture to copy yet. sc_source_forget lets the copy go. */
SC_API int sc_source_hold(sc_device *device, uint64_t id, uint64_t source, uint64_t scene, double time);
/* Source `id` is drawn from a list of other sources' pictures (a store plugin's animation: the art uploaded once
 * as sources of their own, then only where each goes, frame by frame): `items` (as in a snapshot, bottom first;
 * `part` and `effects` are ignored) drawn onto a see-through width x height picture, which is then the source's
 * picture wherever it shows. The list is copied and drawn once, before the next canvas that needs it. A picture
 * uploaded for `id` replaces the list; count < 0 drops it. */
SC_API int sc_source_draws(sc_device *device, uint64_t id, int width, int height, const sc_item *items, int count);
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
    SC_TRANSITION_STINGER = 8,  /* a clip played over the change (7 is the Move, below) */
};
enum { SC_STINGER_MATTE_NONE = 0, SC_STINGER_MATTE_RIGHT = 1, SC_STINGER_MATTE_BELOW = 2 };

typedef struct sc_transition {
    int mode;              /* SC_TRANSITION_* */
    float seconds;         /* how long it runs (the engine's own transitions) */
    float direction[2];    /* where slide, swipe and wipe move to: left (-1, 0), right (1, 0), up (0, -1), down (0, 1) */
    float softness;        /* wipe, leaf and bloom edge, 0 .. 1 */
    int reserved;
    uint64_t wipe_map;     /* wipe: a picture set with sc_mask_set, dark parts change first; 0 = a straight edge */
    uint64_t overlay;      /* stinger: the source showing the clip (a player attached with sc_engine_source_media);
                              0 or no picture yet: it just cuts at cut_seconds */
    float cut_seconds;     /* stinger: when the scenes change under the clip */
    int matte;             /* stinger: SC_STINGER_MATTE_*: a black and white half that says where the new scene shows */
    int matte_only;        /* stinger: draw only the matte's change, not the clip */
    int reserved2;
} sc_transition;

/* Draws the change from scene `from` to scene `to` onto the canvas at `progress` (0 .. 1). */
SC_API int sc_canvas_render_transition(sc_canvas *canvas, uint64_t from, uint64_t to, double time,
                                       const sc_transition *how, float progress);

/* The Move transition (render/move.py): what is in both blooms glides from where it was to where it goes. Python
 * lays both blooms out as pieces and pairs them once; the engine works out every frame's quads (move.cpp) and draws
 * them straight onto the canvas. */
enum { SC_TRANSITION_MOVE = 7 };
enum { SC_MOVE_SMOOTH = 0, SC_MOVE_EASE_OUT = 1, SC_MOVE_EASE_IN = 2, SC_MOVE_LINEAR = 3, SC_MOVE_SPRING = 4 };
enum { SC_MOVE_FADE = 0, SC_MOVE_SCALE = 1, SC_MOVE_SLIDE = 2 };  /* what is in only one of the two blooms */

typedef struct sc_move_piece {
    uint64_t source;       /* the picture, as sc_item.source */
    uint64_t part;         /* nonzero: a sprig's drawing (a snapshot scene), as sc_item.part */
    float quad[4][4];      /* x, y (canvas pixels), u, v: top-left, top-right, bottom-left, bottom-right */
    float opacity;
    int effects;           /* index into the move's effects, -1 = none */
    int paired;            /* a piece of the new bloom: the old piece it moves from (each at most once), -1 = none */
    int reserved;
} sc_move_piece;

typedef struct sc_move {
    const sc_move_piece *pieces;  /* the old bloom's pieces, then the new one's, each bottom first */
    int old_count, new_count;
    const sc_effects *effects;
    int effects_count;
    int easing;            /* SC_MOVE_SMOOTH .. SC_MOVE_SPRING (core/scene.py MOVE_EASINGS order) */
    int unmatched;         /* SC_MOVE_FADE, _SCALE or _SLIDE */
} sc_move;

/* Draws a Move onto the canvas at `progress` (0 .. 1). Sprigs drawn whole come from the snapshot. */
SC_API int sc_canvas_render_move(sc_canvas *canvas, const sc_move *move, double time, float progress);
/* The pieces a Move draws at `progress` on a canvas width x height, bottom first, as sc_move_pieces (paired -1):
 * their count, at most old_count + 2 x new_count, or an error when `capacity` is fewer. */
SC_API int sc_move_frame(const sc_move *move, float progress, int width, int height, sc_move_piece *out,
                         int capacity);

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
    int32_t canvas_draws;    /* canvases drawn so far, over all frames (one nobody takes a picture of isn't drawn);
                                starts again from 0 past 2^31 */
} sc_engine_stats;

/* sc_engine_source_upload flags. */
enum {
    SC_UPLOAD_KEEP = 1,  /* keep a copy so the picture comes back after a rebuild: for pictures that don't change */
};

#define SC_ENGINE_CANVASES 4  /* canvas slots: the horizontal and vertical canvases and room for more */
#define SC_ENGINE_OUTPUTS 4  /* NV12 outputs per canvas: a recording and streams of different sizes */
/* Frames wait for Python's render tick to take them. A tick of 35-40 ms (a heavy bloom, a slow source) left 2-3
 * frames waiting and the oldest went: 482 recording frames over 10 minutes in the first native PC run
 * (2026-11-17). Room for about 100 ms at 60 fps now. */
#define SC_ENGINE_OUTPUT_QUEUE 8  /* finished output frames kept waiting for Python (buffers reused) */
#define SC_ENGINE_SHARE_POOL 9    /* NV12 textures of an output shared on the card: being encoded, waiting, spares */
#define SC_ENGINE_SHARE_QUEUE 6   /* shared frames kept waiting for Python (each holds a texture) */
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
/* As sc_source_hold, on the frame thread: the copy is made on the next frame that has a picture to copy, before
 * that frame's canvases are drawn. After the device was rebuilt it is made again, from the picture then.
 * sc_engine_source_forget lets it go. */
SC_API int sc_engine_source_hold(sc_engine *engine, uint64_t id, uint64_t source, uint64_t scene);
/* Source `id` shows what `media` plays: every frame the engine takes the picture due from the player itself,
 * straight from its decoder thread, so a video's pictures never pass through Python. media = NULL goes back to
 * pictures sent with sc_engine_source_upload (the source keeps the picture it has). The engine keeps the player
 * alive as long as it is attached, and sc_engine_source_forget detaches it. */
struct sc_media;
SC_API int sc_engine_source_media(sc_engine *engine, uint64_t id, struct sc_media *media);
/* Source `id`, shown from a player, is laid out as width x height whatever size the player's pictures are (0 x 0:
 * the player's own size again). One camera opened once for sources asking different modes plays the biggest;
 * each source is laid out at the size it asked for. Stays until changed or sc_engine_source_forget. */
SC_API int sc_engine_source_media_size(sc_engine *engine, uint64_t id, int width, int height);
/* Source `id` shows the pictures another process writes into the named shared memory block `name` (a browser
 * page in SprigCast's browser helper): a 64-byte header (u64 counter, then u32 width, height and slot), then two
 * slots of equal size, each holding one RGBA picture; the writer fills the slot not being read, then the size and
 * slot, and last the counter. Every frame the engine copies a newer picture onto the card itself, so the page's
 * pictures never pass through Python. name = NULL (or empty) goes back to sc_engine_source_upload (the source keeps
 * the picture it has); sc_engine_source_forget detaches it too. */
SC_API int sc_engine_source_shared(sc_engine *engine, uint64_t id, const wchar_t *name);
/* Source `id`'s shared pictures (sc_engine_source_shared) held still (held = 1, a freeze) or shown again (0): while
 * held the engine still takes each new picture, so the writer never waits on it, but the source keeps the one it
 * has. Detaching the block forgets the hold. */
SC_API int sc_engine_source_shared_hold(sc_engine *engine, uint64_t id, int held);
/* Source `id` shows a window or a display, captured with Windows Graphics Capture on the engine's own device: every
 * frame the engine copies the newest picture on the card, so a screen's pictures never pass through memory or
 * Python. `window` is an HWND or `monitor` an HMONITOR (the other 0); both 0 stops the capture (the source keeps the
 * picture it has), and sc_engine_source_forget stops it too. flags: SC_SCREEN_*. interval_ms: at most one picture
 * this often (0 = every one Windows draws). The same values again keep the capture running; new ones start it over.
 * The capture starts on the frame thread: sc_engine_capture_state says how it went. */
enum {
    SC_SCREEN_CURSOR = 1,  /* the mouse pointer drawn in */
    SC_SCREEN_BORDER = 2,  /* Windows' yellow border kept (it can't be turned off before Windows 11) */
    SC_SCREEN_OPAQUE = 4   /* alpha ignored: a game's picture is solid */
};
enum {
    SC_SCREEN_NONE = 0,     /* not asked for */
    SC_SCREEN_STARTING = 1, /* asked for, no picture yet */
    SC_SCREEN_RUNNING = 2,  /* pictures coming */
    SC_SCREEN_ENDED = 3,    /* the window closed or the display went: ask again (a new target) */
    SC_SCREEN_REFUSED = 4   /* this PC can't capture in the engine: capture another way */
};
SC_API int sc_engine_source_capture(sc_engine *engine, uint64_t id, uint64_t window, uint64_t monitor, int flags,
                                    int interval_ms);
/* A capture held still (held = 1, a freeze) or shown again (0): while held the engine still takes Windows'
 * pictures but the source keeps the one it has. Stopping the capture forgets the hold. */
SC_API int sc_engine_source_capture_hold(sc_engine *engine, uint64_t id, int held);
/* How source `id`'s capture is doing: state SC_SCREEN_*, the size of its pictures (0 before the first) and how
 * many it has shown. Any pointer may be NULL. */
SC_API int sc_engine_capture_state(sc_engine *engine, uint64_t id, int *state, int *width, int *height,
                                   uint64_t *frames);
/* Source `id`'s captured picture as BGRA rows (`stride` bytes apart), for a frame Python draws itself. Its size goes
 * to width and height; bgra = NULL asks for the size only. SC_ERR_ARGUMENT when there is no picture yet or it is
 * bigger than max_width x max_height (then width and height say what it needs, and nothing is logged).
 * SC_ERR_TIMEOUT when a frame that never ends holds the engine (0.5 s at most). */
SC_API int sc_engine_capture_read(sc_engine *engine, uint64_t id, uint8_t *bgra, int stride, int max_width,
                                  int max_height, int *width, int *height);
/* Source `id` shows what audio visualizer `visual` draws (sc_visual_open): every frame the engine takes its newest
 * drawing itself, so neither the sound nor the pictures pass through Python. visual = NULL goes back to
 * sc_engine_source_upload (the source keeps the picture it has); sc_engine_source_media and
 * sc_engine_source_forget detach it too. The engine keeps the drawings alive as long as it is attached. */
struct sc_visual;
SC_API int sc_engine_source_visual(sc_engine *engine, uint64_t id, struct sc_visual *visual);
/* As sc_source_draws, for the engine: the newest list is drawn from the next frame on (and again after a rebuild,
 * with the pictures it uses uploaded with SC_UPLOAD_KEEP). A picture uploaded for `id` replaces it. */
SC_API int sc_engine_source_draws(sc_engine *engine, uint64_t id, int width, int height, const sc_item *items,
                                  int count);
/* A browser page drawn on the card by the browser helper (page_texture.cpp): the helper makes a device on the
 * engine's card (its LUID, from sc_d3d_device_luid) and a texture shared as "<block>-tex<n>" with a keyed mutex
 * (key 0: the helper may draw, between sc_page_target_begin and _end; key 1: a picture waits for the engine). In the
 * block's header (sc_engine_source_shared) slot 2 then means "on the card", with n after it; the engine copies the
 * picture (premultiplied RGBA) on the card. */
typedef struct sc_page_target sc_page_target;
SC_API int sc_d3d_device_luid(void *d3d11_device, uint32_t *low, int32_t *high);
/* device = NULL makes a device on the card with that LUID; else the target uses that device (one an earlier target
 * of the same page made, so Qt Quick goes on drawing with the device it started with). */
SC_API int sc_page_target_create(uint32_t luid_low, int32_t luid_high, void *device, int width, int height,
                                 const wchar_t *name, sc_page_target **out);
SC_API int sc_page_target_handles(sc_page_target *target, void **device, void **context, void **texture);
SC_API int sc_page_target_begin(sc_page_target *target, int timeout_ms); /* 1: drawing may start; 0: not yet */
/* Between begin and end: the picture drawn, read back as straight-alpha RGBA rows (for Python's own drawing). */
SC_API int sc_page_target_read(sc_page_target *target, uint8_t *rgba, int stride);
/* Between begin and end: the whole texture one premultiplied colour (a page not drawn yet; tests). */
SC_API int sc_page_target_fill(sc_page_target *target, const float *rgba);
/* publish = 1: the engine may take the picture (key 1); 0: drawn only to be read back, the helper keeps it. */
SC_API int sc_page_target_end(sc_page_target *target, int publish);
SC_API void sc_page_target_destroy(sc_page_target *target);
/* A capture run for another process (capture_feed.cpp): the capture helper program captures a window or a display
 * (one of the two; flags SC_SCREEN_*, interval_ms as sc_engine_source_capture) on a device of its own on the card
 * with that LUID, and shares each picture with the engine: `block` names a small block of shared memory this makes,
 * which the engine is given with sc_engine_source_shared. Returns at once; sc_capture_feed_state says how it is
 * doing (SC_SCREEN_STARTING, _RUNNING with the pictures' size and count, _ENDED: the window closed or the display
 * went, _REFUSED: it can't be captured here). Destroying it ends the capture. */
typedef struct sc_capture_feed sc_capture_feed;
SC_API int sc_capture_feed_create(uint32_t luid_low, int32_t luid_high, uint64_t window, uint64_t monitor, int flags,
                                  int interval_ms, const wchar_t *block, sc_capture_feed **out);
SC_API int sc_capture_feed_state(sc_capture_feed *feed, int *state, int *width, int *height, uint64_t *frames);
SC_API void sc_capture_feed_destroy(sc_capture_feed *feed);
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
/* Starts a Move (sc_move, copied) on canvas `index` from scene `from` to the scene it shows, `seconds` long, timed
 * as sc_engine_canvas_transition. Stopped by sc_engine_canvas_transition with from = 0. */
SC_API int sc_engine_canvas_move(sc_engine *engine, int index, uint64_t from, float seconds, const sc_move *move);
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
/* Links a plain (not SC_OUTPUT_SHARE) output to a writer of its size (writer.cpp): from now on the frame thread
 * hands each finished frame straight to the writer, numbered by when it was drawn: frame n is due at
 * start + n * fps_den / fps_num (performance counter seconds), as Python's recorder numbers them (a draw a little late
 * still gets the next number, a spare draw of a faster canvas is skipped, a hole up to fill_seconds long is filled
 * with the next picture). Nothing goes through the caller per frame. last_pts: the last frame number the
 * writer already has from elsewhere (-1: none). keep = 1 still queues frames for sc_engine_output_take too (other
 * outputs share this one). Linking the same writer again changes start, rate, keep and fill and keeps the
 * numbering. The link ends by itself when the writer refuses (finished or failed), when the output changes, and
 * with sc_engine_output_unlink. The engine keeps a share of the writer, so sc_writer_destroy is safe at any time. */
typedef struct sc_writer sc_writer; /* writer.cpp, below */
typedef struct sc_output_link {
    sc_writer *writer;
    double start;
    int fps_num, fps_den;
    int64_t last_pts;
    int keep;
    double fill_seconds;
} sc_output_link;
SC_API int sc_engine_output_link(sc_engine *engine, int index, int slot, const sc_output_link *link);
/* Ends an output's link (none: nothing to do) after any frame being handed over: *last_pts (optional) gets the last
 * frame number the writer took (-1: none), so whoever feeds it next goes on from there. *linked (optional): 1 if
 * the output was still linked. */
SC_API int sc_engine_output_unlink(sc_engine *engine, int index, int slot, int64_t *last_pts, int *linked);
/* Links an output to the virtual camera instead (interface 22): the frame thread writes each finished frame into
 * the camera's shared block `name` (output/virtualcam.py's layout), in the middle of a width x height frame with black
 * around it when the output is smaller. The engine maps the block itself and unmaps it when the link ends
 * (sc_engine_output_unlink, another link, a new output size), so Python may close its own view at any time. keep: as
 * for a writer. sc_engine_output_camera_frames: the frames written since it was linked (0: not linked). */
SC_API int sc_engine_output_link_camera(sc_engine *engine, int index, int slot, const wchar_t *name, int width,
                                        int height, int keep);
SC_API int sc_engine_output_camera_frames(sc_engine *engine, int index, int slot, uint64_t *frames);
/* IUnknown::Release on a pointer one of the calls above gave. */
SC_API void sc_com_release(void *unknown);
/* Checks of the crash gates (tests only), done by the frame thread on its next frame: kind 1 throws a C++
 * exception, 2 writes through a null pointer, 3 acts as if the graphics card was lost (the engine rebuilds),
 * 4 stalls the frame for 3 s (the watchdog reports it hung, then it goes on). */
SC_API int sc_engine_debug_fault(sc_engine *engine, int kind);

/* ---- Video and sound files (phase 3) -------------------------------------------------------------------------
 *
 * A player for one video, GIF or sound file, the way sources/media.py plays one, on a thread of the engine's
 * own: it decodes a little ahead (a few pictures, about a second of sound) and hands out what is due on the
 * file's own clock, so picture and sound stay together. Nothing here needs Python's lock.
 *
 * It uses the FFmpeg that SprigCast already ships with PyAV, loaded once with sc_media_ffmpeg_load: no second
 * copy. Until that has worked every player call answers SC_ERR_DEVICE and Python plays the file itself.
 *
 * Crash gate: a fault while a file is being read or decoded ends that file ("can't play"), never the app. A
 * player whose thread faulted says so in sc_media_info.faulted and plays nothing more.
 *
 * Every call is safe from any thread. */

typedef struct sc_media sc_media;

enum { SC_MEDIA_PLAYING = 0, SC_MEDIA_PAUSED = 1, SC_MEDIA_STOPPED = 2, SC_MEDIA_ENDED = 3 };

/* sc_media_control: what to do (sources/media.py MediaSource, method for method). */
enum {
    SC_MEDIA_PLAY = 1,        /* from the start when it ended or was stopped, else carry on */
    SC_MEDIA_PAUSE = 2,
    SC_MEDIA_RESTART = 3,
    SC_MEDIA_STOP = 4,        /* stop and show nothing; play starts from the beginning */
    SC_MEDIA_SEEK = 5,        /* value: seconds */
    SC_MEDIA_SLEEP = 6,       /* its bloom went off air: let every picture go; the thread closes the file and ends */
    SC_MEDIA_WAKE = 7,        /* on air again: start over or carry on (restart_on_show) */
    SC_MEDIA_HIDE = 8,        /* its eye went off: pause */
    SC_MEDIA_SHOW = 9,        /* its eye is on again: start over, or carry on */
    SC_MEDIA_RESPEED = 10,    /* the speed changed: decode again from here */
    SC_MEDIA_START = 11,      /* start the decoder thread if none is running. value 1: not while asleep */
    SC_MEDIA_CLOSE = 12,      /* end the thread and let everything go (the source is being removed) */
};

typedef struct sc_media_options {
    int loop;
    int clear_on_end;      /* show nothing once it ends instead of the last picture */
    int restart_on_show;   /* start over each time it comes on screen or is shown again */
    float speed;           /* 0.1 .. 4; sound is resampled to fit, so it plays higher or lower */
} sc_media_options;

typedef struct sc_media_info {
    int state;             /* SC_MEDIA_PLAYING ... */
    int has_video;
    int width, height;     /* the picture's layout size (0 x 0 until the file is open) */
    int hidden;
    int thread;            /* a decoder thread is running */
    int faulted;           /* the decoder thread faulted (crash gate): this player is finished */
    int replaying;         /* a short silent loop (a GIF) is playing from memory, its file closed */
    double position;       /* seconds, within the file when looping */
    double duration;       /* seconds; 0 while unknown */
    double speed;
    uint64_t decoded;      /* pictures decoded so far */
    uint64_t passes;       /* times the file was read through (loops) */
    uint64_t picture_bytes;  /* the largest picture handed out so far: a buffer this big always fits */
    char status[96];       /* "starting", "no file", "ok" or "can't play: ..." */
    int hardware;          /* the pictures now are decoded on the graphics card (sc_media_set_decode) */
} sc_media_info;

/* What sc_media_video_frame answered. */
enum {
    SC_MEDIA_NO_PICTURE = 0,  /* nothing new is due */
    SC_MEDIA_PICTURE = 1,     /* a new picture was copied into the buffer */
    SC_MEDIA_BLANK = 2,       /* show nothing from now on (stopped, or ended with clear_on_end) */
    SC_MEDIA_TOO_SMALL = 3,   /* a picture is due but needs `bytes` bytes: call again with a buffer that big */
};

typedef struct sc_media_picture {
    int format;            /* SC_FMT_I420, SC_FMT_I420A or SC_FMT_RGBA (sc_upload) */
    int width, height;     /* of the data: for I420 the packed rows */
    int stride;
    int matrix;            /* I420: 601, 709 or 2020 */
    int full_range;        /* I420: 1 for full range ("pc"), 0 for limited ("tv") */
    uint64_t bytes;
    double time;           /* where in the timeline the picture belongs, seconds */
} sc_media_picture;

/* Loads FFmpeg from these five library files (UTF-8 paths): libavutil, libavcodec, libavformat, libswscale and
 * libswresample, of the major versions this engine was built for. Loading again once it has worked does nothing. */
SC_API int sc_media_ffmpeg_load(const char *avutil, const char *avcodec, const char *avformat, const char *swscale,
                                const char *swresample);
SC_API int sc_media_ffmpeg_ready(void);
/* Loads FFmpeg's input devices (libavdevice, of the same FFmpeg, UTF-8 path) so a player can show a camera
 * (sc_media_set_device). After sc_media_ffmpeg_load; again does nothing. */
SC_API int sc_media_ffmpeg_load_devices(const char *avdevice);

/* ---- Writing a recording or stream (writer.cpp) -----------------------------------------------------------
 * A file or stream written by a thread of the engine's own with SprigCast's FFmpeg (load it first with
 * sc_media_ffmpeg_load). sc_writer_open returns at once: opening (a stream may take seconds to connect),
 * encoding, muxing and writing all happen on the writer's thread. Pictures are NV12 (`stride` bytes a row, the
 * CbCr rows after the Y rows) with `pts` counted in frames; sound is 48 kHz stereo floats, interleaved, each
 * track's blocks back to back. A picture that finds `queue_frames` waiting is dropped (and counted).
 * sc_writer_finish asks for the rest to be written and the file closed; sc_writer_wait waits for that;
 * sc_writer_destroy gives up whatever is left (a stream stuck connecting stops waiting) and frees it. A fault
 * inside FFmpeg ends the writer (state FAILED, error says why), never the app. */
typedef struct sc_writer_config {
    const char *target;         /* file path or stream URL, UTF-8 */
    const char *format;         /* muxer ("mp4", "matroska", "mov", "flv"), or NULL to go by the target's name */
    const char *video_codec;    /* an FFmpeg encoder: "libx264", "h264_nvenc", "hevc_amf", ... */
    const char *video_options;  /* encoder options, "key=value" lines (separated by \n) */
    int width, height, fps_num, fps_den;
    int video_kbps, keyframe_frames;
    int audio_tracks, audio_kbps; /* AAC tracks (0-6) */
    int queue_frames;             /* pictures that may wait (2-120) */
    const char *format_options;   /* muxer options, "key=value" lines ("movflags=..."), or NULL */
    const char *track_titles;     /* each sound track's title, separated by \n (left out when one track), or NULL */
    int b_frames;                 /* 0-4 */
    /* An ID3D11Device* (the engine's, sc_engine_device): the encoder (NVENC, AMF) takes NV12 textures of that
     * device from an output linked to it (sc_engine_output_link on an SC_OUTPUT_SHARE output); pictures from
     * memory (sc_writer_video) are copied up to the card first. NULL: pictures in memory only. */
    void *d3d11_device;
    /* 1: encode only. The only file written is a small NUT file at `target`, finished once the writer runs: the
     * streams with their codec headers as the encoders made them (a placeholder byte each, for demuxers to open it;
     * for the caller's own muxers to copy). Every encoded packet is taken with sc_writer_packet_take, in the order
     * made (a stream, clips, files written from the same packets). A video encoder started again after a failure
     * (stats.recoveries) puts its codec headers in its keyframes. Keyframes start every keyframe_frames frames of
     * the frame numbers (the first picture at or past each multiple), so writers fed at their own rates key at the
     * same instants. */
    int packets;
} sc_writer_config;
enum { SC_WRITER_OPENING = 0, SC_WRITER_RUNNING = 1, SC_WRITER_FINISHED = 2, SC_WRITER_FAILED = 3 };
/* sc_writer_video / sc_writer_audio results besides SC_OK. LATE: the writer already took a later frame number (two
 * feeds met, as when an output linked to it takes over from Python): never written, never counted as dropped. */
enum { SC_WRITER_DROPPED = 1, SC_WRITER_REFUSED = 2, SC_WRITER_LATE = 3 };
typedef struct sc_writer_stats {
    int state, queued;
    uint64_t frames_in, frames_written, frames_dropped, bytes;
    double write_seconds, longest_write;
    char error[256];
    int64_t last_pts;                          /* the newest frame number taken (-1: none yet) */
    uint64_t frames_late, frames_repeated;     /* SC_WRITER_LATE; frames a filled hole wrote again */
    int on_card;                               /* takes textures now (0 once its encoder left the card) */
    int recoveries;                            /* times the video encoder failed and started again from memory
                                                * (once at most: a second failure fails the writer) */
    double encode_seconds;                     /* time encoding pictures (writing the packets not counted); last, so
                                                * an older engine leaves it 0 */
} sc_writer_stats;
SC_API int sc_writer_open(const sc_writer_config *cfg, sc_writer **out);
SC_API int sc_writer_video(sc_writer *writer, const uint8_t *nv12, int stride, int64_t pts);
/* One picture for frames first .. last (a hole filled with it): one queue place, written last - first + 1 times. */
SC_API int sc_writer_video_span(sc_writer *writer, const uint8_t *nv12, int stride, int64_t first, int64_t last);
SC_API int sc_writer_audio(sc_writer *writer, int track, const float *stereo, int frames);
SC_API int sc_writer_finish(sc_writer *writer);
SC_API int sc_writer_wait(sc_writer *writer, int timeout_ms);
SC_API int sc_writer_stats_get(sc_writer *writer, sc_writer_stats *out);
/* Packets mode (sc_writer_config.packets): one encoded packet. track 0 is the video, 1.. the sound tracks; pts and
 * dts count in tb_num / tb_den seconds. */
typedef struct sc_writer_packet {
    int track, keyframe;
    int64_t pts, dts;
    int tb_num, tb_den;
    int size;
} sc_writer_packet;
/* The oldest packet not taken yet: 1 (its bytes copied to `data`), 0 (none waiting), or SC_ERR_ARGUMENT with
 * info->size set when `capacity` is too small (it stays first). A writer whose packets aren't taken fails once
 * 4096 wait (about a minute). */
SC_API int sc_writer_packet_take(sc_writer *writer, sc_writer_packet *info, uint8_t *data, int capacity);
/* The video bitrate from the next picture on (a stream steering itself); max_kbps / buffer_kbits 0 = unchanged. */
SC_API int sc_writer_set_bitrate(sc_writer *writer, int kbps, int max_kbps, int buffer_kbits);
/* The next picture is a keyframe (a recording starting from these packets, a viewer joining). */
SC_API int sc_writer_keyframe(sc_writer *writer);
SC_API void sc_writer_destroy(sc_writer *writer);

/* ---- A stream sent by the engine (interface 39, writer.cpp) ------------------------------------------------
 * One connection fed by a packets-mode writer: a thread of the engine's own connects to `url` (an RTMP(S) ingest;
 * anything FFmpeg's `format` muxer can write to, a file in tests), and every packet the writer encodes (its video
 * and its first sound track) is packed and sent there without passing through the caller. The packets still wait
 * for sc_writer_packet_take as well (clips, a recording from the same packets).
 * It starts at the first keyframe after it connected (it asks the writer for one), with times counted from that
 * keyframe. With more than max_backlog_s of packets waiting to be sent (counted at `kbps`) it skips packets until
 * fewer than resume_backlog_s wait and a keyframe comes; alone on its writer it asks for that keyframe at once.
 * delay_s holds every packet that long before it may be sent (a stream delay).
 * One connection only: when it drops, the state is FAILED and `error` says why (never the url); the caller
 * destroys it and opens another. A writer that closes ends its links the same way. */
typedef struct sc_link_config {
    const char *url;       /* UTF-8 */
    const char *format;    /* muxer, NULL = "flv" */
    int kbps;              /* the bitrate set, for counting the backlog in seconds */
    double delay_s;
    double max_backlog_s, resume_backlog_s;   /* 0 = 4 and 2 */
    int connect_timeout_ms, send_timeout_ms;  /* 0 = 10000 and 20000 */
    double behind_s;       /* a backlog of this much counts as waiting on the network (0 = 0.25) */
} sc_link_config;
enum { SC_LINK_CONNECTING = 0, SC_LINK_READY = 1, SC_LINK_LIVE = 2, SC_LINK_FINISHED = 3, SC_LINK_FAILED = 4 };
typedef struct sc_link_stats {
    int state;                 /* READY: connected, waiting for its keyframe; LIVE: its first packet went out */
    int congested;             /* skipping packets until it has caught up */
    uint64_t frames, dropped, bytes;          /* video packets sent and skipped, bytes sent */
    uint64_t backlog_bytes;                   /* waiting to be sent (delayed ones not counted) */
    int delayed;                              /* packets held by the delay */
    double send_seconds, longest_send;        /* time inside the muxer and the network */
    double behind_seconds;                    /* time sending with behind_s or more waiting */
    double first_seconds;                     /* the writer's time of the keyframe it started at */
    char error[256];
} sc_link_stats;
typedef struct sc_link sc_link;
SC_API int sc_link_open(sc_writer *writer, const sc_link_config *cfg, sc_link **out);
SC_API int sc_link_stats_get(sc_link *link, sc_link_stats *out);
/* Takes nothing new; what waits (a stream delay's packets too, each at its time) is still sent, then the
 * connection is closed properly and the state is FINISHED. */
SC_API int sc_link_finish(sc_link *link);
/* Closes the connection at once (what waits is given up) and frees it. Waits up to 5 s for its thread; one still
 * stuck in the network after that is left to end by itself. */
SC_API void sc_link_destroy(sc_link *link);

/* ---- A recording written by the engine from a packets-mode writer's packets (interface 40, writer.cpp) ------
 * A thread of the engine's own writes every packet the writer encodes into a file, starting at the next keyframe
 * (it asks the writer for one) with times counted from it: the video and the sound tracks in `tracks` (bit 0 =
 * the first sound track), in that order, a track's title kept when there are several. The packets still wait for
 * sc_writer_packet_take as well.
 * Pause leaves the file open and writes nothing; resume carries on from a keyframe it asks for, as if no time had
 * passed. With split_seconds or split_bytes the recording goes on in "<name> part 2<.ext>", "part 3"... from the
 * first keyframe past either. sc_file_chapter marks a chapter at the newest picture written; a part's chapters
 * are put in the file as it closes (containers that hold them). sc_file_finish asks for a keyframe and ends the
 * file just before it, so nothing still inside the encoder is lost; sc_file_destroy closes what there is at once.
 * A file that can't be written (disk full, drive gone) fails by itself: the writer goes on. */
typedef struct sc_file_config {
    const char *path;            /* UTF-8; its folder exists */
    const char *format;          /* muxer ("matroska", "mp4", ...), NULL = by the path's name */
    const char *format_options;  /* "key=value" lines, or NULL */
    unsigned tracks;             /* sound tracks kept, bit i = track i (0 = the first one only) */
    double split_seconds;        /* 0 = never */
    uint64_t split_bytes;        /* 0 = never */
} sc_file_config;
enum { SC_FILE_WAITING = 0, SC_FILE_WRITING = 1, SC_FILE_PAUSED = 2, SC_FILE_FINISHED = 3, SC_FILE_FAILED = 4 };
typedef struct sc_file_stats {
    int state;                 /* WAITING: no keyframe came yet (nothing written) */
    int parts;                 /* files started so far */
    uint64_t bytes;            /* packet bytes written, all parts */
    uint64_t queued_bytes;     /* waiting for the disk */
    double seconds;            /* into the part being written, of its newest picture */
    double write_seconds, longest_write;
    char error[256];
} sc_file_stats;
typedef struct sc_file sc_file;
SC_API int sc_file_open(sc_writer *writer, const sc_file_config *cfg, sc_file **out);
SC_API int sc_file_pause(sc_file *file);   /* 1 paused, 0 it already was (or the file has ended) */
SC_API int sc_file_resume(sc_file *file);  /* 1 resumed, 0 it wasn't paused */
/* 1 and *seconds (may be NULL) = where in the part being written the mark is; 0 when nothing is being written. */
SC_API int sc_file_chapter(sc_file *file, const char *name, double *seconds);
SC_API int sc_file_finish(sc_file *file);
SC_API int sc_file_stats_get(sc_file *file, sc_file_stats *out);
/* Part `index` (0 ..): its path copied to `path` (UTF-8), its length in seconds and how many chapters it got.
 * SC_ERR_ARGUMENT when there is no such part. */
SC_API int sc_file_part(sc_file *file, int index, char *path, int capacity, double *seconds, int *chapters);
/* Chapter `chapter` of a finished part: its start and end in milliseconds and its title. */
SC_API int sc_file_part_chapter(sc_file *file, int index, int chapter, int64_t *start_ms, int64_t *end_ms,
                                char *title, int capacity);
/* Closes what there is at once (no waiting for a keyframe) and waits up to 10 s for that: 1 closed, 0 still busy
 * on the disk. The numbers and parts can still be read afterwards. */
SC_API int sc_file_stop(sc_file *file);
/* sc_file_stop, then frees it. */
SC_API void sc_file_destroy(sc_file *file);

/* ---- Clips kept by the engine (interface 41, writer.cpp) ----------------------------------------------------
 * A packets-mode writer keeps its last `seconds` of encoded packets itself (0 = none), starting at a video
 * keyframe so a clip plays from its first picture: up to one keyframe interval more than asked, never less
 * (unless less was encoded). The bytes wait in a ring file at `spool_path` (UTF-8; made, and removed when the
 * writer closes; a longer time asked later makes a bigger one beside it) and in memory when that can't be
 * written or spool_path is NULL. */
SC_API int sc_writer_keep(sc_writer *writer, double seconds, const char *spool_path);
/* Seconds of video kept now, and how many packets. */
SC_API int sc_writer_kept(sc_writer *writer, double *seconds, int *packets);
/* 0: packets are no longer queued for sc_writer_packet_take (nobody takes them: the engine sends, writes and
 * keeps them itself) and those waiting are dropped; 1 (as a writer starts): queued again from the next one. */
SC_API int sc_writer_hand_out(sc_writer *writer, int on);
/* What a clip saved now would hold: taken at once (no bytes are read or copied), written later by sc_clip_write
 * on the caller's thread: the video and the sound tracks in `tracks` (bit 0 = the first), with times from the
 * clip's first keyframe. The bytes are read from the ring as it writes; if the ring has gone over them by then
 * (as many seconds again have passed), or the writer closed, it fails. SC_ERR_ARGUMENT from the snapshot: nothing
 * kept yet. */
typedef struct sc_clip sc_clip;
SC_API int sc_clip_snapshot(sc_writer *writer, sc_clip **out);
SC_API int sc_clip_write(sc_clip *clip, const char *path, const char *format, const char *format_options,
                         unsigned tracks);
SC_API void sc_clip_destroy(sc_clip *clip);

/* A player with no file. `playing` = 0 makes it wait for play (a sound file), 1 plays as soon as it has one. */
SC_API int sc_media_create(int playing, sc_media **out);
/* Ends its thread (waits up to 2 s for it; a thread stuck in a file read is left behind) and frees it. */
SC_API void sc_media_destroy(sc_media *media);
SC_API int sc_media_set_options(sc_media *media, const sc_media_options *options);
/* How pictures are made, from the next time the file is opened (call before sc_media_set_file). format: 1 makes
 * every picture SC_FMT_RGBA and 2 SC_FMT_BGRA, with straight alpha, whatever the file holds (a stinger clip, a
 * replay: drawn as they are); 0 as the file has them. max_width, max_height: bigger videos are shrunk to fit
 * while decoding (0 = 1920). grow: 1 also enlarges smaller ones to fit that box. */
SC_API int sc_media_set_picture(sc_media *media, int format, int max_width, int max_height, int grow);
/* hardware: 1 decodes videos on the graphics card where it can (Direct3D 11 on Windows; one device for every
 * player), from the next time the file is opened (call before sc_media_set_file). A file the card can't decode
 * (libvpx's see-through WebM, a codec it lacks) or a PC without it decodes on the CPU, as with 0. */
SC_API int sc_media_set_decode(sc_media *media, int hardware);
/* Another file (UTF-8 path; "" = none) from its start. video: 1 when it should have a picture (told by its
 * name until the file is open). playing: as sc_media_create. */
SC_API int sc_media_set_file(sc_media *media, const char *path, int video, int playing);
/* A camera instead of a file: opened through FFmpeg's input device `format` ("dshow"), trying each of `choices` in
 * turn, one a line: the device's input name, then a tab and its options as "key=value:key=value" (may be empty).
 * Its newest picture is always the one shown (no clock, no end, never a backlog), at most `fps` a second (0 = all
 * it sends; extra MJPEG and raw pictures are skipped before decoding). Unplugged or busy, it is opened again every
 * 2 s; sc_media_info's status says so. mirror: flipped left to right. sc_media_set_picture limits its size and
 * sc_media_set_shrink makes it smaller, as for files. Needs sc_media_ffmpeg_load_devices. */
SC_API int sc_media_set_device(sc_media *media, const char *format, const char *choices, int fps, int mirror);
SC_API int sc_media_set_mirror(sc_media *media, int mirror);
SC_API int sc_media_control(sc_media *media, int what, double value);
/* Shown at most `shrink` of its size anywhere (1, 1/2, 1/4): the next pictures are made that small. */
SC_API int sc_media_set_shrink(sc_media *media, float shrink);
SC_API int sc_media_info_get(sc_media *media, sc_media_info *out);
/* The picture on screen now on the file's clock, if it isn't the one *seen names (start *seen at 0; each taker
 * keeps its own): copied into `out` (`capacity` bytes) and described in *picture, and *seen is updated. Returns
 * SC_MEDIA_NO_PICTURE ... SC_MEDIA_TOO_SMALL, or a negative SC_ERR_*. Never waits for the decoder. */
SC_API int sc_media_video_frame(sc_media *media, uint64_t *seen, sc_media_picture *picture, uint8_t *out,
                                size_t capacity);
/* The next `count` stereo samples (interleaved floats at 48 kHz) due on the file's clock; silence where there is
 * none. asleep: 1 while the source is off air. Never waits for the decoder. */
SC_API int sc_media_read_audio(sc_media *media, float *out, int count, int asleep);
/* A check of the crash gate (tests only): the decoder thread faults at its next step. kind 1 throws a C++
 * exception, kind 2 writes through a null pointer. */
SC_API int sc_media_debug_fault(sc_media *media, int kind);

/* The sound mixer (mixer.cpp, docs/native-engine.md "Sound"): one 48 kHz stereo mix, SC_MIXER_BLOCK samples at a
 * time, on a thread of its own paced by the clock (or by sc_mixer_tick for checks). Never waits on Python. */
typedef struct sc_mixer sc_mixer;
#define SC_MIXER_BLOCK 1024
#define SC_MIXER_TRACKS 8
enum { SC_TRACK_STREAM = 0, SC_TRACK_RECORDING, SC_TRACK_VOD, SC_TRACK_VOICE, SC_TRACK_CLIPS, SC_TRACK_TRACK4,
       SC_TRACK_TRACK5, SC_TRACK_CLEAN };
#define SC_MIXER_MONITOR (-1)      /* a tap entry: the monitor mix (sounds with monitor on) */
#define SC_MIXER_SOURCE_TAP 1000   /* a tap entry SC_MIXER_SOURCE_TAP + id: that sound, after its volume */
#define SC_MIXER_MASTER 0          /* sc_mixer_levels: the stream track; level = the ducking gain now */
typedef struct sc_mixer_source_settings {
    float gain;      /* volume, 1 = as it is */
    int mono;        /* both sides as one */
    float balance;   /* -1 left .. 0 centre .. 1 right */
    int delay_ms;    /* sync offset, 0 .. 2000 */
    uint32_t tracks; /* bit (1 << SC_TRACK_*) per track it goes to */
    int monitor;     /* also in the monitor mix */
    int voice;       /* leads ducking, and is never ducked */
    int bloom_out;   /* sound per bloom: none of its blooms is on air (fades out over 0.25 s) */
} sc_mixer_source_settings;
typedef struct sc_mixer_ducking {
    int enabled;
    float amount_db, threshold_db, attack_ms, release_ms, hold_ms;
} sc_mixer_ducking;
typedef struct sc_mixer_level {
    float peak_db, rms_db; /* the loudest since last asked, and the last block's (after volume and ducking) */
    float level;           /* a sound: its last peak before ducking, after volume (a Ducking filter follows it) */
    uint64_t underruns;    /* a pushed sound: times it ran dry */
} sc_mixer_level;
/* clocked = 1: its own thread mixes a block every 1024 / 48000 s. 0: only sc_mixer_tick mixes (checks). */
SC_API int sc_mixer_create(int clocked, sc_mixer **out);
SC_API void sc_mixer_destroy(sc_mixer *mixer);
SC_API int sc_mixer_source_add(sc_mixer *mixer, const sc_mixer_source_settings *settings, int *id);
SC_API int sc_mixer_source_set(sc_mixer *mixer, int id, const sc_mixer_source_settings *settings);
SC_API int sc_mixer_source_remove(sc_mixer *mixer, int id);
/* Samples (interleaved stereo floats) a sound made: queued (at most 200 ms), played from once a block is ahead. */
SC_API int sc_mixer_source_push(sc_mixer *mixer, int id, const float *stereo, int frames);
/* The sound reads straight from an engine player (NULL: pushed again). Once this returns the mixer no longer
 * reads the old player: unlink before sc_media_destroy. asleep: as sc_media_read_audio. */
SC_API int sc_mixer_source_media(sc_mixer *mixer, int id, sc_media *media, int asleep);
/* A sound's built-in filters (core/filters.py), run in order on its samples before mono, balance and delay. The same
 * filters again with new numbers keep their state. */
enum { SC_FILTER_GAIN = 1, SC_FILTER_NOISE_GATE, SC_FILTER_COMPRESSOR, SC_FILTER_LIMITER, SC_FILTER_INVERT,
       SC_FILTER_DUCKING, SC_FILTER_GRAPH };
typedef struct sc_mixer_filter {
    int kind;     /* SC_FILTER_* */
    int follow;   /* ducking: the sound it ducks under (0: none) */
    float p[6];   /* its settings in filters.py's field order (see mixer.cpp Filter) */
} sc_mixer_filter;
SC_API int sc_mixer_source_filters(sc_mixer *mixer, int id, const sc_mixer_filter *filters, int count);
/* The same, with the text of each SC_FILTER_GRAPH filter (texts[i], NULL for the others): one of FFmpeg's audio
 * filter chains, a filter a line as "name<TAB>args" (core/filters.py FFmpegAudioFilter). Fails, changing nothing,
 * when a chain can't be made: the caller runs that sound's filters itself. Needs sc_mixer_ffmpeg_load_filters. */
SC_API int sc_mixer_source_filters_text(sc_mixer *mixer, int id, const sc_mixer_filter *filters,
                                        const char *const *texts, int count);
/* FFmpeg's libavfilter (PyAV's, beside the libraries sc_media_ffmpeg_load took) and its libavutil, after that. */
SC_API int sc_mixer_ffmpeg_load_filters(const char *avfilter, const char *avutil);
SC_API int sc_mixer_ffmpeg_filters_ready(void);
SC_API int sc_mixer_set_ducking(sc_mixer *mixer, const sc_mixer_ducking *ducking);
/* Push to talk (audio.push_mode): one sound (your mic) is heard only while the key is held (SC_PUSH_TALK), or is
 * silent while it's held (SC_PUSH_MUTE). Letting go takes effect release_ms later (0 .. 2000), so a word's end isn't
 * cut. Each change glides over one block. source 0 or SC_PUSH_OFF: none. The key itself is sc_mixer_push_key
 * (down 1 / up 0), from whatever hears the keyboard; the mixer does the rest per block. */
enum { SC_PUSH_OFF = 0, SC_PUSH_TALK, SC_PUSH_MUTE };
SC_API int sc_mixer_push_set(sc_mixer *mixer, int source, int mode, int release_ms);
SC_API int sc_mixer_push_key(sc_mixer *mixer, int down);
/* An output's queue of finished blocks: each holds `count` entries (SC_TRACK_*, SC_MIXER_MONITOR or
 * SC_MIXER_SOURCE_TAP + id) one after another, SC_MIXER_BLOCK * 2 floats each. At most 64 wait; then the oldest
 * goes and is counted. */
SC_API int sc_mixer_tap_add(sc_mixer *mixer, const int *what, int count, int *id);
SC_API int sc_mixer_tap_remove(sc_mixer *mixer, int id);
/* The oldest waiting block of a tap into `out` (`capacity` floats): returns 1, or 0 when none waits. *index: its
 * number (blocks mixed before it); *dropped: blocks this tap lost so far. */
SC_API int sc_mixer_tap_take(sc_mixer *mixer, int id, float *out, int capacity, int64_t *index, uint64_t *dropped);
/* A tap's blocks go straight into a writer's sound (as sc_writer_audio would, entry i into track i; a tap of one
 * entry into every track), on the mixer's own thread, with no thread of the caller's between. From this call
 * sc_mixer_tap_take gives nothing and blocks wait (at most 64) until sc_mixer_tap_go: the caller first writes what
 * it took before, so no block changes places. writer NULL: taken with sc_mixer_tap_take again. The tap keeps the
 * writer's encoder alive by itself; once the writer finishes or fails its blocks are left out.
 * sc_mixer_tap_pause(1): blocks are left out until (0) (a paused recording). */
SC_API int sc_mixer_tap_writer(sc_mixer *mixer, int id, sc_writer *writer);
SC_API int sc_mixer_tap_go(sc_mixer *mixer, int id);
SC_API int sc_mixer_tap_pause(sc_mixer *mixer, int id, int paused);
/* An unclocked mixer mixes `count` blocks now. */
SC_API int sc_mixer_tick(sc_mixer *mixer, int count);
/* Waits up to timeout_ms for a block numbered after `after` (blocks mixed > after): 1 when there is one, 0 on
 * timeout. *now: blocks mixed so far. */
SC_API int sc_mixer_wait(sc_mixer *mixer, int64_t after, int timeout_ms, int64_t *now);
SC_API int sc_mixer_levels(sc_mixer *mixer, int id, sc_mixer_level *out);

/* Sound capture (capture.cpp): a microphone or desktop sound read with WASAPI on a thread of its own and pushed
 * straight into a mixer's sound (as sc_mixer_source_push), at 48 kHz stereo. It finds its device again by itself
 * (every 2 s) and follows the Windows default. Close it before the mixer or the mixer's sound goes.
 * device: the friendly name (as PortAudio lists it, without " [Loopback]"); NULL or "" = the Windows default;
 * "sprigcast-test-tone" = a 440 Hz tone with no sound card (checks). loopback: desktop sound (an output device;
 * a recording device of that name is read as one). */
typedef struct sc_capture sc_capture;
enum { SC_CAPTURE_STARTING = 0, SC_CAPTURE_CAPTURING, SC_CAPTURE_NOT_FOUND, SC_CAPTURE_LOST, SC_CAPTURE_FAILED };
typedef struct sc_capture_stats {
    int state;            /* SC_CAPTURE_* */
    int channels, rate;   /* what the device gives */
    uint64_t frames;      /* 48 kHz frames pushed so far */
    uint64_t gaps;        /* times Windows said samples were lost */
    wchar_t device[128];  /* the device read now, by name */
    char error[256];      /* why it isn't capturing, in words */
} sc_capture_stats;
SC_API int sc_capture_open(sc_mixer *mixer, int source, const wchar_t *device, int loopback, sc_capture **out);
SC_API int sc_capture_stats_get(sc_capture *capture, sc_capture_stats *out);
SC_API void sc_capture_close(sc_capture *capture);
/* opt_out 1 (the default): a mic the engine opens asks Windows not to turn other apps down for it (communications
 * ducking, IAudioSessionControl2::SetDuckingPreference). Captures opened after the call follow it. */
SC_API int sc_audio_windows_ducking(int opt_out);

/* Sound playback (playback.cpp): one of a mixer's mixes (a track, or the monitor mix) played on a playback device
 * with WASAPI on a thread of its own, so monitoring never passes through Python. At most 120 ms is buffered: older
 * sound is dropped so it never drifts behind. It finds its device again by itself (every 2 s) and follows the
 * Windows default. Close it before the mixer.
 * what: SC_MIXER_MONITOR or a track (SC_TRACK_*). device: the friendly name; NULL or "" = the Windows default;
 * "sprigcast-test-sink" = plays nowhere, in real time (checks). States are SC_CAPTURE_*'s (CAPTURING: playing). */
typedef struct sc_playback sc_playback;
typedef struct sc_playback_stats {
    int state;            /* SC_CAPTURE_* (SC_CAPTURE_CAPTURING = playing) */
    int channels, rate;   /* what the device plays */
    uint64_t frames;      /* device frames played so far */
    uint64_t underruns;   /* times it ran out and played silence */
    uint64_t dropped;     /* 48 kHz frames dropped to stay within 120 ms */
    wchar_t device[128];  /* the device's name */
    char error[256];      /* why it isn't playing, if it isn't */
} sc_playback_stats;
SC_API int sc_playback_open(sc_mixer *mixer, int what, const wchar_t *device, sc_playback **out);
SC_API int sc_playback_stats_get(sc_playback *playback, sc_playback_stats *out);
SC_API void sc_playback_close(sc_playback *playback);

/* Audio visualizer (visual.cpp, the store plugin store/audio_visualizer.py drawn in the engine): bars, a wave, a
 * circle or a line that move with one of a mixer's sounds. A thread of its own takes the sound from a mixer tap,
 * runs a 2048-sample FFT and draws a SC_VISUAL_WIDTH x SC_VISUAL_HEIGHT premultiplied BGRA picture (SC_FMT_PBGRA)
 * at most 30 times a second; once silence has been drawn nothing more is drawn until there is sound again. The
 * same numbers and pixels as audio_visualizer.py's Analyser and Painter. Close it before the mixer.
 * mixer: where it hears (NULL: only sc_visual_push). what: a track (SC_TRACK_*), or SC_MIXER_SOURCE_TAP + a sound
 * (that sound after its volume); < 0 hears nothing. clocked = 0: no thread, only sc_visual_step draws (checks). */
typedef struct sc_visual sc_visual;
#define SC_VISUAL_WIDTH 1280
#define SC_VISUAL_HEIGHT 400
enum { SC_VISUAL_BARS = 0, SC_VISUAL_WAVE, SC_VISUAL_CIRCLE, SC_VISUAL_LINE };
typedef struct sc_visual_look {
    int style;            /* SC_VISUAL_* */
    int bars;             /* 8..128 */
    double smoothing;     /* 0..0.95 */
    double sensitivity;   /* 0.05..10 */
    int mirror;           /* bass in the middle, the same both ways */
    double color[4];      /* r, g, b, a from 0 to 1 */
    double peak[4];       /* the colour the tips light up toward */
} sc_visual_look;
SC_API int sc_visual_open(sc_mixer *mixer, int what, int clocked, sc_visual **out);
SC_API int sc_visual_listen(sc_visual *visual, int what);
SC_API int sc_visual_set_look(sc_visual *visual, const sc_visual_look *look);
/* A block of `frames` stereo samples it hears (with no mixer, or for checks). */
SC_API int sc_visual_push(sc_visual *visual, const float *stereo, int frames);
/* Checks: one step of dt seconds, and its drawing: 1 drawn, 0 silence already drawn (nothing drawn). */
SC_API int sc_visual_step(sc_visual *visual, double dt);
/* Checks: the band heights (0..1) and the wave (-1..1, 256 values) now. Returns how many bands. */
SC_API int sc_visual_values(sc_visual *visual, float *bands, int bands_room, float *wave, int wave_room);
/* Checks: draws these values (already mirrored, if wanted) with the look now into out (SC_VISUAL_WIDTH * 4 a row). */
SC_API int sc_visual_draw(sc_visual *visual, const float *bands, int count, const float *wave, int wave_count,
                          uint8_t *out);
/* The newest drawing if it isn't the one *seen names (then *seen is its number): 1 copied into out
 * (SC_VISUAL_WIDTH * 4 a row), 0 nothing new; SC_ERR_INTERNAL once its thread has faulted. */
SC_API int sc_visual_frame(sc_visual *visual, uint64_t *seen, uint8_t *out);
SC_API void sc_visual_close(sc_visual *visual);

#ifdef __cplusplus
}
#endif

#endif
