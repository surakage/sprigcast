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
#define SC_INTERFACE_VERSION 11

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
/* Source `id` shows what `media` plays: every frame the engine takes the picture due from the player itself,
 * straight from its decoder thread, so a video's pictures never pass through Python. media = NULL goes back to
 * pictures sent with sc_engine_source_upload (the source keeps the picture it has). The engine keeps the player
 * alive as long as it is attached, and sc_engine_source_forget detaches it. */
struct sc_media;
SC_API int sc_engine_source_media(sc_engine *engine, uint64_t id, struct sc_media *media);
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
/* Another file (UTF-8 path; "" = none) from its start. video: 1 when it should have a picture (told by its
 * name until the file is open). playing: as sc_media_create. */
SC_API int sc_media_set_file(sc_media *media, const char *path, int video, int playing);
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

#ifdef __cplusplus
}
#endif

#endif
