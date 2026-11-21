// The person finder behind the Background filter (remove, blur or replace the room behind a camera): MediaPipe's
// selfie segmentation model run as compute shaders on the engine's own device (shader_segmenter.h). No AI library,
// nothing on the processor: the picture is drawn small on the card, the model runs there, and the compositor reads
// the mask it leaves. Masks are made at most 30 times a second per picture (15 when the card is slow), and not at
// all on a card without compute shaders (Direct3D feature level 11) or one too slow for it: the picture is then
// drawn untouched and sc_background_info says why.
#pragma once

#include "internal.h"

#include <functional>

namespace sc {

struct SegmentViews {
    ID3D11ShaderResourceView *mask = nullptr;  // R32F, 1 = person; the model's size
    ID3D11ShaderResourceView *room = nullptr;  // the room blurred without the person (asked for), else null
    float map[4] = {1, 1, 0, 0};               // the picture's u, v into the mask's: uv * map.xy + map.zw
};

// Draws the picture into `target` (width x height, cleared to see-through), its u, v 0..1 landing on map.zw ..
// map.zw + map.xy of the target: letterboxed, so the model sees it unstretched.
using DrawSmall = std::function<void(ID3D11RenderTargetView *target, int width, int height, const float (&map)[4])>;

// The mask for picture `key` (pic_w x pic_h), made again when one is due. `blur` 0..1 with `room`: also the room
// blurred. False when this card can't (the picture is then drawn as it is). The device's lock must be held; the
// render target and viewport are left unset (the caller sets its own again).
bool segment(sc_device &dev, uint64_t key, int pic_w, int pic_h, bool room, float blur, const DrawSmall &draw,
             SegmentViews &out);

// Called as a scene is drawn: no picture asked for a mask for REST_S, so the finder lets go of all it holds on
// this device (shaders, the model, every picture's textures). The filter taken off left them there for the rest of
// the run (PC 2026-11-21); put back on, the finder is made again in the frame that asks.
void segment_rest(sc_device &dev);

}  // namespace sc
