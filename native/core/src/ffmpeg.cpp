#include "ffmpeg.h"

#include <cstdio>

#ifdef _WIN32
#  include <windows.h>
#else
#  include <dlfcn.h>
#endif

namespace sc::ffm {

FFmpeg ff;
std::atomic<bool> ff_ready{false};
std::mutex ff_lock;

void *open_library(const char *path) {
#ifdef _WIN32
    int n = MultiByteToWideChar(CP_UTF8, 0, path, -1, nullptr, 0);
    if (n <= 0) return nullptr;
    std::wstring wide(static_cast<size_t>(n), L'\0');
    MultiByteToWideChar(CP_UTF8, 0, path, -1, wide.data(), n);
    // The library's own folder is searched for what it needs (the other FFmpeg libraries sit beside it).
    return LoadLibraryExW(wide.c_str(), nullptr, LOAD_WITH_ALTERED_SEARCH_PATH);
#else
    return dlopen(path, RTLD_NOW | RTLD_LOCAL);
#endif
}

void *find_symbol(void *library, const char *name) {
#ifdef _WIN32
    return reinterpret_cast<void *>(GetProcAddress(static_cast<HMODULE>(library), name));
#else
    return dlsym(library, name);
#endif
}

std::string av_text(int code) {
    char buf[160] = "";
    if (ff.av_strerror) ff.av_strerror(code, buf, sizeof buf);
    if (!buf[0]) std::snprintf(buf, sizeof buf, "FFmpeg error %d", code);
    return buf;
}

}  // namespace sc::ffm
