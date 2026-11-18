// The audio visualizer as the engine's compositor reaches it (visual.cpp): its drawings go from its own thread to the
// frame thread without passing Python.
#pragma once

#include "media.h"

#include <memory>

namespace sc {

// The drawings behind a visualizer, kept alive for as long as the pointer is held (the visualizer may be closed).
std::shared_ptr<void> visual_hold(sc_visual *visual);
// The newest drawing, if it isn't the one `*seen` names (out->what = SC_MEDIA_PICTURE, else SC_MEDIA_NO_PICTURE).
void visual_due(const std::shared_ptr<void> &drawings, uint64_t *seen, MediaDue *out);

}  // namespace sc
