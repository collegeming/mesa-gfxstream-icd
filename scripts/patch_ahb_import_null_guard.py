#!/usr/bin/env python3
"""Default-on guard for the AndroidHardwareBuffer external-memory import.

Why
---
The device dump puts the crash at AHardwareBuffer_acquire(0) -> RefBase::incStrong
fault 0x8. Disassembly of /system/lib64/libnativewindow.so shows acquire() does not
check its argument: it calls GraphicBuffer::fromAHardwareBuffer() and then tail-calls
incStrong() on the result, so a zero or fd-sized handle faults inside libutils.

In host/vulkan/vk_decoder_global_state.cpp, case
ExternalMemoryImportKind::AndroidHardwareBuffer casts ExternalHandleInfo::handle
straight to AHardwareBuffer* and appends it to the pNext chain of vkAllocateMemory.
The Mali driver consumes that struct and calls AHardwareBuffer_acquire() itself, so
none of the existing gfxstream-side guard calls are on that path. The existing
AHB-NULL-GUARD rejects exactly 0; a non-zero fd-sized handle still reaches the driver.

What
----
1. host/vulkan/ahb_import_guard.h (created): one constexpr comparison that accepts
   only an address that could belong to a userspace allocation (>= 4096), plus a
   once-per-process log gate.
2. vk_decoder_global_state.cpp includes it on Android.
3. `externalMemoryImportResult` is declared next to appendExternalMemoryImport.
4. Inside the AndroidHardwareBuffer case, before the existing AHB-NULL-GUARD, a handle
   that fails the comparison is logged once as

       AHB-IMPORT-NULL handle=%p kind=0x%x: ...

   recorded as VK_ERROR_INVALID_EXTERNAL_HANDLE, and refused. importInfo.buffer is
   not assigned.
5. Both appendExternalMemoryImport callers return the recorded result instead of the
   generic "unsupported external memory handle type" failure.

Only the AndroidHardwareBuffer branch changes. OpaqueFd and DmaBuf keep reading
getFd(); no other import kind casts a handle to a pointer. A plausible address takes
the original assignment with the original bit pattern: the guard adds one comparison
and one branch, and nothing runs on the legal path beyond that.

The script is idempotent: a second run verifies the header and the AHB-IMPORT-NULL-*
blocks instead of patching again. Every anchor must occur exactly once and all checks
run before the first write, so a rejected patch leaves the tree untouched.
"""
import argparse
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("root", type=Path, help="Pinned gfxstream source checkout")
root = parser.parse_args().root.resolve()

decoder = "host/vulkan/vk_decoder_global_state.cpp"
header = "host/vulkan/ahb_import_guard.h"

decoder_path = root / decoder
header_path = root / header

if not decoder_path.is_file():
    raise SystemExit(f"{decoder}: file not found under {root}")

HEADER = """\
// Default-on guard for the AndroidHardwareBuffer external-memory import.
//
// AHardwareBuffer_acquire() does not check its argument. On the device it was
// disassembled as a GraphicBuffer::fromAHardwareBuffer() call followed by a tail call
// to RefBase::incStrong(), so a null or fd-sized "pointer" faults at 0x8 inside
// libutils. The decoder used to reinterpret_cast every PLATFORM_AHB handle into
// VkImportAndroidHardwareBufferInfoANDROID::buffer and hand it to the Mali driver,
// which calls acquire() during vkAllocateMemory.
//
// Zero and small integers cannot be Android userspace addresses. Reject exactly those:
// the comparison is the whole predicate, and a plausible address keeps the original
// pointer value, with no copy, mask or rewrite.
#ifndef GFXSTREAM_HOST_VULKAN_AHB_IMPORT_GUARD_H_
#define GFXSTREAM_HOST_VULKAN_AHB_IMPORT_GUARD_H_

#include <stdint.h>

#include <atomic>

namespace gfxstream {
namespace ahb_import_guard {

// Lowest address a userspace mapping can have (mmap_min_addr is 4096 or 32768 on the
// kernels in use). Anything below this is a null-ish integer or an fd, never an
// AHardwareBuffer*. One comparison covers both 0 and fd-sized handles.
constexpr uint64_t kMinPlausibleAhbAddress = 4096;

constexpr bool AhbImportHandleIsPlausible(int64_t handle) {
    return static_cast<uint64_t>(handle) >= kMinPlausibleAhbAddress;
}

// One line per process: a guest stuck retrying the import must not flood logcat or the
// console pipe, but the first occurrence still identifies the path. The function-local
// static in an inline function is shared by every caller in the process.
inline bool ShouldLogImportFailure() {
    static std::atomic<unsigned> sLogged{0};
    return sLogged.fetch_add(1, std::memory_order_relaxed) == 0;
}

}  // namespace ahb_import_guard
}  // namespace gfxstream

#endif  // GFXSTREAM_HOST_VULKAN_AHB_IMPORT_GUARD_H_
"""

