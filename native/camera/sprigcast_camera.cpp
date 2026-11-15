// SprigCast Camera: a Windows 11 virtual camera (MFCreateVirtualCamera) that shows SprigCast's canvas.
//
// How it fits together
//   * Windows' Frame Server loads this DLL (registered under HKLM by "Set up the camera" in Settings)
//     when an app such as Zoom, Teams, Discord or the Camera app opens "SprigCast Camera".
//   * The media source creates a shared-memory block, Global\SprigCastCamera, writes the size the app
//     asked for and a heartbeat into it, and turns whatever frame SprigCast last wrote there into samples.
//   * SprigCast (the Python app) sees the heartbeat, renders the canvas at that size as NV12 on the GPU
//     and copies each frame in. Nobody watching = no heartbeat = SprigCast does no camera work at all.
//   * SprigCast itself loads this same DLL and calls SC_Start/SC_Stop to add or remove the camera
//     (session lifetime: it disappears when SprigCast closes).
//
// Shared block layout (little endian), see src/sprigcast/output/virtualcam.py:
//   0 u32 magic 'SPVC'   4 u32 version   8 u32 want_w   12 u32 want_h   (camera writes)
//  16 u32 width  20 u32 height  24 u64 seq (odd while writing)                (SprigCast writes)
//  32 u64 heartbeat (GetTickCount64 of the last sample the camera made)        (camera writes)
//  64.. NV12 frame, up to MAX_W * MAX_W * 3 / 2 bytes

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <sddl.h>
#include <mfapi.h>
#include <mfidl.h>
#include <mferror.h>
#include <mfvirtualcamera.h>
#include <ks.h>
#include <ksproxy.h>
#include <ksmedia.h>
#include <new>
#include <cstring>
#include <cstdint>
#include <cstdio>
#include <cstdarg>

#pragma comment(lib, "mfplat.lib")
#pragma comment(lib, "mfuuid.lib")
#pragma comment(lib, "mf.lib")
#pragma comment(lib, "mfsensorgroup.lib")
#pragma comment(lib, "advapi32.lib")
#pragma comment(lib, "ole32.lib")

// {6F3C5A0E-2B9D-4C6B-9F4E-5A1D7C2E8B31}
static const CLSID CLSID_SprigCastCamera = {0x6f3c5a0e, 0x2b9d, 0x4c6b, {0x9f, 0x4e, 0x5a, 0x1d, 0x7c, 0x2e, 0x8b, 0x31}};
static const wchar_t* CLSID_TEXT = L"{6F3C5A0E-2B9D-4C6B-9F4E-5A1D7C2E8B31}";
static const wchar_t* SHARED_NAME = L"Global\\SprigCastCamera";
static const UINT32 MAGIC = 0x43565053;  // 'SPVC'
static const UINT32 VERSION = 2;
static const UINT32 MAX_W = 1920;
static const UINT32 HEADER = 64;
static const UINT32 FPS = 30;
static const SIZE SIZES[] = {{1920, 1080}, {1280, 720}, {1080, 1920}, {720, 1280}};

static HMODULE g_module = nullptr;
static volatile LONG g_objects = 0;
static volatile LONG g_locks = 0;

template <class T> static void SafeRelease(T*& p) { if (p) { p->Release(); p = nullptr; } }

// ---------------------------------------------------------------------------------------------
// Diagnostics: one line per step in %ProgramData%\SprigCast Camera\camera.log (made writable for the
// Frame Server and users by DllRegisterServer), plus OutputDebugString. Shows whether Windows loaded us.

static void LogPath(wchar_t* out, DWORD n) {
    wchar_t base[MAX_PATH] = L"C:\\ProgramData";
    GetEnvironmentVariableW(L"ProgramData", base, MAX_PATH);
    _snwprintf_s(out, n, _TRUNCATE, L"%s\\SprigCast Camera\\camera.log", base);
}

static void Log(const char* fmt, ...) {
    char text[600];
    va_list args;
    va_start(args, fmt);
    vsnprintf(text, sizeof(text), fmt, args);
    va_end(args);
    char exe[MAX_PATH] = "";
    GetModuleFileNameA(nullptr, exe, MAX_PATH);
    const char* name = strrchr(exe, '\\');
    SYSTEMTIME t;
    GetLocalTime(&t);
    char line[900];
    int len = _snprintf_s(line, sizeof(line), _TRUNCATE, "%02d:%02d:%02d.%03d %s[%lu] %s\r\n", t.wHour, t.wMinute,
                          t.wSecond, t.wMilliseconds, name ? name + 1 : exe, GetCurrentProcessId(), text);
    OutputDebugStringA(line);
    wchar_t path[MAX_PATH];
    LogPath(path, MAX_PATH);
    HANDLE f = CreateFileW(path, FILE_APPEND_DATA, FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr, OPEN_ALWAYS,
                           FILE_ATTRIBUTE_NORMAL, nullptr);
    if (f == INVALID_HANDLE_VALUE) return;
    LARGE_INTEGER size;
    if (GetFileSizeEx(f, &size) && size.QuadPart > 2 * 1024 * 1024) {  // keep it small: start over
        CloseHandle(f);
        f = CreateFileW(path, GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr, CREATE_ALWAYS,
                        FILE_ATTRIBUTE_NORMAL, nullptr);
        if (f == INVALID_HANDLE_VALUE) return;
    }
    DWORD wrote = 0;
    if (len > 0) WriteFile(f, line, (DWORD)len, &wrote, nullptr);
    CloseHandle(f);
}

static void Guid(REFGUID g, char* out, size_t n) {
    _snprintf_s(out, n, _TRUNCATE, "{%08lX-%04X-%04X-%02X%02X-%02X%02X%02X%02X%02X%02X}", g.Data1, g.Data2, g.Data3,
                g.Data4[0], g.Data4[1], g.Data4[2], g.Data4[3], g.Data4[4], g.Data4[5], g.Data4[6], g.Data4[7]);
}

// ---------------------------------------------------------------------------------------------
// Shared memory

struct Shared {
    HANDLE map = nullptr;
    BYTE* view = nullptr;

