/* SprigCast.exe: runs the bundled Python in this process, so the app shows up as SprigCast.exe.
 *
 *   SprigCast.exe [--minimized ...]  ->  python -m sprigcast.app.main [--minimized ...]
 *   SprigCast.exe -c "..." / -m x    ->  plain Python (sys.executable is this exe, so anything that starts
 *                                         `sys.executable -c ...` keeps working)
 *
 * A first argument with a single dash is a Python option; the app's own flags use two dashes.
 */
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <windows.h>
#include <dbghelp.h>
#include <shellapi.h>
#include <stdlib.h>

/* The crash guard (sprigcast_crash_guard, called from app/crash_dump.py): when SprigCast crashes in native code,
 * a small minidump goes beside crash.log, and a dump that can't be written never hangs the app.
 *
 * It used to be a Python (ctypes) callback that wrote the dump itself, from the crashing thread: on 2026-11-18 a
 * crash on a worker thread left the window "Not Responding" for 25 minutes with the half-written .dmp locked.
 * Python can't be trusted while it's crashing, and MiniDumpWriteDump from the crashing thread can wait forever
 * on a lock that thread or a suspended one holds. Here the filter is plain C: it wakes a thread made at start,
 * which writes the dump, and waits for it at most CRASH_WAIT_MS. Written in time, the filter answers as before
 * (Windows' error reporting goes on and names the faulting DLL); not, the process is ended.
 *
 * Windows also asks this filter about some exceptions it then survives (one in a window's paint, inside the
 * system's own message callback, is swallowed there): so nothing here ends a run that got its dump, and only the
 * first exception is dumped. Ending it 30 s after any dump turned the CI start-up check's survived paint fault on
 * Mesa into a crash (build 44376095639). */
#define CRASH_WAIT_MS 15000
#define DUMP_TYPE (MiniDumpNormal | MiniDumpWithThreadInfo | MiniDumpWithUnloadedModules)

typedef BOOL(WINAPI *dump_fn)(HANDLE, DWORD, HANDLE, MINIDUMP_TYPE, PMINIDUMP_EXCEPTION_INFORMATION,
                              PMINIDUMP_USER_STREAM_INFORMATION, PMINIDUMP_CALLBACK_INFORMATION);
static dump_fn crash_dump_fn;
static wchar_t crash_path[2048];
static HANDLE crash_go, crash_written;
static DWORD crash_thread_id;
static EXCEPTION_POINTERS *crash_pointers;
static volatile LONG crash_seen;
static MINIDUMP_TYPE crash_dump_type = DUMP_TYPE;

