"""SprigCast's own little line icons, drawn instead of emoji or font symbols.

Emoji look different on every PC (and come in full colour on Windows), so every icon the app shows is drawn
here in one style: thin rounded strokes on a 24 x 24 grid, the same look as the source icons in Build.
The sprig is the logo's two leaves on a stem, filled, so it reads at any size.

    paint_icon(painter, rect, "lock", colour)   # draw into a rect
    icon("search")                              # a QIcon for a button or menu (sharp on high-DPI screens)
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

from sprigcast.app import theme


def _leaf(p: QPainter, base: QPointF, tip: QPointF, bulge: float) -> None:
    """A filled leaf from base to tip, fattest in the middle; bulge > 0 bows it to the left of base->tip."""
    dx, dy = tip.x() - base.x(), tip.y() - base.y()
    nx, ny = -dy, dx  # normal
    a = QPointF(base.x() + dx * 0.25 + nx * bulge, base.y() + dy * 0.25 + ny * bulge)
    b = QPointF(base.x() + dx * 0.8 + nx * bulge * 0.9, base.y() + dy * 0.8 + ny * bulge * 0.9)
    c = QPointF(base.x() + dx * 0.75 - nx * bulge * 0.55, base.y() + dy * 0.75 - ny * bulge * 0.55)
    d = QPointF(base.x() + dx * 0.2 - nx * bulge * 0.55, base.y() + dy * 0.2 - ny * bulge * 0.55)
    path = QPainterPath(base)
    path.cubicTo(a, b, tip)
    path.cubicTo(c, d, base)
    p.fillPath(path, p.pen().color())


def _gear(p: QPainter) -> None:
    p.drawEllipse(QPointF(12, 12), 3, 3)
    path = QPainterPath()
    teeth = 8
    for i in range(teeth * 2):
        angle = math.pi * 2 * i / (teeth * 2) - math.pi / 2
        r_out = 8.5 if i % 2 == 0 else 6.6
        for j, off in enumerate((-0.17, 0.17)):
            point = QPointF(12 + math.cos(angle + off) * r_out, 12 + math.sin(angle + off) * r_out)
            if i == 0 and j == 0:
                path.moveTo(point)
            else:
                path.lineTo(point)
    path.closeSubpath()
    p.drawPath(path)


def _sprig(p: QPainter) -> None:
    pen = p.pen()
    pen.setWidthF(2.0)
    p.setPen(pen)
    stem = QPainterPath(QPointF(12, 21))
    stem.cubicTo(QPointF(12, 16), QPointF(11, 12), QPointF(12, 7.5))
    p.drawPath(stem)
    _leaf(p, QPointF(12, 12.5), QPointF(20.5, 6), 0.42)
    _leaf(p, QPointF(11.7, 16), QPointF(3.5, 11), -0.42)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(pen.color())
    p.drawEllipse(QPointF(12, 5.2), 1.9, 1.9)  # the bud at the top, like the logo's dot


def _draw(p: QPainter, name: str) -> None:
    """Draw `name` on the 24 x 24 grid with the current pen."""
    P = QPointF
    if name == "sprig":
        _sprig(p)
    elif name == "bloom":  # five round petals around a centre
        for i in range(5):
            angle = math.pi * 2 * i / 5 - math.pi / 2
            p.drawEllipse(P(12 + math.cos(angle) * 5, 12 + math.sin(angle) * 5), 3.6, 3.6)
        p.drawEllipse(P(12, 12), 1.6, 1.6)
    elif name == "leaf":
        _leaf(p, P(5, 19), P(19.5, 4.5), 0.42)
    elif name == "gear":
        _gear(p)
    elif name == "search":
        p.drawEllipse(P(10.5, 10.5), 6, 6)
        p.drawLine(P(15, 15), P(20, 20))
    elif name in ("lock", "unlock"):
        p.drawRoundedRect(QRectF(5, 11, 14, 10), 2.5, 2.5)
        shackle = QPainterPath(P(8, 11))
        shackle.lineTo(P(8, 7.5))
        shackle.cubicTo(P(8, 2.5), P(16, 2.5), P(16, 7.5))
        if name == "lock":
            shackle.lineTo(P(16, 11))
        p.drawPath(shackle)
        p.drawLine(P(12, 15), P(12, 17))
    elif name == "link":  # two rounded chain links on a slant
        p.save()
        p.translate(12, 12)
        p.rotate(-45)
        p.drawRoundedRect(QRectF(-9.5, -3.2, 10, 6.4), 3.2, 3.2)
        p.drawRoundedRect(QRectF(-0.5, -3.2, 10, 6.4), 3.2, 3.2)
        p.restore()
    elif name == "key":
        p.drawEllipse(P(8, 12), 4, 4)
        p.drawLine(P(12, 12), P(21, 12))
        p.drawLine(P(18, 12), P(18, 15.5))
        p.drawLine(P(21, 12), P(21, 15))
    elif name == "upload":  # signal bars
        for i, h in enumerate((5, 9, 13)):
            x = 6 + i * 6
            p.drawLine(P(x, 19), P(x, 19 - h))
    elif name == "monitor":
        p.drawRoundedRect(QRectF(3.5, 4.5, 17, 11.5), 2, 2)
        p.drawLine(P(12, 16), P(12, 19.5))
        p.drawLine(P(8, 19.5), P(16, 19.5))
    elif name == "mic":
        p.drawRoundedRect(QRectF(9, 3, 6, 11), 3, 3)
        p.drawArc(QRectF(5.5, 6, 13, 11), 180 * 16, 180 * 16)
        p.drawLine(P(12, 17), P(12, 20.5))
    elif name == "camera":
        p.drawRoundedRect(QRectF(3, 6.5, 12.5, 11), 2.5, 2.5)
        tri = QPainterPath(P(15.5, 10.5))
        tri.lineTo(P(21, 7.5))
        tri.lineTo(P(21, 16.5))
        tri.lineTo(P(15.5, 13.5))
        p.drawPath(tri)
    elif name == "disk":  # a drive with a light
        p.drawRoundedRect(QRectF(3.5, 8, 17, 9), 2.5, 2.5)
        p.drawLine(P(7, 12.5), P(12, 12.5))
        p.drawPoint(P(17, 12.5))
    elif name == "bolt":
        path = QPainterPath(P(13.5, 3))
        for x, y in ((6, 13.5), (11.5, 13.5), (10.5, 21), (18, 10.5), (12.5, 10.5)):
            path.lineTo(P(x, y))
        path.closeSubpath()
        p.drawPath(path)
    elif name == "panel":  # a window with a side panel
        p.drawRoundedRect(QRectF(3.5, 5, 17, 14), 2.5, 2.5)
        p.drawLine(P(14.5, 5), P(14.5, 19))
    elif name == "plus":
        p.drawLine(P(12, 5), P(12, 19))
        p.drawLine(P(5, 12), P(19, 12))
    elif name == "minus":
        p.drawLine(P(5, 12), P(19, 12))
    elif name == "swap":  # two arrows passing: switch together (Link)
        p.drawLine(P(4, 8.5), P(19, 8.5))
        p.drawPolyline([P(15.5, 5), P(19, 8.5), P(15.5, 12)])
        p.drawLine(P(20, 15.5), P(5, 15.5))
        p.drawPolyline([P(8.5, 12), P(5, 15.5), P(8.5, 19)])
    elif name == "compass":
        p.drawEllipse(P(12, 12), 8.5, 8.5)
        needle = QPainterPath(P(15.5, 8.5))
        needle.lineTo(P(13, 13))
        needle.lineTo(P(8.5, 15.5))
        needle.lineTo(P(11, 11))
        needle.closeSubpath()
        p.drawPath(needle)
    elif name == "trash":  # a bin with a lid
        p.drawLine(P(4.5, 7), P(19.5, 7))
        p.drawLine(P(9.5, 7), P(10, 4.5))
        p.drawLine(P(10, 4.5), P(14, 4.5))
        p.drawLine(P(14, 4.5), P(14.5, 7))
        bin_ = QPainterPath(P(6.5, 7))
        bin_.lineTo(P(7.5, 19.5))
        bin_.lineTo(P(16.5, 19.5))
        bin_.lineTo(P(17.5, 7))
        p.drawPath(bin_)
        p.drawLine(P(10.5, 10.5), P(10.5, 16))
        p.drawLine(P(13.5, 10.5), P(13.5, 16))
    elif name == "folder":
        path = QPainterPath(P(3.5, 17.5))
        for x, y in ((3.5, 6.5), (9, 6.5), (11, 8.5), (20.5, 8.5), (20.5, 17.5)):
            path.lineTo(P(x, y))
        path.closeSubpath()
        p.drawPath(path)
    elif name in ("speaker", "speaker-off"):
        body = QPainterPath(P(4, 9.5))
        for x, y in ((8, 9.5), (13, 5), (13, 19), (8, 14.5), (4, 14.5)):
            body.lineTo(P(x, y))
        body.closeSubpath()
        p.drawPath(body)
        if name == "speaker":
            p.drawArc(QRectF(11.5, 8, 6, 8), -60 * 16, 120 * 16)
            p.drawArc(QRectF(11.5, 5, 9, 14), -60 * 16, 120 * 16)
        else:
            p.drawLine(P(16, 9.5), P(21, 14.5))
            p.drawLine(P(21, 9.5), P(16, 14.5))
    elif name == "gamepad":  # a controller: rounded body, d-pad, two buttons
        body = QPainterPath(P(7, 7.5))
        body.lineTo(P(17, 7.5))
        body.cubicTo(P(21, 7.5), P(22, 18.5), P(19, 18.5))
        body.cubicTo(P(17, 18.5), P(16.5, 15.5), P(14.5, 15.5))
        body.lineTo(P(9.5, 15.5))
        body.cubicTo(P(7.5, 15.5), P(7, 18.5), P(5, 18.5))
        body.cubicTo(P(2, 18.5), P(3, 7.5), P(7, 7.5))
        p.drawPath(body)
        p.drawLine(P(6, 11.5), P(10, 11.5))
        p.drawLine(P(8, 9.5), P(8, 13.5))
        p.drawPoint(P(15.5, 10.5))
        p.drawPoint(P(17.5, 12.5))
    elif name == "keyboard":
        p.drawRoundedRect(QRectF(3, 6.5, 18, 11), 2.5, 2.5)
        for x in (7, 10, 13, 16):
            p.drawPoint(P(x, 10))
        for x in (8.5, 11.5, 14.5):
            p.drawPoint(P(x, 12.75))
        p.drawLine(P(9, 15.5), P(15, 15.5))
    elif name == "shutter":  # a ring with a dot: take a screenshot
        p.drawEllipse(P(12, 12), 8, 8)
        p.setBrush(p.pen().color())
        p.drawEllipse(P(12, 12), 3.5, 3.5)
    elif name == "chat":  # a speech bubble with a tail
        bubble = QPainterPath(P(7, 5))
        bubble.lineTo(P(17, 5))
        bubble.cubicTo(P(19.5, 5), P(20.5, 6), P(20.5, 8.5))
        bubble.lineTo(P(20.5, 13))
        bubble.cubicTo(P(20.5, 15.5), P(19.5, 16.5), P(17, 16.5))
        bubble.lineTo(P(10, 16.5))
        bubble.lineTo(P(6, 20))
        bubble.lineTo(P(6.5, 16.4))
        bubble.cubicTo(P(4.5, 16), P(3.5, 15), P(3.5, 13))
        bubble.lineTo(P(3.5, 8.5))
        bubble.cubicTo(P(3.5, 6), P(4.5, 5), P(7, 5))
        p.drawPath(bubble)
    elif name == "note":  # two notes on a beam
        p.drawEllipse(P(7, 17.5), 2.6, 2.1)
        p.drawEllipse(P(17, 15.5), 2.6, 2.1)
        p.drawLine(P(9.6, 17.3), P(9.6, 6.5))
        p.drawLine(P(19.6, 15.3), P(19.6, 4.5))
        p.drawLine(P(9.6, 6.5), P(19.6, 4.5))
    elif name == "trophy":  # a cup with handles on a stand
        cup = QPainterPath(P(7, 4.5))
        cup.lineTo(P(17, 4.5))
        cup.lineTo(P(17, 9))
        cup.cubicTo(P(17, 12.5), P(14.5, 14.5), P(12, 14.5))
        cup.cubicTo(P(9.5, 14.5), P(7, 12.5), P(7, 9))
        cup.closeSubpath()
        p.drawPath(cup)
        p.drawArc(QRectF(3.5, 6, 4.5, 5), 90 * 16, 180 * 16)
        p.drawArc(QRectF(16, 6, 4.5, 5), -90 * 16, 180 * 16)
        p.drawLine(P(12, 14.5), P(12, 18))
        p.drawLine(P(8, 19.5), P(16, 19.5))
    elif name == "ball":  # a ball with seams
        p.drawEllipse(P(12, 12), 8, 8)
        p.drawLine(P(4, 12), P(20, 12))
        p.drawArc(QRectF(6.5, 4, 11, 16), 60 * 16, 60 * 16)
        p.drawArc(QRectF(6.5, 4, 11, 16), -120 * 16, 60 * 16)
        p.drawArc(QRectF(1, 4, 11, 16), -60 * 16, 120 * 16)
        p.drawArc(QRectF(12, 4, 11, 16), 120 * 16, 120 * 16)
    elif name == "broadcast":  # a dot sending waves both ways
        p.setBrush(p.pen().color())
        p.drawEllipse(P(12, 12), 2, 2)
        p.setBrush(Qt.BrushStyle.NoBrush)
        for r in (5, 8.5):
            p.drawArc(QRectF(12 - r, 12 - r, 2 * r, 2 * r), -45 * 16, 90 * 16)
            p.drawArc(QRectF(12 - r, 12 - r, 2 * r, 2 * r), 135 * 16, 90 * 16)
    elif name == "record":  # a ring with a filled centre
        p.drawEllipse(P(12, 12), 8, 8)
        p.setBrush(p.pen().color())
        p.drawEllipse(P(12, 12), 4, 4)
    elif name == "landscape":
        p.drawRoundedRect(QRectF(3, 6.5, 18, 11), 2, 2)
    elif name == "portrait":
        p.drawRoundedRect(QRectF(7.5, 3, 9, 18), 2, 2)
    elif name == "dual":  # a wide and a tall canvas side by side
        p.drawRoundedRect(QRectF(2.5, 8, 12, 8), 1.6, 1.6)
        p.drawRoundedRect(QRectF(16, 4.5, 6, 15), 1.6, 1.6)
    elif name == "clip":  # a clapperboard: save the last few seconds
        p.drawRoundedRect(QRectF(4, 9.5, 16, 10.5), 2, 2)
        p.drawLine(P(4, 9.5), P(19, 5.5))
        p.drawLine(P(9, 8.2), P(11, 10))
        p.drawLine(P(14, 6.9), P(16, 8.6))
    elif name == "heart":
        path = QPainterPath(P(12, 19.5))
        path.cubicTo(P(6, 15.5), P(3.5, 12), P(3.5, 9))
        path.cubicTo(P(3.5, 6), P(5.8, 4.5), P(8, 4.5))
        path.cubicTo(P(9.8, 4.5), P(11.2, 5.6), P(12, 7.2))
        path.cubicTo(P(12.8, 5.6), P(14.2, 4.5), P(16, 4.5))
        path.cubicTo(P(18.2, 4.5), P(20.5, 6), P(20.5, 9))
        path.cubicTo(P(20.5, 12), P(18, 15.5), P(12, 19.5))
        p.drawPath(path)
    elif name == "tree":  # a round crown on a trunk
        p.drawLine(P(12, 21), P(12, 13.5))
        p.drawLine(P(12, 16.5), P(14.5, 14))
        p.drawEllipse(P(12, 9), 6.5, 6)
    elif name == "paw":  # a pad and four toes
        p.drawEllipse(P(12, 15.5), 4.2, 3.6)
        for x, y in ((6.2, 10.5), (9.6, 6.8), (14.4, 6.8), (17.8, 10.5)):
            p.drawEllipse(P(x, y), 1.8, 2.1)
    elif name == "chevron-down":
        path = QPainterPath(P(7, 10))
        path.lineTo(P(12, 15))
        path.lineTo(P(17, 10))
        p.drawPath(path)
    elif name == "chevron-left":
        path = QPainterPath(P(14, 7))
        path.lineTo(P(9, 12))
        path.lineTo(P(14, 17))
        p.drawPath(path)
    elif name == "chevron-up":
        path = QPainterPath(P(7, 14))
        path.lineTo(P(12, 9))
        path.lineTo(P(17, 14))
        p.drawPath(path)
    elif name == "warning":  # a rounded triangle with a mark: something needs a look
        path = QPainterPath(P(12, 4))
        path.lineTo(P(21, 19.5))
        path.lineTo(P(3, 19.5))
        path.closeSubpath()
        p.drawPath(path)
        p.drawLine(P(12, 10), P(12, 13.5))
        p.drawPoint(P(12, 16.6))
    elif name == "clock":  # waiting or starting
        p.drawEllipse(P(12, 12), 8, 8)
        p.drawLine(P(12, 12), P(12, 7.5))
        p.drawLine(P(12, 12), P(15.2, 13.8))
    elif name == "ban":  # a circle with a slash: banned
        p.drawEllipse(P(12, 12), 8, 8)
        p.drawLine(P(6.4, 17.6), P(17.6, 6.4))
    elif name == "moon":  # asleep
        path = QPainterPath()
        path.addEllipse(P(12, 12), 8, 8)
        bite = QPainterPath()
        bite.addEllipse(P(16.5, 8.5), 6.8, 6.8)
        p.drawPath(path.subtracted(bite))
    elif name == "plug":  # unplugged
        p.drawRoundedRect(QRectF(7, 8, 10, 7), 2, 2)
        p.drawLine(P(10, 8), P(10, 4))
        p.drawLine(P(14, 8), P(14, 4))
        p.drawLine(P(12, 15), P(12, 20))
    elif name == "eye":  # shown (Preview on a canvas's right-click card)
        path = QPainterPath(P(3, 12))
        path.quadTo(P(12, 4), P(21, 12))
        path.quadTo(P(12, 20), P(3, 12))
        p.drawPath(path)
        p.drawEllipse(P(12, 12), 2.6, 2.6)
    elif name == "eye-off":  # hidden here
        path = QPainterPath(P(3, 12))
        path.quadTo(P(12, 4), P(21, 12))
        path.quadTo(P(12, 20), P(3, 12))
        p.drawPath(path)
        p.drawEllipse(P(12, 12), 2.6, 2.6)
        p.drawLine(P(5, 19), P(19, 5))
    elif name == "sliders":  # filters on it
        for x, knob in ((7, 9), (12, 15), (17, 8)):
            p.drawLine(P(x, 4.5), P(x, 19.5))
            p.save()
            p.setBrush(p.pen().color())
            p.drawEllipse(P(x, knob), 2, 2)
            p.restore()
    elif name == "close":
        p.drawLine(P(6.5, 6.5), P(17.5, 17.5))
        p.drawLine(P(17.5, 6.5), P(6.5, 17.5))
    elif name in FILTER_ICONS:
        _filter_icon(p, name)
    else:  # anything unknown: a small dot
        p.drawEllipse(P(12, 12), 2, 2)


def _filter_icon(p: QPainter, name: str) -> None:
    """One icon per video filter (the Filters window's list and Add filter menu)."""
    P = QPointF
    if name == "spin":  # an arrow going round
        p.drawArc(QRectF(3.5, 3.5, 17, 17), 40 * 16, 290 * 16)
        p.drawLine(P(19.6, 3.8), P(19.6, 8.6))
        p.drawLine(P(19.6, 8.6), P(14.8, 8.6))
    elif name == "curtain":  # a filled block
        p.save()
        p.setBrush(p.pen().color())
        p.drawRoundedRect(QRectF(4.5, 4.5, 15, 15), 3, 3)
        p.restore()
    elif name == "mist":  # drifting haze
        for y, x0, x1 in ((8, 3.5, 16), (13, 6, 20.5), (18, 3.5, 13)):
            p.drawLine(P(x0, y), P(x1, y))
        p.drawArc(QRectF(14, 4.5, 5, 5), -90 * 16, 270 * 16)
    elif name in ("greenscreen", "backdrop"):  # a person in a frame; dashed frame for a removed background
        if name == "backdrop":
            pen = p.pen()
            pen.setDashPattern([1.6, 1.4])
            p.save()
            p.setPen(pen)
            p.drawRoundedRect(QRectF(3, 4, 18, 16), 3, 3)
            p.restore()
        else:
            p.drawRoundedRect(QRectF(3, 4, 18, 16), 3, 3)
        p.drawEllipse(P(12, 10), 2.6, 2.6)
        p.drawArc(QRectF(7.5, 15.5, 9, 9), 0, 180 * 16)
    elif name == "tone":  # sliders lying down
        for y, knob in ((6, 15.5), (12, 9), (18, 17)):
            p.drawLine(P(4, y), P(20, y))
            p.save()
            p.setBrush(QColor(theme.PANEL))
            p.drawEllipse(P(knob, y), 2.2, 2.2)
            p.restore()
    elif name == "colorcut":  # an eyedropper
        p.drawLine(P(13.5, 4.5), P(19.5, 10.5))  # its collar
        tube = QPainterPath(P(15, 6))
        tube.lineTo(P(6.5, 14.5))
        tube.lineTo(P(4.5, 19.5))
        tube.lineTo(P(9.5, 17.5))
        tube.lineTo(P(18, 9))
        p.drawPath(tube)
    elif name == "lightcut":  # half a sun
        p.drawEllipse(P(12, 12), 4.3, 4.3)
        p.save()
        p.setBrush(p.pen().color())
        p.drawPie(QRectF(7.7, 7.7, 8.6, 8.6), 90 * 16, 180 * 16)
        p.restore()
        for i in range(8):
            a = math.pi * 2 * i / 8
            p.drawLine(P(12 + math.cos(a) * 7.2, 12 + math.sin(a) * 7.2), P(12 + math.cos(a) * 9, 12 + math.sin(a) * 9))
    elif name == "mood":  # a painter's palette
        path = QPainterPath(P(12, 3.5))
        path.cubicTo(P(6.5, 3.5), P(3.5, 7.5), P(3.5, 12))
        path.cubicTo(P(3.5, 17), P(7.5, 20.5), P(12, 20.5))
        path.cubicTo(P(13.5, 20.5), P(14, 19), P(13.2, 17.8))
        path.cubicTo(P(12.2, 16.3), P(13, 14.5), P(15, 14.5))
        path.lineTo(P(17, 14.5))
        path.cubicTo(P(19.2, 14.5), P(20.5, 12.8), P(20.5, 10.5))
        path.cubicTo(P(20.5, 6.5), P(16.5, 3.5), P(12, 3.5))
        p.drawPath(path)
        p.save()
        c = p.pen().color()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c)
        for x, y in ((8, 11.5), (10, 7.5), (14.5, 7.5)):
            p.drawEllipse(P(x, y), 1.3, 1.3)
        p.restore()
    elif name == "freeze":  # a snowflake
        for i in range(3):
            a = math.pi / 2 + math.pi * i / 3
            dx, dy = math.cos(a) * 9, math.sin(a) * 9
            p.drawLine(P(12 - dx, 12 - dy), P(12 + dx, 12 + dy))
        for sign in (-1, 1):
            y = 12 + sign * 9
            p.drawLine(P(9.5, y - sign * 2.2), P(12, y - sign * 0.3))
            p.drawLine(P(14.5, y - sign * 2.2), P(12, y - sign * 0.3))
    elif name == "glow":  # a four-point sparkle and a small one
        star = QPainterPath(P(11, 3))
        for x, y in ((12.8, 8.4), (18.5, 10.5), (12.8, 12.6), (11, 18), (9.2, 12.6), (3.5, 10.5), (9.2, 8.4)):
            star.lineTo(P(x, y))
        star.closeSubpath()
        p.drawPath(star)
        small = QPainterPath(P(18.5, 14.5))
        for x, y in ((19.4, 17.1), (21.5, 18), (19.4, 18.9), (18.5, 21.5), (17.6, 18.9), (15.5, 18), (17.6, 17.1)):
            small.lineTo(P(x, y))
        small.closeSubpath()
        p.drawPath(small)
    elif name == "mosaic":  # four blocks
        for x, y in ((3.5, 3.5), (13.5, 3.5), (3.5, 13.5), (13.5, 13.5)):
            p.drawRoundedRect(QRectF(x, y, 7, 7), 1.5, 1.5)
    elif name == "drift":  # arrows passing each other
        p.drawLine(P(3.5, 8), P(18, 8))
        p.drawLine(P(18, 8), P(14.5, 4.5))
        p.drawLine(P(20.5, 16), P(6, 16))
        p.drawLine(P(6, 16), P(9.5, 19.5))
    elif name == "shadow":  # a square with its shadow
        p.drawRoundedRect(QRectF(3.5, 3.5, 12.5, 12.5), 3, 3)
        path = QPainterPath(P(20, 8))
        path.lineTo(P(20, 17))
        path.cubicTo(P(20, 18.8), P(18.8, 20), P(17, 20))
        path.lineTo(P(8, 20))
        p.drawPath(path)
    elif name == "frame":  # a round cut-out in a frame
        p.drawRoundedRect(QRectF(3.5, 3.5, 17, 17), 4, 4)
        p.drawEllipse(P(12, 12), 4.8, 4.8)
    elif name == "crisp":  # a cut gem
        path = QPainterPath(P(12, 3))
        for x, y in ((20, 11.5), (12, 21), (4, 11.5)):
            path.lineTo(P(x, y))
        path.closeSubpath()
        p.drawPath(path)
        p.drawLine(P(4, 11.5), P(20, 11.5))
    elif name == "outline":  # a dashed ring
        pen = p.pen()
        pen.setCapStyle(Qt.PenCapStyle.FlatCap)
        pen.setDashPattern([1.7, 1.5])
        p.save()
        p.setPen(pen)
        p.drawEllipse(P(12, 12), 8, 8)
        p.restore()


