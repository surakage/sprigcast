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
            src/mixer.cpp src/capture.cpp src/playback.cpp src/visual.cpp src/capture_wgc.cpp src/move.cpp
            src/capture_feed.cpp src/segmenter.cpp)
target_include_directories(sprigcore PUBLIC include)

# The Background filter's model (models/README.md), built into the DLL: nothing to download or find at run time.
set(SEG_MODEL ${CMAKE_CURRENT_SOURCE_DIR}/models/selfie-wide.scseg)
set(SEG_HEADER ${CMAKE_BINARY_DIR}/generated/segmenter_model.h)
set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS ${SEG_MODEL})
file(READ ${SEG_MODEL} SEG_HEX HEX)
string(REGEX REPLACE "([0-9a-f][0-9a-f])" "0x\\1," SEG_BYTES "${SEG_HEX}")
string(REGEX REPLACE "((0x..,){32})" "\\1\n" SEG_BYTES "${SEG_BYTES}")
file(WRITE ${SEG_HEADER}.tmp "// Made from models/selfie-wide.scseg when the engine is configured. Don't edit.\n#pragma once\n"
     "static const unsigned char SEGMENTER_MODEL[] = {\n${SEG_BYTES}\n};\n")
configure_file(${SEG_HEADER}.tmp ${SEG_HEADER} COPYONLY)
target_include_directories(sprigcore PRIVATE ${CMAKE_BINARY_DIR}/generated)
# FFmpeg headers only (the libraries are loaded at run time from PyAV: native/third_party/ffmpeg/README.md).
target_include_directories(sprigcore SYSTEM PRIVATE ../third_party/ffmpeg/include)
target_compile_definitions(sprigcore PRIVATE UNICODE _UNICODE NOMINMAX WIN32_LEAN_AND_MEAN _WIN32_WINNT=0x0A00)
target_link_libraries(sprigcore PRIVATE d3d11 dxgi d3dcompiler winmm ole32 windowsapp)

# The shaders, compiled when the engine is built (tools/shader_bake.cpp): compiling them at run time held frames up
# to 3.4 s on slow PCs. Built and run on Windows; elsewhere (a syntax check) they compile at run time as before.
if(CMAKE_HOST_WIN32)
  add_executable(shader_bake tools/shader_bake.cpp)
  target_include_directories(shader_bake PRIVATE src)
  target_compile_definitions(shader_bake PRIVATE UNICODE _UNICODE NOMINMAX WIN32_LEAN_AND_MEAN)
  target_link_libraries(shader_bake PRIVATE d3dcompiler)
  set(BAKED_DIR ${CMAKE_BINARY_DIR}/baked)
  file(GLOB SHADER_SOURCES CONFIGURE_DEPENDS ${CMAKE_CURRENT_SOURCE_DIR}/src/shader_*.h)
  add_custom_command(OUTPUT ${BAKED_DIR}/baked_shaders.h
                     COMMAND ${CMAKE_COMMAND} -E make_directory ${BAKED_DIR}
                     COMMAND shader_bake ${BAKED_DIR}/baked_shaders.h
                     DEPENDS shader_bake ${SHADER_SOURCES}
                     COMMENT "Baking the engine's shaders")
  target_sources(sprigcore PRIVATE ${BAKED_DIR}/baked_shaders.h)
  target_include_directories(sprigcore PRIVATE ${BAKED_DIR})
  target_compile_definitions(sprigcore PRIVATE SPRIGCORE_BAKED)
endif()

add_executable(sprigcore_tests tests/core_tests.cpp)
target_link_libraries(sprigcore_tests PRIVATE sprigcore)

# The capture helper (tools/capture_helper.cpp): captures run in it, so ending it gives Windows' capture memory back.
add_executable(SprigCastCapture tools/capture_helper.cpp)
target_link_libraries(SprigCastCapture PRIVATE sprigcore)

if(MSVC)
  # Warnings are errors, and every Release build keeps a .pdb so crash dumps name the engine's functions.
  # /EHa: the crash gate (catch (...) at every exported function) also stops Windows faults.
  target_compile_options(sprigcore PRIVATE /W4 /WX /permissive- /Zi /EHa)
  target_link_options(sprigcore PRIVATE /DEBUG /OPT:REF /OPT:ICF)
  target_compile_options(sprigcore_tests PRIVATE /W4 /permissive-)
  target_compile_options(SprigCastCapture PRIVATE /W4 /WX /permissive- /Zi)
  target_link_options(SprigCastCapture PRIVATE /DEBUG /OPT:REF /OPT:ICF)
  if(SPRIGCORE_ASAN)
    target_compile_options(sprigcore PRIVATE /fsanitize=address)
    target_compile_options(sprigcore_tests PRIVATE /fsanitize=address)
    target_compile_options(SprigCastCapture PRIVATE /fsanitize=address)
    target_compile_definitions(sprigcore_tests PRIVATE SPRIGCORE_ASAN)
  endif()
else()
  target_compile_options(sprigcore PRIVATE -Wall -Wextra -Werror)
  target_link_options(sprigcore PRIVATE -static-libgcc -static-libstdc++)
endif()

enable_testing()
add_test(NAME engine_checks COMMAND sprigcore_tests)
# The test program finds sprigcore.dll next to itself.
# Asked to check itself it says "ready" and ends: it starts, finds sprigcore.dll and stops cleanly.
add_test(NAME capture_helper_starts COMMAND SprigCastCapture --check)
set_tests_properties(capture_helper_starts PROPERTIES TIMEOUT 30 PASS_REGULAR_EXPRESSION "ready")
set_target_properties(sprigcore sprigcore_tests SprigCastCapture PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY_RELEASE ${CMAKE_BINARY_DIR}/Release
  RUNTIME_OUTPUT_DIRECTORY_DEBUG ${CMAKE_BINARY_DIR}/Debug)
