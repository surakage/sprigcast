// What every source file of the engine shares, on any system: errors, the log and the crash gate. (The parts
// that draw add Direct3D on top in internal.h; the media player builds without it, so its checks also run on
// Linux.)
#pragma once

#define SPRIGCORE_BUILD
#include "sprigcore.h"

#include <exception>
#include <new>
#include <string>

namespace sc {

void log_line(int level, const std::string &message);
int fail(int code, const std::string &message);
void set_last_error(const std::string &message);
// The calling thread's name as Task Manager, a debugger and the CPU-by-thread report show it (Windows 10 1607+;
// does nothing elsewhere).
void name_thread(const wchar_t *name);

// Every exported function runs inside this: no C++ exception crosses into Python. Built with /EHa, catch (...)
// also stops Windows faults (an access violation in a driver or in our own code).
template <typename F>
int guarded(const char *name, F &&body) {
    try {
        set_last_error("");
        return body();
    } catch (const std::bad_alloc &) {
        return fail(SC_ERR_OUT_OF_MEMORY, std::string(name) + ": out of memory");
    } catch (const std::exception &e) {
        return fail(SC_ERR_INTERNAL, std::string(name) + ": " + e.what());
    } catch (...) {
        return fail(SC_ERR_INTERNAL, std::string(name) + ": fault");
    }
}

}  // namespace sc
