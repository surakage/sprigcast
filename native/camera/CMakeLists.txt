# SprigCast Camera: the Windows 11 virtual camera media source (see sprigcast_camera.cpp).
# Build: cmake -S native/camera -B build/camera -A x64 && cmake --build build/camera --config Release
cmake_minimum_required(VERSION 3.20)
project(sprigcast_camera CXX)
set(CMAKE_CXX_STANDARD 17)
# Static C runtime: the Frame Server loads this DLL, and it shouldn't need a VC++ redistributable there.
set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")
add_library(sprigcast_camera SHARED sprigcast_camera.cpp sprigcast_camera.def)
target_compile_definitions(sprigcast_camera PRIVATE UNICODE _UNICODE NTDDI_VERSION=0x0A00000B _WIN32_WINNT=0x0A00)
target_compile_options(sprigcast_camera PRIVATE /W4 /permissive-)
target_link_libraries(sprigcast_camera PRIVATE mfplat mfuuid mf mfsensorgroup advapi32 ole32 strmiids)
