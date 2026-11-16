# SprigCast.exe: a small launcher so the running app shows as SprigCast.exe (Task Manager, Streamer.bot,
# StreamBridge), not pythonw.exe. It embeds the bundled Python and runs `-m sprigcast.app.main` in the same
# process, so there's no second process. It lives next to python312.dll in the install's python folder.
# Build: cmake -S native/launcher -B build/launcher -A x64 -DPY_HOME=<that python folder>
#        cmake --build build/launcher --config Release
cmake_minimum_required(VERSION 3.20)
project(sprigcast_launcher C RC)
if(NOT PY_HOME)
  message(FATAL_ERROR "Pass -DPY_HOME=<the bundled Python folder>")
endif()
if(NOT SPRIGCAST_ICON)
  set(SPRIGCAST_ICON "${PY_HOME}/../sprigcast.ico")
endif()
file(TO_NATIVE_PATH "${SPRIGCAST_ICON}" ICON_NATIVE)
string(REPLACE "\\" "\\\\" ICON_ESCAPED "${ICON_NATIVE}")
configure_file(launcher.rc.in ${CMAKE_BINARY_DIR}/launcher.rc @ONLY)
add_executable(SprigCast WIN32 launcher.c ${CMAKE_BINARY_DIR}/launcher.rc)
target_compile_definitions(SprigCast PRIVATE UNICODE _UNICODE)
target_include_directories(SprigCast PRIVATE "${PY_HOME}/include")
file(GLOB PY_LIB "${PY_HOME}/libs/python3[0-9]*.lib")
list(FILTER PY_LIB EXCLUDE REGEX "_d\\.lib$")
target_link_libraries(SprigCast PRIVATE ${PY_LIB} shell32)
