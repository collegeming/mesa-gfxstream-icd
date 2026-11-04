#!/usr/bin/env python3
"""Allow the udmabuf-backed host-visible pool path on kernels newer than 6.6.

Why
---
`IsAndroidKernel6_6()` gates `VulkanAllocateHostVisibleAsUdmabuf`, which is the feature that
makes a host-visible allocation a GpuPool sub-allocation imported as a dma-buf:

    SystemBlob/udmabuf branch -> HostVisiblePool::alloc(...) -> poolOffset recorded
    -> UdmabufCreator::handleFromFd(pool memfd, memfdBase + offset, size)
    -> crosvm returns VIRTIO_GPU_MAP_INFO_POOL
    -> guest maps gpu_pool_base + pool_offset

That is ONE object: guest CPU, crosvm and Mali all touch the same pool pages. It sidesteps
identity, flush and copyback at once -- and it is the path the ASG rings already use
successfully on this device (`MAPBLOB: res=18 POOL offset=0x70f000`).

The gate hardcodes `major == 6 && minor == 6`, which was the kernel Android shipped when the
helper was written. The host here runs 6.18 with `/dev/udmabuf` present. The udmabuf UAPI this
code needs has not changed in any way that matters:

  * `struct udmabuf_create { __u32 memfd; __u32 flags; __u64 offset; __u64 size; }` -- the
    `offset`/`size` form (what `handleFromFd` uses to alias a sub-range of the pool memfd) has
    been the ABI since udmabuf gained range support in Linux 5.8; 6.6 and 6.18 are identical
    here.
  * `UDMABUF_CREATE` returns a dma-buf fd on success and -1 with errno on failure; unchanged.
  * the memfd seal requirement (`F_SEAL_SHRINK` present, no `F_SEAL_WRITE`) is enforced by
    `udmabuf_create()` -> `check_memfd_seals()` in drivers/dma-buf/udmabuf.c, also unchanged
    since 5.8. The pool memfd is created by gfxstream/crosvm and is not write-sealed; the code
    already relies on this for the ASG rings on this same kernel.

So the old rule was "the only kernel Android shipped at the time", not an ABI constraint.

What
----
Accept any kernel >= 6.6, or any kernel at all when `GFXSTREAM_UDMABUF_ALLOW_ANY_KERNEL=1` is
set, so a device test can A/B without a rebuild and the default stays conservative for kernels
older than the helper's assumption.

Nothing else changes: the pool allocator, the pool partition envs, the folio policy and the
memfd fallback are untouched. If the ioctl fails for any reason the existing code path already
falls back (`handleFromFd` returns nullopt -> the caller keeps the memfd behaviour).
"""
import argparse
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("root", type=Path, help="Pinned gfxstream source checkout")
root = parser.parse_args().root.resolve()


def edit(rel, old, new):
    path = root / rel
    text = path.read_text(encoding="utf-8")
    if text.count(old) != 1:
        raise SystemExit(f"{rel}: expected one occurrence, found {text.count(old)}")
    path.write_text(text.replace(old, new), encoding="utf-8")
    print("patched", rel)


edit(
    "common/base/UdmabufCreator_linux.cpp",
    """    // udmabuf only needed on 6.6 kernel
    if (major != 6 || minor != 6) {
        return false;
    }

    return true;
}""",
    """    // The gate used to be `major == 6 && minor == 6`, i.e. the kernel Android happened to ship
    // when this helper was written, not an ABI boundary. The udmabuf UAPI used here --
    // `struct udmabuf_create { memfd, flags, offset, size }` plus UDMABUF_CREATE returning a
    // dma-buf fd, and check_memfd_seals() requiring F_SEAL_SHRINK -- has been unchanged since
    // udmabuf gained range support in Linux 5.8. Accept anything from 6.6 on, and let
    // GFXSTREAM_UDMABUF_ALLOW_ANY_KERNEL=1 bypass the version check entirely so a device test
    // can A/B the feature without a rebuild. A kernel whose ioctl is incompatible fails in
    // handleFromFd() and the caller keeps the existing memfd path, so this cannot break
    // allocation -- it can only decline to enable the pool.
    if (gfxstream::base::getEnvironmentVariable("GFXSTREAM_UDMABUF_ALLOW_ANY_KERNEL") == "1") {
        return HasUdmabufDeviceStub();
    }

    if (major < 6 || (major == 6 && minor < 6)) {
        return false;
    }

    return true;
}""",
)

# IsAndroidKernel6_6() is defined before HasUdmabufDevice(), so it cannot call it yet. Move the
# device probe ahead of it instead of adding an extra declaration to a header.
edit(
    "common/base/UdmabufCreator_linux.cpp",
    """bool IsAndroidKernel6_6() {""",
    """bool HasUdmabufDevice() { return access("/dev/udmabuf", F_OK) == 0; }

bool IsAndroidKernel6_6() {""",
)
edit(
    "common/base/UdmabufCreator_linux.cpp",
    """bool HasUdmabufDevice() { return access("/dev/udmabuf", F_OK) == 0; }

UdmabufCreator::UdmabufCreator() {}""",
    """UdmabufCreator::UdmabufCreator() {}""",
)
# Use the real probe, not a stub name.
edit(
    "common/base/UdmabufCreator_linux.cpp",
    "return HasUdmabufDeviceStub();",
    "return HasUdmabufDevice();",
)
# getEnvironmentVariable lives in a base header; include it explicitly rather than relying on a
# transitive include.
edit(
    "common/base/UdmabufCreator_linux.cpp",
    '#include "gfxstream/memory/UdmabufCreator.h"',
    '#include "gfxstream/memory/UdmabufCreator.h"\n\n#include "gfxstream/system/System.h"',
)

print("udmabuf kernel gate relaxed (env-overridable)")
