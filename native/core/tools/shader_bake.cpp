// Compiles the engine's shaders once, when the engine is built, into a header of bytecode (baked_shaders.h) that
// sprigcore.dll carries. Compiling them at run time took up to 3.4 s a shader on the software renderer and on slow
// PCs, and a frame that needed one waited for it (CI start-up check, 2026-11-19).
// Usage: shader_bake <output header>
#include <windows.h>
#include <d3dcompiler.h>

#include <cstdint>
#include <cstdio>
#include <string>

#include "shader_compositor.h"
#include "shader_draw.h"
#include "shader_nv12.h"
#include "shader_repack.h"
#include "shader_transition.h"
#include "shader_hash.h"

namespace {

struct Job {
    const char *name;
    const char *source;
    size_t size;
};

}  // namespace

int main(int argc, char **argv) {
    if (argc != 2) {
        std::fprintf(stderr, "usage: shader_bake <output header>\n");
        return 2;
    }
    const Job jobs[] = {
        {"draw", SHADER, sizeof SHADER - 1},
        {"compositor", COMPOSITOR_HLSL, sizeof COMPOSITOR_HLSL - 1},
        {"nv12", NV12_HLSL, sizeof NV12_HLSL - 1},
        {"repack", REPACK_HLSL, sizeof REPACK_HLSL - 1},
        {"transition", TRANSITION_HLSL, sizeof TRANSITION_HLSL - 1},
    };
    const char *stages[][2] = {{"vs_main", "vs_4_0"}, {"ps_main", "ps_4_0"}};
    std::string arrays, table;
    int n = 0;
    for (const Job &job : jobs) {
        for (const auto &stage : stages) {
            ID3DBlob *code = nullptr, *errors = nullptr;
            HRESULT hr = D3DCompile(job.source, job.size, "sprigcore", nullptr, nullptr, stage[0], stage[1],
                                    D3DCOMPILE_OPTIMIZATION_LEVEL3, 0, &code, &errors);
            if (FAILED(hr)) {
                std::fprintf(stderr, "shader_bake: %s %s failed: %s\n", job.name, stage[0],
                             errors ? static_cast<const char *>(errors->GetBufferPointer()) : "no message");
                return 1;
            }
            const auto *bytes = static_cast<const unsigned char *>(code->GetBufferPointer());
            const size_t count = code->GetBufferSize();
            arrays += "static const unsigned char BAKED_" + std::to_string(n) + "[] = {  // " + job.name + " " +
                      stage[0] + "\n";
            for (size_t i = 0; i < count; ++i) {
                arrays += std::to_string(bytes[i]) + (i + 1 < count ? "," : "");
                if (i % 32 == 31) arrays += "\n";
            }
            arrays += "};\n";
            char row[200];
            std::snprintf(row, sizeof row, "    {0x%016llxull, %zu, \"%s\", \"%s\", BAKED_%d, %zu},\n",
                          static_cast<unsigned long long>(sc::shader_hash(job.source, job.size)), job.size, stage[0],
                          stage[1], n, count);
            table += row;
            code->Release();
            if (errors) errors->Release();
            ++n;
        }
    }
    std::string out = "// Made by tools/shader_bake.cpp when the engine is built. Don't edit.\n#pragma once\n\n" +
                      arrays + "\nstatic const sc::BakedShader BAKED_SHADERS[] = {\n" + table + "};\n";
    FILE *f = nullptr;
    if (fopen_s(&f, argv[1], "wb") != 0 || !f) {
        std::fprintf(stderr, "shader_bake: can't write %s\n", argv[1]);
        return 1;
    }
    std::fwrite(out.data(), 1, out.size(), f);
    std::fclose(f);
    std::printf("shader_bake: %d shaders baked into %s\n", n, argv[1]);
    return 0;
}
