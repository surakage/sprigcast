// SprigCast Camera as a DirectShow video capture source, the way OBS's virtual camera works.
//
// Why: Windows 11's own virtual cameras (MFCreateVirtualCamera, sprigcast_camera.cpp) are refused on some
// PCs before our code ever runs. A DirectShow capture filter is registered once (regsvr32, under "Video
// Capture Sources") and every app that lists DirectShow cameras sees it: Chrome/Edge/Electron apps such as
// TikTok LIVE Studio and Discord, Zoom, OBS, ... The filter runs inside the app that opened the camera.
//
// Frames: same shared block layout as the media source, but named Local\SprigCastCamera (this login
// session), because an ordinary app may not create Global\ objects. The filter writes the size the app
// picked and a heartbeat; SprigCast renders at that size as NV12 and copies frames in. The filter turns
// NV12 into whatever the app asked for (NV12, YUY2 or I420).
//
// Written from the DirectShow documentation without the SDK base classes, so it is one small file.

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <sddl.h>
#include <dshow.h>
#include <dvdmedia.h>
#include <ks.h>
#include <ksmedia.h>
#include <ksproxy.h>
#include <new>
#include <cstring>
#include <cstdint>

#pragma comment(lib, "strmiids.lib")

void Log(const char* fmt, ...);  // sprigcast_camera.cpp

// {622E03C7-86C4-4F91-82C0-6EB79B42DCBF}
extern const CLSID CLSID_SprigCastDShow = {0x622e03c7, 0x86c4, 0x4f91, {0x82, 0xc0, 0x6e, 0xb7, 0x9b, 0x42, 0xdc, 0xbf}};
extern const wchar_t* DSHOW_CLSID_TEXT = L"{622E03C7-86C4-4F91-82C0-6EB79B42DCBF}";
static const wchar_t* NAME = L"SprigCast Camera";
static const wchar_t* LOCAL_SHARED = L"Local\\SprigCastCamera";
static const UINT32 MAGIC = 0x43565053;  // 'SPVC'
static const UINT32 BLOCK_VERSION = 1;
static const UINT32 MAX_W = 1920;
static const UINT32 HEADER = 64;
static const REFERENCE_TIME FRAME = 10000000LL / 30;
static const SIZE SIZES[] = {{1920, 1080}, {1280, 720}, {1080, 1920}, {720, 1280}, {640, 360}};
static const int N_SIZES = ARRAYSIZE(SIZES);
// MEDIASUBTYPE_I420 isn't in every SDK; it is the FOURCC GUID like the others.
static const GUID SUBTYPE_I420 = {MAKEFOURCC('I', '4', '2', '0'), 0x0000, 0x0010, {0x80, 0x00, 0x00, 0xaa, 0x00, 0x38, 0x9b, 0x71}};
struct Format { const GUID* subtype; DWORD fourcc; WORD bits; };
static const Format FORMATS[] = {
    {&MEDIASUBTYPE_NV12, MAKEFOURCC('N', 'V', '1', '2'), 12},
    {&MEDIASUBTYPE_YUY2, MAKEFOURCC('Y', 'U', 'Y', '2'), 16},
    {&SUBTYPE_I420, MAKEFOURCC('I', '4', '2', '0'), 12},
};
static const int N_FORMATS = ARRAYSIZE(FORMATS);

extern volatile LONG g_objects;  // sprigcast_camera.cpp: DllCanUnloadNow

template <class T> static void Release_(T*& p) { if (p) { p->Release(); p = nullptr; } }

static void FreeType(AM_MEDIA_TYPE& mt) {
    if (mt.cbFormat) CoTaskMemFree(mt.pbFormat);
    mt.cbFormat = 0; mt.pbFormat = nullptr;
    Release_(mt.pUnk);
}
static void DeleteType(AM_MEDIA_TYPE* mt) { if (mt) { FreeType(*mt); CoTaskMemFree(mt); } }
static HRESULT CopyType(AM_MEDIA_TYPE& to, const AM_MEDIA_TYPE& from) {
    to = from;
    to.pUnk = nullptr;
    if (from.cbFormat) {
        to.pbFormat = static_cast<BYTE*>(CoTaskMemAlloc(from.cbFormat));
        if (!to.pbFormat) { to.cbFormat = 0; return E_OUTOFMEMORY; }
        memcpy(to.pbFormat, from.pbFormat, from.cbFormat);
    }
    return S_OK;
}
static AM_MEDIA_TYPE* NewType(const AM_MEDIA_TYPE& from) {
    auto* mt = static_cast<AM_MEDIA_TYPE*>(CoTaskMemAlloc(sizeof(AM_MEDIA_TYPE)));
    if (mt && FAILED(CopyType(*mt, from))) { CoTaskMemFree(mt); mt = nullptr; }
    return mt;
}