    bool Open() {
        if (view) return true;
        // SYSTEM, Local Service (the Frame Server), admins and the signed-in user may read and write.
        PSECURITY_DESCRIPTOR sd = nullptr;
        if (!ConvertStringSecurityDescriptorToSecurityDescriptorW(
                L"D:(A;;GA;;;SY)(A;;GA;;;LS)(A;;GA;;;BA)(A;;GA;;;IU)", SDDL_REVISION_1, &sd, nullptr))
            return false;
        SECURITY_ATTRIBUTES sa = {sizeof(sa), sd, FALSE};
        DWORD size = HEADER + MAX_W * MAX_W * 3 / 2;
        map = CreateFileMappingW(INVALID_HANDLE_VALUE, &sa, PAGE_READWRITE, 0, size, SHARED_NAME);
        LocalFree(sd);
        if (!map) return false;
        view = static_cast<BYTE*>(MapViewOfFile(map, FILE_MAP_ALL_ACCESS, 0, 0, 0));
        if (!view) { CloseHandle(map); map = nullptr; return false; }
        *reinterpret_cast<volatile UINT32*>(view + 0) = MAGIC;
        *reinterpret_cast<volatile UINT32*>(view + 4) = VERSION;
        return true;
    }
    void Close() {
        if (view) UnmapViewOfFile(view);
        if (map) CloseHandle(map);
        view = nullptr; map = nullptr;
    }
    void Want(UINT32 w, UINT32 h) {
        if (!view) return;
        *reinterpret_cast<volatile UINT32*>(view + 8) = w;
        *reinterpret_cast<volatile UINT32*>(view + 12) = h;
    }
    void Beat() {
        if (view) *reinterpret_cast<volatile UINT64*>(view + 32) = GetTickCount64();
    }
    // Copy the latest frame if it is new and the right size. Returns true when `out` was filled.
    bool Read(BYTE* out, UINT32 w, UINT32 h, UINT64& last_seq) {
        if (!view) return false;
        volatile UINT64* seq = reinterpret_cast<volatile UINT64*>(view + 24);
        UINT64 before = *seq;
        if ((before & 1) || before == last_seq) return false;
        if (*reinterpret_cast<volatile UINT32*>(view + 16) != w || *reinterpret_cast<volatile UINT32*>(view + 20) != h)
            return false;
        MemoryBarrier();
        memcpy(out, view + HEADER, (size_t)w * h * 3 / 2);
        MemoryBarrier();
        if (*seq != before) return false;  // written while we copied: keep the previous frame
        last_seq = before;
        return true;
    }
};

// ---------------------------------------------------------------------------------------------
// IKsControl: the Frame Server asks for camera properties; we have none.

#define KS_NOT_FOUND HRESULT_FROM_WIN32(ERROR_SET_NOT_FOUND)

// ---------------------------------------------------------------------------------------------
// Media stream

class Source;

class Stream : public IMFMediaStream2, public IKsControl {
public:
    Stream(Source* source, IMFStreamDescriptor* sd) : source_(source), sd_(sd) {
        sd_->AddRef();
        InitializeSRWLock(&lock_);
        MFCreateEventQueue(&queue_);
        InterlockedIncrement(&g_objects);
    }

    // IUnknown
    STDMETHODIMP QueryInterface(REFIID riid, void** ppv) override {
        if (!ppv) return E_POINTER;
        if (riid == IID_IUnknown || riid == __uuidof(IMFMediaEventGenerator) || riid == __uuidof(IMFMediaStream) ||
            riid == __uuidof(IMFMediaStream2))
            *ppv = static_cast<IMFMediaStream2*>(this);
        else if (riid == __uuidof(IKsControl))
            *ppv = static_cast<IKsControl*>(this);
        else { *ppv = nullptr; return E_NOINTERFACE; }
        AddRef();
        return S_OK;
    }
    STDMETHODIMP_(ULONG) AddRef() override { return InterlockedIncrement(&ref_); }
    STDMETHODIMP_(ULONG) Release() override {
        ULONG n = InterlockedDecrement(&ref_);
        if (!n) delete this;
        return n;
    }

    // IMFMediaEventGenerator
    STDMETHODIMP GetEvent(DWORD flags, IMFMediaEvent** ev) override {
        IMFMediaEventQueue* q = nullptr;
        AcquireSRWLockExclusive(&lock_);
        if (queue_) { q = queue_; q->AddRef(); }
        ReleaseSRWLockExclusive(&lock_);
        if (!q) return MF_E_SHUTDOWN;
        HRESULT hr = q->GetEvent(flags, ev);  // may block: not under the lock
        q->Release();
        return hr;
    }
    STDMETHODIMP BeginGetEvent(IMFAsyncCallback* cb, IUnknown* state) override {
        Guard g(lock_);
        return queue_ ? queue_->BeginGetEvent(cb, state) : MF_E_SHUTDOWN;
    }
    STDMETHODIMP EndGetEvent(IMFAsyncResult* r, IMFMediaEvent** ev) override {
        Guard g(lock_);
        return queue_ ? queue_->EndGetEvent(r, ev) : MF_E_SHUTDOWN;
    }
    STDMETHODIMP QueueEvent(MediaEventType t, REFGUID ext, HRESULT status, const PROPVARIANT* v) override {
        Guard g(lock_);
        return queue_ ? queue_->QueueEventParamVar(t, ext, status, v) : MF_E_SHUTDOWN;
    }

    // IMFMediaStream
    STDMETHODIMP GetMediaSource(IMFMediaSource** out) override;
    STDMETHODIMP GetStreamDescriptor(IMFStreamDescriptor** out) override {
        if (!out) return E_POINTER;
        Guard g(lock_);
        if (!queue_) return MF_E_SHUTDOWN;
        *out = sd_; sd_->AddRef();
        return S_OK;
    }
    STDMETHODIMP RequestSample(IUnknown* token) override;

    // IMFMediaStream2
    STDMETHODIMP SetStreamState(MF_STREAM_STATE s) override {
        Guard g(lock_);
        if (!queue_) return MF_E_SHUTDOWN;
        if (s == MF_STREAM_STATE_PAUSED) return MF_E_INVALIDREQUEST;
        state_ = s;
        return S_OK;
    }
    STDMETHODIMP GetStreamState(MF_STREAM_STATE* s) override {
        if (!s) return E_POINTER;
        Guard g(lock_);
        *s = state_;
        return S_OK;
    }

    // IKsControl
    STDMETHODIMP KsProperty(PKSPROPERTY, ULONG, LPVOID, ULONG, ULONG*) override { return KS_NOT_FOUND; }
    STDMETHODIMP KsMethod(PKSMETHOD, ULONG, LPVOID, ULONG, ULONG*) override { return KS_NOT_FOUND; }
    STDMETHODIMP KsEvent(PKSEVENT, ULONG, LPVOID, ULONG, ULONG*) override { return KS_NOT_FOUND; }

