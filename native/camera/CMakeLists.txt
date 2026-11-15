# SprigCast Camera: a DirectShow capture source (sprigcast_dshow.cpp) plus the Windows 11 media source
# (sprigcast_camera.cpp), in one DLL.
# Build: cmake -S native/camera -B build/camera -A x64 && cmake --build build/camera --config Release
# 32-bit apps need a 32-bit copy: -A Win32 builds sprigcast_camera32.dll.
cmake_minimum_required(VERSION 3.20)
project(sprigcast_camera CXX)
set(CMAKE_CXX_STANDARD 17)
# Static C runtime: Windows services and other apps load this DLL, and it shouldn't need a VC++ redistributable.
set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")
add_library(sprigcast_camera SHARED sprigcast_camera.cpp sprigcast_dshow.cpp sprigcast_camera.def)
if(CMAKE_SIZEOF_VOID_P EQUAL 4)
  set_target_properties(sprigcast_camera PROPERTIES OUTPUT_NAME sprigcast_camera32)
endif()
target_compile_definitions(sprigcast_camera PRIVATE UNICODE _UNICODE NTDDI_VERSION=0x0A00000B _WIN32_WINNT=0x0A00)
target_compile_options(sprigcast_camera PRIVATE /W4 /permissive-)
target_link_libraries(sprigcast_camera PRIVATE mfplat mfuuid mf mfsensorgroup advapi32 ole32 strmiids)
