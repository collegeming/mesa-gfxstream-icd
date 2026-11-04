#!/usr/bin/env python3
"""P0: derive the staging buffer's coherence from HOST_COHERENT, not HOST_VISIBLE.

`VkEmulation::StagingBuffer::create()` sets

    // Non-host coherent memory would require manual flush/invalidate
    mIsHostCoherent = (memType.propertyFlags & VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT);

Both readback (`readColorBufferToBytesLocked`, `readColorBufferPixelsScaled`,
`readBufferToBytes`) and upload (`updateColorBufferFromBytesLocked`) guard
`vkInvalidateMappedMemoryRanges()` / `vkFlushMappedMemoryRanges()` with
`if (!mStaging.mIsHostCoherent)`.  Every staging candidate is HOST_VISIBLE, so that
guard is always false and the cache maintenance never runs.

On this Mali the preferred staging type is type 1
(DEVICE_LOCAL|HOST_VISIBLE|HOST_CACHED, flags 0xb) and it is *not* HOST_COHERENT
(coherentTypeMask=0x1f9 does not include bit 1; see
mali-heap-query-device-41285849957.log).  The selector explicitly prefers
HOST_CACHED, so the staging buffer lands exactly there.  The result is stale
64-byte cache lines in every readback; 64 B is 16 pixels of B8G8R8A8, which is the
observed 16x1 dash quantum.

This script changes only the derivation.  It deliberately leaves
`getStagingMemoryTypeIndex()` alone: fixing the guard must work for whichever
type the selector picks, and silently preferring a different memory type would
hide the defect instead of repairing it.  When the chosen type is truly
HOST_COHERENT the maintenance is skipped; otherwise both directions run:

  * upload: CPU writes staging, then vkFlushMappedMemoryRanges before the GPU
    copy reads it;
  * readback: fence wait, then vkInvalidateMappedMemoryRanges before the CPU
    memcpy reads it.

Usage:
    python -m py_compile scripts/patch_render_staging_coherent.py
    python scripts/patch_render_staging_coherent.py <gfxstream-root> [--check]

Exit code 1 with --check means the tree still needs the change.  Idempotent: the
STAGING-COHERENT-P0 marker identifies this patch, and a second run verifies the
guarded block is intact instead of editing again.
"""
import argparse
import sys
from pathlib import Path

MARKER = "STAGING-COHERENT-P0"

OLD = """    // Non-host coherent memory would require manual flush/invalidate
    mIsHostCoherent = (memType.propertyFlags & VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT);
"""

NEW = """    // STAGING-COHERENT-P0: HOST_VISIBLE only says the CPU may map the range; it says
    // nothing about whether CPU accesses observe GPU writes or vice versa.  Deriving
    // this flag from HOST_VISIBLE makes every invalidate/flush guard below unreachable
    // on a driver whose preferred staging type is HOST_VISIBLE without HOST_COHERENT --
    // on Mali that is type 1 (DEVICE_LOCAL|HOST_VISIBLE|HOST_CACHED, flags 0xb).
    // Readback then returns stale CPU cache lines at 64-byte granularity, exactly 16
    // pixels of a 1280-wide B8G8R8A8 row.  Only HOST_COHERENT permits skipping the
    // CPU cache maintenance; a non-coherent staging type must run it in both
    // directions (flush before GPU reads an upload, invalidate before CPU reads a
    // readback).
    mIsHostCoherent =
        (memType.propertyFlags & VK_MEMORY_PROPERTY_HOST_COHERENT_BIT) != 0;
"""


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

    if MARKER in text:
        if text.count(NEW) != 1:
            print(f"{path}: {MARKER} is present but the patched block is not intact",
                  file=sys.stderr)
            return 2
        print(f"already patched: {path}")
        return 0
    if NEW in text:
        print(f"already patched: {path}")
        return 0

    occurrences = text.count(OLD)
    if occurrences != 1:
        print(f"anchor not found exactly once in {path} (found {occurrences}); "
              "file has a different shape than the pinned baseline",
              file=sys.stderr)
        return 2

    if args.check:
        print(f"needs patch: {path}")
        return 1

    path.write_text(text.replace(OLD, NEW, 1), encoding="utf-8")
    print(f"patched: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
