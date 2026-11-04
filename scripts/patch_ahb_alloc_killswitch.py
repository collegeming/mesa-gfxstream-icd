#!/usr/bin/env python3
"""Runtime kill switch for every host-side AHardwareBuffer_allocate call.

Why
---
The crash under investigation is a SIGSEGV in libutils RefBase::incStrong with
fault 0x8 and x0=0, reached from libnativewindow AHardwareBuffer_acquire. The
working hypothesis is that vendor gralloc returns a null GraphicBuffer inside
AHardwareBuffer_allocate(), so the crash happens before allocAhb() can return
and no C++ branch on the result can observe it. Confirming or rejecting that
needs one binary that can be run with and without the allocation, because a
build-time change would also change everything else in the .so.

What
----
GFXSTREAM_DISABLE_AHB_ALLOC, when present in the environment (any value),
makes every AHardwareBuffer_allocate() entry point log and return nullptr
before the call:

1. allocAhb() in host/vulkan/vk_common_operations.cpp (ColorBuffer images);
2. allocateAhbFirstMemory() in host/vulkan/vk_decoder_global_state.cpp, the
   host-visible AHB-first memory added by patch_ahb_first_hostvisible.py.

Both callers already handle nullptr. allocExternalMemory() logs
"Falling back to non-exportable allocation" and breaks to the ordinary
allocation path; the AHB-first branch releases nothing, logs
AHB-FIRST-FALLBACK and keeps the private-memfd path. Unset keeps today's
behavior, so the same .so can be A/B'd on the device.

Log line, once per call:

    GFXSTREAM_AHB_ALLOC_DISABLED: shape=%ux%u format=%u usage=0x%llx

For allocAhb() the fields are the VkImage extent, VkFormat and VkImageUsageFlags.
For allocateAhbFirstMemory() they are the AHardwareBuffer_Desc it would have
used: width=size, height=1, AHARDWAREBUFFER_FORMAT_BLOB, and the CPU/GPU usage
bits.

The script is idempotent: a second run recognizes the AHB-ALLOC-KILLSWITCH-n
markers and verifies the guarded block is intact instead of patching again.
Every replacement requires exactly one occurrence of the source text. All
anchors are checked before any file is written, so a rejected patch leaves the
tree untouched.
"""
import argparse
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("root", type=Path, help="Pinned gfxstream source checkout")
root = parser.parse_args().root.resolve()

ops = "host/vulkan/vk_common_operations.cpp"
decoder = "host/vulkan/vk_decoder_global_state.cpp"

originals = {rel: (root / rel).read_text(encoding="utf-8") for rel in (ops, decoder)}
texts = dict(originals)

# ---------------------------------------------------------------------------
# getenv() is not declared by any header vk_common_operations.cpp includes.
# ---------------------------------------------------------------------------
if "#include <cstdlib>" in texts[ops]:
    print("already patched", ops, "(cstdlib)")
else:
    anchor = "#include <algorithm>\n"
    if texts[ops].count(anchor) != 1:
        raise SystemExit(
            f"{ops}: expected one <algorithm> include, found {texts[ops].count(anchor)}")
    texts[ops] = texts[ops].replace(anchor, anchor + "#include <cstdlib>\n")
    print("patched", ops, "(cstdlib)")

# ---------------------------------------------------------------------------
# 1. ColorBuffer images: return before AHardwareBuffer_allocate is reached.
# ---------------------------------------------------------------------------
# 2. Host-visible AHB-first memory: same switch, same nullptr contract.
# ---------------------------------------------------------------------------
edits = [
    (
        ops,
        """static AHardwareBuffer* allocAhb(const VkImageCreateInfo* imageCreateInfo) {
""",
        """static AHardwareBuffer* allocAhb(const VkImageCreateInfo* imageCreateInfo) {
    // AHB-ALLOC-KILLSWITCH-1: runtime switch for the ColorBuffer AHB allocation. With
    // GFXSTREAM_DISABLE_AHB_ALLOC present (any value) this returns before
    // AHardwareBuffer_allocate(), so one .so can answer whether the RefBase::incStrong crash
    // happens inside that call; unset keeps the shipped behavior. The caller breaks to its
    // existing non-exportable allocation fallback on nullptr. Logged on every call.
    if (getenv("GFXSTREAM_DISABLE_AHB_ALLOC") != nullptr) {
        GFXSTREAM_WARNING(
            "GFXSTREAM_AHB_ALLOC_DISABLED: shape=%ux%u format=%u usage=0x%llx",
            (unsigned)imageCreateInfo->extent.width, (unsigned)imageCreateInfo->extent.height,
            (unsigned)imageCreateInfo->format, (unsigned long long)imageCreateInfo->usage);
        return nullptr;
    }
""",
        "AHB-ALLOC-KILLSWITCH-1",
    ),
    (
        decoder,
        """    static AHardwareBuffer* allocateAhbFirstMemory(VkDeviceSize size) {
        if (!size || size > 0x7fffffffull) return nullptr;
""",
        """    static AHardwareBuffer* allocateAhbFirstMemory(VkDeviceSize size) {
        if (!size || size > 0x7fffffffull) return nullptr;
        // AHB-ALLOC-KILLSWITCH-2: the same runtime switch for the host-visible AHB-first memory
        // added by patch_ahb_first_hostvisible.py. A nullptr here keeps the existing
        // private-memfd path, which logs AHB-FIRST-FALLBACK and continues without the AHB.
        if (getenv("GFXSTREAM_DISABLE_AHB_ALLOC") != nullptr) {
            GFXSTREAM_WARNING(
                "GFXSTREAM_AHB_ALLOC_DISABLED: shape=%ux%u format=%u usage=0x%llx", (unsigned)size,
                1u, (unsigned)AHARDWAREBUFFER_FORMAT_BLOB,
                (unsigned long long)(AHARDWAREBUFFER_USAGE_CPU_READ_OFTEN |
                                     AHARDWAREBUFFER_USAGE_CPU_WRITE_OFTEN |
                                     AHARDWAREBUFFER_USAGE_GPU_DATA_BUFFER));
            return nullptr;
        }
""",
        "AHB-ALLOC-KILLSWITCH-2",
    ),
]

for rel, old, new, marker in edits:
    text = texts[rel]
    if marker in text:
        if text.count(new) != 1:
            raise SystemExit(f"{rel}: {marker} present but the guarded block is not intact")
        print("already patched", rel, f"({marker})")
        continue
    if text.count(old) != 1:
        raise SystemExit(f"{rel}: expected one {marker} anchor, found {text.count(old)}")
    texts[rel] = text.replace(old, new)
    print("patched", rel, f"({marker})")

for rel, text in texts.items():
    if text != originals[rel]:
        (root / rel).write_text(text, encoding="utf-8")

print("AHB allocation runtime kill switch installed")