// Media type number i: size i / N_FORMATS, format i % N_FORMATS.
static void MakeType(int i, AM_MEDIA_TYPE& mt) {
    const SIZE& s = SIZES[i / N_FORMATS];
    const Format& f = FORMATS[i % N_FORMATS];
    ZeroMemory(&mt, sizeof(mt));
    auto* vih = static_cast<VIDEOINFOHEADER*>(CoTaskMemAlloc(sizeof(VIDEOINFOHEADER)));
    if (!vih) return;
    ZeroMemory(vih, sizeof(*vih));
    DWORD bytes = (DWORD)s.cx * s.cy * f.bits / 8;
    vih->AvgTimePerFrame = FRAME;
    vih->dwBitRate = bytes * 8 * 30;
    vih->bmiHeader.biSize = sizeof(BITMAPINFOHEADER);
    vih->bmiHeader.biWidth = s.cx;
    vih->bmiHeader.biHeight = s.cy;
    vih->bmiHeader.biPlanes = 1;
    vih->bmiHeader.biBitCount = f.bits;
    vih->bmiHeader.biCompression = f.fourcc;
    vih->bmiHeader.biSizeImage = bytes;
    mt.majortype = MEDIATYPE_Video;
    mt.subtype = *f.subtype;
    mt.formattype = FORMAT_VideoInfo;
    mt.bFixedSizeSamples = TRUE;
    mt.lSampleSize = bytes;
    mt.cbFormat = sizeof(VIDEOINFOHEADER);
    mt.pbFormat = reinterpret_cast<BYTE*>(vih);
}

// What we can make: a video type in one of our formats, at one of our sizes (or any even size the app asks
// for, up to MAX_W). Fills w, h and the format index.
static bool Parse(const AM_MEDIA_TYPE* mt, UINT32& w, UINT32& h, int& format) {
    if (!mt || mt->majortype != MEDIATYPE_Video || !mt->pbFormat) return false;
    const BITMAPINFOHEADER* bmi = nullptr;
    if (mt->formattype == FORMAT_VideoInfo && mt->cbFormat >= sizeof(VIDEOINFOHEADER))
        bmi = &reinterpret_cast<const VIDEOINFOHEADER*>(mt->pbFormat)->bmiHeader;
    else if (mt->formattype == FORMAT_VideoInfo2 && mt->cbFormat >= sizeof(VIDEOINFOHEADER2))
        bmi = &reinterpret_cast<const VIDEOINFOHEADER2*>(mt->pbFormat)->bmiHeader;
    if (!bmi) return false;
    format = -1;
    for (int i = 0; i < N_FORMATS; i++)
        if (mt->subtype == *FORMATS[i].subtype) format = i;
    LONG height = bmi->biHeight < 0 ? -bmi->biHeight : bmi->biHeight;
    if (format < 0 || bmi->biWidth <= 0 || height <= 0 || (bmi->biWidth & 1) || (height & 1)) return false;
    if ((UINT32)bmi->biWidth > MAX_W || (UINT32)height > MAX_W) return false;
    w = (UINT32)bmi->biWidth;
    h = (UINT32)height;
    return true;
}

// ---------------------------------------------------------------------------------------------
// The shared block (same layout as sprigcast_camera.cpp), in this login session.

struct LocalShared {
    HANDLE map = nullptr;
    BYTE* view = nullptr;

    bool Open() {
        if (view) return true;
        // The signed-in user (SprigCast) may write even when the app with the camera runs as admin:
        // users get full access, and the low-integrity label stops "no write up" from blocking SprigCast.
        PSECURITY_DESCRIPTOR sd = nullptr;
        if (!ConvertStringSecurityDescriptorToSecurityDescriptorW(
                L"D:(A;;GA;;;SY)(A;;GA;;;BA)(A;;GA;;;IU)(A;;GA;;;OW)S:(ML;;NW;;;LW)", SDDL_REVISION_1, &sd, nullptr))
            sd = nullptr;
        SECURITY_ATTRIBUTES sa = {sizeof(sa), sd, FALSE};
        DWORD size = HEADER + MAX_W * MAX_W * 3 / 2;
        map = CreateFileMappingW(INVALID_HANDLE_VALUE, sd ? &sa : nullptr, PAGE_READWRITE, 0, size, LOCAL_SHARED);
        if (sd) LocalFree(sd);
        if (!map) { Log("dshow: shared block failed %lu", GetLastError()); return false; }
        view = static_cast<BYTE*>(MapViewOfFile(map, FILE_MAP_ALL_ACCESS, 0, 0, 0));
        if (!view) { CloseHandle(map); map = nullptr; return false; }
        *reinterpret_cast<volatile UINT32*>(view + 0) = MAGIC;
        *reinterpret_cast<volatile UINT32*>(view + 4) = BLOCK_VERSION;
        return true;
    }
    void Close() {
        if (view) {
            *reinterpret_cast<volatile UINT32*>(view + 8) = 0;
            *reinterpret_cast<volatile UINT32*>(view + 12) = 0;
            UnmapViewOfFile(view);
        }
        if (map) CloseHandle(map);
        view = nullptr; map = nullptr;
    }
    void Want(UINT32 w, UINT32 h) {
        if (!view) return;
        *reinterpret_cast<volatile UINT32*>(view + 8) = w;
        *reinterpret_cast<volatile UINT32*>(view + 12) = h;
        *reinterpret_cast<volatile UINT64*>(view + 32) = GetTickCount64();
    }
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
        if (*seq != before) return false;
        last_seq = before;
        return true;
    }
};

// ---------------------------------------------------------------------------------------------
// Small enumerators

class Filter;
class Pin;

