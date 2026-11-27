// SprigCast Camera's own version. Bump it (one number) whenever anything under native/camera changes what the
// DLL does. SprigCast and Setup compare it with the installed camera's, and only a higher number asks Windows'
// permission to update. The bytes differ on every build even when nothing changed, so comparing them asked
// on every update (PC, test builds 6 and 7, 2026-11-27). tests/test_virtualcam.py checks it was bumped.
#define SPRIGCAST_CAMERA_VERSION 2