    // Called by the source. The Frame Server hands us its sample allocator (IMFSampleAllocatorControl);
    // samples from it cross into the app's process cheaply. Without one we fall back to memory buffers.
    HRESULT SetAllocator(IUnknown* allocator) {
        Guard g(lock_);
        SafeRelease(allocator_);
        return allocator ? allocator->QueryInterface(IID_PPV_ARGS(&allocator_)) : S_OK;
    }
    HRESULT Start() {
        Guard g(lock_);
        if (!queue_) return MF_E_SHUTDOWN;
        IMFMediaTypeHandler* handler = nullptr;
        IMFMediaType* type = nullptr;
        HRESULT hr = sd_->GetMediaTypeHandler(&handler);
        if (SUCCEEDED(hr)) hr = handler->GetCurrentMediaType(&type);
        if (SUCCEEDED(hr)) hr = MFGetAttributeSize(type, MF_MT_FRAME_SIZE, &width_, &height_);
        if (SUCCEEDED(hr) && allocator_) {
            allocator_->UninitializeSampleAllocator();
            if (FAILED(allocator_->InitializeSampleAllocator(10, type))) SafeRelease(allocator_);
        }
        SafeRelease(type);
        SafeRelease(handler);
        if (FAILED(hr)) return hr;
        delete[] frame_;
        frame_ = new (std::nothrow) BYTE[(size_t)width_ * height_ * 3 / 2];
        if (!frame_) return E_OUTOFMEMORY;
        Placeholder();
        have_frame_ = false;
        last_seq_ = 0;
        last_new_ = GetTickCount64();
        next_due_ = 0;
        state_ = MF_STREAM_STATE_RUNNING;
        return queue_->QueueEventParamVar(MEStreamStarted, GUID_NULL, S_OK, nullptr);
    }
    HRESULT Stop() {
        Guard g(lock_);
        if (!queue_) return MF_E_SHUTDOWN;
        state_ = MF_STREAM_STATE_STOPPED;
        if (allocator_) allocator_->UninitializeSampleAllocator();
        return queue_->QueueEventParamVar(MEStreamStopped, GUID_NULL, S_OK, nullptr);
    }
    void Shutdown() {
        Guard g(lock_);
        if (queue_) { queue_->Shutdown(); queue_->Release(); queue_ = nullptr; }
        if (allocator_) { allocator_->UninitializeSampleAllocator(); SafeRelease(allocator_); }
        state_ = MF_STREAM_STATE_STOPPED;
    }
    UINT32 Width() { Guard g(lock_); return width_; }
    UINT32 Height() { Guard g(lock_); return height_; }

private:
    struct Guard {
        SRWLOCK& l;
        explicit Guard(SRWLOCK& lock) : l(lock) { AcquireSRWLockExclusive(&l); }
        ~Guard() { ReleaseSRWLockExclusive(&l); }
    };

    ~Stream() {
        if (queue_) { queue_->Shutdown(); queue_->Release(); }
        SafeRelease(allocator_);
        SafeRelease(sd_);
        delete[] frame_;
        InterlockedDecrement(&g_objects);
    }

    void Placeholder() {  // SprigCast's dark canopy green until the first frame arrives
        size_t y = (size_t)width_ * height_;
        memset(frame_, 38, y);
        for (size_t i = 0; i < y / 2; i += 2) { frame_[y + i] = 122; frame_[y + i + 1] = 116; }
    }

    LONG ref_ = 1;
    SRWLOCK lock_;
    Source* source_;  // not owned: the source outlives its stream's use
    IMFStreamDescriptor* sd_;
    IMFMediaEventQueue* queue_ = nullptr;
    IMFVideoSampleAllocatorEx* allocator_ = nullptr;
    MF_STREAM_STATE state_ = MF_STREAM_STATE_STOPPED;
    UINT32 width_ = 0, height_ = 0;
    BYTE* frame_ = nullptr;
    bool have_frame_ = false;
    UINT64 last_seq_ = 0;
    ULONGLONG last_new_ = 0;
    ULONGLONG next_due_ = 0;
};

// ---------------------------------------------------------------------------------------------
// Media source

class Source : public IMFMediaSource2, public IMFGetService, public IKsControl, public IMFSampleAllocatorControl {
public:
    static HRESULT Create(IMFAttributes* from, Source** out) {
        Source* s = new (std::nothrow) Source();
        if (!s) return E_OUTOFMEMORY;
        HRESULT hr = s->Init(from);
        if (FAILED(hr)) { s->Release(); return hr; }
        *out = s;
        return S_OK;
    }

    // IUnknown
    STDMETHODIMP QueryInterface(REFIID riid, void** ppv) override {
        if (!ppv) return E_POINTER;
        if (riid == IID_IUnknown || riid == __uuidof(IMFMediaEventGenerator) || riid == __uuidof(IMFMediaSource) ||
            riid == __uuidof(IMFMediaSourceEx) || riid == __uuidof(IMFMediaSource2))
            *ppv = static_cast<IMFMediaSource2*>(this);
        else if (riid == __uuidof(IMFSampleAllocatorControl))
            *ppv = static_cast<IMFSampleAllocatorControl*>(this);
        else if (riid == __uuidof(IMFGetService))
            *ppv = static_cast<IMFGetService*>(this);
        else if (riid == __uuidof(IKsControl))
            *ppv = static_cast<IKsControl*>(this);
        else { *ppv = nullptr; return E_NOINTERFACE; }
        AddRef();
        return S_OK;
    }
    STDMETHODIMP_(ULONG) AddRef() override { return InterlockedIncrement(&ref_); }
    STDMETHODIMP_(ULONG) Release() override {
        ULONG n = InterlockedDecrement(&ref_);
        if (!n) delete this;
        return n;
    }

    // IMFMediaEventGenerator
    STDMETHODIMP GetEvent(DWORD flags, IMFMediaEvent** ev) override {
        IMFMediaEventQueue* q = nullptr;
        AcquireSRWLockExclusive(&lock_);
        if (queue_) { q = queue_; q->AddRef(); }
        ReleaseSRWLockExclusive(&lock_);
        if (!q) return MF_E_SHUTDOWN;
        HRESULT hr = q->GetEvent(flags, ev);
        q->Release();
        return hr;
    }
    STDMETHODIMP BeginGetEvent(IMFAsyncCallback* cb, IUnknown* state) override {
        Lock g(this);
        return queue_ ? queue_->BeginGetEvent(cb, state) : MF_E_SHUTDOWN;
    }
    STDMETHODIMP EndGetEvent(IMFAsyncResult* r, IMFMediaEvent** ev) override {
        Lock g(this);
        return queue_ ? queue_->EndGetEvent(r, ev) : MF_E_SHUTDOWN;
    }
    STDMETHODIMP QueueEvent(MediaEventType t, REFGUID ext, HRESULT status, const PROPVARIANT* v) override {
        Lock g(this);
        return queue_ ? queue_->QueueEventParamVar(t, ext, status, v) : MF_E_SHUTDOWN;
    }

