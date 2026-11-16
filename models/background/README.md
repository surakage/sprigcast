# Background remover models

Downloaded by the Background remover store plugin (`src/sprigcast/store/background_remover.py`) the first
time it's used. They are not part of the installer. The plugin checks each file's SHA-256 before using it,
so a changed file needs a new name (and a new hash in the plugin).

| File | Made from | Input | Used for |
|---|---|---|---|
| `selfie-wide-v1.onnx` | MediaPipe Selfie Segmentation, landscape (`selfie_segmenter_landscape.tflite`, float16) | 256 x 144 RGB, 0 to 1 | Default, wide cameras |
| `selfie-square-v1.onnx` | MediaPipe Selfie Segmentation (`selfie_segmenter.tflite`, float16) | 256 x 256 RGB, 0 to 1 | Default, square or tall cameras |
| `selfie-multiclass-v1.onnx` | MediaPipe Selfie Multiclass Segmentation (`selfie_multiclass_256x256.tflite`, float32) | 256 x 256 RGB, -1 to 1 | Better edges (6 classes: background, hair, body skin, face skin, clothes, others) |

## License

The original models are by Google and licensed under the
[Apache License, Version 2.0](https://www.apache.org/licenses/LICENSE-2.0). See their model cards:
[Selfie Segmentation](https://storage.googleapis.com/mediapipe-assets/Model%20Card%20MediaPipe%20Selfie%20Segmentation.pdf)
and [Multiclass Segmentation](https://storage.googleapis.com/mediapipe-assets/Model%20Card%20Multiclass%20Segmentation.pdf).
The `.tflite` files came from `https://storage.googleapis.com/mediapipe-models/image_segmenter/`.

Changes made here: converted from TensorFlow Lite to ONNX (opset 17) with tf2onnx 1.17, and, in the two
selfie models, the last layer (MediaPipe's custom `Convolution2DTransposeBias`, a 2 x 2 stride-2
transposed convolution with bias) rewritten as standard ONNX `ConvTranspose` with `fix_transpose_conv.py`.
Weights are unchanged.

## Remaking them

    pip install tf2onnx tensorflow-cpu==2.16.* onnx==1.16.2
    python -m tf2onnx.convert --tflite selfie_segmenter_landscape.tflite --output wide.onnx --opset 17
    python fix_transpose_conv.py wide.onnx selfie-wide-v1.onnx
