"""Turns a MediaPipe selfie segmentation model (as ONNX) into the engine's own small program (segmenter.cpp).

The engine runs the model itself as compute shaders, so no AI library ships with SprigCast. This reads the ONNX
graph, drops the layout-only nodes (Transpose, Reshape), folds each activation into the layer before it, lays every
tensor out in one buffer (channels, rows, columns) and writes:

    magic "SCSEG1\\0\\0", u32 version 1, u32 input width, u32 input height, u32 arena floats,
    u32 op count, u32 weight floats, u32 output offset,
    ops: op count x 16 i32 (see OP_FIELDS), then the weights as float16.

Run it with the ONNX files the old Background remover kept (git show 940808d:models/background/selfie-wide-v1.onnx):

    python native/core/tools/make_segmenter.py selfie-wide-v1.onnx src/sprigcast/data/segmenter/selfie-wide.bin

`--check` also runs the program in numpy (run_program) and compares it with ONNX Runtime on a few pictures.
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

import numpy as np

# Op kinds (segmenter.cpp and shader_segmenter.h use the same numbers).
CONV, DWCONV, GAP, SCALE, ADD, RESIZE2X, CONVT2X = 1, 2, 3, 4, 5, 6, 7
ACT_NONE, ACT_RELU, ACT_HSWISH, ACT_SIGMOID = 0, 1, 2, 3
OP_FIELDS = ("kind", "src", "src2", "dst", "weights", "bias", "cin", "cout", "h", "w", "oh", "ow", "k", "stride",
             "pad", "act")
MAGIC = b"SCSEG1\0\0"


def compile_model(path: str):
    import onnx
    from onnx import numpy_helper

    model = onnx.load(path)
    g = model.graph
    init = {i.name: numpy_helper.to_array(i) for i in g.initializer}
    (inp,) = g.input
    dims = [d.dim_value for d in inp.type.tensor_type.shape.dim]  # 1, H, W, 3
    in_h, in_w = dims[1], dims[2]

    alias: dict[str, str] = {}

    def real(name: str) -> str:
        while name in alias:
            name = alias[name]
        return name

    consumers: dict[str, int] = {}
    nodes = []
    for n in g.node:
        if n.op_type in ("Transpose", "Reshape"):
            alias[n.output[0]] = n.input[0]
            continue
        nodes.append(n)
    for n in nodes:
        for i in n.input:
            if i and i not in init:
                consumers[real(i)] = consumers.get(real(i), 0) + 1

    shapes: dict[str, tuple[int, int, int]] = {real(inp.name): (3, in_h, in_w)}
    ops: list[dict] = []
    weights: list[np.ndarray] = []
    wpos = 0
    produced_by: dict[str, dict] = {}

    def add_weights(a: np.ndarray) -> int:
        nonlocal wpos
        at = wpos
        weights.append(a.astype(np.float32).ravel())
        wpos += a.size
        return at

    def attr(n, name, default=None):
        for a in n.attribute:
            if a.name == name:
                return onnx.helper.get_attribute_value(a)
        return default

    for n in nodes:
        t = n.op_type
        ins = [real(i) for i in n.input if i and i not in init]
        out = n.output[0]
        if t in ("Relu", "HardSwish", "Sigmoid"):
            act = {"Relu": ACT_RELU, "HardSwish": ACT_HSWISH, "Sigmoid": ACT_SIGMOID}[t]
            src = ins[0]
            op = produced_by.get(src)
            if op is not None and op["act"] == ACT_NONE and consumers.get(src, 0) == 1 and op["kind"] in (
                    CONV, DWCONV, CONVT2X):
                op["act"] = act
                alias[out] = src
                produced_by[out] = op
                continue
            raise SystemExit(f"{t} at {out} can't be folded into the layer before it")
        if t == "Conv":
            w = init[n.input[1]]
            b = init[n.input[2]] if len(n.input) > 2 else np.zeros(w.shape[0], np.float32)
            c, h, wd = shapes[ins[0]]
            k = int(w.shape[2])
            stride = int(attr(n, "strides", [1, 1])[0])
            pads = attr(n, "pads", [0, 0, 0, 0])  # top, left, bottom, right
            group = int(attr(n, "group", 1))
            if pads[0] != pads[1] or k != w.shape[3]:
                raise SystemExit(f"{out}: unexpected padding {pads}")
            pad = int(pads[0])
            oh = (h + pads[0] + pads[2] - k) // stride + 1
            ow = (wd + pads[1] + pads[3] - k) // stride + 1
            if group == 1:
                kind = CONV
            elif group == c == w.shape[0]:
                kind = DWCONV
            else:
                raise SystemExit(f"{out}: grouped convolution")
            op = dict(kind=kind, src=ins[0], src2="", dst=out, weights=add_weights(w), bias=add_weights(b),
                      cin=c, cout=int(w.shape[0]), h=h, w=wd, oh=oh, ow=ow, k=k, stride=stride, pad=pad,
                      act=ACT_NONE)
            shapes[out] = (int(w.shape[0]), oh, ow)
        elif t in ("GlobalAveragePool", "ReduceMean"):
            c, h, wd = shapes[ins[0]]
            op = dict(kind=GAP, src=ins[0], src2="", dst=out, weights=0, bias=0, cin=c, cout=c, h=h, w=wd, oh=1,
                      ow=1, k=0, stride=0, pad=0, act=ACT_NONE)
            shapes[out] = (c, 1, 1)
        elif t == "Mul":
            a, b = ins
            if shapes[b][1:] != (1, 1):
                a, b = b, a
            c, h, wd = shapes[a]
            if shapes[b] != (c, 1, 1):
                raise SystemExit(f"{out}: Mul isn't a channel scale")
            op = dict(kind=SCALE, src=a, src2=b, dst=out, weights=0, bias=0, cin=c, cout=c, h=h, w=wd, oh=h, ow=wd,
                      k=0, stride=0, pad=0, act=ACT_NONE)
            shapes[out] = (c, h, wd)
        elif t == "Add":
            a, b = ins
            if shapes[a] != shapes[b]:
                raise SystemExit(f"{out}: Add of different shapes")
            c, h, wd = shapes[a]
            op = dict(kind=ADD, src=a, src2=b, dst=out, weights=0, bias=0, cin=c, cout=c, h=h, w=wd, oh=h, ow=wd,
                      k=0, stride=0, pad=0, act=ACT_NONE)
            shapes[out] = (c, h, wd)
        elif t == "Resize":
            scales = init[n.input[2]].tolist()
            if scales != [1.0, 1.0, 2.0, 2.0] or attr(n, "mode") != b"linear" or \
                    attr(n, "coordinate_transformation_mode") != b"half_pixel":
                raise SystemExit(f"{out}: only 2x bilinear (half pixel) resizes")
            c, h, wd = shapes[ins[0]]
            op = dict(kind=RESIZE2X, src=ins[0], src2="", dst=out, weights=0, bias=0, cin=c, cout=c, h=h, w=wd,
                      oh=h * 2, ow=wd * 2, k=0, stride=0, pad=0, act=ACT_NONE)
            shapes[out] = (c, h * 2, wd * 2)
        elif t == "ConvTranspose":
            w = init[n.input[1]]  # cin, cout, 2, 2
            b = init[n.input[2]]
            if list(w.shape[2:]) != [2, 2] or attr(n, "strides") != [2, 2]:
                raise SystemExit(f"{out}: only 2x2 stride 2 transposed convolutions")
            c, h, wd = shapes[ins[0]]
            op = dict(kind=CONVT2X, src=ins[0], src2="", dst=out, weights=add_weights(w), bias=add_weights(b),
                      cin=c, cout=int(w.shape[1]), h=h, w=wd, oh=h * 2, ow=wd * 2, k=2, stride=2, pad=0,
                      act=ACT_NONE)
            shapes[out] = (int(w.shape[1]), h * 2, wd * 2)
        else:
            raise SystemExit(f"Unsupported layer {t} ({out})")
        ops.append(op)
        produced_by[out] = op

    out_name = real(g.output[0].name)
    if shapes[out_name][0] != 1:
        raise SystemExit("The model's output isn't one channel")

    # Lay the tensors out in one buffer, reusing a tensor's space once nothing reads it any more (first fit).
    order = [real(inp.name)] + [op["dst"] for op in ops]
    last_use = {name: i for i, name in enumerate(order)}
    for i, op in enumerate(ops, start=1):
        for name in (op["src"], op["src2"]):
            if name:
                last_use[name] = max(last_use[name], i)
    last_use[out_name] = len(order)

    def size(name: str) -> int:
        c, h, wd = shapes[name]
        return (c * h * wd + 63) // 64 * 64

    offsets: dict[str, int] = {}
    live: list[tuple[int, int, str]] = []  # (start, end, name)
    arena = 0
    for i, name in enumerate(order):
        live = [blk for blk in live if last_use[blk[2]] >= i]  # a tensor read by op i stays until op i is done
        need = size(name)
        at = 0
        for start, end, _ in sorted(live):
            if start - at >= need:
                break
            at = max(at, end)
        offsets[name] = at
        live.append((at, at + need, name))
        arena = max(arena, at + need)
    for op in ops:
        op["src"] = offsets[op["src"]]
        op["src2"] = offsets[op["src2"]] if op["src2"] else 0
        op["dst"] = offsets[op["dst"]]
    return dict(in_w=in_w, in_h=in_h, arena=arena, ops=ops, weights=np.concatenate(weights),
                output=offsets[out_name])


def write_program(p: dict, path: str) -> None:
    w16 = p["weights"].astype(np.float16)
    with open(path, "wb") as f:
        f.write(MAGIC)
        f.write(struct.pack("<7I", 1, p["in_w"], p["in_h"], p["arena"], len(p["ops"]), w16.size, p["output"]))
        for op in p["ops"]:
            f.write(struct.pack("<16i", *(int(op[k]) for k in OP_FIELDS)))
        f.write(w16.tobytes())


def read_program(path: str) -> dict:
    data = Path(path).read_bytes()
    if data[:8] != MAGIC:
        raise ValueError("not a segmenter program")
    version, in_w, in_h, arena, n_ops, n_w, output = struct.unpack_from("<7I", data, 8)
    pos = 8 + 28
    ops = []
    for _ in range(n_ops):
        ops.append(dict(zip(OP_FIELDS, struct.unpack_from("<16i", data, pos))))
        pos += 64
    weights = np.frombuffer(data, np.float16, n_w, pos).astype(np.float32)
    return dict(in_w=in_w, in_h=in_h, arena=arena, ops=ops, weights=weights, output=output)


def _act(x: np.ndarray, act: int) -> np.ndarray:
    if act == ACT_RELU:
        return np.maximum(x, 0)
    if act == ACT_HSWISH:
        return x * np.clip(x + 3, 0, 6) / 6
    if act == ACT_SIGMOID:
        return 1 / (1 + np.exp(-np.clip(x, -60, 60)))
    return x


def run_program(p: dict, rgb: np.ndarray) -> np.ndarray:
    """The program in numpy, the way the shaders run it. rgb: H x W x 3 floats 0..1. Gives H x W, 0..1."""
    arena = np.zeros(p["arena"], np.float32)
    wts = p["weights"]
    arena[:rgb.size] = np.ascontiguousarray(rgb.transpose(2, 0, 1)).ravel()

    def get(at, c, h, w):
        return arena[at:at + c * h * w].reshape(c, h, w)

    for op in p["ops"]:
        kind, cin, cout, h, w, oh, ow = op["kind"], op["cin"], op["cout"], op["h"], op["w"], op["oh"], op["ow"]
        x = get(op["src"], cin, h, w)
        if kind in (CONV, DWCONV):
            k, s, pad = op["k"], op["stride"], op["pad"]
            bias = wts[op["bias"]:op["bias"] + cout]
            xp = np.zeros((cin, (oh - 1) * s + k, (ow - 1) * s + k), np.float32)
            src = x[:, :min(h, xp.shape[1] - pad), :min(w, xp.shape[2] - pad)]
            xp[:, pad:pad + src.shape[1], pad:pad + src.shape[2]] = src
            if kind == CONV:
                wt = wts[op["weights"]:op["weights"] + cout * cin * k * k].reshape(cout, cin, k, k)
                y = np.zeros((cout, oh, ow), np.float32)
                for dy in range(k):
                    for dx in range(k):
                        patch = xp[:, dy:dy + (oh - 1) * s + 1:s, dx:dx + (ow - 1) * s + 1:s]
                        y += np.tensordot(wt[:, :, dy, dx], patch, axes=1)
            else:
                wt = wts[op["weights"]:op["weights"] + cout * k * k].reshape(cout, k, k)
                y = np.zeros((cout, oh, ow), np.float32)
                for dy in range(k):
                    for dx in range(k):
                        y += wt[:, dy, dx, None, None] * xp[:, dy:dy + (oh - 1) * s + 1:s, dx:dx + (ow - 1) * s + 1:s]
            y += bias[:, None, None]
        elif kind == GAP:
            y = x.mean(axis=(1, 2), keepdims=True)
        elif kind == SCALE:
            y = x * get(op["src2"], cin, 1, 1)
        elif kind == ADD:
            y = x + get(op["src2"], cin, h, w)
        elif kind == RESIZE2X:
            def axis(n):
                pos = (np.arange(n * 2) + 0.5) / 2 - 0.5
                pos = np.clip(pos, 0, n - 1)
                lo = np.floor(pos).astype(int)
                hi = np.minimum(lo + 1, n - 1)
                return lo, hi, (pos - lo).astype(np.float32)
            ylo, yhi, fy = axis(h)
            xlo, xhi, fx = axis(w)
            rows = x[:, ylo] * (1 - fy)[None, :, None] + x[:, yhi] * fy[None, :, None]
            y = rows[:, :, xlo] * (1 - fx) + rows[:, :, xhi] * fx
        elif kind == CONVT2X:
            wt = wts[op["weights"]:op["weights"] + cin * cout * 4].reshape(cin, cout, 2, 2)
            bias = wts[op["bias"]:op["bias"] + cout]
            y = np.zeros((cout, oh, ow), np.float32)
            for dy in range(2):
                for dx in range(2):
                    y[:, dy::2, dx::2] = np.tensordot(wt[:, :, dy, dx].T, x, axes=1)
            y += bias[:, None, None]
        else:
            raise ValueError(kind)
        y = _act(y, op["act"])
        arena[op["dst"]:op["dst"] + y.size] = y.ravel()
    return arena[p["output"]:p["output"] + p["in_w"] * p["in_h"]].reshape(p["in_h"], p["in_w"]).copy()


def main(argv: list[str]) -> int:
    check = "--check" in argv
    args = [a for a in argv if a != "--check"]
    src, dst = args
    p = compile_model(src)
    write_program(p, dst)
    print(f"{dst}: {len(p['ops'])} layers, {p['weights'].size} weights, input {p['in_w']} x {p['in_h']}, "
          f"{p['arena'] * 4 // 1024} KB of tensors")
    if check:
        import onnxruntime as ort

        prog = read_program(dst)
        sess = ort.InferenceSession(src, providers=["CPUExecutionProvider"])
        rng = np.random.default_rng(1)
        worst = 0.0
        for i in range(4):
            rgb = rng.random((prog["in_h"], prog["in_w"], 3), dtype=np.float32)
            if i == 0:  # something smoother than noise
                yy, xx = np.mgrid[0:prog["in_h"], 0:prog["in_w"]]
                rgb = np.stack([xx / prog["in_w"], yy / prog["in_h"], 0.5 + 0 * xx], -1).astype(np.float32)
            ref = sess.run(None, {sess.get_inputs()[0].name: rgb[None]})[0].reshape(prog["in_h"], prog["in_w"])
            got = run_program(prog, rgb)
            worst = max(worst, float(np.abs(ref - got).max()))
        print(f"largest difference from ONNX Runtime: {worst:.5f}")
        return 0 if worst < 0.02 else 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
