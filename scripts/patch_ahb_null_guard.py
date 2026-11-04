#!/usr/bin/env python3
"""Fail closed when AndroidAHB external memory resolves to a null handle.

Why
---
The device crash dump shows libutils RefBase::incStrong taking a SIGSEGV at
fault 0x8 with x0=0, called from AHardwareBuffer_acquire() inside the Mali
driver's vkAllocateMemory. The import struct it received carried
{handle=0, streamHandleType=STREAM_HANDLE_TYPE_PLATFORM_AHB}: a zero handle
was treated as an AHardwareBuffer pointer. Three code paths can produce that
value -- a guest-supplied null handle, a failed AHB export that still
publishes ExternalHandleInfo, and an export returning success with no handle
-- and a fourth can release it. All four must fail before the pointer reaches
the driver.

What
----
1. appendExternalMemoryImport: refuse handle 0 in the AndroidHardwareBuffer
   branch; never put it in VkImportAndroidHardwareBufferInfoANDROID.buffer.
2. allocExternalMemory AndroidAHB fallback: only publish info->handleInfo
   when the export actually returned a handle, so a failed export leaves the
   handle unset instead of exporting {0, PLATFORM_AHB} to the guest.
3. exportMemoryHandle AndroidAHB: a null exportHandle is an error and returns
   std::nullopt rather than a zero descriptor.
4. freeExternalMemoryLocked: only AHardwareBuffer_release a non-null handle.

The script is idempotent: a second run recognizes its own AHB-NULL-GUARD-n
markers and verifies the guarded block is intact instead of patching again.
Every replacement requires exactly one occurrence of the pinned-commit text.
"""
import argparse
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("root", type=Path, help="Pinned gfxstream source checkout")
root = parser.parse_args().root.resolve()


def edit(rel, old, new, marker):
    path = root / rel
    text = path.read_text(encoding="utf-8")
    if marker in text:
        if text.count(new) != 1:
            raise SystemExit(f"{rel}: {marker} present but the guarded block is not intact")
        print("already patched", rel, f"({marker})")
        return
    if text.count(old) != 1:
        raise SystemExit(f"{rel}: expected one {marker} anchor, found {text.count(old)}")
    path.write_text(text.replace(old, new), encoding="utf-8")
    print("patched", rel, f"({marker})")


decoder = "host/vulkan/vk_decoder_global_state.cpp"
ops = "host/vulkan/vk_common_operations.cpp"

# ---------------------------------------------------------------------------
# 1. A zero handle is not an AHardwareBuffer: reject it at the import entry.
# ---------------------------------------------------------------------------
edit(
    decoder,
    """                    // RefBase::incStrong. Select the import struct by the actual handle type.
                    importInfo.buffer = reinterpret_cast<AHardwareBuffer*>(handleInfo.handle);""",
    """                    // RefBase::incStrong. Select the import struct by the actual handle type.
                    // AHB-NULL-GUARD-1: an unset handle is not an AHardwareBuffer. Importing 0
                    // makes the driver call AHardwareBuffer_acquire(nullptr) during
                    // vkAllocateMemory and SIGSEGV in RefBase::incStrong. Fail the import.
                    if (handleInfo.handle == 0) {
                        GFXSTREAM_ERROR(
                            "AHB-NULL-GUARD: refusing to import a null AHardwareBuffer handle");
                        return false;
                    }
                    importInfo.buffer = reinterpret_cast<AHardwareBuffer*>(handleInfo.handle);""",
    "AHB-NULL-GUARD-1",
)

# ---------------------------------------------------------------------------
# 2. A failed export leaves the handle unset; never publish {0, PLATFORM_AHB}.
# ---------------------------------------------------------------------------
edit(
    ops,
    """                validHandle = (VK_SUCCESS == exportRes) && (NULL != exportHandle);
                info->handleInfo = ExternalHandleInfo{
                    .handle = reinterpret_cast<ExternalHandleType>(exportHandle),
                    .streamHandleType = STREAM_HANDLE_TYPE_PLATFORM_AHB,
                };""",
    """                validHandle = (VK_SUCCESS == exportRes) && (NULL != exportHandle);
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
                }""",
    "AHB-NULL-GUARD-2",
)

# ---------------------------------------------------------------------------
# 3. Export success with a null handle is still a failure: return no handle.
# ---------------------------------------------------------------------------
edit(
    decoder,
    """                if (res != VK_SUCCESS) {
                    GFXSTREAM_INFO("%s: vkGetMemoryAndroidHardwareBufferANDROID failed: %s",
                                   __func__, string_VkResult(res));
                    return std::nullopt;
                }

                ret.handle = reinterpret_cast<ExternalHandleType>(exportHandle);""",
    """                if (res != VK_SUCCESS) {
                    GFXSTREAM_INFO("%s: vkGetMemoryAndroidHardwareBufferANDROID failed: %s",
                                   __func__, string_VkResult(res));
                    return std::nullopt;
                }
                // AHB-NULL-GUARD-3: success with a null handle still means there is no AHB to
                // export. Returning it would put handle=0 on the wire and hand it to
                // AHardwareBuffer_acquire() in the importer, which SIGSEGVs.
                if (exportHandle == nullptr) {
                    GFXSTREAM_ERROR(
                        "%s: vkGetMemoryAndroidHardwareBufferANDROID returned a null AHB handle",
                        __func__);
                    return std::nullopt;
                }

                ret.handle = reinterpret_cast<ExternalHandleType>(exportHandle);""",
    "AHB-NULL-GUARD-3",
)

# ---------------------------------------------------------------------------
# 4. Release only a handle that exists; zero is not an AHardwareBuffer.
# ---------------------------------------------------------------------------
edit(
    ops,
    """            case STREAM_HANDLE_TYPE_PLATFORM_AHB:
                AHardwareBuffer_release(static_cast<AHardwareBuffer*>(
                    reinterpret_cast<void*>(info->handleInfo->handle)));
                break;""",
    """            case STREAM_HANDLE_TYPE_PLATFORM_AHB:
                // AHB-NULL-GUARD-4: release only an AHB that was actually exported. A zero
                // handle must not be walked by the allocator as a RefBase pointer.
                if (info->handleInfo->handle != 0) {
                    AHardwareBuffer_release(static_cast<AHardwareBuffer*>(
                        reinterpret_cast<void*>(info->handleInfo->handle)));
                }
                break;""",
    "AHB-NULL-GUARD-4",
)

print("AHB null-handle fail-closed guards installed")
