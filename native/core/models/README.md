# The engine's models

## `selfie-wide.scseg`: the Background filter's person finder

Built into `sprigcore.dll` (CMakeLists.txt turns it into `segmenter_model.h`) and run by `src/segmenter.cpp` as
compute shaders. 256 x 144 RGB in (0 to 1), one channel out (1 = a person).

Made from MediaPipe Selfie Segmentation, landscape (`selfie_segmenter_landscape.tflite`, float16) by Google,
licensed under the [Apache License, Version 2.0](https://www.apache.org/licenses/LICENSE-2.0)
([model card](https://storage.googleapis.com/mediapipe-assets/Model%20Card%20MediaPipe%20Selfie%20Segmentation.pdf)).
Changes made here: converted to ONNX (tf2onnx, as the old Background remover did; its
`models/background/selfie-wide-v1.onnx` is in git history at 940808d), then to SprigCast's own layer list with
`tools/make_segmenter.py`, which folds each activation into the layer before it and keeps the weights as float16.
The weights are unchanged.

Remaking it:

    git show 940808d:models/background/selfie-wide-v1.onnx > selfie-wide-v1.onnx
    pip install onnx onnxruntime numpy
    python native/core/tools/make_segmenter.py selfie-wide-v1.onnx native/core/models/selfie-wide.scseg --check

`--check` runs the program in numpy and compares it with ONNX Runtime (largest difference 0.00000 on
2026-11-21).