    // IMFMediaSource
    STDMETHODIMP GetCharacteristics(DWORD* c) override {
        if (!c) return E_POINTER;
        Lock g(this);
        if (!queue_) return MF_E_SHUTDOWN;
        *c = MFMEDIASOURCE_IS_LIVE;
        return S_OK;
    }
    STDMETHODIMP CreatePresentationDescriptor(IMFPresentationDescriptor** out) override {
        if (!out) return E_POINTER;
        Lock g(this);
        if (!queue_) return MF_E_SHUTDOWN;
        return pd_->Clone(out);
    }
    STDMETHODIMP Start(IMFPresentationDescriptor* pd, const GUID* format, const PROPVARIANT* at) override {
        if (!pd || !at) return E_INVALIDARG;
        if (format && *format != GUID_NULL) return MF_E_UNSUPPORTED_TIME_FORMAT;
        Lock g(this);
        if (!queue_) return MF_E_SHUTDOWN;
        BOOL selected = FALSE;
        IMFStreamDescriptor* sd = nullptr;
        HRESULT hr = pd->GetStreamDescriptorByIndex(0, &selected, &sd);
        if (FAILED(hr)) return hr;
        // Use the media type the app picked on its copy of the descriptor.
        IMFMediaTypeHandler *theirs = nullptr, *ours = nullptr;
        IMFMediaType* type = nullptr;
        if (SUCCEEDED(sd->GetMediaTypeHandler(&theirs)) && SUCCEEDED(theirs->GetCurrentMediaType(&type)) &&
            SUCCEEDED(stream_sd_->GetMediaTypeHandler(&ours)))
            ours->SetCurrentMediaType(type);
        SafeRelease(type); SafeRelease(theirs); SafeRelease(ours); SafeRelease(sd);
        if (!selected) return S_OK;
        if (!shared_.Open()) return HRESULT_FROM_WIN32(GetLastError());
        hr = queue_->QueueEventParamUnk(started_once_ ? MEUpdatedStream : MENewStream, GUID_NULL, S_OK,
                                        static_cast<IMFMediaStream2*>(stream_));
        if (SUCCEEDED(hr)) hr = stream_->Start();
        Log("source start %ux%u: 0x%08lX", stream_->Width(), stream_->Height(), hr);
        if (SUCCEEDED(hr)) {
            shared_.Want(stream_->Width(), stream_->Height());
            started_once_ = true;
            PROPVARIANT now;
            PropVariantInit(&now);
            now.vt = VT_I8;
            now.hVal.QuadPart = MFGetSystemTime();
            hr = queue_->QueueEventParamVar(MESourceStarted, GUID_NULL, S_OK, &now);
        }
        return hr;
    }
    STDMETHODIMP Stop() override {
        Lock g(this);
        if (!queue_) return MF_E_SHUTDOWN;
        HRESULT hr = stream_->Stop();
        shared_.Want(0, 0);
        if (SUCCEEDED(hr)) hr = queue_->QueueEventParamVar(MESourceStopped, GUID_NULL, S_OK, nullptr);
        return hr;
    }
    STDMETHODIMP Pause() override { return MF_E_INVALID_STATE_TRANSITION; }
    STDMETHODIMP Shutdown() override {
        Log("source shutdown");
        Lock g(this);
        if (!queue_) return MF_E_SHUTDOWN;
        if (stream_) stream_->Shutdown();
        queue_->Shutdown();
        SafeRelease(queue_);
        shared_.Want(0, 0);
        shared_.Close();
        return S_OK;
    }

    // IMFMediaSourceEx
    STDMETHODIMP GetSourceAttributes(IMFAttributes** out) override {
        if (!out) return E_POINTER;
        Lock g(this);
        if (!queue_) return MF_E_SHUTDOWN;
        *out = attributes_; attributes_->AddRef();
        return S_OK;
    }
    STDMETHODIMP GetStreamAttributes(DWORD id, IMFAttributes** out) override {
        if (!out) return E_POINTER;
        if (id != 0) return MF_E_INVALIDSTREAMNUMBER;
        Lock g(this);
        if (!queue_) return MF_E_SHUTDOWN;
        *out = stream_sd_; stream_sd_->AddRef();
        return S_OK;
    }
    STDMETHODIMP SetD3DManager(IUnknown*) override { return S_OK; }  // system-memory samples only

    // IMFMediaSource2
    STDMETHODIMP SetMediaType(DWORD id, IMFMediaType* type) override {
        if (id != 0) return MF_E_INVALIDSTREAMNUMBER;
        if (!type) return E_POINTER;
        Lock g(this);
        if (!queue_) return MF_E_SHUTDOWN;
        return S_OK;  // Start() picks up the type from the presentation descriptor
    }

    // IMFSampleAllocatorControl
    STDMETHODIMP SetDefaultAllocator(DWORD id, IUnknown* allocator) override {
        if (id != 0) return MF_E_INVALIDSTREAMNUMBER;
        if (!allocator) return E_POINTER;
        Log("allocator provided");
        Lock g(this);
        if (!queue_) return MF_E_SHUTDOWN;
        return stream_->SetAllocator(allocator);
    }
    STDMETHODIMP GetAllocatorUsage(DWORD id, DWORD* input, MFSampleAllocatorUsage* usage) override {
        if (id != 0) return MF_E_INVALIDSTREAMNUMBER;
        if (!input || !usage) return E_POINTER;
        *input = id;
        *usage = MFSampleAllocatorUsage_UsesProvidedAllocator;
        return S_OK;
    }

    // IMFGetService
    STDMETHODIMP GetService(REFGUID, REFIID, LPVOID*) override { return MF_E_UNSUPPORTED_SERVICE; }

    // IKsControl
    STDMETHODIMP KsProperty(PKSPROPERTY, ULONG, LPVOID, ULONG, ULONG*) override { return KS_NOT_FOUND; }
    STDMETHODIMP KsMethod(PKSMETHOD, ULONG, LPVOID, ULONG, ULONG*) override { return KS_NOT_FOUND; }
    STDMETHODIMP KsEvent(PKSEVENT, ULONG, LPVOID, ULONG, ULONG*) override { return KS_NOT_FOUND; }

    Shared& shared() { return shared_; }

private:
    struct Lock {
        Source* s;
        explicit Lock(Source* src) : s(src) { AcquireSRWLockExclusive(&s->lock_); }
        ~Lock() { ReleaseSRWLockExclusive(&s->lock_); }
    };

    Source() { InitializeSRWLock(&lock_); InterlockedIncrement(&g_objects); }
    ~Source() {
        if (queue_) queue_->Shutdown();
        SafeRelease(queue_);
        if (stream_) { stream_->Shutdown(); stream_->Release(); }
        SafeRelease(stream_sd_);
        SafeRelease(pd_);
        SafeRelease(attributes_);
        shared_.Close();
        InterlockedDecrement(&g_objects);
    }

