// The media player as the engine's compositor reaches it (media.cpp): pictures go from the decoder thread to
// the frame thread without passing Python.
#pragma once

#include "base.h"

#include <cstdint>
#include <memory>

namespace sc {

struct MediaDue {
    int what = SC_MEDIA_NO_PICTURE;     // SC_MEDIA_NO_PICTURE, SC_MEDIA_PICTURE or SC_MEDIA_BLANK
    sc_upload how{};                    // SC_MEDIA_PICTURE: how to read `data`
    const uint8_t *data = nullptr;
    std::shared_ptr<const void> keep;   // holds `data` for as long as the caller needs it
};

// The player behind a handle, kept alive for as long as the pointer is held (the handle may be destroyed).
std::shared_ptr<void> media_hold(sc_media *media);
// The picture due now on the file's clock, if it isn't the one `*seen` names. Never waits for the decoder.
void media_due(const std::shared_ptr<void> &player, uint64_t *seen, MediaDue *out);

}  // namespace sc