INCLUDE_ANCHOR = '#include "vk_decoder_global_state.h"\n'
INCLUDE_BLOCK = """\
#include "vk_decoder_global_state.h"
#if defined(__ANDROID__)
#include "ahb_import_guard.h"
#endif
"""

SETUP = """\
        // AHB-IMPORT-NULL-1-SETUP: set by the AHB import guard when it refuses a handle,
        // so the callers below can return the import-specific error.
        VkResult externalMemoryImportResult = VK_SUCCESS;
"""
LAMBDA_ANCHOR = """\
        auto appendExternalMemoryImport = [&](const ExternalHandleInfo& handleInfo) -> bool {
"""

BEGIN = "                    // AHB-IMPORT-NULL-1-BEGIN\n"
END = "                    // AHB-IMPORT-NULL-1-END\n"
GUARD = (
    BEGIN
    + """\
                    // AHardwareBuffer_acquire() does not check its argument (it tail-calls
                    // RefBase::incStrong), so a handle that is not an address crashes the
                    // driver in vkAllocateMemory. Zero and fd-sized integers are the observed
                    // shapes; a plausible address keeps the pinned assignment unchanged.
                    if (!::gfxstream::ahb_import_guard::AhbImportHandleIsPlausible(
                            handleInfo.handle)) {
                        if (::gfxstream::ahb_import_guard::ShouldLogImportFailure()) {
                            GFXSTREAM_ERROR(
                                "AHB-IMPORT-NULL handle=%p kind=0x%x: not an AHardwareBuffer "
                                "address; refusing the import",
                                reinterpret_cast<void*>(
                                    static_cast<uintptr_t>(handleInfo.handle)),
                                handleInfo.streamHandleType);
                        }
                        externalMemoryImportResult = VK_ERROR_INVALID_EXTERNAL_HANDLE;
                        return false;
                    }
"""
    + END
)
CASE_ANCHOR = """\
                case ExternalMemoryImportKind::AndroidHardwareBuffer:
#if defined(__ANDROID__)
"""

COLOR_ANCHOR = """\
                    if (!appendExternalMemoryImport(*dupHandleInfo)) {
                        GFXSTREAM_ERROR("Unsupported ColorBuffer external memory handle type: %u",
                                        dupHandleInfo->streamHandleType);
                        return VK_ERROR_OUT_OF_DEVICE_MEMORY;
                    }
"""
COLOR_GUARDED = """\
                    if (!appendExternalMemoryImport(*dupHandleInfo)) {
                        // AHB-IMPORT-NULL-2: an AHB handle rejected by the guard already chose
                        // its result; keep the unsupported-type error for the other kinds.
                        if (externalMemoryImportResult != VK_SUCCESS) {
                            return externalMemoryImportResult;
                        }
                        GFXSTREAM_ERROR("Unsupported ColorBuffer external memory handle type: %u",
                                        dupHandleInfo->streamHandleType);
                        return VK_ERROR_OUT_OF_DEVICE_MEMORY;
                    }
"""

BUFFER_ANCHOR = """\
                if (!appendExternalMemoryImport(*dupHandleInfo)) {
                    GFXSTREAM_ERROR("Unsupported Buffer external memory handle type: %u",
                                    dupHandleInfo->streamHandleType);
                    return VK_ERROR_OUT_OF_DEVICE_MEMORY;
                }
"""
BUFFER_GUARDED = """\
                if (!appendExternalMemoryImport(*dupHandleInfo)) {
                    // AHB-IMPORT-NULL-3: same split for Buffer imports.
                    if (externalMemoryImportResult != VK_SUCCESS) {
                        return externalMemoryImportResult;
                    }
                    GFXSTREAM_ERROR("Unsupported Buffer external memory handle type: %u",
                                    dupHandleInfo->streamHandleType);
                    return VK_ERROR_OUT_OF_DEVICE_MEMORY;
                }
"""

