#!/usr/bin/env python3
"""Stop exporting memory that has no AHB backing in AndroidAHB mode.

Why
---
VkEmulation::allocExternalMemory() does not add VkExportMemoryAllocateInfo in
AndroidAHB mode. Only a ColorBuffer is backed by an AHardwareBuffer: allocAhb()
allocates it, the allocation imports it, and the handle is stored in
info->handleInfo. Every other allocation (VkBuffer, SystemBlob, GpuPool) is an
ordinary Vulkan allocation whose driver object has no AHB field.

The AndroidAHB branch of allocExternalMemory() nevertheless fell back to
vkGetMemoryAndroidHardwareBufferANDROID() for those allocations. (Classic
VkBuffers already return earlier through a dma-buf export; the remaining caller
is a ColorBuffer allocation whose AHB import did not happen, e.g. allocAhb()
refused or failed, and any future caller that reaches the arm without an AHB
handle.) Mali's implementation does not check the result:
vkGetMemoryAndroidHardwareBufferANDROID loads the memory object's AHB field
(mem+0x198), which is written only by an AHB import or an AHB export allocation,
and passes it to AHardwareBuffer_acquire() without a null test. With no AHB
backing the field is NULL, and AHardwareBuffer_acquire(NULL) faults at 0x8 in
RefBase::incStrong.

What
----
Only the `else` arm of the AndroidAHB branch changes. When info->handleInfo is
absent, or names a handle type other than STREAM_HANDLE_TYPE_PLATFORM_AHB, the
driver is not asked to export anything:

    validHandle = false;
    exportRes = VK_ERROR_INVALID_EXTERNAL_HANDLE;

plus one rate-limited diagnostic line per process:

    AHB-EXPORT-SKIP no-ahb-backing size=%llu

That is the result the driver is required to return for a memory that was not
allocated with an AHB export chain, so the caller's existing failure path is
unchanged: allocExternalMemory() logs "Failed to get external memory" and returns
false, exactly as it already did when a driver returned an error or a null
handle. The AndroidAHB import fast path (handleInfo present and
STREAM_HANDLE_TYPE_PLATFORM_AHB, set by the ColorBuffer import above) is
byte-identical and still skips the getter.

patch_ahb_null_guard.py runs before this script and leaves its AHB-NULL-GUARD-2
publish wrapper around the getter; replacing the whole arm supersedes it, since
the getter is no longer called at all.

The helper header holds only the once-per-process log gate. It is included at
global scope next to the AHB allocation switch, before the gfxstream namespaces
open, so ::gfxstream::ahb_export_guard resolves from the call site.

The script is idempotent: a second run verifies the header, the include and the
guarded block instead of patching again. Every anchor must occur exactly once and
all checks run before the first write, so a rejected patch leaves the tree
untouched.
"""
import argparse
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("root", type=Path, help="Pinned gfxstream source checkout")
root = parser.parse_args().root.resolve()

ops = "host/vulkan/vk_common_operations.cpp"
header = "host/vulkan/ahb_export_guard.h"

ops_path = root / ops
header_path = root / header

if not ops_path.is_file():
    raise SystemExit(f"{ops}: file not found under {root}")

HEADER = """\
// Rate limit for the AHB export guard in allocExternalMemory().
//
// AndroidAHB mode only gives ColorBuffers an AHardwareBuffer backing (allocated,
// imported and recorded in info->handleInfo). Asking the driver to export any
// other allocation is invalid, and Mali's vkGetMemoryAndroidHardwareBufferANDROID
// does not check: it loads the memory object's AHB field (+0x198), written only by
// an AHB import or an AHB export allocation, and calls AHardwareBuffer_acquire()
// on it without a null test. The caller refuses locally instead.
//
// This header holds only the once-per-process log gate; the decision stays at the
// call site so the existing PLATFORM_AHB fast path is untouched.
#ifndef GFXSTREAM_HOST_VULKAN_AHB_EXPORT_GUARD_H_
#define GFXSTREAM_HOST_VULKAN_AHB_EXPORT_GUARD_H_

#include <atomic>

namespace gfxstream {
namespace ahb_export_guard {

// One AHB-EXPORT-SKIP line per process: the first no-backing allocation identifies
// the path and its size, and a caller retrying the export cannot flood the console.
// The function-local static in an inline function is shared by every caller in the
// process, and one relaxed fetch_add makes the gate single-winner under threads.
inline bool ShouldLogSkip() {
    static std::atomic<unsigned> sLogged{0};
    return sLogged.fetch_add(1, std::memory_order_relaxed) == 0;
}

}  // namespace ahb_export_guard
}  // namespace gfxstream

#endif  // GFXSTREAM_HOST_VULKAN_AHB_EXPORT_GUARD_H_
"""

# The AHB allocation switch is inserted by patch_ahb_alloc_killswitch.py before this
# script runs. Its Android include block is the only one in the file, and it already
# sits at global scope, before the gfxstream namespaces.
INCLUDE_ANCHOR = '#include "ahb_alloc_off_file.h"\n'
INCLUDE_LINE = '#include "ahb_export_guard.h"\n'
INCLUDE_BLOCK = INCLUDE_ANCHOR + INCLUDE_LINE