    HRESULT Init(IMFAttributes* from) {
        HRESULT hr = MFCreateEventQueue(&queue_);
        if (SUCCEEDED(hr)) hr = MFCreateAttributes(&attributes_, 8);
        // The Frame Server expects the activator's attributes on the source, plus a sensor profile list.
        if (SUCCEEDED(hr) && from) hr = from->CopyAllItems(attributes_);
        IMFSensorProfileCollection* profiles = nullptr;
        IMFSensorProfile* profile = nullptr;
        if (SUCCEEDED(hr)) hr = MFCreateSensorProfileCollection(&profiles);
        if (SUCCEEDED(hr)) hr = MFCreateSensorProfile(KSCAMERAPROFILE_Legacy, 0, nullptr, &profile);
        if (SUCCEEDED(hr)) hr = profile->AddProfileFilter(0, L"((RES==;FRT<=30,1;SUT==))");
        if (SUCCEEDED(hr)) hr = profiles->AddProfile(profile);
        SafeRelease(profile);
        if (SUCCEEDED(hr)) hr = MFCreateSensorProfile(KSCAMERAPROFILE_HighFrameRate, 0, nullptr, &profile);
        if (SUCCEEDED(hr)) hr = profile->AddProfileFilter(0, L"((RES==;FRT>=60,1;SUT==))");
        if (SUCCEEDED(hr)) hr = profiles->AddProfile(profile);
        SafeRelease(profile);
        if (SUCCEEDED(hr)) hr = attributes_->SetUnknown(MF_DEVICEMFT_SENSORPROFILE_COLLECTION, profiles);
        SafeRelease(profiles);
        if (FAILED(hr)) Log("source attributes failed: 0x%08lX", hr);
        const DWORD n = ARRAYSIZE(SIZES);
        IMFMediaType* types[n] = {};
        for (DWORD i = 0; SUCCEEDED(hr) && i < n; i++) {
            hr = MFCreateMediaType(&types[i]);
            IMFMediaType* t = types[i];
            if (SUCCEEDED(hr)) hr = t->SetGUID(MF_MT_MAJOR_TYPE, MFMediaType_Video);
            if (SUCCEEDED(hr)) hr = t->SetGUID(MF_MT_SUBTYPE, MFVideoFormat_NV12);
            if (SUCCEEDED(hr)) hr = MFSetAttributeSize(t, MF_MT_FRAME_SIZE, SIZES[i].cx, SIZES[i].cy);
            if (SUCCEEDED(hr)) hr = MFSetAttributeRatio(t, MF_MT_FRAME_RATE, FPS, 1);
            if (SUCCEEDED(hr)) hr = t->SetUINT32(MF_MT_AVG_BITRATE, SIZES[i].cx * SIZES[i].cy * 12 * FPS);
            if (SUCCEEDED(hr)) hr = MFSetAttributeRatio(t, MF_MT_PIXEL_ASPECT_RATIO, 1, 1);
            if (SUCCEEDED(hr)) hr = t->SetUINT32(MF_MT_INTERLACE_MODE, MFVideoInterlace_Progressive);
            if (SUCCEEDED(hr)) hr = t->SetUINT32(MF_MT_ALL_SAMPLES_INDEPENDENT, TRUE);
            if (SUCCEEDED(hr)) hr = t->SetUINT32(MF_MT_FIXED_SIZE_SAMPLES, TRUE);
            if (SUCCEEDED(hr)) hr = t->SetUINT32(MF_MT_DEFAULT_STRIDE, SIZES[i].cx);
            if (SUCCEEDED(hr)) hr = t->SetUINT32(MF_MT_SAMPLE_SIZE, SIZES[i].cx * SIZES[i].cy * 3 / 2);
            if (SUCCEEDED(hr)) hr = t->SetUINT32(MF_MT_VIDEO_NOMINAL_RANGE, MFNominalRange_16_235);
            if (SUCCEEDED(hr)) hr = t->SetUINT32(MF_MT_YUV_MATRIX, MFVideoTransferMatrix_BT709);
        }
        if (SUCCEEDED(hr)) hr = MFCreateStreamDescriptor(0, n, types, &stream_sd_);
        IMFMediaTypeHandler* handler = nullptr;
        if (SUCCEEDED(hr)) hr = stream_sd_->GetMediaTypeHandler(&handler);
        if (SUCCEEDED(hr)) hr = handler->SetCurrentMediaType(types[0]);
        SafeRelease(handler);
        for (DWORD i = 0; i < n; i++) SafeRelease(types[i]);
        if (SUCCEEDED(hr)) hr = stream_sd_->SetGUID(MF_DEVICESTREAM_STREAM_CATEGORY, PINNAME_VIDEO_CAPTURE);
        if (SUCCEEDED(hr)) hr = stream_sd_->SetUINT32(MF_DEVICESTREAM_STREAM_ID, 0);
        if (SUCCEEDED(hr)) hr = stream_sd_->SetUINT32(MF_DEVICESTREAM_FRAMESERVER_SHARED, 1);
        if (SUCCEEDED(hr)) hr = stream_sd_->SetUINT32(MF_DEVICESTREAM_ATTRIBUTE_FRAMESOURCE_TYPES,
                                                      MFFrameSourceTypes_Color);
        if (SUCCEEDED(hr)) hr = MFCreatePresentationDescriptor(1, &stream_sd_, &pd_);
        if (SUCCEEDED(hr)) hr = pd_->SelectStream(0);
        if (SUCCEEDED(hr)) {
            stream_ = new (std::nothrow) Stream(this, stream_sd_);
            if (!stream_) hr = E_OUTOFMEMORY;
        }
        return hr;
    }

    LONG ref_ = 1;
    SRWLOCK lock_;
    IMFMediaEventQueue* queue_ = nullptr;
    IMFAttributes* attributes_ = nullptr;
    IMFStreamDescriptor* stream_sd_ = nullptr;
    IMFPresentationDescriptor* pd_ = nullptr;
    Stream* stream_ = nullptr;
    bool started_once_ = false;
    Shared shared_;

    friend class Stream;
};

STDMETHODIMP Stream::GetMediaSource(IMFMediaSource** out) {
    if (!out) return E_POINTER;
    Guard g(lock_);
    if (!queue_) return MF_E_SHUTDOWN;
    *out = static_cast<IMFMediaSource2*>(source_);
    (*out)->AddRef();
    return S_OK;
}

