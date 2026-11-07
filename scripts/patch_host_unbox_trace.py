#!/usr/bin/env python3
"""Add diagnostic-only unbox/image-lifecycle tracing to the host decoder.

This patch changes no control flow, no return values and no driver calls. It
only adds stderr logging with a fixed `GFXDIAG:` prefix so that logcat can be
grepped, and it publishes the opcode of the packet currently being decoded in
a thread-local so that the unbox-miss and device-lost sites can report it.

Anchors are exact and asserted; a mismatch aborts the build.
"""
import sys
from pathlib import Path

if len(sys.argv) != 2:
    sys.exit("usage: patch_host_unbox_trace.py <gfxstream-source>")

ROOT = Path(sys.argv[1])

HEADER_REL = "host/vulkan/gfxdiag_unbox_trace.h"
HEADER = """// Diagnostic-only cross-file state used by the GFXDIAG host tracing patch.
//
// The decoder publishes the opcode of the packet it is currently decoding on
// this thread; boxed-handle and image-lifecycle diagnostics read it. No
// behavior, return value or driver call depends on this file.

#pragma once

#include <cstdint>
#include <cstdio>
#include <cstdlib>

namespace gfxstream {
namespace host {
namespace vk {

inline uint32_t& gfxDiagCurrentOpcodeStorage() {
    static thread_local uint32_t opcode = 0;
    return opcode;
}

inline void gfxDiagSetCurrentOpcode(uint32_t opcode) { gfxDiagCurrentOpcodeStorage() = opcode; }

inline uint32_t gfxDiagCurrentOpcode() { return gfxDiagCurrentOpcodeStorage(); }

// GFXSTREAM_TRACE_IMAGES=1 enables the per-image create/destroy trace. The
// variable is read once per process; the value is fixed before the VM starts.
inline bool gfxDiagTraceImagesEnabled() {
    static const bool enabled = []() {
        const char* value = std::getenv("GFXSTREAM_TRACE_IMAGES");
        return value != nullptr && value[0] == '1' && value[1] == '\\0';
    }();
    return enabled;
}

}  // namespace vk
}  // namespace host
}  // namespace gfxstream
"""


def edit(rel, old, new):
    path = ROOT / rel
    with open(path, "r", encoding="utf-8", newline="") as handle:
        text = handle.read()
    count = text.count(old)
    if count != 1:
        first = old.splitlines()[0] if old.splitlines() else old
        raise SystemExit(f"{rel}: expected exactly one anchor, found {count}: {first}")
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text.replace(old, new))
    print("patched", rel)


header_path = ROOT / HEADER_REL
if header_path.exists():
    raise SystemExit(f"{HEADER_REL}: already exists, refusing to double-apply")
with open(header_path, "w", encoding="utf-8", newline="") as handle:
    handle.write(HEADER)
print("created", HEADER_REL)

# 1. Publish the current packet opcode at the decode() entry. m_lastOpcode is
#    only updated after the packet finished, so it cannot be used from inside
#    an unbox failure.
edit(
    "host/vulkan/vk_decoder.cpp",
    """        uint32_t opcode;
        std::memcpy(&opcode, ptr, sizeof(uint32_t));

        // A generated file, so keep the footprint to these few lines: the logic lives in""",
    """        uint32_t opcode;
        std::memcpy(&opcode, ptr, sizeof(uint32_t));
        gfxDiagSetCurrentOpcode(opcode);

        // A generated file, so keep the footprint to these few lines: the logic lives in""",
)

