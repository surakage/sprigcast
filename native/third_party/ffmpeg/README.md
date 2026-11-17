# FFmpeg headers (9.0.2)

The public headers of FFmpeg 9.0.2 (tag `n9.0.2`, https://github.com/FFmpeg/FFmpeg), unchanged, that
`native/core/src/media.cpp` is compiled against. They are FFmpeg's own files under the LGPL 2.1 or later; each
keeps its licence notice. `libavutil/avconfig.h` is the one file FFmpeg's build makes (little-endian x86-64).

SprigCast doesn't link FFmpeg and ships no second copy of it: the engine loads, at run time, the same FFmpeg
libraries that PyAV brings with it (`av.libs`), and checks that their major versions are the ones these headers
describe before it uses them. When PyAV moves to another FFmpeg, fetch that tag's headers here.

Only headers are here: nothing in this folder is built.