class EnumTypes : public IEnumMediaTypes {
public:
    EnumTypes(const AM_MEDIA_TYPE* only, int pos = 0) : pos_(pos) {
        if (only) { has_only_ = true; CopyType(only_, *only); }
        InterlockedIncrement(&g_objects);
    }
    STDMETHODIMP QueryInterface(REFIID riid, void** ppv) override {
        if (!ppv) return E_POINTER;
        if (riid == IID_IUnknown || riid == IID_IEnumMediaTypes) { *ppv = this; AddRef(); return S_OK; }
        *ppv = nullptr;
        return E_NOINTERFACE;
    }
    STDMETHODIMP_(ULONG) AddRef() override { return InterlockedIncrement(&ref_); }
    STDMETHODIMP_(ULONG) Release() override { ULONG n = InterlockedDecrement(&ref_); if (!n) delete this; return n; }
    STDMETHODIMP Next(ULONG n, AM_MEDIA_TYPE** out, ULONG* fetched) override {
        if (!out) return E_POINTER;
        ULONG got = 0;
        while (got < n && pos_ < Count()) {
            AM_MEDIA_TYPE mt;
            if (has_only_) CopyType(mt, only_); else MakeType(pos_, mt);
            out[got] = static_cast<AM_MEDIA_TYPE*>(CoTaskMemAlloc(sizeof(AM_MEDIA_TYPE)));
            if (!out[got]) { FreeType(mt); break; }
            *out[got] = mt;
            got++; pos_++;
        }
        if (fetched) *fetched = got;
        return got == n ? S_OK : S_FALSE;
    }
    STDMETHODIMP Skip(ULONG n) override { pos_ += n; return pos_ <= Count() ? S_OK : S_FALSE; }
    STDMETHODIMP Reset() override { pos_ = 0; return S_OK; }
    STDMETHODIMP Clone(IEnumMediaTypes** out) override {
        if (!out) return E_POINTER;
        *out = new (std::nothrow) EnumTypes(has_only_ ? &only_ : nullptr, pos_);
        return *out ? S_OK : E_OUTOFMEMORY;
    }
private:
    ~EnumTypes() { if (has_only_) FreeType(only_); InterlockedDecrement(&g_objects); }
    int Count() const { return has_only_ ? 1 : N_SIZES * N_FORMATS; }
    LONG ref_ = 1;
    int pos_;
    bool has_only_ = false;
    AM_MEDIA_TYPE only_ = {};
};

class EnumPins : public IEnumPins {
public:
    EnumPins(IPin* pin, int pos = 0) : pin_(pin), pos_(pos) { pin_->AddRef(); InterlockedIncrement(&g_objects); }
    STDMETHODIMP QueryInterface(REFIID riid, void** ppv) override {
        if (!ppv) return E_POINTER;
        if (riid == IID_IUnknown || riid == IID_IEnumPins) { *ppv = this; AddRef(); return S_OK; }
        *ppv = nullptr;
        return E_NOINTERFACE;
    }
    STDMETHODIMP_(ULONG) AddRef() override { return InterlockedIncrement(&ref_); }
    STDMETHODIMP_(ULONG) Release() override { ULONG n = InterlockedDecrement(&ref_); if (!n) delete this; return n; }
    STDMETHODIMP Next(ULONG n, IPin** out, ULONG* fetched) override {
        if (!out) return E_POINTER;
        ULONG got = 0;
        if (n > 0 && pos_ == 0) { out[0] = pin_; pin_->AddRef(); got = 1; pos_ = 1; }
        if (fetched) *fetched = got;
        return got == n ? S_OK : S_FALSE;
    }
    STDMETHODIMP Skip(ULONG n) override { pos_ += n; return pos_ <= 1 ? S_OK : S_FALSE; }
    STDMETHODIMP Reset() override { pos_ = 0; return S_OK; }
    STDMETHODIMP Clone(IEnumPins** out) override {
        if (!out) return E_POINTER;
        *out = new (std::nothrow) EnumPins(pin_, pos_);
        return *out ? S_OK : E_OUTOFMEMORY;
    }
private:
    ~EnumPins() { pin_->Release(); InterlockedDecrement(&g_objects); }
    LONG ref_ = 1;
    IPin* pin_;
    int pos_;
};

// ---------------------------------------------------------------------------------------------
// The output pin: negotiates a format, then a thread pushes 30 frames a second while the graph runs.

class Pin : public IPin, public IAMStreamConfig, public IKsPropertySet {
public:
    explicit Pin(Filter* filter) : filter_(filter) { InitializeCriticalSection(&lock_); }
    ~Pin() {
        StopThread();
        Disconnect();
        if (have_format_) FreeType(format_);
        DeleteCriticalSection(&lock_);
    }

    // IUnknown: the pin lives and dies with its filter.
    STDMETHODIMP QueryInterface(REFIID riid, void** ppv) override;
    STDMETHODIMP_(ULONG) AddRef() override;
    STDMETHODIMP_(ULONG) Release() override;

