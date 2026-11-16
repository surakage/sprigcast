"""Rewrite MediaPipe's custom TFL_Convolution2DTransposeBias node (left by tf2onnx) as ONNX ConvTranspose.

Usage: python fix_transpose_conv.py in.onnx out.onnx (see README.md). Assumes a 2 x 2 kernel at stride 2,
as in the MediaPipe selfie models; the node's input is NHWC.
"""
import sys, onnx, numpy as np
from onnx import numpy_helper, helper
src, dst = sys.argv[1], sys.argv[2]
m = onnx.load(src); g = m.graph
inits = {i.name: i for i in g.initializer}
nodes = list(g.node)
for idx, n in enumerate(nodes):
    if n.op_type != "TFL_Convolution2DTransposeBias":
        continue
    x, w, b = n.input
    perm = [n2 for n2 in nodes if n2.output[0] == x][0]
    print("feeding transpose perm", [a.ints for a in perm.attribute])
    W = numpy_helper.to_array(inits[w])           # (out, kh, kw, in)
    B = numpy_helper.to_array(inits[b])
    Wt = np.ascontiguousarray(W.transpose(3, 0, 1, 2))   # (in, out, kh, kw)
    kh, kw = W.shape[1], W.shape[2]
    g.initializer.append(numpy_helper.from_array(Wt, w + "_ct"))
    g.initializer.append(numpy_helper.from_array(B, b + "_ct"))
    new = [
        helper.make_node("Transpose", [x], [n.name + "_nchw"], perm=[0, 3, 1, 2]),
        helper.make_node("ConvTranspose", [n.name + "_nchw", w + "_ct", b + "_ct"], [n.name + "_ct"],
                         kernel_shape=[kh, kw], strides=[2, 2]),
        helper.make_node("Transpose", [n.name + "_ct"], [n.output[0]], perm=[0, 2, 3, 1]),
    ]
    pos = list(g.node).index(n)
    g.node.remove(n)
    for k, nn in enumerate(new):
        g.node.insert(pos + k, nn)
used = {i for n in g.node for i in n.input}
keep = [i for i in g.initializer if i.name in used]  # drop the old weights
del g.initializer[:]
g.initializer.extend(keep)
onnx.checker.check_model(m)
onnx.save(m, dst)
