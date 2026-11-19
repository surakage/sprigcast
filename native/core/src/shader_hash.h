// Which shader source a piece of baked bytecode was made from (tools/shader_bake.cpp, sprigcore.cpp compile()):
// FNV-1a over the source text, so a shader changed since the bake is compiled, never drawn from old bytecode.
#pragma once

#include <cstddef>
#include <cstdint>

namespace sc {

struct BakedShader {
    uint64_t hash;
    size_t size;
    const char *entry;
    const char *profile;
    const unsigned char *code;
    size_t code_size;
};

inline uint64_t shader_hash(const char *text, size_t size) {
    uint64_t h = 1469598103934665603ull;
    for (size_t i = 0; i < size; ++i) {
        h ^= static_cast<unsigned char>(text[i]);
        h *= 1099511628211ull;
    }
    return h;
}

}  // namespace sc