    // IPin
    STDMETHODIMP Connect(IPin* receiver, const AM_MEDIA_TYPE* mt) override {
        if (!receiver) return E_POINTER;
        Lock g(this);
        if (peer_) return VFW_E_ALREADY_CONNECTED;
        if (mt && mt->majortype != GUID_NULL && mt->formattype != GUID_NULL && mt->pbFormat)
            return TryConnect(receiver, mt);
        if (have_format_) return TryConnect(receiver, &format_);
        // Our own list first, in our order; then whatever the receiver suggests.
        for (int i = 0; i < N_SIZES * N_FORMATS; i++) {
            AM_MEDIA_TYPE ours;
            MakeType(i, ours);
            bool partial = mt && ((mt->majortype != GUID_NULL && mt->majortype != ours.majortype) ||
                                  (mt->subtype != GUID_NULL && mt->subtype != ours.subtype));
            HRESULT hr = partial ? VFW_E_NO_ACCEPTABLE_TYPES : TryConnect(receiver, &ours);
            FreeType(ours);
            if (SUCCEEDED(hr)) return hr;
        }
        IEnumMediaTypes* theirs = nullptr;
        if (SUCCEEDED(receiver->EnumMediaTypes(&theirs))) {
            AM_MEDIA_TYPE* t = nullptr;
            while (theirs->Next(1, &t, nullptr) == S_OK) {
                HRESULT hr = TryConnect(receiver, t);
                DeleteType(t);
                if (SUCCEEDED(hr)) { theirs->Release(); return hr; }
            }
            theirs->Release();
        }
        return VFW_E_NO_ACCEPTABLE_TYPES;
    }
    STDMETHODIMP ReceiveConnection(IPin*, const AM_MEDIA_TYPE*) override { return E_UNEXPECTED; }
    STDMETHODIMP Disconnect() override {
        Lock g(this);
        if (!peer_) return S_FALSE;
        if (allocator_) { allocator_->Decommit(); Release_(allocator_); }
        Release_(input_);
        Release_(peer_);
        FreeType(mt_);
        return S_OK;
    }
    STDMETHODIMP ConnectedTo(IPin** out) override {
        if (!out) return E_POINTER;
        Lock g(this);
        *out = peer_;
        if (!peer_) return VFW_E_NOT_CONNECTED;
        peer_->AddRef();
        return S_OK;
    }
    STDMETHODIMP ConnectionMediaType(AM_MEDIA_TYPE* out) override {
        if (!out) return E_POINTER;
        Lock g(this);
        if (!peer_) { ZeroMemory(out, sizeof(*out)); return VFW_E_NOT_CONNECTED; }
        return CopyType(*out, mt_);
    }
    STDMETHODIMP QueryPinInfo(PIN_INFO* info) override;
    STDMETHODIMP QueryDirection(PIN_DIRECTION* dir) override {
        if (!dir) return E_POINTER;
        *dir = PINDIR_OUTPUT;
        return S_OK;
    }
    STDMETHODIMP QueryId(LPWSTR* id) override {
        if (!id) return E_POINTER;
        *id = static_cast<LPWSTR>(CoTaskMemAlloc(sizeof(L"Video")));
        if (!*id) return E_OUTOFMEMORY;
        memcpy(*id, L"Video", sizeof(L"Video"));
        return S_OK;
    }
    STDMETHODIMP QueryAccept(const AM_MEDIA_TYPE* mt) override {
        UINT32 w, h; int f;
        return Parse(mt, w, h, f) ? S_OK : S_FALSE;
    }
    STDMETHODIMP EnumMediaTypes(IEnumMediaTypes** out) override {
        if (!out) return E_POINTER;
        Lock g(this);
        *out = new (std::nothrow) EnumTypes(have_format_ ? &format_ : nullptr);
        return *out ? S_OK : E_OUTOFMEMORY;
    }
    STDMETHODIMP QueryInternalConnections(IPin**, ULONG*) override { return E_NOTIMPL; }
    STDMETHODIMP EndOfStream() override { return E_UNEXPECTED; }
    STDMETHODIMP BeginFlush() override { return E_UNEXPECTED; }
    STDMETHODIMP EndFlush() override { return E_UNEXPECTED; }
    STDMETHODIMP NewSegment(REFERENCE_TIME, REFERENCE_TIME, double) override { return S_OK; }

    // IAMStreamConfig: apps list our sizes and pick one before connecting.
    STDMETHODIMP SetFormat(AM_MEDIA_TYPE* mt) override {
        Lock g(this);
        if (!mt) {  // back to "anything"
            if (have_format_) { FreeType(format_); have_format_ = false; }
            return S_OK;
        }
        UINT32 w, h; int f;
        if (!Parse(mt, w, h, f)) return VFW_E_INVALIDMEDIATYPE;
        if (peer_ && !IsEqualType(*mt, mt_)) {
            if (filter_running()) return VFW_E_NOT_STOPPED;
            if (peer_->QueryAccept(mt) != S_OK) return VFW_E_INVALIDMEDIATYPE;
        }
        if (have_format_) FreeType(format_);
        have_format_ = SUCCEEDED(CopyType(format_, *mt));
        return S_OK;
    }
    STDMETHODIMP GetFormat(AM_MEDIA_TYPE** out) override {
        if (!out) return E_POINTER;
        Lock g(this);
        if (peer_) *out = NewType(mt_);
        else if (have_format_) *out = NewType(format_);
        else { AM_MEDIA_TYPE mt; MakeType(0, mt); *out = NewType(mt); FreeType(mt); }
        return *out ? S_OK : E_OUTOFMEMORY;
    }
    STDMETHODIMP GetNumberOfCapabilities(int* count, int* size) override {
        if (!count || !size) return E_POINTER;
        *count = N_SIZES * N_FORMATS;
        *size = sizeof(VIDEO_STREAM_CONFIG_CAPS);
        return S_OK;
    }
    STDMETHODIMP GetStreamCaps(int i, AM_MEDIA_TYPE** out, BYTE* caps) override {
        if (!out || !caps) return E_POINTER;
        if (i < 0 || i >= N_SIZES * N_FORMATS) return S_FALSE;
        AM_MEDIA_TYPE mt;
        MakeType(i, mt);
        *out = NewType(mt);
        FreeType(mt);
        if (!*out) return E_OUTOFMEMORY;
        const SIZE& s = SIZES[i / N_FORMATS];
        auto* c = reinterpret_cast<VIDEO_STREAM_CONFIG_CAPS*>(caps);
        ZeroMemory(c, sizeof(*c));
        c->guid = FORMAT_VideoInfo;
        c->InputSize = c->MinCroppingSize = c->MaxCroppingSize = c->MinOutputSize = c->MaxOutputSize = s;
        c->OutputGranularityX = c->OutputGranularityY = 2;
        c->MinFrameInterval = c->MaxFrameInterval = FRAME;
        c->MinBitsPerSecond = c->MaxBitsPerSecond = s.cx * s.cy * FORMATS[i % N_FORMATS].bits * 30;
        return S_OK;
    }

