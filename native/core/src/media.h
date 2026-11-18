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
    const uint8_t *data = nullptr;      // null when the picture is on the card (`texture`)
    void *texture = nullptr;            // ID3D11Texture2D on the engine's device (media_card): NV12, `slice`
    unsigned slice = 0;
    std::shared_ptr<const void> keep;   // holds `data` for as long as the caller needs it
};

// The player behind a handle, kept alive for as long as the pointer is held (the handle may be destroyed).
std::shared_ptr<void> media_hold(sc_media *media);
// The picture due now on the file's clock, if it isn't the one `*seen` names. Never waits for the decoder.
void media_due(const std::shared_ptr<void> &player, uint64_t *seen, MediaDue *out);
// The engine's Direct3D device (ID3D11Device, or null when it has none): videos asked to decode on the card
// decode on it from their next opening, and their pictures stay there for media_due. Each new device (a rebuild)
// is given again; pictures decoded on an older one are copied to memory.
void media_card(void *device);
// That device is going away: forgotten if it is the one given.
void media_card_gone(void *device);
// The engine couldn't use a picture on the card: pictures come in memory until the next media_card.
void media_card_refused();

}  // namespace sc
