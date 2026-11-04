#!/usr/bin/env python3
"""Runtime A/B switch for the explicit ColorBuffer AHardwareBuffer_allocate path.

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
GFXSTREAM_DISABLE_AHB_ALLOC, when present in the environment (any value), makes
the explicit ColorBuffer allocation log and return nullptr before
AHardwareBuffer_allocate():

    allocAhb() in host/vulkan/vk_common_operations.cpp.

The caller already handles nullptr: allocExternalMemory() logs "Falling back to
non-exportable allocation" and breaks to the ordinary allocation path.

The switch is installed in exactly one place, and only on the explicit
allocation path. allocateAhbFirstMemory() in vk_decoder_global_state.cpp (the
startup/first-allocation path added by patch_ahb_first_hostvisible.py) is left
byte-identical: it is not an explicit AHardwareBuffer_allocate request and
toggling it would change startup behavior instead of testing one call site.

The lookup is lazy. It is a function-local

    static const bool disableAhbAlloc = getenv("GFXSTREAM_DISABLE_AHB_ALLOC") != nullptr;

so it is evaluated on the first allocation call, never during library load (no
static initializer calls getenv), and the environment is read once per process.
With the variable unset the only difference against the pinned source is that
one predicted-not-taken branch; the allocation code, its error handling and the
fallback are byte-identical. With it set the call is skipped, the log line below
is emitted once per allocation, and nullptr drives the existing fallback:

    GFXSTREAM_AHB_ALLOC_DISABLED: shape=%ux%u format=%u usage=0x%llx

For allocAhb() the fields are the VkImage extent, VkFormat and VkImageUsageFlags.

The script is idempotent: a second run recognizes the BEGIN/END markers and
verifies the guarded block is intact instead of patching again. Every
replacement requires exactly one occurrence of the source text. All anchors are
checked before any file is written, so a rejected patch leaves the tree
untouched.
"""
import argparse
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("root", type=Path, help="Pinned gfxstream source checkout")
root = parser.parse_args().root.resolve()

ops = "host/vulkan/vk_common_operations.cpp"
decoder = "host/vulkan/vk_decoder_global_state.cpp"

ops_path = root / ops
if not ops_path.is_file():
    raise SystemExit(f"{ops}: file not found under {root}")

before = ops_path.read_text(encoding="utf-8")
text = before

INCLUDE = "#include <cstdlib>\n"
BEGIN = "    // AHB-ALLOC-KILLSWITCH-1-BEGIN\n"
END = "    // AHB-ALLOC-KILLSWITCH-1-END\n"
GUARD = (
    BEGIN
    + """    // Runtime A/B switch for the explicit ColorBuffer AHardwareBuffer_allocate path only.
    // A function-local static keeps the lookup lazy: it runs on the first allocation, never
    // during library load, and the environment is read once per process. With
    // GFXSTREAM_DISABLE_AHB_ALLOC unset the allocation below is unchanged; with it set (any
    // value) the call is skipped and the caller takes its existing non-exportable fallback.
    static const bool disableAhbAlloc = getenv("GFXSTREAM_DISABLE_AHB_ALLOC") != nullptr;
    if (disableAhbAlloc) {
        GFXSTREAM_WARNING(
            "GFXSTREAM_AHB_ALLOC_DISABLED: shape=%ux%u format=%u usage=0x%llx",
            (unsigned)imageCreateInfo->extent.width, (unsigned)imageCreateInfo->extent.height,
            (unsigned)imageCreateInfo->format, (unsigned long long)imageCreateInfo->usage);
        return nullptr;
    }
"""
    + END
)

# ---------------------------------------------------------------------------
# getenv() is not declared by any header vk_common_operations.cpp includes.
# ---------------------------------------------------------------------------
if INCLUDE in text:
    print("already patched", ops, "(cstdlib)")
else:
    anchor = "#include <algorithm>\n"
    if text.count(anchor) != 1:
        raise SystemExit(
            f"{ops}: expected one <algorithm> include, found {text.count(anchor)}")
    text = text.replace(anchor, anchor + INCLUDE)
    print("patched", ops, "(cstdlib)")

# ---------------------------------------------------------------------------
# Exactly one kill switch, in allocAhb() only.
# ---------------------------------------------------------------------------
anchor = "static AHardwareBuffer* allocAhb(const VkImageCreateInfo* imageCreateInfo) {\n"
if text.count(anchor) != 1:
    raise SystemExit(f"{ops}: expected one allocAhb() anchor, found {text.count(anchor)}")

if BEGIN in text:
    if text.count(BEGIN) != 1 or text.count(END) != 1:
        raise SystemExit(f"{ops}: kill switch markers are not paired")
    if text.count(GUARD) != 1:
        raise SystemExit(f"{ops}: kill switch marker present but the guarded block is not intact")
    print("already patched", ops, "(AHB-ALLOC-KILLSWITCH-1)")
else:
    text = text.replace(anchor, anchor + GUARD)
    print("patched", ops, "(AHB-ALLOC-KILLSWITCH-1)")

# The AHB-first path is intentionally untouched. An old two-site tree would make the
# A/B ambiguous, so refuse it instead of guessing.
decoder_path = root / decoder
if decoder_path.is_file():
    decoder_text = decoder_path.read_text(encoding="utf-8")
    if "AHB-ALLOC-KILLSWITCH-2" in decoder_text:
        raise SystemExit(
            f"{decoder}: old AHB-FIRST kill switch (AHB-ALLOC-KILLSWITCH-2) is still present; "
            "start from a clean pinned tree so only the explicit allocAhb() path is switched")

if text != before:
    ops_path.write_text(text, encoding="utf-8")

print("AHB allocation runtime kill switch installed (explicit allocAhb() path only)")