before = decoder_path.read_text(encoding="utf-8")
existing_header = header_path.read_text(encoding="utf-8") if header_path.is_file() else None

if existing_header is not None and existing_header != HEADER:
    raise SystemExit(f"{header}: exists but does not match the import guard; refusing to overwrite")

# ---------------------------------------------------------------------------
# Validate every anchor before the first write.
# ---------------------------------------------------------------------------
text = before

if INCLUDE_BLOCK in text:
    if text.count(INCLUDE_BLOCK) != 1:
        raise SystemExit(f"{decoder}: import guard include block is not unique")
    include_present = True
elif '#include "ahb_import_guard.h"' in text:
    raise SystemExit(f"{decoder}: import guard include exists outside the Android block")
else:
    if text.count(INCLUDE_ANCHOR) != 1:
        raise SystemExit(
            f"{decoder}: expected one include anchor, found {text.count(INCLUDE_ANCHOR)}")
    include_present = False

if SETUP in text:
    setup_present = True
else:
    setup_present = False

if BEGIN in text or END in text:
    if text.count(BEGIN) != 1 or text.count(END) != 1:
        raise SystemExit(f"{decoder}: AHB-IMPORT-NULL-1 markers are not paired")
    if text.count(GUARD) != 1:
        raise SystemExit(f"{decoder}: AHB-IMPORT-NULL-1 marker present but the guard is not intact")
    guard_present = True
else:
    guard_present = False

for anchor, guarded, label in (
    (COLOR_ANCHOR, COLOR_GUARDED, "AHB-IMPORT-NULL-2"),
    (BUFFER_ANCHOR, BUFFER_GUARDED, "AHB-IMPORT-NULL-3"),
):
    if guarded in text:
        if text.count(guarded) != 1:
            raise SystemExit(f"{decoder}: {label} block is not unique")
    elif anchor in text:
        if text.count(anchor) != 1:
            raise SystemExit(f"{decoder}: expected one {label} anchor, found {text.count(anchor)}")
    else:
        raise SystemExit(f"{decoder}: {label} anchor not found")

if not guard_present:
    if text.count(LAMBDA_ANCHOR) != 1:
        raise SystemExit(
            f"{decoder}: expected one appendExternalMemoryImport anchor, "
            f"found {text.count(LAMBDA_ANCHOR)}")
    if text.count(CASE_ANCHOR) != 1:
        raise SystemExit(
            f"{decoder}: expected one AndroidHardwareBuffer case anchor, "
            f"found {text.count(CASE_ANCHOR)}")

if not setup_present and "VkResult externalMemoryImportResult" in text:
    raise SystemExit(f"{decoder}: externalMemoryImportResult exists in an unexpected form")

# ---------------------------------------------------------------------------
# Checks passed: write the helper, then the four insertions.
# ---------------------------------------------------------------------------
if existing_header is None:
    header_path.write_text(HEADER, encoding="utf-8", newline="\n")
    print("created", header)
else:
    print("already created", header)

if not include_present:
    text = text.replace(INCLUDE_ANCHOR, INCLUDE_BLOCK, 1)
    print("patched", decoder, "(helper include)")

if not setup_present:
    text = text.replace(LAMBDA_ANCHOR, SETUP + LAMBDA_ANCHOR, 1)
    print("patched", decoder, "(AHB-IMPORT-NULL-1-SETUP)")

if not guard_present:
    text = text.replace(CASE_ANCHOR, CASE_ANCHOR + GUARD, 1)
    print("patched", decoder, "(AHB-IMPORT-NULL-1)")

if COLOR_GUARDED not in text:
    text = text.replace(COLOR_ANCHOR, COLOR_GUARDED, 1)
    print("patched", decoder, "(AHB-IMPORT-NULL-2)")

if BUFFER_GUARDED not in text:
    text = text.replace(BUFFER_ANCHOR, BUFFER_GUARDED, 1)
    print("patched", decoder, "(AHB-IMPORT-NULL-3)")

if text != before:
    decoder_path.write_text(text, encoding="utf-8", newline="\n")

print("AHB import handle guard installed (AndroidHardwareBuffer branch only)")
