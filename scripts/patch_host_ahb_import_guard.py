#!/usr/bin/env python3
"""P2 host patch: refuse an AHB import whose memoryTypeBits do not intersect the image's.

Single-site, byte-exact edit of VkEmulation::allocExternalMemory()'s AndroidAHB
branch in host/vulkan/vk_common_operations.cpp.

Before: when the intersection is empty the host forces the AHB's memoryTypeBits
        and binds the image to a memory type its own requirements exclude.
After:  the AHB is released and the allocation follows the existing
        non-exportable fallback (same as the AHB property query failure and the
        AHB-SIZE-MISMATCH guard). A GFXDIAG line records both bit masks, the
        ColorBuffer handle, the VkImage handle, extent, format and sizes.

Any anchor mismatch, duplicate anchor, or already-patched tree aborts the build.
"""

import sys
from pathlib import Path

OLD = b'''                // Find a memory type that satisfies both the image and AHB requirements
                uint32_t combinedBits = ahbProps.memoryTypeBits &
                                        cbInfoPtr->imageMemReqs.memoryTypeBits;
                if (!combinedBits) {
                    // Mali may report zero/disjoint image requirements before AHB import.
                    // Use the imported AHB's authoritative memory types and allocation size.
                    GFXSTREAM_WARNING(
                        "No common memory type for AHB (0x%x) and image (0x%x). "
                        "Using AHB memory type bits for import.",
                        ahbProps.memoryTypeBits, cbInfoPtr->imageMemReqs.memoryTypeBits);
                    combinedBits = ahbProps.memoryTypeBits;
                }
'''

NEW = b'''                // Find a memory type that satisfies both the image and AHB requirements
                uint32_t combinedBits = ahbProps.memoryTypeBits &
                                        cbInfoPtr->imageMemReqs.memoryTypeBits;
                if (!combinedBits) {
                    // P2: never bind an image to an AHB whose memory types do not
                    // intersect the image requirements. Mali accepts such a bind and
                    // faults on the first GPU access; refuse the import and use the
                    // same non-exportable fallback as the AHB query/size failures.
                    GFXSTREAM_WARNING(
                        "No common memory type for AHB (0x%x) and image (0x%x). "
                        "Falling back to non-exportable allocation.",
                        ahbProps.memoryTypeBits, cbInfoPtr->imageMemReqs.memoryTypeBits);
                    GFXSTREAM_WARNING(
                        "GFXDIAG: reject AHB import ahb=%p cb=%u image=%p %ux%u fmt=%d "
                        "ahbBits=0x%x imageBits=0x%x ahbSize=%llu imageSize=%llu",
                        (void*)ahb, cbInfoPtr->handle, (void*)cbInfoPtr->image, cbInfoPtr->width,
                        cbInfoPtr->height, (int)cbInfoPtr->imageCreateInfoShallow.format,
                        ahbProps.memoryTypeBits, cbInfoPtr->imageMemReqs.memoryTypeBits,
                        (unsigned long long)ahbProps.allocationSize,
                        (unsigned long long)cbInfoPtr->imageMemReqs.size);
                    AHardwareBuffer_release(ahb);
                    break;
                }
'''

TARGET = Path("host/vulkan/vk_common_operations.cpp")

OLD_TAIL = b"Using AHB memory type bits for import."
NEW_MARKER = b"GFXDIAG: reject AHB import"


def fail(message):
    print(f"patch_host_ahb_import_guard: FAIL: {message}", file=sys.stderr)
    raise SystemExit(1)


def main():
    if len(sys.argv) != 2:
        fail("usage: patch_host_ahb_import_guard.py <source-root>")

    target = Path(sys.argv[1]) / TARGET
    if not target.is_file():
        fail(f"missing {target}")

    data = target.read_bytes()

    if data.count(NEW_MARKER) != 0:
        fail(f"{target} already carries '{NEW_MARKER.decode()}'; refusing to patch twice")
    if data.count(OLD) != 1:
        fail(f"anchor count is {data.count(OLD)}, expected exactly 1 in {target}")
    if data.count(OLD_TAIL) != 1:
        fail(f"'{OLD_TAIL.decode()}' count is {data.count(OLD_TAIL)}, expected exactly 1")

    patched = data.replace(OLD, NEW, 1)

    checks = [
        (patched.count(NEW_MARKER), 1, f"'{NEW_MARKER.decode()}' present"),
        (patched.count(b"No common memory type for AHB"), 1,
         "'No common memory type for AHB' kept exactly once"),
        (patched.count(b"Falling back to non-exportable allocation."), 2,
         "non-exportable fallback string occurrences (AHB size guard + P2)"),
        (patched.count(OLD_TAIL), 0, "forced-AHB-memory-type tail removed"),
        (patched.count(b"combinedBits = ahbProps.memoryTypeBits;"), 0,
         "no forced AHB memoryTypeBits assignment left"),
    ]
    for actual, expected, description in checks:
        if actual != expected:
            fail(f"post-check {description}: count {actual}, expected {expected}")

    if patched == data:
        fail("replacement produced no change")

    target.write_bytes(patched)

    line = patched[: patched.index(NEW_MARKER)].count(b"\n") + 1
    print(f"patch_host_ahb_import_guard: patched {target} at line {line} "
          f"(+{len(NEW) - len(OLD)} bytes)")


if __name__ == "__main__":
    main()