static DWORD WINAPI crash_writer(LPVOID unused) {
    (void)unused;
    WaitForSingleObject(crash_go, INFINITE);
    HANDLE file = CreateFileW(crash_path, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (file != INVALID_HANDLE_VALUE) {
        MINIDUMP_EXCEPTION_INFORMATION info;
        info.ThreadId = crash_thread_id;
        info.ExceptionPointers = crash_pointers;
        info.ClientPointers = FALSE;
        crash_dump_fn(GetCurrentProcess(), GetCurrentProcessId(), file, crash_dump_type, &info, NULL, NULL);
        CloseHandle(file);
    }
    SetEvent(crash_written);
    return 0;  /* one dump per run: this thread's work is done */
}

static LONG WINAPI crash_filter(EXCEPTION_POINTERS *pointers) {
    UINT code = pointers && pointers->ExceptionRecord ? (UINT)pointers->ExceptionRecord->ExceptionCode : 1u;
    if (InterlockedExchange(&crash_seen, 1) != 0) {
        /* dumped already (or being dumped for another thread): wait for that one, then answer as Windows would */
        WaitForSingleObject(crash_written, CRASH_WAIT_MS);
        return EXCEPTION_CONTINUE_SEARCH;
    }
    crash_thread_id = GetCurrentThreadId();
    crash_pointers = pointers;
    SetEvent(crash_go);
    if (WaitForSingleObject(crash_written, CRASH_WAIT_MS) != WAIT_OBJECT_0) {
        TerminateProcess(GetCurrentProcess(), code);  /* the dump never finished: end now, never hang */
        return EXCEPTION_EXECUTE_HANDLER;
    }
    return EXCEPTION_CONTINUE_SEARCH;  /* as before: Windows Error Reporting records it and ends the process */
}

__declspec(dllexport) int sprigcast_crash_guard(const wchar_t *dump_path) {
    if (dump_path == NULL || crash_go != NULL || wcslen(dump_path) >= sizeof(crash_path) / sizeof(wchar_t))
        return 0;
    HMODULE dbghelp = LoadLibraryW(L"dbghelp.dll");  /* loaded now: nothing to load while crashing */
    crash_dump_fn = dbghelp ? (dump_fn)(void *)GetProcAddress(dbghelp, "MiniDumpWriteDump") : NULL;
    if (crash_dump_fn == NULL)
        return 0;
    wcscpy_s(crash_path, sizeof(crash_path) / sizeof(wchar_t), dump_path);
    /* The start-up check on GitHub only (SPRIGCAST_DUMP_MEMORY=1): also the kilobyte around whatever a register
     * or a stack points at, so packaging/read_dump.py can say which object a crash was in. A dump a person sends
     * never holds memory. */
    wchar_t memory[4];
    if (GetEnvironmentVariableW(L"SPRIGCAST_DUMP_MEMORY", memory, 4) == 1 && memory[0] == L'1')
        crash_dump_type = (MINIDUMP_TYPE)(DUMP_TYPE | MiniDumpWithIndirectlyReferencedMemory);
    crash_go = CreateEventW(NULL, TRUE, FALSE, NULL);
    crash_written = CreateEventW(NULL, TRUE, FALSE, NULL);
    if (crash_go == NULL || crash_written == NULL)
        return 0;
    /* a small stack: it only ever waits, then writes one dump */
    HANDLE writer = CreateThread(NULL, 256 * 1024, crash_writer, NULL, STACK_SIZE_PARAM_IS_A_RESERVATION, NULL);
    if (writer == NULL)
        return 0;
    CloseHandle(writer);
    SetUnhandledExceptionFilter(crash_filter);
    return 1;
}

/* On a laptop with two graphics chips, ask for the strong one (NVIDIA Optimus, AMD PowerXpress / switchable
   graphics). The drivers look for these names in the program's own .exe. Qt's OpenGL (the window and previews)
   follows them; the engine picks the high-performance card itself (native/core sprigcore.cpp preferred_adapter).
   Tester CRGPlays (i7-7700HQ, HD 630 + GTX 1070, 2026-11-27): everything was drawn on the HD 630 at 98-100%.
   A PC with one card is unaffected. */
__declspec(dllexport) DWORD NvOptimusEnablement = 0x00000001;
__declspec(dllexport) int AmdPowerXpressRequestHighPerformance = 1;

int WINAPI wWinMain(HINSTANCE instance, HINSTANCE previous, PWSTR command_line, int show) {
    (void)instance; (void)previous; (void)command_line; (void)show;
    int argc = 0;
    wchar_t **argv = CommandLineToArgvW(GetCommandLineW(), &argc);
    if (argv == NULL || argc < 1)
        return 1;
    int python_args = argc > 1 && argv[1][0] == L'-' && argv[1][1] != L'-' && argv[1][1] != L'\0';
    wchar_t **args = (wchar_t **)calloc((size_t)argc + 3, sizeof(wchar_t *));
    if (args == NULL)
        return 1;
    int n = 0;
    args[n++] = argv[0];
    if (!python_args) {
        args[n++] = L"-m";
        args[n++] = L"sprigcast.app.main";
    }
    for (int i = 1; i < argc; i++)
        args[n++] = argv[i];
    int code = Py_Main(n, args);
    free(args);
    LocalFree(argv);
    return code;
}