# This is the fallback as it stands after patch_ahb_null_guard.py (which runs before
# this script): the export getter plus its AHB-NULL-GUARD-2 publish-on-success wrapper.
# Replacing the whole arm supersedes both, because the getter is no longer called at
# all for a memory without AHB backing.
OLD_ELSE = """\
            } else {
                // Fallback: use the old export path if AHB import wasn't used
                VkMemoryGetAndroidHardwareBufferInfoANDROID getAhbInfo = {
                    .sType = VK_STRUCTURE_TYPE_MEMORY_GET_ANDROID_HARDWARE_BUFFER_INFO_ANDROID,
                    .pNext = nullptr,
                    .memory = info->memory,
                };
                AHardwareBuffer* exportHandle = nullptr;
                exportRes =
                    vk->vkGetMemoryAndroidHardwareBufferANDROID(mDevice, &getAhbInfo, &exportHandle);
                validHandle = (VK_SUCCESS == exportRes) && (NULL != exportHandle);
                // AHB-NULL-GUARD-2: only publish a handle that was actually exported. Writing
                // {handle=0, PLATFORM_AHB} here makes the guest import a null AHB and the
                // driver SIGSEGV in AHardwareBuffer_acquire. On failure handleInfo stays unset.
                if (validHandle) {
                    info->handleInfo = ExternalHandleInfo{
                        .handle = reinterpret_cast<ExternalHandleType>(exportHandle),
                        .streamHandleType = STREAM_HANDLE_TYPE_PLATFORM_AHB,
                    };
                } else {
                    GFXSTREAM_ERROR(
                        "AHB-NULL-GUARD: Android hardware buffer export returned no handle "
                        "(result=%d)",
                        (int)exportRes);
                }
            }
"""

NEW_ELSE = """\
            } else {
                // No AHB backing: this memory was not imported from an AHB (that arm is
                // above) and was not allocated with an AHB export chain either, because
                // this function does not add VkExportMemoryAllocateInfo in AndroidAHB
                // mode. Asking the driver to export it is invalid, and Mali does not
                // check: vkGetMemoryAndroidHardwareBufferANDROID() loads the memory
                // object's AHB field (+0x198), written only by an AHB import or an AHB
                // export allocation, and passes it to AHardwareBuffer_acquire()
                // unguarded. Refuse here and report what the driver should have returned.
                if (::gfxstream::ahb_export_guard::ShouldLogSkip()) {
                    GFXSTREAM_WARNING("AHB-EXPORT-SKIP no-ahb-backing size=%llu",
                                      static_cast<unsigned long long>(info->size));
                }
                validHandle = false;
                exportRes = VK_ERROR_INVALID_EXTERNAL_HANDLE;
            }
"""

GETTER = "vkGetMemoryAndroidHardwareBufferANDROID"
GETTER_CALL = "vkGetMemoryAndroidHardwareBufferANDROID(mDevice"
LOG_LINE = "AHB-EXPORT-SKIP no-ahb-backing size=%llu"
GATE = "::gfxstream::ahb_export_guard::ShouldLogSkip()"

before = ops_path.read_text(encoding="utf-8")

existing_header = header_path.read_text(encoding="utf-8") if header_path.is_file() else None
if existing_header is not None and existing_header != HEADER:
    raise SystemExit(
        f"{header}: exists but does not match the AHB export guard helper; "
        "refusing to overwrite")

# Validate every anchor before writing anything.
text = before
if INCLUDE_BLOCK in text:
    if text.count(INCLUDE_BLOCK) != 1:
        raise SystemExit(f"{ops}: guard include block appears {text.count(INCLUDE_BLOCK)} times")
elif INCLUDE_LINE in text:
    raise SystemExit(
        f"{ops}: guard include exists without the AHB allocation switch include; "
        "start from a clean pinned tree")
else:
    if text.count(INCLUDE_ANCHOR) != 1:
        raise SystemExit(
            f"{ops}: expected one AHB allocation switch include anchor, "
            f"found {text.count(INCLUDE_ANCHOR)}")

if NEW_ELSE in text:
    if text.count(NEW_ELSE) != 1:
        raise SystemExit(f"{ops}: guarded AHB export block appears {text.count(NEW_ELSE)} times")
    if OLD_ELSE in text:
        raise SystemExit(f"{ops}: both the old AHB export fallback and the guard are present")
else:
    if text.count(OLD_ELSE) != 1:
        raise SystemExit(
            f"{ops}: expected one old AHB export fallback block, found {text.count(OLD_ELSE)}")

# The normal path must still be exactly the imported-AHB fast path, and after the patch
# no AHB export call may remain anywhere in this translation unit. Comments that name the
# extension entry point are fine; a call is not.
if GETTER_CALL in text.replace(OLD_ELSE, ""):
    raise SystemExit(
        f"{ops}: {GETTER_CALL} appears outside the guarded fallback; only the AndroidAHB "
        "fallback may call the getter")

# Checks passed: write the helper first, then the include and the guarded branch.
if existing_header is None:
    header_path.write_text(HEADER, encoding="utf-8", newline="\n")
    print("created", header)
else:
    print("already created", header)

if INCLUDE_BLOCK not in text:
    text = text.replace(INCLUDE_ANCHOR, INCLUDE_BLOCK, 1)
    print("patched", ops, "(guard include)")
else:
    print("already patched", ops, "(guard include)")

if NEW_ELSE not in text:
    text = text.replace(OLD_ELSE, NEW_ELSE, 1)
    print("patched", ops, "(AHB-EXPORT-GUARD-1)")
else:
    print("already patched", ops, "(AHB-EXPORT-GUARD-1)")

if text != before:
    ops_path.write_text(text, encoding="utf-8", newline="\n")

after = ops_path.read_text(encoding="utf-8")
for needle, label in (
    (GETTER_CALL, "AHB export call"),
    ("getAhbInfo", "old AHB export info"),
    ("AHB-NULL-GUARD-2", "superseded publish wrapper"),
):
    if needle in after:
        raise SystemExit(
            f"{ops}: {label} is still present after patching; the fallback must not reach "
            "the driver")
if after.count(LOG_LINE) != 1 or after.count(GATE) != 1:
    raise SystemExit(f"{ops}: guarded AHB export block is incomplete")

print("AHB export guard installed (AndroidAHB fallback only)")
