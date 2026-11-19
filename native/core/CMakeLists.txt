# SprigCast's native engine, sprigcore.dll (docs/native-engine.md), and its checks (sprigcore_tests.exe).
# Build: cmake -S native/core -B build/core -A x64 && cmake --build build/core --config Release
# Checks: ctest --test-dir build/core -C Release --output-on-failure
# AddressSanitizer build (CI): add -DSPRIGCORE_ASAN=ON
cmake_minimum_required(VERSION 3.20)
project(sprigcore CXX)
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
option(SPRIGCORE_ASAN "Build with AddressSanitizer" OFF)

if(MSVC)
  # Static C runtime: nothing extra to install on old PCs. (AddressSanitizer needs the DLL runtime.)
  if(SPRIGCORE_ASAN)
    set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>DLL")
  else()
    set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")
  endif()
endif()

add_library(sprigcore SHARED src/base.cpp src/sprigcore.cpp src/compositor.cpp src/engine.cpp src/transition.cpp
            src/nv12.cpp src/nv12_source.cpp src/ffmpeg.cpp src/media.cpp src/writer.cpp src/page_texture.cpp
            src/mixer.cpp src/capture.cpp src/playback.cpp src/visual.cpp src/capture_wgc.cpp src/move.cpp)
target_include_directories(sprigcore PUBLIC include)
# FFmpeg headers only (the libraries are loaded at run time from PyAV: native/third_party/ffmpeg/README.md).
target_include_directories(sprigcore SYSTEM PRIVATE ../third_party/ffmpeg/include)
target_compile_definitions(sprigcore PRIVATE UNICODE _UNICODE NOMINMAX WIN32_LEAN_AND_MEAN _WIN32_WINNT=0x0A00)
target_link_libraries(sprigcore PRIVATE d3d11 dxgi d3dcompiler winmm ole32 windowsapp)

add_executable(sprigcore_tests tests/core_tests.cpp)
target_link_libraries(sprigcore_tests PRIVATE sprigcore)

if(MSVC)
  # Warnings are errors, and every Release build keeps a .pdb so crash dumps name the engine's functions.
  # /EHa: the crash gate (catch (...) at every exported function) also stops Windows faults.
  target_compile_options(sprigcore PRIVATE /W4 /WX /permissive- /Zi /EHa)
  target_link_options(sprigcore PRIVATE /DEBUG /OPT:REF /OPT:ICF)
  target_compile_options(sprigcore_tests PRIVATE /W4 /permissive-)
  if(SPRIGCORE_ASAN)
    target_compile_options(sprigcore PRIVATE /fsanitize=address)
    target_compile_options(sprigcore_tests PRIVATE /fsanitize=address)
    target_compile_definitions(sprigcore_tests PRIVATE SPRIGCORE_ASAN)
  endif()
else()
  target_compile_options(sprigcore PRIVATE -Wall -Wextra -Werror)
  target_link_options(sprigcore PRIVATE -static-libgcc -static-libstdc++)
endif()

enable_testing()
add_test(NAME engine_checks COMMAND sprigcore_tests)
# The test program finds sprigcore.dll next to itself.
set_target_properties(sprigcore sprigcore_tests PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY_RELEASE ${CMAKE_BINARY_DIR}/Release
  RUNTIME_OUTPUT_DIRECTORY_DEBUG ${CMAKE_BINARY_DIR}/Debug)
