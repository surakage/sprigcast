// Sound capture (sc_capture_*, docs/native-engine.md "Sound"): a microphone or desktop sound read with WASAPI on a
// thread of the engine's own and pushed straight into the mixer, so a sound card's samples never pass through
// Python. Converted to 48 kHz stereo float by Windows itself (AUTOCONVERTPCM) or, if the driver refuses, here
// (front left and right kept, a linear resampler).
//
// It finds its device again by itself: unplugged or reset devices are retried every 2 s, and a sound following the
// Windows default moves to the new default when it changes. A device it can't find says so in its stats; SprigCast
// then captures in Python instead (audio/native_mixer.py).
//
// The device name "sprigcast-test-tone" makes a 440 Hz tone with no sound card, on any system (checks).
#include "base.h"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <cwchar>
#include <iterator>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include "sound_devices.h"

#ifdef _WIN32
using Microsoft::WRL::ComPtr;
#endif

namespace {

constexpr int RATE = 48000;
constexpr auto RETRY = std::chrono::seconds(2);
constexpr auto DEFAULT_CHECK = std::chrono::seconds(2);
const std::wstring TEST_TONE = L"sprigcast-test-tone";

using sc::sound::narrow;

// Interleaved frames of any channel count and rate -> 48 kHz stereo (front left and right; mono doubled).
struct Converter {
    int channels = 2, rate = RATE;
    double position = 0.0;     // where the next output sample falls (see run)
    float last[2] = {0, 0};    // the previous input frame (stereo)
    bool primed = false;

    void reset(int c, int r) {
        channels = std::max(1, c);
        rate = std::max(1, r);
        position = 0.0;
        primed = false;
    }

    void run(const float *in, size_t frames, std::vector<float> &out) {
        out.clear();
        if (!frames) return;
        auto at = [&](size_t i, int side) {
            const float *f = in + i * static_cast<size_t>(channels);
            return channels == 1 ? f[0] : f[side];
        };
        if (rate == RATE) {
            out.resize(frames * 2);
            for (size_t i = 0; i < frames; ++i) {
                out[2 * i] = at(i, 0);
                out[2 * i + 1] = at(i, 1);
            }
            return;
        }
        if (!primed) {
            last[0] = at(0, 0);
            last[1] = at(0, 1);
            primed = true;
        }
        const double step = static_cast<double>(rate) / RATE;
        // Frame 0 is `last` (the previous call's final frame), frame k is in[k - 1]; `position` is where the next
        // output sample falls on that scale.
        auto frame = [&](size_t k, int side) { return k == 0 ? last[side] : at(k - 1, side); };
        while (position < static_cast<double>(frames)) {
            const size_t k = static_cast<size_t>(position);
            const float t = static_cast<float>(position - static_cast<double>(k));
            for (int side = 0; side < 2; ++side) {
                const float a = frame(k, side), b = frame(k + 1, side);
                out.push_back(a + (b - a) * t);
            }
            position += step;
        }
        position -= static_cast<double>(frames);
        last[0] = at(frames - 1, 0);
        last[1] = at(frames - 1, 1);
    }
};

}  // namespace

struct sc_capture {
    sc_mixer *mixer = nullptr;
    int source = 0;
    std::wstring device;  // empty: the Windows default
    bool loopback = false;
    std::atomic<bool> stop{false};
    std::thread thread;
    std::mutex lock;  // guards the stats below
    sc_capture_stats stats{};
    Converter converter;
    std::vector<float> converted;

    void set_state(int state, const std::string &error = "", const std::wstring &name = L"") {
        std::lock_guard<std::mutex> hold(lock);
        const bool changed = stats.state != state;
        stats.state = state;
        std::snprintf(stats.error, sizeof(stats.error), "%s", error.c_str());
        if (!name.empty()) {
            const size_t n = std::min(name.size(), std::size(stats.device) - 1);
            std::wmemcpy(stats.device, name.c_str(), n);
            stats.device[n] = 0;
        }
        if (changed && state == SC_CAPTURE_CAPTURING)
            sc::log_line(SC_LOG_INFO, "sound capture: capturing " + narrow(name) + " in the engine");
        else if (changed && !error.empty())
            sc::log_line(SC_LOG_WARNING, "sound capture: " + error);
    }

