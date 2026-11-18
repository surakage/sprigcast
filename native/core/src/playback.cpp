// Sound playback (sc_playback_*, docs/native-engine.md "Sound"): one of the mixer's mixes, usually the monitor mix,
// played on a playback device with WASAPI on a thread of the engine's own, so monitoring never passes through
// Python. Played as 48 kHz stereo float converted by Windows itself (AUTOCONVERTPCM) or, if the driver refuses,
// here (a linear resampler; mono devices get both sides, surround ones the front left and right).
//
// The mixer's clock and the sound card's drift apart a little, so at most 120 ms waits here: older sound is dropped
// (it never falls behind), and when it runs dry the card gets silence until the mix catches up.
//
// It finds its device again by itself: unplugged or reset devices are retried every 2 s, and playing on the Windows
// default moves to the new default when it changes.
//
// The device name "sprigcast-test-sink" plays nowhere, taking sound at the real rate (checks).
#include "base.h"
#include "sound_devices.h"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <cwchar>
#include <deque>
#include <iterator>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#ifdef _WIN32
using Microsoft::WRL::ComPtr;
#endif

namespace {

constexpr int RATE = 48000;
constexpr double LATENCY_S = 0.12;
constexpr auto RETRY = std::chrono::seconds(2);
constexpr auto DEFAULT_CHECK = std::chrono::seconds(2);
const std::wstring TEST_SINK = L"sprigcast-test-sink";

using sc::sound::narrow;

// 48 kHz stereo -> another rate, stereo (linear). Keeps its place between calls.
struct Resampler {
    int rate = RATE;
    double position = 0.0;  // where the next output sample falls, on the scale where frame 0 is `last`
    float last[2] = {0, 0};

    void reset(int r) {
        rate = std::max(1, r);
        position = 0.0;
        last[0] = last[1] = 0.0f;
    }

    void run(const float *in, size_t frames, std::deque<float> &out) {
        if (rate == RATE) {
            out.insert(out.end(), in, in + frames * 2);
            return;
        }
        const double step = static_cast<double>(RATE) / rate;
        auto frame = [&](size_t k, int side) { return k == 0 ? last[side] : in[(k - 1) * 2 + side]; };
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
        if (frames) {
            last[0] = in[(frames - 1) * 2];
            last[1] = in[(frames - 1) * 2 + 1];
        }
    }
};

}  // namespace

struct sc_playback {
    sc_mixer *mixer = nullptr;
    int tap = 0;
    std::wstring device;  // empty: the Windows default
    std::atomic<bool> stop{false};
    std::thread thread;
    std::mutex lock;  // guards the stats below
    sc_playback_stats stats{};
    Resampler resampler;
    std::deque<float> waiting;  // device-rate stereo frames, interleaved
    std::vector<float> block = std::vector<float>(SC_MIXER_BLOCK * 2);
    bool dry = true;  // ran out: waits for a little sound before playing again (no crackle of single blocks)

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
            sc::log_line(SC_LOG_INFO, "sound playback: playing on " + narrow(name) + " from the engine");
        else if (changed && !error.empty())
            sc::log_line(SC_LOG_WARNING, "sound playback: " + error);
    }

    // The mix blocks waiting in the tap, at the device's rate; never more than LATENCY_S kept.
    void pull(int rate) {
        if (resampler.rate != rate) {
            resampler.reset(rate);
            waiting.clear();
        }
        int64_t index = 0;
        uint64_t lost = 0;
        while (sc_mixer_tap_take(mixer, tap, block.data(), static_cast<int>(block.size()), &index, &lost) == 1)
            resampler.run(block.data(), SC_MIXER_BLOCK, waiting);
        const size_t most = static_cast<size_t>(rate * LATENCY_S) * 2;
        if (waiting.size() > most) {
            const size_t extra = waiting.size() - most;
            waiting.erase(waiting.begin(), waiting.begin() + static_cast<std::ptrdiff_t>(extra));
            std::lock_guard<std::mutex> hold(lock);
            stats.dropped += extra / 2;
        }
    }

    // `frames` frames for a device of `channels` channels into out (float, interleaved); silence when dry.
    void fill(float *out, size_t frames, int channels, int rate) {
        const size_t start_after = static_cast<size_t>(rate / 50) * 2;  // 20 ms
        if (dry && waiting.size() >= start_after + frames * 2) dry = false;
        size_t have = dry ? 0 : std::min(frames, waiting.size() / 2);
        std::fill(out, out + frames * static_cast<size_t>(channels), 0.0f);
        for (size_t i = 0; i < have; ++i) {
            const float l = waiting[2 * i], r = waiting[2 * i + 1];
            float *f = out + i * static_cast<size_t>(channels);
            if (channels == 1) {
                f[0] = 0.5f * (l + r);
            } else {
                f[0] = l;
                f[1] = r;
            }
        }
        waiting.erase(waiting.begin(), waiting.begin() + static_cast<std::ptrdiff_t>(have * 2));
        std::lock_guard<std::mutex> hold(lock);
        stats.frames += frames;
        if (!dry && have < frames) {
            dry = true;
            ++stats.underruns;
        }
    }

    void wait(std::chrono::milliseconds how_long) {
        const auto until = std::chrono::steady_clock::now() + how_long;
        while (!stop && std::chrono::steady_clock::now() < until)
            std::this_thread::sleep_for(std::chrono::milliseconds(20));
    }

    void sink() {
        set_state(SC_CAPTURE_CAPTURING, "", TEST_SINK);
        {
            std::lock_guard<std::mutex> hold(lock);
            stats.channels = 2;
            stats.rate = RATE;
        }
        std::vector<float> out(480 * 2);
        auto next = std::chrono::steady_clock::now();
        while (!stop) {
            pull(RATE);
            fill(out.data(), 480, 2, RATE);
            next += std::chrono::milliseconds(10);
            std::this_thread::sleep_until(next);
        }
    }

