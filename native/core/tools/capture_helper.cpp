// SprigCastCapture.exe: the capture helper (docs/native-engine.md "Capture helper"). SprigCast starts it when a
// bloom on screen has a display, window or game capture, tells it over its standard input what to capture, and
// ends it when nothing is captured any more: every capture Windows starts keeps about one picture of graphics
// memory until the program that started it ends (PC 2026-11-19), and ending this small program gives it all back
// while SprigCast runs on.
//
// It only passes commands to sprigcore.dll (sc_capture_feed_*, capture_feed.cpp), which captures on a device of
// its own and shares each picture with the engine on the graphics card. Lines in, one command each:
//   start <id> <luid low> <luid high> <window> <monitor> <flags> <interval ms> <block name>
//   stop <id>
//   quit
// Lines out: "ready" once, then "state <id> <state> <width> <height> <pictures>" whenever a capture's state or
// size changes (and its count a few times a second), and "log <level> <message>" for the engine's log lines.
// The end of its input (SprigCast gone, however it went) ends it. Started with "--check" it reads nothing: it says
// "ready" and ends (the build's check that it starts and finds sprigcore.dll).
#include "sprigcore.h"

#include <atomic>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <map>
#include <mutex>
#include <sstream>
#include <string>
#include <thread>

namespace {

struct Running {
    sc_capture_feed *feed = nullptr;
    int state = -1, width = 0, height = 0;
    uint64_t frames = 0;
    std::chrono::steady_clock::time_point told{};
};

std::mutex lock;  // `feeds` and standard output
std::map<uint64_t, Running> feeds;
std::atomic<bool> done{false};

void say(const std::string &line) {  // `lock` held
    std::fputs(line.c_str(), stdout);
    std::fputc('\n', stdout);
    std::fflush(stdout);
}

void pass_log() {  // `lock` held
    char buffer[4096];
    for (;;) {
        const int got = sc_log_drain(buffer, static_cast<int>(sizeof buffer) - 1);
        if (got <= 0) return;
        buffer[got] = 0;
        std::istringstream lines(buffer);
        std::string line;
        while (std::getline(lines, line))
            if (!line.empty()) say("log " + line);
    }
}

// Twenty times a second: what changed is said. A capture's count alone is said every half second.
void report() {
    while (!done.load()) {
        {
            std::lock_guard<std::mutex> hold(lock);
            const auto now = std::chrono::steady_clock::now();
            for (auto &[id, r] : feeds) {
                int state = 0, w = 0, h = 0;
                uint64_t frames = 0;
                if (sc_capture_feed_state(r.feed, &state, &w, &h, &frames) != SC_OK) continue;
                const bool changed = state != r.state || w != r.width || h != r.height || (frames && !r.frames);
                if (!changed && (frames == r.frames || now - r.told < std::chrono::milliseconds(500))) continue;
                r.state = state;
                r.width = w;
                r.height = h;
                r.frames = frames;
                r.told = now;
                say("state " + std::to_string(id) + " " + std::to_string(state) + " " + std::to_string(w) + " " +
                    std::to_string(h) + " " + std::to_string(frames));
            }
            pass_log();
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
    }
}

void stop(uint64_t id) {  // `lock` held
    auto found = feeds.find(id);
    if (found == feeds.end()) return;
    sc_capture_feed_destroy(found->second.feed);
    feeds.erase(found);
}

}  // namespace

int main(int argc, char **argv) {
    const bool check = argc > 1 && !std::strcmp(argv[1], "--check");
    std::thread reporter(report);
    {
        std::lock_guard<std::mutex> hold(lock);
        say("ready");
    }
    std::string line;
    while (!check && std::getline(std::cin, line)) {
        std::istringstream in(line);
        std::string what;
        in >> what;
        std::lock_guard<std::mutex> hold(lock);
        if (what == "quit") break;
        if (what == "stop") {
            uint64_t id = 0;
            if (in >> id) stop(id);
        } else if (what == "start") {
            uint64_t id = 0, window = 0, monitor = 0;
            uint32_t low = 0;
            int32_t high = 0;
            int flags = 0, interval = 0;
            std::string block;
            if (!(in >> id >> low >> high >> window >> monitor >> flags >> interval >> block) || !id) {
                say("log 3 capture helper: a start line it can't read");
                continue;
            }
            stop(id);  // the same source asked again: its new capture replaces the old
            const std::wstring name(block.begin(), block.end());  // block names are plain letters and digits
            Running r;
            if (sc_capture_feed_create(low, high, window, monitor, flags, interval, name.c_str(), &r.feed) != SC_OK) {
                say("state " + std::to_string(id) + " " + std::to_string(SC_SCREEN_REFUSED) + " 0 0 0");
                say(std::string("log 3 capture helper: ") + sc_last_error());
                continue;
            }
            feeds[id] = r;
        }
    }
    done.store(true);
    reporter.join();
    std::lock_guard<std::mutex> hold(lock);
    while (!feeds.empty()) stop(feeds.begin()->first);
    pass_log();
    std::fflush(stdout);
    std::_Exit(0);  // at once: the engine's capture thread never ends by itself, and Windows frees the rest
}
