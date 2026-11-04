#!/usr/bin/env python3
"""P2 (diagnostic): make ColorBuffer readbacks quiesced before they are compared.

`readColorBufferToBytesLocked()` submits its copy on VkEmulation's queue.  The CIB dump
thread (`GFXSTREAM_CIB_DUMP_REQUEST`) calls it from a separate thread at an arbitrary
instant, while the guest keeps submitting frames on the same device.  Two dumps taken
milliseconds apart can therefore describe different points of the same frame, which is
exactly the "complete frame / sparse frame" alternation in the evidence set.

With `GFXSTREAM_CIB_DUMP_QUIESCE=1` the readback first waits for the device to go idle.
That makes the dump a ground-truth sample of what Mali actually wrote:

  * quiesced dump is complete  -> the holes were a read/render race (or staging
    coherency), not the rendered image;
  * quiesced dump still holey  -> the holes are in the image/memory written by Mali.

Usage:
    python scripts/patch_render_readback_quiesce.py <gfxstream-root> [--check]
"""
import argparse
import sys
from pathlib import Path

OLD = """    // Record our synchronization commands.
    const VkCommandBufferBeginInfo beginInfo = {
"""

NEW = """    // Diagnostic: dumps are requested from a separate thread at an arbitrary instant, and
    // the guest keeps submitting frames on the same device.  Waiting for the device to go
    // idle makes consecutive dumps comparable: if the hole pattern survives a quiesced
    // read it lives in the image, otherwise the earlier read raced the frame.
    if (gfxstream::base::getEnvironmentVariable("GFXSTREAM_CIB_DUMP_QUIESCE") == "1") {
        gfxstream::base::AutoLock queueLock(*mQueueLock);
        VK_CHECK(vk->vkDeviceWaitIdle(mDevice));
    }

    // Record our synchronization commands.
    const VkCommandBufferBeginInfo beginInfo = {
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="gfxstream source root")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    path = args.root.resolve() / "host/vulkan/vk_common_operations.cpp"
    if not path.is_file():
        print(f"missing: {path}", file=sys.stderr)
        return 2

    text = path.read_text(encoding="utf-8")
    if NEW in text:
        print(f"already patched: {path}")
        return 0
    if OLD not in text:
        print("anchor not found; readback shape differs from the pinned baseline",
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