FILTER_ICONS = ("spin", "curtain", "mist", "greenscreen", "backdrop", "tone", "colorcut", "lightcut", "mood", "freeze",
                "glow", "mosaic", "drift", "shadow", "frame", "crisp", "outline")

NAMES = ("sprig", "bloom", "leaf", "gear", "search", "lock", "unlock", "link", "key", "upload", "monitor", "mic",
         "camera", "disk", "bolt", "panel", "plus", "compass", "folder", "trash", "speaker", "speaker-off",
         "gamepad", "keyboard", "shutter", "chat", "note", "trophy", "ball", "broadcast", "record", "landscape",
         "portrait", "dual", "clip", "heart", "tree", "paw", "chevron-down", "chevron-up", "chevron-left", "minus", "swap", "warning", "clock", "moon", "plug", "eye", "eye-off", "sliders", "close") + FILTER_ICONS


def paint_icon(p: QPainter, r: QRectF, name: str, colour: str | QColor | None = None, width: float = 1.9) -> None:
    """Draw icon `name` centred in `r` (square-ish), in `colour` (default: the normal text colour).
    Its box starts on a whole pixel, so lines land on the same pixels every time (crisper)."""
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    side = min(r.width(), r.height())
    p.translate(round(r.center().x() - side / 2), round(r.center().y() - side / 2))
    p.scale(side / 24, side / 24)
    pen = QPen(QColor(colour or theme.TEXT), width * 24 / max(side, 1))  # `width` screen pixels at any size
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    _draw(p, name)
    p.restore()


def pixmap(name: str, colour: str | QColor | None = None, size: int = 18, ratio: float = 2.0) -> QPixmap:
    pix = QPixmap(int(size * ratio), int(size * ratio))
    pix.setDevicePixelRatio(ratio)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    paint_icon(p, QRectF(0, 0, size, size), name, colour)
    p.end()
    return pix


_cache: dict[tuple, QIcon] = {}


def icon(name: str, colour: str | QColor | None = None, size: int = 18) -> QIcon:
    """A QIcon of `name`, drawn once per colour."""
    key = (name, QColor(colour or theme.TEXT).name(QColor.NameFormat.HexArgb), size)
    if key not in _cache:
        made = QIcon()
        # One picture per common Windows scale (100-300%), so Qt never stretches or shrinks one: a 2x
        # picture shrunk to 150% is what made the top bar's icons look soft (PC, 2026-11-20).
        for ratio in (1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0):
            made.addPixmap(pixmap(name, colour, size, ratio))
        _cache[key] = made
    return _cache[key]