# 2. Log every unbox miss (all handle types) with type, handle, live boxed
#    handle count and current opcode, before the existing type dispatch runs.
edit(
    "host/vulkan/vulkan_boxed_handles.cpp",
    """        BoxedHandleInfo* info = sBoxedHandleManager.get((uint64_t)(uintptr_t)boxed);
        if (info == nullptr) {
            if constexpr (std::is_same_v<VkObjectT, VkCommandBuffer> ||""",
    """        BoxedHandleInfo* info = sBoxedHandleManager.get((uint64_t)(uintptr_t)boxed);
        if (info == nullptr) {
            fprintf(stderr,
                    "GFXDIAG: unbox-miss type=%s boxed=%p handles=%llu opcode=%u\\n",
                    GetTypeStr<VkObjectT>(), (void*)(uintptr_t)boxed,
                    (unsigned long long)sBoxedHandleManager.getHandlesCount(),
                    gfxDiagCurrentOpcode());
            fflush(stderr);
            if constexpr (std::is_same_v<VkObjectT, VkCommandBuffer> ||""",
)

# 3. Trace the raw -> boxed transition on successful image creation, gated by
#    GFXSTREAM_TRACE_IMAGES=1.
edit(
    "host/vulkan/vk_decoder_global_state.cpp",
    """        if (boxImage) {
            *pImage = new_boxed_non_dispatchable_VkImage(*pImage);
            imageInfo.boxed = *pImage;
        }
        return createRes;""",
    """        if (boxImage) {
            const VkImage rawImage = *pImage;
            *pImage = new_boxed_non_dispatchable_VkImage(*pImage);
            imageInfo.boxed = *pImage;
            if (gfxDiagTraceImagesEnabled()) {
                fprintf(stderr,
                        "GFXDIAG: image-create raw=%p boxed=0x%llx handles=%llu opcode=%u\\n",
                        (void*)rawImage, (unsigned long long)(uintptr_t)*pImage,
                        (unsigned long long)sBoxedHandleManager.getHandlesCount(),
                        gfxDiagCurrentOpcode());
                fflush(stderr);
            }
        }
        return createRes;""",
)

# 4. Trace image destroy immediately before the boxed handle is deleted.
edit(
    "host/vulkan/vk_decoder.cpp",
    """                delete_VkImage(boxed_image_preserve);
                vkReadStream->clearPool();""",
    """                if (gfxDiagTraceImagesEnabled()) {
                    fprintf(stderr,
                            "GFXDIAG: image-destroy boxed=0x%llx unboxed=%p handles=%llu "
                            "opcode=%u\\n",
                            (unsigned long long)(uintptr_t)boxed_image_preserve, (void*)image,
                            (unsigned long long)sBoxedHandleManager.getHandlesCount(),
                            gfxDiagCurrentOpcode());
                    fflush(stderr);
                }
                delete_VkImage(boxed_image_preserve);
                vkReadStream->clearPool();""",
)

# 5. Report the last decoded packet opcode when the driver reports device
#    lost. The existing fatal is preserved.
edit(
    "host/vulkan/vk_decoder_global_state.cpp",
    """    void on_DeviceLost() {
        m_vkEmulation->getDeviceLostHelper().onDeviceLost();""",
    """    void on_DeviceLost() {
        fprintf(stderr, "GFXDIAG: device-lost opcode=%u\\n", gfxDiagCurrentOpcode());
        fflush(stderr);
        m_vkEmulation->getDeviceLostHelper().onDeviceLost();""",
)

# Includes last so the anchors above stay simple and each include edit is
# independently asserted.
edit(
    "host/vulkan/vk_decoder.cpp",
    """#include "vulkan_boxed_handles.h"
""",
    """#include "vulkan_boxed_handles.h"
#include "gfxdiag_unbox_trace.h"
""",
)

edit(
    "host/vulkan/vulkan_boxed_handles.cpp",
    """#include "vulkan_boxed_handles.h"
""",
    """#include "vulkan_boxed_handles.h"
#include "gfxdiag_unbox_trace.h"
""",
)

edit(
    "host/vulkan/vk_decoder_global_state.cpp",
    """#include "vk_decoder_global_state.h"
""",
    """#include "vk_decoder_global_state.h"
#include "gfxdiag_unbox_trace.h"
""",
)

print("host unbox trace patch applied")