    // IKsPropertySet: say we are a capture pin, so apps treat us as a camera.
    STDMETHODIMP Set(REFGUID, DWORD, LPVOID, DWORD, LPVOID, DWORD) override { return E_NOTIMPL; }
    STDMETHODIMP Get(REFGUID set, DWORD id, LPVOID, DWORD, LPVOID data, DWORD size, DWORD* returned) override {
        if (set != AMPROPSETID_Pin) return E_PROP_SET_UNSUPPORTED;
        if (id != AMPROPERTY_PIN_CATEGORY) return E_PROP_ID_UNSUPPORTED;
        if (returned) *returned = sizeof(GUID);
        if (!data) return returned ? S_OK : E_POINTER;
        if (size < sizeof(GUID)) return E_UNEXPECTED;
        *static_cast<GUID*>(data) = PIN_CATEGORY_CAPTURE;
        return S_OK;
    }
    STDMETHODIMP QuerySupported(REFGUID set, DWORD id, DWORD* support) override {
        if (set != AMPROPSETID_Pin) return E_PROP_SET_UNSUPPORTED;
        if (id != AMPROPERTY_PIN_CATEGORY) return E_PROP_ID_UNSUPPORTED;
        if (support) *support = KSPROPERTY_SUPPORT_GET;
        return S_OK;
    }

    // Called by the filter
    HRESULT Active() {  // Stopped -> Paused: commit buffers, open the shared block, start the thread
        Lock g(this);
        if (!peer_ || !allocator_) return S_OK;
        HRESULT hr = allocator_->Commit();
        if (FAILED(hr)) return hr;
        UINT32 w = 0, h = 0; int f = 0;
        Parse(&mt_, w, h, f);
        width_ = w; height_ = h; format_index_ = f;
        delete[] nv12_;
        nv12_ = new (std::nothrow) BYTE[(size_t)w * h * 3 / 2];
        if (!nv12_) return E_OUTOFMEMORY;
        Placeholder();
        last_seq_ = 0;
        last_new_ = 0;
        shared_.Open();
        shared_.Want(w, h);
        stop_ = false;
        frames_ = 0;
        thread_ = CreateThread(nullptr, 0, ThreadMain, this, 0, nullptr);
        Log("dshow: streaming %ux%u %s", w, h, f == 0 ? "NV12" : f == 1 ? "YUY2" : "I420");
        return thread_ ? S_OK : HRESULT_FROM_WIN32(GetLastError());
    }
    void Inactive() {  // -> Stopped
        StopThread();
        Lock g(this);
        if (allocator_) allocator_->Decommit();
        shared_.Close();
        delete[] nv12_;
        nv12_ = nullptr;
    }

private:
    struct Lock {
        Pin* p;
        explicit Lock(Pin* pin) : p(pin) { EnterCriticalSection(&p->lock_); }
        ~Lock() { LeaveCriticalSection(&p->lock_); }
    };
    static bool IsEqualType(const AM_MEDIA_TYPE& a, const AM_MEDIA_TYPE& b) {
        return a.majortype == b.majortype && a.subtype == b.subtype && a.formattype == b.formattype &&
               a.cbFormat == b.cbFormat && (!a.cbFormat || !memcmp(a.pbFormat, b.pbFormat, a.cbFormat));
    }
    bool filter_running();