    void push(const float *frames, size_t count, int channels, int rate) {
        if (converter.channels != channels || converter.rate != rate) converter.reset(channels, rate);
        converter.run(frames, count, converted);
        const int n = static_cast<int>(converted.size() / 2);
        if (n) sc_mixer_source_push(mixer, source, converted.data(), n);
        std::lock_guard<std::mutex> hold(lock);
        stats.frames += static_cast<uint64_t>(n);
        stats.channels = channels;
        stats.rate = rate;
    }

    void wait(std::chrono::milliseconds how_long) {
        const auto until = std::chrono::steady_clock::now() + how_long;
        while (!stop && std::chrono::steady_clock::now() < until) std::this_thread::sleep_for(std::chrono::milliseconds(20));
    }

    void tone() {
        set_state(SC_CAPTURE_CAPTURING, "", TEST_TONE);
        std::vector<float> block(480 * 2);
        double phase = 0.0;
        auto next = std::chrono::steady_clock::now();
        while (!stop) {
            for (int i = 0; i < 480; ++i) {
                const float v = 0.25f * static_cast<float>(std::sin(phase));
                block[2 * i] = block[2 * i + 1] = v;
                phase += 2.0 * 3.14159265358979 * 440.0 / RATE;
            }
            push(block.data(), 480, 2, RATE);
            next += std::chrono::milliseconds(10);
            std::this_thread::sleep_until(next);
        }
    }

#ifdef _WIN32
    // The device to read: by name (the friendly name PortAudio lists), or the default. A desktop sound set to a
    // sound mixer app's mix (a recording device) is read as a recording device. Sets *as_loopback.
    ComPtr<IMMDevice> find(IMMDeviceEnumerator *devices, bool *as_loopback, std::wstring *name, std::wstring *id) {
        ComPtr<IMMDevice> found;
        *as_loopback = loopback;
        if (device.empty()) {
            if (FAILED(devices->GetDefaultAudioEndpoint(loopback ? eRender : eCapture, eConsole, &found))) return {};
        } else {
            for (int pass = 0; pass < 2 && !found; ++pass) {
                const bool render = pass == 0 ? loopback : false;
                if (pass == 1 && !loopback) break;  // a microphone is only ever a recording device
                ComPtr<IMMDeviceCollection> all;
                if (FAILED(devices->EnumAudioEndpoints(render ? eRender : eCapture, DEVICE_STATE_ACTIVE, &all)))
                    continue;
                UINT count = 0;
                all->GetCount(&count);
                for (UINT i = 0; i < count && !found; ++i) {
                    ComPtr<IMMDevice> one;
                    if (FAILED(all->Item(i, &one))) continue;
                    if (friendly(one.Get()) == device) {
                        found = one;
                        *as_loopback = render;
                    }
                }
            }
        }
        if (!found) return {};
        *name = friendly(found.Get());
        LPWSTR raw = nullptr;
        if (SUCCEEDED(found->GetId(&raw)) && raw) {
            *id = raw;
            CoTaskMemFree(raw);
        }
        return found;
    }

    static std::wstring friendly(IMMDevice *one) { return sc::sound::friendly(one); }

    std::wstring default_id(IMMDeviceEnumerator *devices) { return sc::sound::default_id(devices, loopback); }

