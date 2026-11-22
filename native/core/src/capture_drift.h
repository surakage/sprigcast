// How fast a sound device really delivers, against the clock the mix runs on (capture.cpp).
//
// A capture card's sound follows its HDMI source's clock, which can run slow: the Elgato HD60 X gave about 47,450
// samples a second of 48,000 on the PC (2026-11-22) with none lost by Windows. Played at 48,000 it runs dry every few
// seconds however much is kept ahead (mixer.cpp's cushion), each time a click. Measured over a long window, the
// shortfall is evened out by the capture's resampler instead: those 47,450 become 48,000, at the source's own pitch.
#pragma once

#include <cmath>
#include <cstddef>
#include <cstdint>

namespace sc::sound {

struct DriftMeter {
    static constexpr double SETTLE_S = 3.0;    // the first seconds of a capture come in a burst: not counted
    static constexpr double WINDOW_S = 20.0;   // long enough that 10 ms packets arriving unevenly average out
    static constexpr double DEADBAND = 0.002;  // within 0.2%: a good clock, left exactly as it is
    static constexpr double MOST = 0.03;       // more than 3% off is no clock: stalls or bursts, left alone
    static constexpr double STALL_S = 0.25;    // nothing for this long (no signal, a reset): the window starts again

    double ratio = 1.0;  // delivered / nominal, what the resampler uses (1.0: untouched)

    // `frames` arrived at `now` (seconds on a steady clock) from a device that says it runs at `rate`.
    // True when `ratio` changed by a noticeable step (worth a log line).
    bool feed(size_t frames, int rate, double now) {
        if (!started_) {
            started_ = true;
            first_ = last_ = now;
            return false;
        }
        if (now - last_ > STALL_S) window_start_ = -1.0;  // a pause isn't a slow clock
        last_ = now;
        if (now - first_ < SETTLE_S || rate <= 0) return false;
        if (window_start_ < 0.0) {  // what arrives from here on is counted against the time since
            window_start_ = now;
            counted_ = 0;
            return false;
        }
        counted_ += frames;
        const double elapsed = now - window_start_;
        if (elapsed < WINDOW_S) return false;
        const double measured = static_cast<double>(counted_) / elapsed / static_cast<double>(rate);
        window_start_ = now;
        counted_ = 0;
        if (std::fabs(measured - 1.0) > MOST) return false;
        const double target = std::fabs(measured - 1.0) < DEADBAND ? 1.0 : measured;
        const double before = ratio;
        ratio = std::fabs(target - ratio) < 0.0002 ? target : ratio + (target - ratio) * 0.5;  // eased, never a jump
        if (std::fabs(ratio - 1.0) < 0.0002) ratio = 1.0;
        return std::fabs(ratio - before) >= 0.001 || (before != 1.0 && ratio == 1.0);
    }

    void reset() {
        *this = DriftMeter{};
    }

  private:
    bool started_ = false;
    double first_ = 0.0, last_ = 0.0, window_start_ = -1.0;
    uint64_t counted_ = 0;
};

}  // namespace sc::sound
