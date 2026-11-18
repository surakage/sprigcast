// What the engine's frame thread needs of a writer (writer.cpp) to hand it frames itself (an output linked to it,
// sc_engine_output_link): a share of the writer's insides that outlives sc_writer_destroy, so a link left behind
// is never a dangling pointer (a writer that is finished or given up just refuses what it is handed).
#pragma once

#include "sprigcore.h"

#include <cstdint>
#include <memory>

namespace sc {

std::shared_ptr<void> writer_share(sc_writer *w);
// A picture of exactly the writer's size (NV12, rows `width` bytes) for frames first .. last: SC_OK, or one of
// SC_WRITER_DROPPED / SC_WRITER_REFUSED / SC_WRITER_LATE, or SC_ERR_ARGUMENT for a picture of another size.
// May throw (out of memory): the caller is inside a guard.
int writer_feed(const std::shared_ptr<void> &shared, const uint8_t *nv12, int width, int height, int64_t first,
                int64_t last);
int64_t writer_last(const std::shared_ptr<void> &shared);  // the newest frame number taken (-1: none)
bool writer_size(const std::shared_ptr<void> &shared, int *width, int *height);

}  // namespace sc