STDMETHODIMP Stream::RequestSample(IUnknown* token) {
    UINT32 w, h;
    {
        Guard g(lock_);
        if (!queue_) return MF_E_SHUTDOWN;
        if (state_ != MF_STREAM_STATE_RUNNING) return MF_E_INVALIDREQUEST;
        w = width_; h = height_;
    }
    // Pace to FPS: the pipeline asks again as soon as it gets a sample.
    ULONGLONG now = GetTickCount64();
    if (next_due_ > now) Sleep((DWORD)(next_due_ - now));
    next_due_ = max(next_due_, now) + 1000 / FPS;

    Shared& shared = source_->shared();
    shared.Want(w, h);
    shared.Beat();
    DWORD bytes = w * h * 3 / 2;
    IMFSample* sample = nullptr;
    IMFMediaBuffer* buffer = nullptr;
    HRESULT hr = S_OK;
    bool pooled = false;
    {
        Guard g(lock_);
        if (allocator_ && SUCCEEDED(allocator_->AllocateSample(&sample))) pooled = true;
    }
    if (pooled) {
        hr = sample->GetBufferByIndex(0, &buffer);
    } else {
        hr = MFCreateSample(&sample);
        if (SUCCEEDED(hr)) hr = MFCreateMemoryBuffer(bytes, &buffer);
        if (SUCCEEDED(hr)) hr = sample->AddBuffer(buffer);
    }
    if (SUCCEEDED(hr)) {
        // Copy row by row: the allocator's buffers may have a wider pitch than the picture.
        IMF2DBuffer2* two_d = nullptr;
        BYTE* data = nullptr;
        BYTE* start = nullptr;
        LONG pitch = (LONG)w;
        DWORD length = 0;
        bool locked_2d = SUCCEEDED(buffer->QueryInterface(IID_PPV_ARGS(&two_d))) &&
                         SUCCEEDED(two_d->Lock2DSize(MF2DBuffer_LockFlags_Write, &data, &pitch, &start, &length));
        if (!locked_2d) {
            SafeRelease(two_d);
            hr = buffer->Lock(&data, nullptr, nullptr);
            pitch = (LONG)w;
        }
        if (SUCCEEDED(hr) && pitch > 0) {
            Guard g(lock_);
            if (frame_ && width_ == w && height_ == h) {
                if (shared.Read(frame_, w, h, last_seq_)) {
                    have_frame_ = true;
                    last_new_ = GetTickCount64();
                } else if (have_frame_ && GetTickCount64() - last_new_ > 2000) {
                    Placeholder();  // SprigCast stopped sending (closed, or the camera was turned off)
                    have_frame_ = false;
                }
                if ((UINT32)pitch == w) memcpy(data, frame_, bytes);
                else {
                    for (UINT32 row = 0; row < h; row++) memcpy(data + (size_t)row * pitch, frame_ + (size_t)row * w, w);
                    BYTE* uv = data + (size_t)pitch * h;
                    const BYTE* src = frame_ + (size_t)w * h;
                    for (UINT32 row = 0; row < h / 2; row++) memcpy(uv + (size_t)row * pitch, src + (size_t)row * w, w);
                }
            }
        }
        if (locked_2d) { two_d->Unlock2D(); SafeRelease(two_d); }
        else if (SUCCEEDED(hr)) buffer->Unlock();
        if (SUCCEEDED(hr)) hr = buffer->SetCurrentLength(locked_2d ? length : bytes);
    }
    if (SUCCEEDED(hr)) hr = sample->SetSampleTime(MFGetSystemTime());
    if (SUCCEEDED(hr)) hr = sample->SetSampleDuration(10000000LL / FPS);
    if (SUCCEEDED(hr) && token) hr = sample->SetUnknown(MFSampleExtension_Token, token);
    if (SUCCEEDED(hr)) {
        Guard g(lock_);
        hr = queue_ ? queue_->QueueEventParamUnk(MEMediaSample, GUID_NULL, S_OK, sample) : MF_E_SHUTDOWN;
    }
    SafeRelease(buffer);
    SafeRelease(sample);
    return hr;
}

// ---------------------------------------------------------------------------------------------
// Activator: what the Frame Server creates from our CLSID; ActivateObject makes the source.

class Activator : public IMFActivate {
public:
    Activator() { InterlockedIncrement(&g_objects); }
    HRESULT Init() {
        HRESULT hr = MFCreateAttributes(&attrs_, 4);
        if (SUCCEEDED(hr)) hr = attrs_->SetUINT32(MF_VIRTUALCAMERA_PROVIDE_ASSOCIATED_CAMERA_SOURCES, 1);
        if (SUCCEEDED(hr)) hr = attrs_->SetGUID(MFT_TRANSFORM_CLSID_Attribute, CLSID_SprigCastCamera);
        if (SUCCEEDED(hr)) hr = Source::Create(attrs_, &source_);
        return hr;
    }

    STDMETHODIMP QueryInterface(REFIID riid, void** ppv) override {
        if (!ppv) return E_POINTER;
        if (riid == IID_IUnknown || riid == __uuidof(IMFAttributes) || riid == __uuidof(IMFActivate)) {
            *ppv = static_cast<IMFActivate*>(this);
            AddRef();
            return S_OK;
        }
        *ppv = nullptr;
        return E_NOINTERFACE;
    }
    STDMETHODIMP_(ULONG) AddRef() override { return InterlockedIncrement(&ref_); }
    STDMETHODIMP_(ULONG) Release() override {
        ULONG n = InterlockedDecrement(&ref_);
        if (!n) delete this;
        return n;
    }

    STDMETHODIMP ActivateObject(REFIID riid, void** ppv) override {
        AcquireSRWLockExclusive(&lock_);
        HRESULT hr = S_OK;
        if (!source_) hr = Source::Create(attrs_, &source_);
        if (SUCCEEDED(hr)) hr = source_->QueryInterface(riid, ppv);
        ReleaseSRWLockExclusive(&lock_);
        char iid[48];
        Guid(riid, iid, sizeof(iid));
        Log("activate %s: 0x%08lX", iid, hr);
        return hr;
    }
    // The Frame Server shuts the source down itself; shutting it down here too broke Start (MF_E_SHUTDOWN).
    STDMETHODIMP ShutdownObject() override { return S_OK; }
    STDMETHODIMP DetachObject() override {
        AcquireSRWLockExclusive(&lock_);
        SafeRelease(source_);
        ReleaseSRWLockExclusive(&lock_);
        return S_OK;
    }

