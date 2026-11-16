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
#include <shellapi.h>
#include <stdlib.h>

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
