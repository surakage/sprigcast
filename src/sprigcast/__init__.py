"""SprigCast: a lightweight streaming app with horizontal and vertical canvases."""

__version__ = "0.1.0"

import os as _os

# NumPy's maths library (OpenBLAS) starts a thread per CPU core as soon as it loads. SprigCast only
# copies and reshapes arrays, so those threads would just sit there next to your game.
_os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
