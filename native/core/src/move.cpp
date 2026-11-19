// The Move transition's arithmetic: render/move.py frame() and the helpers it uses, in the same order, so the
// engine and Python's compositor draw the same picture at the same progress. See move.h.
#include "move.h"

#include <algorithm>
#include <cmath>
#include <map>
#include <utility>

namespace sc {
namespace {

constexpr double PI = 3.14159265358979323846;
constexpr double TAU = 2.0 * PI;
constexpr double SCALE_TO = 0.8;    // "scale": what is leaving shrinks to this, what is arriving grows from it
constexpr double SLIDE_EDGE = 0.25; // "slide": how far past the nearest edge, as a share of the canvas

using Quad = MoveCorner[4];

double clamp01(double t) { return std::min(1.0, std::max(0.0, t)); }

double smooth(double t) {
    t = clamp01(t);
    return t * t * (3.0 - 2.0 * t);
}

double floor_mod(double a, double b) {  // Python's a % b: the sign of b
    double r = std::fmod(a, b);
    if (r != 0.0 && ((r < 0.0) != (b < 0.0))) r += b;
    return r;
}

struct Shape {  // centre x, centre y, width, angle, height, shear (move.py _shape)
    double cx, cy, w, angle, height, shear;
};

Shape shape_of(const Quad &q) {
    const double ex0 = q[1].x - q[0].x, ex1 = q[1].y - q[0].y;
    const double ey0 = q[2].x - q[0].x, ey1 = q[2].y - q[0].y;
    const double w = std::hypot(ex0, ex1);
    const double angle = w > 1e-9 ? std::atan2(ex1, ex0) : 0.0;
    const double c = std::cos(angle), s = std::sin(angle);
    return {(q[0].x + q[3].x) / 2, (q[0].y + q[3].y) / 2, w, angle, -ey0 * s + ey1 * c, ey0 * c + ey1 * s};
}

void quad_of(const Shape &sh, const double uv[4][2], Quad &out) {
    const double c = std::cos(sh.angle), s = std::sin(sh.angle);
    const double ex0 = sh.w * c, ex1 = sh.w * s;
    const double ey0 = sh.shear * c - sh.height * s, ey1 = sh.shear * s + sh.height * c;
    const double x0 = sh.cx - (ex0 + ey0) / 2, y0 = sh.cy - (ex1 + ey1) / 2;
    const double xy[4][2] = {{x0, y0}, {x0 + ex0, y0 + ex1}, {x0 + ey0, y0 + ey1}, {x0 + ex0 + ey0, y0 + ex1 + ey1}};
    for (int i = 0; i < 4; ++i) out[i] = {xy[i][0], xy[i][1], uv[i][0], uv[i][1]};
}

void uvs_of(const Quad &q, double uv[4][2]) {
    for (int i = 0; i < 4; ++i) {
        uv[i][0] = q[i].u;
        uv[i][1] = q[i].v;
    }
}

void quad_from(const sc_move_piece &p, Quad &out) {
    for (int i = 0; i < 4; ++i) out[i] = {p.quad[i][0], p.quad[i][1], p.quad[i][2], p.quad[i][3]};
}

void between(const Quad &a, const Quad &b, double e, Quad &out) {
    const Shape sa = shape_of(a), sb = shape_of(b);
    const double turn = floor_mod(sb.angle - sa.angle + PI, TAU) - PI;
    Shape sh = {sa.cx + (sb.cx - sa.cx) * e, sa.cy + (sb.cy - sa.cy) * e, sa.w + (sb.w - sa.w) * e,
                sa.angle + turn * e, sa.height + (sb.height - sa.height) * e, sa.shear + (sb.shear - sa.shear) * e};
    double uv[4][2];
    for (int i = 0; i < 4; ++i) {
        const double u0 = a[i].u, v0 = a[i].v, u1 = b[i].u, v1 = b[i].v;
        const double u = u0 + (u1 - u0) * e, v = v0 + (v1 - v0) * e;
        // A spring passes its end: keep the crop within the two it moves between (never past the picture).
        uv[i][0] = std::min(std::max(u, std::min(u0, u1)), std::max(u0, u1));
        uv[i][1] = std::min(std::max(v, std::min(v0, v1)), std::max(v0, v1));
    }
    quad_of(sh, uv, out);
}

void scaled(const Quad &q, double k, Quad &out) {
    Shape sh = shape_of(q);
    sh.w *= k;
    sh.height *= k;
    sh.shear *= k;
    double uv[4][2];
    uvs_of(q, uv);
    quad_of(sh, uv, out);
}

bool covers(const Quad &q, double cw, double ch) {
    double minx = q[0].x, maxx = q[0].x, miny = q[0].y, maxy = q[0].y;
    for (int i = 1; i < 4; ++i) {
        minx = std::min(minx, q[i].x);
        maxx = std::max(maxx, q[i].x);
        miny = std::min(miny, q[i].y);
        maxy = std::max(maxy, q[i].y);
    }
    if (minx > 0.5 || miny > 0.5 || maxx < cw - 0.5 || maxy < ch - 0.5) return false;
    const double area = (q[1].x - q[0].x) * (q[2].y - q[0].y) - (q[1].y - q[0].y) * (q[2].x - q[0].x);
    return std::abs(area) >= cw * ch - 1.0;  // not turned
}

void slid(const Quad &q, double k, double cw, double ch, Quad &out) {
    double minx = q[0].x, maxx = q[0].x, miny = q[0].y, maxy = q[0].y;
    for (int i = 1; i < 4; ++i) {
        minx = std::min(minx, q[i].x);
        maxx = std::max(maxx, q[i].x);
        miny = std::min(miny, q[i].y);
        maxy = std::max(maxy, q[i].y);
    }
    const double cx = (minx + maxx) / 2, cy = (miny + maxy) / 2;
    // left, right, up, down: the first nearest wins, as Python's min() over its dict
    const int ways[4][2] = {{-1, 0}, {1, 0}, {0, -1}, {0, 1}};
    const double gaps[4] = {maxx, cw - minx, maxy, ch - miny};
    const double toward[4] = {cx, cw - cx, cy, ch - cy};
    int way = 0;
    for (int i = 1; i < 4; ++i)
        if (toward[i] < toward[way]) way = i;
    const double reach = gaps[way] + SLIDE_EDGE * (ways[way][0] ? cw : ch);
    const double dx = ways[way][0] * reach * k, dy = ways[way][1] * reach * k;
    for (int i = 0; i < 4; ++i) out[i] = {q[i].x + dx, q[i].y + dy, q[i].u, q[i].v};
}

MoveDraw draw_of(const sc_move_piece &p, const Quad &q, double opacity) {
    MoveDraw d;
    d.source = p.source;
    d.part = p.part;
    d.effects = p.effects;
    for (int i = 0; i < 4; ++i) d.quad[i] = q[i];
    d.opacity = opacity;
    return d;
}

}  // namespace

bool move_plan(const sc_move &move, MovePlan &out, const char *&why) {
    why = nullptr;
    if (move.old_count < 0 || move.new_count < 0 || move.effects_count < 0 ||
        move.old_count > 100000 || move.new_count > 100000 || move.effects_count > 100000)
        why = "bad counts";
    else if ((move.old_count + move.new_count > 0 && !move.pieces) || (move.effects_count > 0 && !move.effects))
        why = "no pieces or effects";
    else if (move.easing < SC_MOVE_SMOOTH || move.easing > SC_MOVE_SPRING)
        why = "unknown easing";
    else if (move.unmatched < SC_MOVE_FADE || move.unmatched > SC_MOVE_SLIDE)
        why = "unknown way in or out";
    if (why) return false;
    out = MovePlan();
    out.old_pieces.assign(move.pieces, move.pieces + move.old_count);
    out.new_pieces.assign(move.pieces + move.old_count, move.pieces + move.old_count + move.new_count);
    if (move.effects_count) out.effects.assign(move.effects, move.effects + move.effects_count);
    out.easing = move.easing;
    out.unmatched = move.unmatched;
    std::vector<bool> taken(out.old_pieces.size(), false);
    for (auto *list : {&out.old_pieces, &out.new_pieces}) {
        for (sc_move_piece &p : *list) {
            if (p.effects < -1 || p.effects >= move.effects_count) p.effects = -1;
            if (!(p.opacity >= 0.0f)) p.opacity = 0.0f;  // NaN too
            if (list == &out.old_pieces) {
                p.paired = -1;
                continue;
            }
            // each old piece moves to one new piece at most
            if (p.paired < 0 || p.paired >= move.old_count || taken[static_cast<size_t>(p.paired)]) p.paired = -1;
            else taken[static_cast<size_t>(p.paired)] = true;
        }
    }
    return true;
}

double move_ease(int easing, double t) {
    t = clamp01(t);
    switch (easing) {
        case SC_MOVE_LINEAR: return t;
        case SC_MOVE_EASE_IN: return t * t * t;
        case SC_MOVE_EASE_OUT: return 1.0 - std::pow(1.0 - t, 3);
        case SC_MOVE_SPRING: {  // back-out: passes its spot by about 8 % and comes back
            const double c = 1.70158, u = t - 1.0;
            return 1.0 + (c + 1.0) * u * u * u + c * u * u;
        }
        default:  // smooth: cubic ease-in-out
            return t < 0.5 ? 4 * t * t * t : 1.0 - std::pow(-2.0 * t + 2.0, 3) / 2;
    }
}

void move_frame(const MovePlan &plan, double progress, double cw, double ch, std::vector<MoveDraw> &out) {
    out.clear();
    const auto &old = plan.old_pieces;
    const auto &now = plan.new_pieces;
    const double t = clamp01(progress);
    const double e = move_ease(plan.easing, t);
    const double arrive = smooth(t / 0.5);
    const double leave = 1.0 - smooth((t - 0.5) / 0.5);
    const double move = move_ease(plan.easing == SC_MOVE_SPRING ? SC_MOVE_SMOOTH : plan.easing, t);

    std::vector<int> new_of_old(old.size(), -1);
    for (size_t j = 0; j < now.size(); ++j)
        if (now[j].paired >= 0) new_of_old[static_cast<size_t>(now[j].paired)] = static_cast<int>(j);

    auto coming_or_going = [&](const sc_move_piece &p, double k, double alpha) {
        Quad q;
        quad_from(p, q);
        Quad moved;
        if (covers(q, cw, ch)) {
            // a background just fades: scaled or slid, it would show bare canvas round its edges
        } else if (plan.unmatched == SC_MOVE_SCALE) {
            scaled(q, 1.0 - (1.0 - SCALE_TO) * k, moved);
            std::copy(std::begin(moved), std::end(moved), std::begin(q));
        } else if (plan.unmatched == SC_MOVE_SLIDE) {
            slid(q, k, cw, ch, moved);
            std::copy(std::begin(moved), std::end(moved), std::begin(q));
        }
        out.push_back(draw_of(p, q, p.opacity * alpha));
    };
    auto gliding = [&](size_t j) {
        const sc_move_piece &piece = now[j], &before = old[static_cast<size_t>(piece.paired)];
        Quad a, b, q;
        quad_from(before, a);
        quad_from(piece, b);
        between(a, b, e, q);
        const double alpha = before.opacity + (piece.opacity - before.opacity) * t;
        if (before.source == piece.source && before.part == piece.part) {
            out.push_back(draw_of(piece, q, alpha));
            return;
        }
        // Paired by name: two sources, so the old one hands over to the new one as they move.
        const Shape sh = shape_of(q);
        double uv[4][2];
        Quad qa, qb;
        uvs_of(a, uv);
        quad_of(sh, uv, qa);
        uvs_of(b, uv);
        quad_of(sh, uv, qb);
        out.push_back(draw_of(before, qa, before.opacity * leave));
        out.push_back(draw_of(piece, qb, piece.opacity * arrive));
    };

    // Stacking (see move.py): paired pieces in the old bloom's order for the first half and the new one's after;
    // a lone piece stays just above the paired piece it sat on in its own bloom, what arrives above what leaves.
    std::vector<int> paired;  // new indexes
    for (size_t j = 0; j < now.size(); ++j)
        if (now[j].paired >= 0) paired.push_back(static_cast<int>(j));
    if (t < 0.5)
        std::stable_sort(paired.begin(), paired.end(), [&](int x, int y) { return now[x].paired < now[y].paired; });
    // (anchor: the paired new index or -1, 0 old / 1 new) -> lone pieces drawn right after it
    std::map<std::pair<int, int>, std::vector<int>> after;
    int anchor = -1;
    for (size_t k = 0; k < old.size(); ++k) {
        if (new_of_old[k] >= 0) anchor = new_of_old[k];
        else after[{anchor, 0}].push_back(static_cast<int>(k));
    }
    anchor = -1;
    for (size_t k = 0; k < now.size(); ++k) {
        if (now[k].paired >= 0) anchor = static_cast<int>(k);
        else after[{anchor, 1}].push_back(static_cast<int>(k));
    }
    auto lone = [&](int at) {
        auto found = after.find({at, 0});
        if (found != after.end())
            for (int k : found->second) coming_or_going(old[static_cast<size_t>(k)], move, leave);
        found = after.find({at, 1});
        if (found != after.end())
            for (int k : found->second) coming_or_going(now[static_cast<size_t>(k)], 1.0 - move, arrive);
    };
    lone(-1);
    for (int j : paired) {
        gliding(static_cast<size_t>(j));
        lone(j);
    }
    out.erase(std::remove_if(out.begin(), out.end(), [](const MoveDraw &d) { return !(d.opacity > 0.0); }),
              out.end());
}

}  // namespace sc