    // One session with one device, until it goes away, the default moves or stop. Returns false when it couldn't
    // start (then the caller waits before trying again).
    bool session(IMMDeviceEnumerator *devices) {
        bool as_loopback = loopback;
        std::wstring name, id;
        ComPtr<IMMDevice> found = find(devices, &as_loopback, &name, &id);
        if (!found) {
            set_state(SC_CAPTURE_NOT_FOUND, device.empty() ? "no default device" :
                      "no device named " + narrow(device));
            return false;
        }
        ComPtr<IAudioClient> client;
        HRESULT hr = found->Activate(__uuidof(IAudioClient), CLSCTX_ALL, nullptr,
                                     reinterpret_cast<void **>(client.GetAddressOf()));
        if (FAILED(hr)) {
            set_state(SC_CAPTURE_LOST, "opening " + narrow(name) + " failed (" + std::to_string(hr) + ")", name);
            return false;
        }
        const DWORD flags = as_loopback ? AUDCLNT_STREAMFLAGS_LOOPBACK : 0;
        const REFERENCE_TIME buffer = 1000000;  // 100 ms
        // First choice: Windows converts to 48 kHz stereo float itself.
        WAVEFORMATEX want{};
        want.wFormatTag = WAVE_FORMAT_IEEE_FLOAT;
        want.nChannels = 2;
        want.nSamplesPerSec = RATE;
        want.wBitsPerSample = 32;
        want.nBlockAlign = 8;
        want.nAvgBytesPerSec = RATE * 8;
        int channels = 2, rate = RATE;
        bool is_float = true;
        int bits = 32;
        hr = client->Initialize(AUDCLNT_SHAREMODE_SHARED,
                                flags | AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY,
                                buffer, 0, &want, nullptr);
        if (FAILED(hr)) {  // the driver's own format, converted here
            client.Reset();
            found->Activate(__uuidof(IAudioClient), CLSCTX_ALL, nullptr, reinterpret_cast<void **>(client.GetAddressOf()));
            WAVEFORMATEX *mix = nullptr;
            if (!client || FAILED(client->GetMixFormat(&mix)) || !mix) {
                set_state(SC_CAPTURE_LOST, "no sound format from " + narrow(name), name);
                return false;
            }
            channels = mix->nChannels;
            rate = static_cast<int>(mix->nSamplesPerSec);
            bits = mix->wBitsPerSample;
            is_float = mix->wFormatTag == WAVE_FORMAT_IEEE_FLOAT;
            if (mix->wFormatTag == WAVE_FORMAT_EXTENSIBLE && mix->cbSize >= 22) {
                const auto *ext = reinterpret_cast<WAVEFORMATEXTENSIBLE *>(mix);
                static const GUID float_format = {0x00000003, 0x0000, 0x0010,
                                                  {0x80, 0x00, 0x00, 0xaa, 0x00, 0x38, 0x9b, 0x71}};
                is_float = IsEqualGUID(ext->SubFormat, float_format) != 0;
            }
            hr = client->Initialize(AUDCLNT_SHAREMODE_SHARED, flags, buffer, 0, mix, nullptr);
            CoTaskMemFree(mix);
            if (FAILED(hr) || (!is_float && bits != 16 && bits != 32)) {
                set_state(SC_CAPTURE_LOST, "starting " + narrow(name) + " failed (" + std::to_string(hr) + ")",
                          name);
                return false;
            }
        }
        ComPtr<IAudioCaptureClient> reader;
        if (FAILED(client->GetService(__uuidof(IAudioCaptureClient), reinterpret_cast<void **>(reader.GetAddressOf())))
            || FAILED(client->Start())) {
            set_state(SC_CAPTURE_LOST, "starting " + narrow(name) + " failed", name);
            return false;
        }
        set_state(SC_CAPTURE_CAPTURING, "", name);
        {
            std::lock_guard<std::mutex> hold(lock);
            stats.channels = channels;
            stats.rate = rate;
        }
        std::vector<float> as_float;
        auto checked = std::chrono::steady_clock::now();
        bool going = true;
        while (!stop && going) {
            UINT32 waiting = 0;
            hr = reader->GetNextPacketSize(&waiting);
            while (SUCCEEDED(hr) && waiting) {
                BYTE *data = nullptr;
                UINT32 frames = 0;
                DWORD got = 0;
                hr = reader->GetBuffer(&data, &frames, &got, nullptr, nullptr);
                if (FAILED(hr)) break;
                const size_t samples = static_cast<size_t>(frames) * static_cast<size_t>(channels);
                as_float.assign(samples, 0.0f);
                if (!(got & AUDCLNT_BUFFERFLAGS_SILENT) && data) {
                    if (is_float && bits == 32) {
                        std::memcpy(as_float.data(), data, samples * sizeof(float));
                    } else if (bits == 16) {
                        const auto *s = reinterpret_cast<const int16_t *>(data);
                        for (size_t i = 0; i < samples; ++i) as_float[i] = static_cast<float>(s[i]) / 32768.0f;
                    } else {
                        const auto *s = reinterpret_cast<const int32_t *>(data);
                        for (size_t i = 0; i < samples; ++i) as_float[i] = static_cast<float>(s[i]) / 2147483648.0f;
                    }
                }
                if (got & AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY) {
                    std::lock_guard<std::mutex> hold(lock);
                    ++stats.gaps;
                }
                reader->ReleaseBuffer(frames);
                push(as_float.data(), frames, channels, rate);
                hr = reader->GetNextPacketSize(&waiting);
            }
            if (FAILED(hr)) {  // unplugged, disabled, the driver restarted, or exclusive mode taken
                set_state(SC_CAPTURE_LOST, narrow(name) + " went away (" + std::to_string(hr) + "), retrying", name);
                going = false;
                break;
            }
            const auto now = std::chrono::steady_clock::now();
            if (device.empty() && now - checked >= DEFAULT_CHECK) {
                checked = now;
                const std::wstring current = default_id(devices);
                if (!current.empty() && current != id) {
                    sc::log_line(SC_LOG_INFO, "sound capture: the Windows default changed, following it");
                    break;  // straight over to the new default
                }
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
        }
        client->Stop();
        return going;
    }

    void capture() {
        const HRESULT com = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
        {
            ComPtr<IMMDeviceEnumerator> devices;
            if (FAILED(CoCreateInstance(__uuidof(MMDeviceEnumerator), nullptr, CLSCTX_ALL,
                                        __uuidof(IMMDeviceEnumerator),
                                        reinterpret_cast<void **>(devices.GetAddressOf())))) {
                set_state(SC_CAPTURE_FAILED, "Windows sound isn't running");
            } else {
                while (!stop) {
                    if (!session(devices.Get())) wait(RETRY);
                }
            }
        }
        if (SUCCEEDED(com)) CoUninitialize();
    }
#else
    void capture() { set_state(SC_CAPTURE_FAILED, "sound capture needs Windows"); }
#endif

    void run() {
        sc::name_thread(L"sprigcast-sound-capture");
        try {
            if (device == TEST_TONE)
                tone();
            else
                capture();
        } catch (...) {  // crash gate: this capture stops, the app and the mix go on
            set_state(SC_CAPTURE_FAILED, "the capture thread faulted");
        }
    }
};

extern "C" {

SC_API int sc_capture_open(sc_mixer *mixer, int source, const wchar_t *device, int loopback, sc_capture **out) {
    return sc::guarded("sc_capture_open", [&]() -> int {
        if (!mixer || !out) return sc::fail(SC_ERR_ARGUMENT, "sc_capture_open: no mixer");
        *out = nullptr;
        auto c = std::make_unique<sc_capture>();
        c->mixer = mixer;
        c->source = source;
        c->device = device ? device : L"";
        c->loopback = loopback != 0;
        c->stats.state = SC_CAPTURE_STARTING;
        c->thread = std::thread([p = c.get()] { p->run(); });
        *out = c.release();
        return SC_OK;
    });
}

SC_API int sc_capture_stats_get(sc_capture *capture, sc_capture_stats *out) {
    return sc::guarded("sc_capture_stats_get", [&]() -> int {
        if (!capture || !out) return sc::fail(SC_ERR_ARGUMENT, "sc_capture_stats_get: nothing given");
        std::lock_guard<std::mutex> hold(capture->lock);
        *out = capture->stats;
        return SC_OK;
    });
}

SC_API void sc_capture_close(sc_capture *capture) {
    if (!capture) return;
    sc::guarded("sc_capture_close", [&]() -> int {
        capture->stop = true;
        if (capture->thread.joinable()) capture->thread.join();
        delete capture;
        return SC_OK;
    });
}

}  // extern "C"