    HRESULT TryConnect(IPin* receiver, const AM_MEDIA_TYPE* mt) {
        UINT32 w, h; int f;
        if (!Parse(mt, w, h, f)) return VFW_E_TYPE_NOT_ACCEPTED;
        HRESULT hr = receiver->ReceiveConnection(this, mt);
        if (FAILED(hr)) return hr;
        IMemInputPin* input = nullptr;
        hr = receiver->QueryInterface(IID_PPV_ARGS(&input));
        IMemAllocator* allocator = nullptr;
        if (SUCCEEDED(hr)) {
            ALLOCATOR_PROPERTIES want = {}, got = {};
            input->GetAllocatorRequirements(&want);
            if (FAILED(input->GetAllocator(&allocator)))
                hr = CoCreateInstance(CLSID_MemoryAllocator, nullptr, CLSCTX_INPROC_SERVER, IID_PPV_ARGS(&allocator));
            if (SUCCEEDED(hr)) {
                LONG bytes = (LONG)((size_t)w * h * FORMATS[f].bits / 8);
                want.cBuffers = max(want.cBuffers, 3L);
                want.cbBuffer = max(want.cbBuffer, bytes);
                want.cbAlign = max(want.cbAlign, 1L);
                hr = allocator->SetProperties(&want, &got);
                if (SUCCEEDED(hr) && got.cbBuffer < bytes) hr = E_FAIL;
            }
            if (SUCCEEDED(hr)) hr = input->NotifyAllocator(allocator, FALSE);
        }
        if (FAILED(hr)) {
            Release_(allocator);
            Release_(input);
            receiver->Disconnect();
            return hr;
        }
        peer_ = receiver; peer_->AddRef();
        input_ = input;
        allocator_ = allocator;
        FreeType(mt_);
        CopyType(mt_, *mt);
        return S_OK;
    }

    void StopThread() {
        HANDLE t;
        {
            Lock g(this);
            t = thread_;
            thread_ = nullptr;
            stop_ = true;
        }
        if (t) {
            if (allocator_) allocator_->Decommit();  // wakes a GetBuffer that is waiting
            WaitForSingleObject(t, 5000);
            CloseHandle(t);
        }
    }

    static DWORD WINAPI ThreadMain(LPVOID p) { static_cast<Pin*>(p)->Run(); return 0; }
    void Run();
    void Placeholder() {  // SprigCast's dark canopy green until SprigCast sends a frame
        size_t y = (size_t)width_ * height_;
        memset(nv12_, 38, y);
        for (size_t i = 0; i < y / 2; i += 2) { nv12_[y + i] = 122; nv12_[y + i + 1] = 116; }
    }
    void Convert(BYTE* out) {
        UINT32 w = width_, h = height_;
        const BYTE* Y = nv12_;
        const BYTE* UV = nv12_ + (size_t)w * h;
        if (format_index_ == 0) {
            memcpy(out, nv12_, (size_t)w * h * 3 / 2);
        } else if (format_index_ == 2) {  // I420: Y, then U plane, then V plane
            memcpy(out, Y, (size_t)w * h);
            BYTE* U = out + (size_t)w * h;
            BYTE* V = U + (size_t)w * h / 4;
            for (size_t i = 0; i < (size_t)w * h / 4; i++) { U[i] = UV[2 * i]; V[i] = UV[2 * i + 1]; }
        } else {  // YUY2: Y0 U Y1 V per pixel pair, chroma shared by two rows
            for (UINT32 row = 0; row < h; row++) {
                const BYTE* y = Y + (size_t)row * w;
                const BYTE* uv = UV + (size_t)(row / 2) * w;
                BYTE* o = out + (size_t)row * w * 2;
                for (UINT32 x = 0; x < w; x += 2) {
                    o[0] = y[x]; o[1] = uv[x]; o[2] = y[x + 1]; o[3] = uv[x + 1];
                    o += 4;
                }
            }
        }
    }

    Filter* filter_;
    CRITICAL_SECTION lock_;
    IPin* peer_ = nullptr;
    IMemInputPin* input_ = nullptr;
    IMemAllocator* allocator_ = nullptr;
    AM_MEDIA_TYPE mt_ = {};
    AM_MEDIA_TYPE format_ = {};
    bool have_format_ = false;
    HANDLE thread_ = nullptr;
    volatile bool stop_ = false;
    UINT32 width_ = 0, height_ = 0;
    int format_index_ = 0;
    BYTE* nv12_ = nullptr;
    UINT64 last_seq_ = 0;
    ULONGLONG last_new_ = 0;
    LONGLONG frames_ = 0;
    LocalShared shared_;
};

// ---------------------------------------------------------------------------------------------
// The filter

class Filter : public IBaseFilter, public IAMFilterMiscFlags {
public:
    Filter() : pin_(this) { InitializeCriticalSection(&lock_); InterlockedIncrement(&g_objects); }

    STDMETHODIMP QueryInterface(REFIID riid, void** ppv) override {
        if (!ppv) return E_POINTER;
        if (riid == IID_IUnknown || riid == IID_IPersist || riid == IID_IMediaFilter || riid == IID_IBaseFilter)
            *ppv = static_cast<IBaseFilter*>(this);
        else if (riid == IID_IAMFilterMiscFlags)
            *ppv = static_cast<IAMFilterMiscFlags*>(this);
        else if (riid == IID_IAMStreamConfig || riid == IID_IKsPropertySet)  // some apps ask the filter
            return pin_.QueryInterface(riid, ppv);
        else { *ppv = nullptr; return E_NOINTERFACE; }
        AddRef();
        return S_OK;
    }
    STDMETHODIMP_(ULONG) AddRef() override { return InterlockedIncrement(&ref_); }
    STDMETHODIMP_(ULONG) Release() override { ULONG n = InterlockedDecrement(&ref_); if (!n) delete this; return n; }

    // IPersist
    STDMETHODIMP GetClassID(CLSID* id) override { if (!id) return E_POINTER; *id = CLSID_SprigCastDShow; return S_OK; }