#ifdef _WIN32
    ComPtr<IMMDevice> find(IMMDeviceEnumerator *devices, std::wstring *name, std::wstring *id) {
        ComPtr<IMMDevice> found;
        if (device.empty()) {
            if (FAILED(devices->GetDefaultAudioEndpoint(eRender, eConsole, &found))) return {};
        } else {
            ComPtr<IMMDeviceCollection> all;
            if (FAILED(devices->EnumAudioEndpoints(eRender, DEVICE_STATE_ACTIVE, &all))) return {};
            UINT count = 0;
            all->GetCount(&count);
            for (UINT i = 0; i < count && !found; ++i) {
                ComPtr<IMMDevice> one;
                if (SUCCEEDED(all->Item(i, &one)) && sc::sound::friendly(one.Get()) == device) found = one;
            }
        }
        if (!found) return {};
        *name = sc::sound::friendly(found.Get());
        *id = sc::sound::id_of(found.Get());
        return found;
    }

    // One session with one device, until it goes away, the default moves or stop. False when it couldn't start.
    bool session(IMMDeviceEnumerator *devices) {
        std::wstring name, id;
        ComPtr<IMMDevice> found = find(devices, &name, &id);
        if (!found) {
            set_state(SC_CAPTURE_NOT_FOUND, device.empty() ? "no default playback device" :
                      narrow(device) + " isn't plugged in");
            return false;
        }
        ComPtr<IAudioClient> client;
        HRESULT hr = found->Activate(__uuidof(IAudioClient), CLSCTX_ALL, nullptr,
                                     reinterpret_cast<void **>(client.GetAddressOf()));
        if (FAILED(hr)) {
            set_state(SC_CAPTURE_LOST, "opening " + narrow(name) + " failed (" + std::to_string(hr) + ")", name);
            return false;
        }
        const REFERENCE_TIME buffer = 500000;  // 50 ms
        WAVEFORMATEX want{};
        want.wFormatTag = WAVE_FORMAT_IEEE_FLOAT;
        want.nChannels = 2;
        want.nSamplesPerSec = RATE;
        want.wBitsPerSample = 32;
        want.nBlockAlign = 8;
        want.nAvgBytesPerSec = RATE * 8;
        int channels = 2, rate = RATE, bits = 32;
        bool is_float = true;
        hr = client->Initialize(AUDCLNT_SHAREMODE_SHARED,
                                AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY, buffer, 0,
                                &want, nullptr);
        if (FAILED(hr)) {  // the device's own format, converted here
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
            hr = client->Initialize(AUDCLNT_SHAREMODE_SHARED, 0, buffer, 0, mix, nullptr);
            CoTaskMemFree(mix);
            if (FAILED(hr) || channels < 1 || (!is_float && bits != 16 && bits != 32) || (is_float && bits != 32)) {
                set_state(SC_CAPTURE_LOST, "starting " + narrow(name) + " failed (" + std::to_string(hr) + ")",
                          name);
                return false;
            }
        }
        UINT32 size = 0;
        ComPtr<IAudioRenderClient> writer;
        if (FAILED(client->GetBufferSize(&size)) ||
            FAILED(client->GetService(__uuidof(IAudioRenderClient), reinterpret_cast<void **>(writer.GetAddressOf())))) {
            set_state(SC_CAPTURE_LOST, "starting " + narrow(name) + " failed", name);
            return false;
        }
        {  // starts with silence, so the card has something while the first blocks come
            BYTE *data = nullptr;
            if (SUCCEEDED(writer->GetBuffer(size, &data))) writer->ReleaseBuffer(size, AUDCLNT_BUFFERFLAGS_SILENT);
        }
        if (FAILED(client->Start())) {
            set_state(SC_CAPTURE_LOST, "starting " + narrow(name) + " failed", name);
            return false;
        }
        set_state(SC_CAPTURE_CAPTURING, "", name);
        {
            std::lock_guard<std::mutex> hold(lock);
            stats.channels = channels;
            stats.rate = rate;
        }
        std::vector<float> out;
        auto checked = std::chrono::steady_clock::now();
        bool going = true;
        while (!stop) {
            pull(rate);
            UINT32 padding = 0;
            hr = client->GetCurrentPadding(&padding);
            if (SUCCEEDED(hr) && padding < size) {
                const UINT32 frames = size - padding;
                BYTE *data = nullptr;
                hr = writer->GetBuffer(frames, &data);
                if (SUCCEEDED(hr)) {
                    out.resize(static_cast<size_t>(frames) * static_cast<size_t>(channels));
                    fill(out.data(), frames, channels, rate);
                    const size_t samples = out.size();
                    if (is_float) {
                        std::memcpy(data, out.data(), samples * sizeof(float));
                    } else if (bits == 16) {
                        auto *d = reinterpret_cast<int16_t *>(data);
                        for (size_t i = 0; i < samples; ++i)
                            d[i] = static_cast<int16_t>(std::clamp(out[i], -1.0f, 1.0f) * 32767.0f);
                    } else {
                        auto *d = reinterpret_cast<int32_t *>(data);
                        for (size_t i = 0; i < samples; ++i)
                            d[i] = static_cast<int32_t>(static_cast<double>(std::clamp(out[i], -1.0f, 1.0f)) *
                                                        2147483647.0);
                    }
                    hr = writer->ReleaseBuffer(frames, 0);
                }
            }
            if (FAILED(hr)) {  // unplugged, disabled, the driver restarted, or exclusive mode taken
                set_state(SC_CAPTURE_LOST, narrow(name) + " went away (" + std::to_string(hr) + "), retrying", name);
                going = false;
                break;
            }
            const auto now = std::chrono::steady_clock::now();
            if (device.empty() && now - checked >= DEFAULT_CHECK) {
                checked = now;
                const std::wstring current = sc::sound::default_id(devices, true);
                if (!current.empty() && current != id) {
                    sc::log_line(SC_LOG_INFO, "sound playback: the Windows default changed, following it");
                    break;
                }
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
        }
        client->Stop();
        return going;
    }

    void play() {
        const HRESULT com = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
        {
            ComPtr<IMMDeviceEnumerator> devices;
            if (FAILED(CoCreateInstance(__uuidof(MMDeviceEnumerator), nullptr, CLSCTX_ALL,
                                        __uuidof(IMMDeviceEnumerator),
                                        reinterpret_cast<void **>(devices.GetAddressOf())))) {
                set_state(SC_CAPTURE_FAILED, "Windows sound isn't running");
            } else {
                while (!stop) {
                    if (!session(devices.Get())) {
                        // nothing plays meanwhile: the mix isn't kept waiting for it
                        for (auto until = std::chrono::steady_clock::now() + RETRY;
                             !stop && std::chrono::steady_clock::now() < until;) {
                            pull(resampler.rate);
                            waiting.clear();
                            std::this_thread::sleep_for(std::chrono::milliseconds(20));
                        }
                    }
                }
            }
        }
        if (SUCCEEDED(com)) CoUninitialize();
    }
#else
    void play() { set_state(SC_CAPTURE_FAILED, "sound playback needs Windows"); }
#endif

    void run() {
        sc::name_thread(L"sprigcast-sound-playback");
        try {
            if (device == TEST_SINK)
                sink();
            else
                play();
        } catch (...) {  // crash gate: this playback stops, the app and the mix go on
            set_state(SC_CAPTURE_FAILED, "the playback thread faulted");
        }
    }
};

