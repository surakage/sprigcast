// What capture.cpp and playback.cpp share about Windows sound devices.
#pragma once

#include <string>

#ifdef _WIN32
#include <windows.h>
#include <objbase.h>
#include <propidl.h>
#include <audioclient.h>
#include <audiopolicy.h>
#include <mmdeviceapi.h>
#include <wrl/client.h>
#endif

namespace sc::sound {

inline std::string narrow(const std::wstring &text) {
    std::string out;
    for (wchar_t c : text) out += (c > 0 && c < 128) ? static_cast<char>(c) : '?';
    return out;
}

#ifdef _WIN32
// A device's friendly name (the one PortAudio lists). PKEY_Device_FriendlyName, spelled out so no header has to
// define it here.
inline std::wstring friendly(IMMDevice *one) {
    static const PROPERTYKEY key = {{0xa45c254e, 0xdf1c, 0x4efd, {0x80, 0x20, 0x67, 0xd1, 0x46, 0xa8, 0x50, 0xe0}}, 14};
    Microsoft::WRL::ComPtr<IPropertyStore> store;
    if (FAILED(one->OpenPropertyStore(STGM_READ, &store))) return L"";
    PROPVARIANT value;
    PropVariantInit(&value);
    std::wstring out;
    if (SUCCEEDED(store->GetValue(key, &value)) && value.vt == VT_LPWSTR && value.pwszVal) out = value.pwszVal;
    PropVariantClear(&value);
    return out;
}

inline std::wstring id_of(IMMDevice *one) {
    LPWSTR raw = nullptr;
    std::wstring out;
    if (one && SUCCEEDED(one->GetId(&raw)) && raw) {
        out = raw;
        CoTaskMemFree(raw);
    }
    return out;
}

// The Windows default's id for playback (render) or recording, "" if none.
inline std::wstring default_id(IMMDeviceEnumerator *devices, bool render) {
    Microsoft::WRL::ComPtr<IMMDevice> now;
    if (FAILED(devices->GetDefaultAudioEndpoint(render ? eRender : eCapture, eConsole, &now))) return L"";
    return id_of(now.Get());
}
#endif

}  // namespace sc::sound
