// The engine's log queue, last error and version: the parts of the C interface that need no graphics card.
#include "base.h"

#ifdef _WIN32
#include <windows.h>
#endif

#include <cstring>
#include <deque>
#include <mutex>

namespace {

thread_local std::string g_last_error;
std::mutex g_log_mutex;
std::deque<std::string> g_log;
constexpr size_t LOG_KEEP = 512;

}  // namespace

namespace sc {

void set_last_error(const std::string &message) { g_last_error = message; }

void name_thread(const wchar_t *name) {
#ifdef _WIN32
    using Describe = HRESULT(WINAPI *)(HANDLE, PCWSTR);
    FARPROC found = GetProcAddress(GetModuleHandleW(L"kernel32.dll"), "SetThreadDescription");
    if (found) reinterpret_cast<Describe>(reinterpret_cast<void (*)()>(found))(GetCurrentThread(), name);
#else
    (void)name;
#endif
}

void sound_thread_priority() {
#ifdef _WIN32
    // Sound can't wait: another app pinning the CPU at 100% (the user's PC live, 2026-11-21, SprigCast at 15%)
    // made viewers hear choppy voice. Highest within SprigCast's class (above normal while live), as OBS does for
    // its audio threads; a short burst of work every 10 ms, so it never starves anything else.
    SetThreadPriority(GetCurrentThread(), THREAD_PRIORITY_HIGHEST);
#endif
}

void sender_thread_priority() {
#ifdef _WIN32
    SetThreadPriority(GetCurrentThread(), THREAD_PRIORITY_ABOVE_NORMAL);
#endif
}

void log_line(int level, const std::string &message) {
    std::lock_guard<std::mutex> lock(g_log_mutex);
    g_log.push_back(std::to_string(level) + " " + message);
    while (g_log.size() > LOG_KEEP) g_log.pop_front();
}

int fail(int code, const std::string &message) {
    g_last_error = message;
    log_line(code == SC_ERR_ARGUMENT ? SC_LOG_WARNING : SC_LOG_ERROR, message);
    return code;
}

}  // namespace sc

extern "C" {

SC_API int sc_interface_version(void) { return SC_INTERFACE_VERSION; }

SC_API const char *sc_build_info(void) {
#define SC_STR2(x) #x
#define SC_STR(x) SC_STR2(x)
#if defined(_MSC_VER)
#  define SC_COMPILER "MSVC " SC_STR(_MSC_VER)
#elif defined(__clang__)
#  define SC_COMPILER "clang " __clang_version__
#elif defined(__GNUC__)
#  define SC_COMPILER "gcc " __VERSION__
#else
#  define SC_COMPILER "unknown compiler"
#endif
    return "sprigcore interface " SC_STR(SC_INTERFACE_VERSION) ", " SC_COMPILER ", built " __DATE__;
}

SC_API const char *sc_last_error(void) { return g_last_error.c_str(); }

SC_API int sc_log_drain(char *buffer, int size) {
    if (!buffer || size <= 0) return 0;
    std::lock_guard<std::mutex> lock(g_log_mutex);
    int used = 0;
    while (!g_log.empty()) {
        const std::string &line = g_log.front();
        int need = static_cast<int>(line.size()) + 1;
        if (need > size) {  // longer than the whole buffer: cut it rather than block the queue forever
            if (used) break;
            std::memcpy(buffer, line.data(), size - 1);
            buffer[size - 1] = '\n';
            g_log.pop_front();
            return size;
        }
        if (used + need > size) break;
        std::memcpy(buffer + used, line.data(), line.size());
        buffer[used + line.size()] = '\n';
        used += need;
        g_log.pop_front();
    }
    return used;
}

}  // extern "C"