extern "C" {

SC_API int sc_playback_open(sc_mixer *mixer, int what, const wchar_t *device, sc_playback **out) {
    return sc::guarded("sc_playback_open", [&]() -> int {
        if (!mixer || !out) return sc::fail(SC_ERR_ARGUMENT, "sc_playback_open: no mixer");
        *out = nullptr;
        auto p = std::make_unique<sc_playback>();
        p->mixer = mixer;
        p->device = device ? device : L"";
        p->stats.state = SC_CAPTURE_STARTING;
        const int rc = sc_mixer_tap_add(mixer, &what, 1, &p->tap);
        if (rc != SC_OK) return rc;
        p->thread = std::thread([q = p.get()] { q->run(); });
        *out = p.release();
        return SC_OK;
    });
}

SC_API int sc_playback_stats_get(sc_playback *playback, sc_playback_stats *out) {
    return sc::guarded("sc_playback_stats_get", [&]() -> int {
        if (!playback || !out) return sc::fail(SC_ERR_ARGUMENT, "sc_playback_stats_get: nothing given");
        std::lock_guard<std::mutex> hold(playback->lock);
        *out = playback->stats;
        return SC_OK;
    });
}

SC_API void sc_playback_close(sc_playback *playback) {
    if (!playback) return;
    sc::guarded("sc_playback_close", [&]() -> int {
        playback->stop = true;
        if (playback->thread.joinable()) playback->thread.join();
        sc_mixer_tap_remove(playback->mixer, playback->tap);
        delete playback;
        return SC_OK;
    });
}

}  // extern "C"
