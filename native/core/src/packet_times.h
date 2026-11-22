// Packet time checks for the writer (writer.cpp), kept apart so core_tests can check them.
#pragma once

#include <cstdint>
#include <limits>

namespace sc {

// A packet can't be decoded after it's shown. When an encoder hands back a decode time later than the
// picture time (never right, B-frames or not), the picture time stands in for it. true when it changed.
inline bool even_out_dts(int64_t pts, int64_t &dts) {
    constexpr int64_t none = std::numeric_limits<int64_t>::min();  // AV_NOPTS_VALUE
    if (pts == none || dts == none || dts <= pts) return false;
    dts = pts;
    return true;
}

}  // namespace sc
