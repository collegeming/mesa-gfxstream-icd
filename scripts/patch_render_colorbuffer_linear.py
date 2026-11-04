#!/usr/bin/env python3
"""P1: make the ColorBuffer/AHB layout contract explicit instead of binding across it.

Two independent facts from the deployed host (9b93c0c + ahbfirst-v3 patch):

1. `createVkColorBufferLocked()` draws a ColorBuffer with
   `VK_IMAGE_TILING_OPTIMAL` whenever the allocation is not HOST_VISIBLE.  The CIB1
   ColorBuffer is DEVICE_LOCAL, so it is OPTIMAL.  But the guest asks for
   `VK_IMAGE_TILING_LINEAR` and queries the subresource layout (stride 5120); the AHB
   that is exported and re-imported on the guest side is a linear contract.

2. The AHB import reports memoryTypeBits=0x2 while the OPTIMAL image requires 0x1040
   (logcat-t1.log: "No common memory type for AHB (0x2) and image (0x1040)").  The code
   then binds the image to a memory type outside its own memoryTypeBits, which violates
   VUID-vkBindImageMemory-memory-01047; the driver may pick a layout no AHB consumer
   shares.

This script is a diagnostic switch, not a default behaviour change:
  * `GFXSTREAM_COLORBUFFER_LINEAR=1` forces the ColorBuffer image to LINEAR, which is
    the layout the guest/CIB contract and the AHB agree on.
  * `GFXSTREAM_AHB_STRICT=1` refuses the disjoint bind instead of silently continuing.

Usage:
    python scripts/patch_render_colorbuffer_linear.py <gfxstream-root> [--check]
"""
import argparse
import sys
from pathlib import Path

OLD_TILING = """    VkImageTiling tiling = (infoPtr->memoryProperty & VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT)
                               ? VK_IMAGE_TILING_LINEAR
                               : VK_IMAGE_TILING_OPTIMAL;
"""

NEW_TILING = """    VkImageTiling tiling = (infoPtr->memoryProperty & VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT)
                               ? VK_IMAGE_TILING_LINEAR
                               : VK_IMAGE_TILING_OPTIMAL;
    // CIB1 exports this image through an AHardwareBuffer and the guest imports the same
    // handle as a VK_IMAGE_TILING_LINEAR image whose subresource layout it queries.  On
    // Mali the imported AHB advertises memory type bits disjoint from this OPTIMAL
    // image's (0x2 vs 0x1040), so the bind below lands outside
    // VUID-vkBindImageMemory-memory-01047 and the driver is free to use a layout that
    // the AHB consumer does not share.  Opt-in until measured on the device.
    if (gfxstream::base::getEnvironmentVariable("GFXSTREAM_COLORBUFFER_LINEAR") == "1") {
        tiling = VK_IMAGE_TILING_LINEAR;
    }
"""

OLD_DISJOINT = """                if (!combinedBits) {
                    // Mali may report zero/disjoint image requirements before AHB import.
                    // Use the imported AHB's authoritative memory types and allocation size.
                    GFXSTREAM_WARNING(
                        "No common memory type for AHB (0x%x) and image (0x%x). "
                        "Using AHB memory type bits for import.",
                        ahbProps.memoryTypeBits, cbInfoPtr->imageMemReqs.memoryTypeBits);
                    combinedBits = ahbProps.memoryTypeBits;
                }
"""

NEW_DISJOINT = """                if (!combinedBits) {
                    // Mali can report an AHB type set disjoint from this image's requirements
                    // (measured: AHB 0x2, OPTIMAL BGRA8 0x1040).  Binding anyway puts the image
                    // on a memory type outside its memoryTypeBits, which is
                    // VUID-vkBindImageMemory-memory-01047 and leaves the layout undefined for
                    // every other consumer of the AHB.  Fail closed on request.
                    GFXSTREAM_ERROR(
                        "AHB-MEMTYPE-DISJOINT: ahb=0x%x image=0x%x size=%llu tiling=%d; "
                        "set GFXSTREAM_COLORBUFFER_LINEAR=1 to draw the ColorBuffer LINEAR",
                        ahbProps.memoryTypeBits, cbInfoPtr->imageMemReqs.memoryTypeBits,
                        (unsigned long long)ahbProps.allocationSize,
                        (int)cbInfoPtr->imageCreateInfoShallow.tiling);
                    if (gfxstream::base::getEnvironmentVariable("GFXSTREAM_AHB_STRICT") == "1") {
                        AHardwareBuffer_release(ahb);
                        break;
                    }
                    combinedBits = ahbProps.memoryTypeBits;
                }
"""


def patch_one(text: str, old: str, new: str, label: str) -> tuple[str, str]:
    if new in text:
        return text, f"already patched: {label}"
    if old not in text:
        raise SystemExit(f"anchor not found: {label}")
    return text.replace(old, new, 1), f"patched: {label}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="gfxstream source root")
    parser.add_argument("--check", action="store_true",
                        help="report whether the patch is needed; do not write")
    args = parser.parse_args()

    path = args.root.resolve() / "host/vulkan/vk_common_operations.cpp"
    if not path.is_file():
        print(f"missing: {path}", file=sys.stderr)
        return 2

    text = path.read_text(encoding="utf-8")
    changed = []

    if NEW_TILING not in text:
        if OLD_TILING not in text:
            print("anchor not found: tiling", file=sys.stderr)
            return 2
        if not args.check:
            text = text.replace(OLD_TILING, NEW_TILING, 1)
        changed.append("tiling")

    if NEW_DISJOINT not in text:
        if OLD_DISJOINT not in text:
            print("anchor not found: AHB memory type fallback", file=sys.stderr)
            return 2
        if not args.check:
            text = text.replace(OLD_DISJOINT, NEW_DISJOINT, 1)
        changed.append("ahb-memtype")

    if args.check:
        if changed:
            print(f"needs patch: {path} ({', '.join(changed)})")
            return 1
        print(f"already patched: {path}")
        return 0

    if changed:
        path.write_text(text, encoding="utf-8")
        print(f"patched: {path} ({', '.join(changed)})")
    else:
        print(f"already patched: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
