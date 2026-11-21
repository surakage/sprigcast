// FFmpeg as the engine reaches it: SprigCast's own copy (PyAV's libraries), loaded at run time by
// sc_media_ffmpeg_load and called through this table (media.cpp plays files, writer.cpp records and streams).
#pragma once

#if defined(_MSC_VER)
#  pragma warning(push, 0)
#endif
extern "C" {
#include <libavcodec/avcodec.h>
#include <libavformat/avformat.h>
#include <libavutil/avutil.h>
#include <libavutil/channel_layout.h>
#include <libavutil/dict.h>
#include <libavutil/error.h>
#include <libavutil/frame.h>
#include <libavutil/hwcontext.h>
#include <libavutil/pixdesc.h>
#include <libswresample/swresample.h>
#include <libswscale/swscale.h>
}
#if defined(_MSC_VER)
#  pragma warning(pop)
#endif

#include <atomic>
#include <mutex>
#include <string>

// libavfilter's types, only ever handled by pointer (see FilterGraphApi).
struct AVFilterGraph;
struct AVFilterContext;
struct AVFilter;

namespace sc::ffm {

#define SC_FF_FUNCTIONS(X)                                                                                         \
    X(0, avutil_version) X(0, av_frame_alloc) X(0, av_frame_free) X(0, av_frame_unref) X(0, av_strerror)           \
    X(0, av_channel_layout_default) X(0, av_channel_layout_compare)                                                \
    X(0, av_hwdevice_ctx_create) X(0, av_hwdevice_find_type_by_name) X(0, av_hwframe_transfer_data)                \
    X(0, av_hwdevice_ctx_alloc) X(0, av_hwdevice_ctx_init) X(0, av_frame_clone)                \
    X(0, av_buffer_ref) X(0, av_buffer_unref) X(0, av_pix_fmt_desc_get) X(1, avcodec_get_hw_config)                \
    X(1, avcodec_version) X(1, avcodec_find_decoder) X(1, avcodec_find_decoder_by_name)                            \
    X(1, avcodec_alloc_context3) X(1, avcodec_free_context) X(1, avcodec_parameters_to_context)                    \
    X(1, avcodec_open2) X(1, avcodec_send_packet) X(1, avcodec_receive_frame) X(1, avcodec_flush_buffers)          \
    X(1, av_packet_alloc) X(1, av_packet_free) X(1, av_packet_unref)                                               \
    X(2, avformat_version) X(2, avformat_open_input) X(2, avformat_find_stream_info) X(2, avformat_close_input)    \
    X(2, av_read_frame) X(2, av_seek_frame) X(2, av_find_input_format)                                     \
    X(3, swscale_version) X(3, sws_getContext) X(3, sws_scale) X(3, sws_freeContext)                               \
    X(4, swresample_version) X(4, swr_alloc_set_opts2) X(4, swr_init) X(4, swr_convert) X(4, swr_free)             \
    X(4, swr_get_out_samples)                                                                                      \
    X(0, av_dict_parse_string) X(0, av_dict_free) X(0, av_dict_set) X(0, av_dict_get) X(0, av_frame_get_buffer)                      \
    X(0, av_frame_make_writable) X(1, avcodec_find_encoder) X(1, avcodec_find_encoder_by_name)                     \
    X(1, avcodec_send_frame) X(1, avcodec_receive_packet) X(1, avcodec_parameters_from_context) X(1, avcodec_parameters_copy)                    \
    X(1, av_packet_rescale_ts) X(2, avformat_alloc_output_context2) X(2, avformat_new_stream)                      \
    X(2, avio_open2) X(2, avio_closep) X(2, avformat_write_header) X(2, av_interleaved_write_frame)                \
    X(2, av_write_trailer) X(2, avformat_free_context) X(2, avformat_network_init)                                     \
    X(0, av_hwframe_ctx_alloc) X(0, av_hwframe_ctx_init) X(0, av_hwframe_get_buffer) X(0, av_buffer_create) X(0, av_mallocz) X(0, av_realloc)

struct FFmpeg {
#define X(lib, name) decltype(&::name) name = nullptr;
    SC_FF_FUNCTIONS(X)
#undef X
};

extern FFmpeg ff;
extern std::atomic<bool> ff_ready;
extern std::mutex ff_lock;

// FFmpeg's audio filters (libavfilter), for the sound mixer's filters only Python had (equalizer, RNNoise, ...):
// loaded on their own by sc_mixer_ffmpeg_load_filters, after the rest. Declared here, not from its headers (the
// engine is built without them): the graph and its filters are only ever handled by pointer.
struct FilterGraphApi {
    unsigned (*avfilter_version)(void) = nullptr;
    AVFilterGraph *(*avfilter_graph_alloc)(void) = nullptr;
    void (*avfilter_graph_free)(AVFilterGraph **) = nullptr;
    const AVFilter *(*avfilter_get_by_name)(const char *) = nullptr;
    int (*avfilter_graph_create_filter)(AVFilterContext **, const AVFilter *, const char *, const char *,
                                        void *, AVFilterGraph *) = nullptr;
    int (*avfilter_link)(AVFilterContext *, unsigned, AVFilterContext *, unsigned) = nullptr;
    int (*avfilter_graph_config)(AVFilterGraph *, void *) = nullptr;
    int (*av_buffersrc_add_frame_flags)(AVFilterContext *, AVFrame *, int) = nullptr;
    int (*av_buffersink_get_frame)(AVFilterContext *, AVFrame *) = nullptr;
    int (*av_opt_set_int)(void *, const char *, int64_t, int) = nullptr;  // libavutil's
};
extern FilterGraphApi fg;
extern std::atomic<bool> fg_ready;

void *open_library(const char *path);
void *find_symbol(void *library, const char *name);
std::string av_text(int code);

}  // namespace sc::ffm