    // IMFAttributes, passed through
    STDMETHODIMP GetItem(REFGUID k, PROPVARIANT* v) override { return attrs_->GetItem(k, v); }
    STDMETHODIMP GetItemType(REFGUID k, MF_ATTRIBUTE_TYPE* t) override { return attrs_->GetItemType(k, t); }
    STDMETHODIMP CompareItem(REFGUID k, REFPROPVARIANT v, BOOL* r) override { return attrs_->CompareItem(k, v, r); }
    STDMETHODIMP Compare(IMFAttributes* a, MF_ATTRIBUTES_MATCH_TYPE m, BOOL* r) override { return attrs_->Compare(a, m, r); }
    STDMETHODIMP GetUINT32(REFGUID k, UINT32* v) override { return attrs_->GetUINT32(k, v); }
    STDMETHODIMP GetUINT64(REFGUID k, UINT64* v) override { return attrs_->GetUINT64(k, v); }
    STDMETHODIMP GetDouble(REFGUID k, double* v) override { return attrs_->GetDouble(k, v); }
    STDMETHODIMP GetGUID(REFGUID k, GUID* v) override { return attrs_->GetGUID(k, v); }
    STDMETHODIMP GetStringLength(REFGUID k, UINT32* n) override { return attrs_->GetStringLength(k, n); }
    STDMETHODIMP GetString(REFGUID k, LPWSTR s, UINT32 n, UINT32* l) override { return attrs_->GetString(k, s, n, l); }
    STDMETHODIMP GetAllocatedString(REFGUID k, LPWSTR* s, UINT32* l) override { return attrs_->GetAllocatedString(k, s, l); }
    STDMETHODIMP GetBlobSize(REFGUID k, UINT32* n) override { return attrs_->GetBlobSize(k, n); }
    STDMETHODIMP GetBlob(REFGUID k, UINT8* b, UINT32 n, UINT32* l) override { return attrs_->GetBlob(k, b, n, l); }
    STDMETHODIMP GetAllocatedBlob(REFGUID k, UINT8** b, UINT32* l) override { return attrs_->GetAllocatedBlob(k, b, l); }
    STDMETHODIMP GetUnknown(REFGUID k, REFIID r, LPVOID* p) override { return attrs_->GetUnknown(k, r, p); }
    STDMETHODIMP SetItem(REFGUID k, REFPROPVARIANT v) override { return attrs_->SetItem(k, v); }
    STDMETHODIMP DeleteItem(REFGUID k) override { return attrs_->DeleteItem(k); }
    STDMETHODIMP DeleteAllItems() override { return attrs_->DeleteAllItems(); }
    STDMETHODIMP SetUINT32(REFGUID k, UINT32 v) override { return attrs_->SetUINT32(k, v); }
    STDMETHODIMP SetUINT64(REFGUID k, UINT64 v) override { return attrs_->SetUINT64(k, v); }
    STDMETHODIMP SetDouble(REFGUID k, double v) override { return attrs_->SetDouble(k, v); }
    STDMETHODIMP SetGUID(REFGUID k, REFGUID v) override { return attrs_->SetGUID(k, v); }
    STDMETHODIMP SetString(REFGUID k, LPCWSTR v) override { return attrs_->SetString(k, v); }
    STDMETHODIMP SetBlob(REFGUID k, const UINT8* b, UINT32 n) override { return attrs_->SetBlob(k, b, n); }
    STDMETHODIMP SetUnknown(REFGUID k, IUnknown* u) override { return attrs_->SetUnknown(k, u); }
    STDMETHODIMP LockStore() override { return attrs_->LockStore(); }
    STDMETHODIMP UnlockStore() override { return attrs_->UnlockStore(); }
    STDMETHODIMP GetCount(UINT32* n) override { return attrs_->GetCount(n); }
    STDMETHODIMP GetItemByIndex(UINT32 i, GUID* k, PROPVARIANT* v) override { return attrs_->GetItemByIndex(i, k, v); }
    STDMETHODIMP CopyAllItems(IMFAttributes* d) override { return attrs_->CopyAllItems(d); }

private:
    ~Activator() {
        SafeRelease(source_);
        SafeRelease(attrs_);
        InterlockedDecrement(&g_objects);
    }
    LONG ref_ = 1;
    SRWLOCK lock_ = SRWLOCK_INIT;
    IMFAttributes* attrs_ = nullptr;
    Source* source_ = nullptr;
};

class Factory : public IClassFactory {
public:
    STDMETHODIMP QueryInterface(REFIID riid, void** ppv) override {
        if (!ppv) return E_POINTER;
        if (riid == IID_IUnknown || riid == IID_IClassFactory) { *ppv = this; AddRef(); return S_OK; }
        *ppv = nullptr;
        return E_NOINTERFACE;
    }
    STDMETHODIMP_(ULONG) AddRef() override { return 2; }  // static object
    STDMETHODIMP_(ULONG) Release() override { return 1; }
    STDMETHODIMP CreateInstance(IUnknown* outer, REFIID riid, void** ppv) override {
        if (outer) return CLASS_E_NOAGGREGATION;
        Activator* a = new (std::nothrow) Activator();
        if (!a) return E_OUTOFMEMORY;
        HRESULT hr = a->Init();
        if (SUCCEEDED(hr)) hr = a->QueryInterface(riid, ppv);
        char iid[48];
        Guid(riid, iid, sizeof(iid));
        Log("create activator as %s: 0x%08lX", iid, hr);
        a->Release();
        return hr;
    }
    STDMETHODIMP LockServer(BOOL lock) override {
        if (lock) InterlockedIncrement(&g_locks); else InterlockedDecrement(&g_locks);
        return S_OK;
    }
};

static Factory g_factory;

// ---------------------------------------------------------------------------------------------
// DLL exports

BOOL APIENTRY DllMain(HMODULE module, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_ATTACH) {
        g_module = module;
        DisableThreadLibraryCalls(module);
        Log("loaded (version %u)", VERSION);
    }
    return TRUE;
}

STDAPI DllGetClassObject(REFCLSID clsid, REFIID riid, LPVOID* ppv) {
    char id[48];
    Guid(clsid, id, sizeof(id));
    Log("class object for %s", id);
    if (clsid != CLSID_SprigCastCamera) return CLASS_E_CLASSNOTAVAILABLE;
    return g_factory.QueryInterface(riid, ppv);
}

STDAPI DllCanUnloadNow() { return (g_objects == 0 && g_locks == 0) ? S_OK : S_FALSE; }

