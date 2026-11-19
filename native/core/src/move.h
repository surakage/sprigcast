// The Move transition's arithmetic (render/move.py, line for line): what is in both blooms glides from where it
// was to where it goes. Python lays both blooms out as flat lists of pieces and pairs them once, when the change
// starts; every frame the engine works out the quads here and draws them straight onto the canvas (no extra
// pictures and no mixing pass). Plain numbers only, no Direct3D, so it is checked against Python on its own.
#pragma once

#include "sprigcore.h"

#include <cstdint>
#include <vector>

namespace sc {

struct MoveCorner {
    double x, y, u, v;
};

struct MoveDraw {
    uint64_t source = 0;
    uint64_t part = 0;
    int effects = -1;
    MoveCorner quad[4] = {};  // top-left, top-right, bottom-left, bottom-right
    double opacity = 1.0;
};

// A Move as the engine keeps it: copied from sc_move once, when it starts.
struct MovePlan {
    std::vector<sc_move_piece> old_pieces, new_pieces;
    std::vector<sc_effects> effects;
    int easing = SC_MOVE_SMOOTH;
    int unmatched = SC_MOVE_FADE;
};

// Checks and copies a caller's sc_move. False (and `why`) when it doesn't hold together.
bool move_plan(const sc_move &move, MovePlan &out, const char *&why);

// render/move.py ease(): where a change is at time t (0 .. 1), by SC_MOVE_* easing.
double move_ease(int easing, double t);

// render/move.py frame(): what to draw `progress` (0 .. 1) of the way through, bottom first, on a canvas cw x ch.
void move_frame(const MovePlan &plan, double progress, double cw, double ch, std::vector<MoveDraw> &out);

}  // namespace sc