    // IMediaFilter
    STDMETHODIMP Stop() override {
        EnterCriticalSection(&lock_);
        FILTER_STATE was = state_;
        state_ = State_Stopped;
        LeaveCriticalSection(&lock_);
        if (was != State_Stopped) pin_.Inactive();
        return S_OK;
    }
    STDMETHODIMP Pause() override {
        EnterCriticalSection(&lock_);
        FILTER_STATE was = state_;
        state_ = State_Paused;
        LeaveCriticalSection(&lock_);
        return was == State_Stopped ? pin_.Active() : S_OK;
    }
    STDMETHODIMP Run(REFERENCE_TIME start) override {
        EnterCriticalSection(&lock_);
        FILTER_STATE was = state_;
        start_ = start;
        LeaveCriticalSection(&lock_);
        HRESULT hr = was == State_Stopped ? pin_.Active() : S_OK;
        EnterCriticalSection(&lock_);
        state_ = State_Running;
        LeaveCriticalSection(&lock_);
        return hr;
    }
    STDMETHODIMP GetState(DWORD, FILTER_STATE* state) override {
        if (!state) return E_POINTER;
        EnterCriticalSection(&lock_);
        *state = state_;
        LeaveCriticalSection(&lock_);
        return *state == State_Paused ? VFW_S_CANT_CUE : S_OK;  // live source: nothing to show while paused
    }
    STDMETHODIMP SetSyncSource(IReferenceClock* clock) override {
        EnterCriticalSection(&lock_);
        if (clock) clock->AddRef();
        Release_(clock_);
        clock_ = clock;
        LeaveCriticalSection(&lock_);
        return S_OK;
    }
    STDMETHODIMP GetSyncSource(IReferenceClock** clock) override {
        if (!clock) return E_POINTER;
        EnterCriticalSection(&lock_);
        *clock = clock_;
        if (clock_) clock_->AddRef();
        LeaveCriticalSection(&lock_);
        return S_OK;
    }

    // IBaseFilter
    STDMETHODIMP EnumPins(IEnumPins** out) override {
        if (!out) return E_POINTER;
        *out = new (std::nothrow) ::EnumPins(&pin_);
        return *out ? S_OK : E_OUTOFMEMORY;
    }
    STDMETHODIMP FindPin(LPCWSTR id, IPin** out) override {
        if (!id || !out) return E_POINTER;
        if (wcscmp(id, L"Video")) { *out = nullptr; return VFW_E_NOT_FOUND; }
        *out = &pin_;
        pin_.AddRef();
        return S_OK;
    }
    STDMETHODIMP QueryFilterInfo(FILTER_INFO* info) override {
        if (!info) return E_POINTER;
        wcsncpy_s(info->achName, NAME, _TRUNCATE);
        info->pGraph = graph_;
        if (graph_) graph_->AddRef();
        return S_OK;
    }
    STDMETHODIMP JoinFilterGraph(IFilterGraph* graph, LPCWSTR) override {
        graph_ = graph;  // not AddRef'd: the graph owns us (DirectShow's rule)
        return S_OK;
    }
    STDMETHODIMP QueryVendorInfo(LPWSTR*) override { return E_NOTIMPL; }

    // IAMFilterMiscFlags
    STDMETHODIMP_(ULONG) GetMiscFlags() override { return AM_FILTER_MISC_FLAGS_IS_SOURCE; }

    // For the pin's thread
    bool Running() {
        EnterCriticalSection(&lock_);
        bool r = state_ == State_Running;
        LeaveCriticalSection(&lock_);
        return r;
    }
    // Stream time now, or -1 without a clock.
    REFERENCE_TIME StreamTime() {
        EnterCriticalSection(&lock_);
        REFERENCE_TIME now = -1;
        if (clock_ && SUCCEEDED(clock_->GetTime(&now))) now -= start_;
        else now = -1;
        LeaveCriticalSection(&lock_);
        return now;
    }

private:
    ~Filter() {
        Stop();
        Release_(clock_);
        DeleteCriticalSection(&lock_);
        InterlockedDecrement(&g_objects);
    }
    LONG ref_ = 1;
    CRITICAL_SECTION lock_;
    FILTER_STATE state_ = State_Stopped;
    REFERENCE_TIME start_ = 0;
    IReferenceClock* clock_ = nullptr;
    IFilterGraph* graph_ = nullptr;
    Pin pin_;

    friend class Pin;
};

STDMETHODIMP Pin::QueryInterface(REFIID riid, void** ppv) {
    if (!ppv) return E_POINTER;
    if (riid == IID_IUnknown || riid == IID_IPin) *ppv = static_cast<IPin*>(this);
    else if (riid == IID_IAMStreamConfig) *ppv = static_cast<IAMStreamConfig*>(this);
    else if (riid == IID_IKsPropertySet) *ppv = static_cast<IKsPropertySet*>(this);
    else { *ppv = nullptr; return E_NOINTERFACE; }
    AddRef();
    return S_OK;
}
STDMETHODIMP_(ULONG) Pin::AddRef() { return filter_->AddRef(); }
STDMETHODIMP_(ULONG) Pin::Release() { return filter_->Release(); }
STDMETHODIMP Pin::QueryPinInfo(PIN_INFO* info) {
    if (!info) return E_POINTER;
    info->pFilter = filter_;
    filter_->AddRef();
    info->dir = PINDIR_OUTPUT;
    wcsncpy_s(info->achName, L"Video", _TRUNCATE);
    return S_OK;
}
bool Pin::filter_running() { return filter_->Running(); }