static HRESULT Register(bool on) {
    wchar_t key[128];
    wsprintfW(key, L"Software\\Classes\\CLSID\\%s", CLSID_TEXT);
    if (!on) {
        LSTATUS s = RegDeleteTreeW(HKEY_LOCAL_MACHINE, key);
        return (s == ERROR_SUCCESS || s == ERROR_FILE_NOT_FOUND) ? S_OK : HRESULT_FROM_WIN32(s);
    }
    // A log folder the Frame Server (Local Service) and users may write to; regsvr32 runs elevated.
    wchar_t log[MAX_PATH];
    LogPath(log, MAX_PATH);
    *wcsrchr(log, L'\\') = 0;
    PSECURITY_DESCRIPTOR sd = nullptr;
    if (ConvertStringSecurityDescriptorToSecurityDescriptorW(
            L"D:(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;0x1301bf;;;LS)(A;OICI;0x1301bf;;;BU)", SDDL_REVISION_1, &sd,
            nullptr)) {
        SECURITY_ATTRIBUTES sa = {sizeof(sa), sd, FALSE};
        if (!CreateDirectoryW(log, &sa) && GetLastError() == ERROR_ALREADY_EXISTS)
            SetFileSecurityW(log, DACL_SECURITY_INFORMATION, sd);
        LocalFree(sd);
    }
    Log("registered");
    wchar_t path[MAX_PATH];
    if (!GetModuleFileNameW(g_module, path, MAX_PATH)) return HRESULT_FROM_WIN32(GetLastError());
    wchar_t inproc[160];
    wsprintfW(inproc, L"%s\\InprocServer32", key);
    const wchar_t* name = L"SprigCast Camera";
    const wchar_t* model = L"Both";
    LSTATUS s = RegSetKeyValueW(HKEY_LOCAL_MACHINE, key, nullptr, REG_SZ, name, (DWORD)((wcslen(name) + 1) * 2));
    if (s == ERROR_SUCCESS)
        s = RegSetKeyValueW(HKEY_LOCAL_MACHINE, inproc, nullptr, REG_SZ, path, (DWORD)((wcslen(path) + 1) * 2));
    if (s == ERROR_SUCCESS)
        s = RegSetKeyValueW(HKEY_LOCAL_MACHINE, inproc, L"ThreadingModel", REG_SZ, model,
                            (DWORD)((wcslen(model) + 1) * 2));
    return HRESULT_FROM_WIN32(s);
}

STDAPI DllRegisterServer() { return Register(true); }
STDAPI DllUnregisterServer() { return Register(false); }

// Called by SprigCast itself (ctypes): add the camera for this session, or remove it.
// The camera object lives in the multithreaded apartment, so both calls run on a short-lived MTA thread
// whatever thread Python calls from (Qt's UI thread is single-threaded, which Media Foundation dislikes).
static IMFVirtualCamera* g_camera = nullptr;
static volatile LONG g_stage = 0;  // last step SC_Start reached: 1 startup, 2 create, 3 start, 4 running
static CO_MTA_USAGE_COOKIE g_mta = nullptr;

struct Call { HRESULT (*fn)(const wchar_t*); const wchar_t* arg; HRESULT hr; };

static DWORD WINAPI MtaThread(LPVOID p) {
    Call* call = static_cast<Call*>(p);
    HRESULT co = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    call->hr = call->fn(call->arg);
    if (SUCCEEDED(co)) CoUninitialize();
    return 0;
}

static HRESULT OnMta(HRESULT (*fn)(const wchar_t*), const wchar_t* arg) {
    Call call = {fn, arg, E_FAIL};
    HANDLE t = CreateThread(nullptr, 0, MtaThread, &call, 0, nullptr);
    if (!t) return HRESULT_FROM_WIN32(GetLastError());
    WaitForSingleObject(t, INFINITE);
    CloseHandle(t);
    return call.hr;
}

static HRESULT StartCamera(const wchar_t* friendly_name) {
    if (g_camera) return S_FALSE;
    if (!g_mta && FAILED(CoIncrementMTAUsage(&g_mta))) g_mta = nullptr;  // keep the MTA alive between calls
    g_stage = 1;
    HRESULT hr = MFStartup(MF_VERSION, MFSTARTUP_NOSOCKET);
    if (SUCCEEDED(hr)) {
        g_stage = 2;
        hr = MFCreateVirtualCamera(MFVirtualCameraType_SoftwareCameraSource, MFVirtualCameraLifetime_Session,
                                   MFVirtualCameraAccess_CurrentUser, friendly_name ? friendly_name : L"SprigCast Camera",
                                   CLSID_TEXT, nullptr, 0, &g_camera);
    }
    Log("create virtual camera: 0x%08lX", hr);
    if (SUCCEEDED(hr)) { g_stage = 3; hr = g_camera->Start(nullptr); Log("start virtual camera: 0x%08lX", hr); }
    if (SUCCEEDED(hr)) g_stage = 4;
    if (FAILED(hr) && g_camera) { g_camera->Remove(); SafeRelease(g_camera); }
    return hr;
}

static HRESULT StopCamera(const wchar_t*) {
    if (!g_camera) return S_FALSE;
    // Remove only: Shutdown as well shuts the media source down twice and the camera lingers.
    HRESULT hr = g_camera->Remove();
    SafeRelease(g_camera);
    g_stage = 0;
    return hr;
}

extern "C" __declspec(dllexport) HRESULT __stdcall SC_Start(const wchar_t* friendly_name) {
    return OnMta(StartCamera, friendly_name);
}

extern "C" __declspec(dllexport) HRESULT __stdcall SC_Stop() { return OnMta(StopCamera, nullptr); }

// Make the camera the way Windows would (COM from the registry, activate, describe), but in this process.
// 0 when the source itself works, so a failing SC_Start points at Windows rather than at the camera.
static HRESULT SelfTest(const wchar_t*) {
    HRESULT hr = MFStartup(MF_VERSION, MFSTARTUP_NOSOCKET);
    IMFActivate* activate = nullptr;
    IMFMediaSource* source = nullptr;
    IMFPresentationDescriptor* pd = nullptr;
    if (SUCCEEDED(hr)) hr = CoCreateInstance(CLSID_SprigCastCamera, nullptr, CLSCTX_INPROC_SERVER, IID_PPV_ARGS(&activate));
    Log("self test: create 0x%08lX", hr);
    if (SUCCEEDED(hr)) hr = activate->ActivateObject(IID_PPV_ARGS(&source));
    if (SUCCEEDED(hr)) hr = source->CreatePresentationDescriptor(&pd);
    Log("self test: source 0x%08lX", hr);
    SafeRelease(pd);
    if (source) { source->Shutdown(); SafeRelease(source); }
    if (activate) { activate->ShutdownObject(); SafeRelease(activate); }
    return hr;
}

extern "C" __declspec(dllexport) HRESULT __stdcall SC_SelfTest() { return OnMta(SelfTest, nullptr); }

extern "C" __declspec(dllexport) UINT32 __stdcall SC_Stage() { return (UINT32)g_stage; }

extern "C" __declspec(dllexport) UINT32 __stdcall SC_Version() { return VERSION; }
