"""Draws scenes onto canvases on the GPU with moderngl, and reads the result back."""

from __future__ import annotations

import numpy as np

import moderngl

from sprigcast.core.scene import Canvas, Scene

VERTEX_SHADER = """
#version 330
uniform vec2 canvas_size;
in vec2 in_pos;      // canvas pixels, origin top-left
in vec2 in_uv;
out vec2 uv;
void main() {
    // Map canvas pixels to clip space so that canvas row 0 lands in framebuffer row 0.
    // A plain fbo.read() then returns rows top to bottom, with no flip needed.
    vec2 ndc = in_pos / canvas_size * 2.0 - 1.0;
    gl_Position = vec4(ndc, 0.0, 1.0);
    uv = in_uv;
}
"""

FRAGMENT_SHADER = """
#version 330
uniform sampler2D tex;
in vec2 uv;
out vec4 color;
void main() {
    color = texture(tex, uv);
}
"""


class _CanvasTarget:
    def __init__(self, ctx: moderngl.Context, canvas: Canvas):
        self.size = (canvas.width, canvas.height)
        self.color = ctx.texture(self.size, 4)
        self.fbo = ctx.framebuffer(color_attachments=[self.color])

    def release(self) -> None:
        self.fbo.release()
        self.color.release()


class Compositor:
    """Owns the GL context, one framebuffer per canvas, and one texture per source."""

    def __init__(self, ctx: moderngl.Context | None = None):
        self.ctx = ctx or create_context()
        self.program = self.ctx.program(vertex_shader=VERTEX_SHADER, fragment_shader=FRAGMENT_SHADER)
        self.vbo = self.ctx.buffer(reserve=4 * 4 * 4, dynamic=True)  # 4 vertices x (x, y, u, v) float32
        self.vao = self.ctx.vertex_array(self.program, [(self.vbo, "2f 2f", "in_pos", "in_uv")])
        self.textures: dict[str, moderngl.Texture] = {}
        self.targets: dict[str, _CanvasTarget] = {}

    # Sources -------------------------------------------------------------

    def upload(self, source_id: str, frame: np.ndarray, channel_order: str = "RGBA") -> None:
        """Copy a new frame for a source to the GPU, resizing its texture if needed.

        BGRA frames (screen capture) are uploaded as-is and swizzled when sampled, so no CPU conversion.
        """
        h, w = frame.shape[:2]
        tex = self.textures.get(source_id)
        if tex is None or tex.size != (w, h):
            if tex is not None:
                tex.release()
            tex = self.ctx.texture((w, h), 4)
            tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
            self.textures[source_id] = tex
        tex.swizzle = "BGRA" if channel_order == "BGRA" else "RGBA"
        tex.write(np.ascontiguousarray(frame, dtype=np.uint8))

    def forget(self, source_id: str) -> None:
        tex = self.textures.pop(source_id, None)
        if tex is not None:
            tex.release()

    # Canvases ------------------------------------------------------------

    def _target(self, canvas: Canvas) -> _CanvasTarget:
        target = self.targets.get(canvas.id)
        if target is None or target.size != (canvas.width, canvas.height):
            if target is not None:
                target.release()
            target = _CanvasTarget(self.ctx, canvas)
            self.targets[canvas.id] = target
        return target

    def render(self, canvas: Canvas, scene: Scene | None, source_sizes: dict[str, tuple[int, int]]) -> None:
        """Draw a scene's visible items back to front onto its canvas."""
        target = self._target(canvas)
        target.fbo.use()
        self.ctx.viewport = (0, 0, canvas.width, canvas.height)
        self.ctx.clear(0.0, 0.0, 0.0, 1.0)
        if scene is None:
            return
        self.ctx.enable(moderngl.BLEND)
        self.ctx.blend_func = (moderngl.SRC_ALPHA, moderngl.ONE_MINUS_SRC_ALPHA, moderngl.ONE, moderngl.ONE_MINUS_SRC_ALPHA)
        self.program["canvas_size"].value = (float(canvas.width), float(canvas.height))
        self.program["tex"].value = 0
        for item in scene.items:
            tex = self.textures.get(item.source)
            size = source_sizes.get(item.source)
            if not item.visible or tex is None or size is None:
                continue
            quad = item.quad(size)
            if not quad:
                continue
            self.vbo.write(np.array(quad, dtype="f4").tobytes())
            tex.use(location=0)
            self.vao.render(moderngl.TRIANGLE_STRIP, vertices=4)

    def read(self, canvas_id: str) -> bytes:
        """Return the canvas as tightly packed RGBA rows, top row first."""
        return self.targets[canvas_id].fbo.read(components=4, alignment=1)

    def release(self) -> None:
        for tex in self.textures.values():
            tex.release()
        for target in self.targets.values():
            target.release()
        self.vao.release()
        self.vbo.release()
        self.program.release()
        self.ctx.release()


def create_context() -> moderngl.Context:
    """A headless OpenGL 3.3 context. On Windows this uses WGL; on Linux it falls back to EGL."""
    try:
        return moderngl.create_standalone_context(require=330)
    except Exception:
        return moderngl.create_standalone_context(require=330, backend="egl")