void Pin::Run() {
    ULONGLONG next = GetTickCount64();
    while (!stop_) {
        ULONGLONG now = GetTickCount64();
        if (next > now) Sleep((DWORD)(next - now));
        next = max(next, now) + 1000 / 30;
        if (stop_) break;
        shared_.Want(width_, height_);  // also the heartbeat SprigCast watches
        if (shared_.Read(nv12_, width_, height_, last_seq_)) {
            last_new_ = GetTickCount64();
        } else if (last_new_ && GetTickCount64() - last_new_ > 2000) {
            Placeholder();  // SprigCast stopped sending (closed, or the camera was turned off)
            last_new_ = 0;
        }
        if (!filter_->Running()) continue;  // paused: a live source sends nothing
        IMediaSample* sample = nullptr;
        if (FAILED(allocator_->GetBuffer(&sample, nullptr, nullptr, 0)) || !sample) continue;
        BYTE* data = nullptr;
        LONG need = (LONG)((size_t)width_ * height_ * FORMATS[format_index_].bits / 8);
        if (SUCCEEDED(sample->GetPointer(&data)) && sample->GetSize() >= need) {
            Convert(data);
            sample->SetActualDataLength(need);
            REFERENCE_TIME t = filter_->StreamTime();
            if (t < 0) t = frames_ * FRAME;
            REFERENCE_TIME end = t + FRAME;
            sample->SetTime(&t, &end);
            sample->SetSyncPoint(TRUE);
            sample->SetDiscontinuity(frames_ == 0);
            frames_++;
            if (input_) input_->Receive(sample);
        }
        sample->Release();
    }
}

// ---------------------------------------------------------------------------------------------
// COM factory and registration (called from sprigcast_camera.cpp)

class DShowFactory : public IClassFactory {
public:
    STDMETHODIMP QueryInterface(REFIID riid, void** ppv) override {
        if (!ppv) return E_POINTER;
        if (riid == IID_IUnknown || riid == IID_IClassFactory) { *ppv = this; AddRef(); return S_OK; }
        *ppv = nullptr;
        return E_NOINTERFACE;
    }
    STDMETHODIMP_(ULONG) AddRef() override { return 2; }
    STDMETHODIMP_(ULONG) Release() override { return 1; }
    STDMETHODIMP CreateInstance(IUnknown* outer, REFIID riid, void** ppv) override {
        if (outer) return CLASS_E_NOAGGREGATION;
        Filter* f = new (std::nothrow) Filter();
        if (!f) return E_OUTOFMEMORY;
        HRESULT hr = f->QueryInterface(riid, ppv);
        f->Release();
        Log("dshow: filter created 0x%08lX", hr);
        return hr;
    }
    STDMETHODIMP LockServer(BOOL) override { return S_OK; }
};
static DShowFactory g_dshow_factory;

HRESULT DShowClassObject(REFIID riid, void** ppv) { return g_dshow_factory.QueryInterface(riid, ppv); }

// Add (or remove) "SprigCast Camera" under DirectShow's Video Capture Sources. COM is already set up by
// regsvr32; the COM class itself (InprocServer32) is written by sprigcast_camera.cpp's Register().
HRESULT DShowRegister(bool on) {
    HRESULT co = CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);
    IFilterMapper2* mapper = nullptr;
    HRESULT hr = CoCreateInstance(CLSID_FilterMapper2, nullptr, CLSCTX_INPROC_SERVER, IID_PPV_ARGS(&mapper));
    if (SUCCEEDED(hr)) {
        if (on) {
            REGPINTYPES type = {&MEDIATYPE_Video, &MEDIASUBTYPE_NULL};
            REGFILTERPINS2 pin = {};
            pin.dwFlags = REG_PINFLAG_B_OUTPUT;
            pin.cInstances = 1;
            pin.nMediaTypes = 1;
            pin.lpMediaType = &type;
            pin.clsPinCategory = &PIN_CATEGORY_CAPTURE;
            REGFILTER2 filter = {};
            filter.dwVersion = 2;
            filter.dwMerit = MERIT_DO_NOT_USE;
            filter.cPins2 = 1;
            filter.rgPins2 = &pin;
            mapper->UnregisterFilter(&CLSID_VideoInputDeviceCategory, DSHOW_CLSID_TEXT, CLSID_SprigCastDShow);
            hr = mapper->RegisterFilter(CLSID_SprigCastDShow, NAME, nullptr, &CLSID_VideoInputDeviceCategory,
                                        DSHOW_CLSID_TEXT, &filter);
        } else {
            hr = mapper->UnregisterFilter(&CLSID_VideoInputDeviceCategory, DSHOW_CLSID_TEXT, CLSID_SprigCastDShow);
            if (hr == HRESULT_FROM_WIN32(ERROR_FILE_NOT_FOUND)) hr = S_OK;
        }
        mapper->Release();
    }
    Log("dshow: %s 0x%08lX", on ? "registered" : "unregistered", hr);
    if (SUCCEEDED(co)) CoUninitialize();
    return hr;
}
