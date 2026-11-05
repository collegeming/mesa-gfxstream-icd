#!/usr/bin/env python3
"""Install the diagnostic-only AHB lifecycle tracer.

The tracer is a header-only signal handler and breadcrumb ring in
host/vulkan/ahb_crash_trace.h. It is compiled into libgfxstream_backend.so by
including it from the AHB-bearing translation units. Behaviour is unchanged
unless the process environment contains exactly GFXSTREAM_CRASH_TRACE=1; the
handler then prints raw PCs with module load offsets and the last AHB
breadcrumbs to stderr (or GFXSTREAM_CRASH_TRACE_FILE), and chains to the
previously installed handler so crosvm's own crash dump and _exit(128+sig)
still happen.

Every breadcrumb carries the writer's tid, so the crash record shows whether
the faulting thread is the one that last touched an AHardwareBuffer or an
unrelated compositor/driver worker.

Labels are fixed and grep-able:

  AHB-LIFE alloc enter / AHB-LIFE alloc return   allocAhb()
  AHB-LIFE alloc-ret enter / AHB-LIFE alloc-ret  AHB-first BLOB allocation
  AHB-LIFE free pre / free post / free skip-null freeExternalMemoryLocked()
  AHB-LIFE release / release skip-null           every other release
  AHB-LIFE import handle / import buffer         decoder AHB import
  AHB-LIFE guest-import [null]                   guest pNext AHB import
  AHB-LIFE cb-import / cb-import buffer          ColorBuffer AHB import
  AHB-LIFE ahbfirst-import buffer                AHB-first import
  AHB-LIFE alias-acquire / skip-null             importExternalMemory alias

The script is idempotent. Every insertion carries a marker; a second run
verifies the marked block instead of patching again. Anchors that belong to
optional earlier patches are skipped with a note when absent.
"""
import argparse
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("root", type=Path, help="Patched gfxstream source checkout")
root = parser.parse_args().root.resolve()

HEADER_SOURCE = Path(__file__).resolve().parent / "ahb_crash_trace.h"
HEADER_TARGET = root / "host/vulkan/ahb_crash_trace.h"
OPS = root / "host/vulkan/vk_common_operations.cpp"
DECODER = root / "host/vulkan/vk_decoder_global_state.cpp"
OBJECT_MANAGER = root / "host/common/external_object_manager.cpp"


def edit(path: Path, old: str, new: str, marker: str, required: bool = True) -> bool:
    text = path.read_text(encoding="utf-8")
    if marker in text:
        if text.count(new) != 1:
            raise SystemExit(f"{path.name}: {marker} present but the marked block is not intact")
        print(f"already patched {path.name} ({marker})")
        return True
    count = text.count(old)
    if count == 0 and not required:
        print(f"optional anchor absent in {path.name} ({marker})")
        return False
    if count != 1:
        raise SystemExit(f"{path.name}: expected one {marker} anchor, found {count}")
    path.write_text(text.replace(old, new), encoding="utf-8")
    print(f"patched {path.name} ({marker})")
    return True


# ---------------------------------------------------------------------------
# 0. Ship the header. It must match the CI copy byte for byte.
# ---------------------------------------------------------------------------
header_text = HEADER_SOURCE.read_text(encoding="utf-8")
for needle in ("GFXSTREAM_CRASH_TRACE", "_Unwind_Backtrace", 'w.str("tid=")',
               "kRingSlots = 64", "static_assert(kRingSlots >= 64"):
    assert needle in header_text, f"tracer header is missing {needle}"
if HEADER_TARGET.is_file():
    if HEADER_TARGET.read_text(encoding="utf-8") != header_text:
        raise SystemExit(f"{HEADER_TARGET}: exists but does not match the tracer header")
    print("already copied host/vulkan/ahb_crash_trace.h")
else:
    HEADER_TARGET.write_text(header_text, encoding="utf-8")
    print("copied host/vulkan/ahb_crash_trace.h")

# ---------------------------------------------------------------------------
# 1. Includes. The tracer is header-only with inline state, so all including
#    translation units share one handler, one maps table and one ring.
# ---------------------------------------------------------------------------
INCLUDE_OLD = """#ifdef __ANDROID__
#include "ahb_alloc_off_file.h"
#endif
"""
INCLUDE_NEW = """#ifdef __ANDROID__
#include "ahb_alloc_off_file.h"
#endif

// AHB-CRASH-TRACE-INCLUDE-BEGIN
#include "ahb_crash_trace.h"
// AHB-CRASH-TRACE-INCLUDE-END
"""
edit(OPS, INCLUDE_OLD, INCLUDE_NEW, "AHB-CRASH-TRACE-INCLUDE")

DECODER_INCLUDE_OLD = """#include <limits>
namespace gfxstream { namespace host { namespace cib {
"""
DECODER_INCLUDE_NEW = """#include <limits>

// AHB-CRASH-TRACE-INCLUDE-DECODER-BEGIN
#include "ahb_crash_trace.h"
// AHB-CRASH-TRACE-INCLUDE-DECODER-END
namespace gfxstream { namespace host { namespace cib {
"""
edit(DECODER, DECODER_INCLUDE_OLD, DECODER_INCLUDE_NEW, "AHB-CRASH-TRACE-INCLUDE-DECODER")

# Blob descriptors can own a PLATFORM_AHB handle and release it from
# host/common, a third translation unit outside host/vulkan.
OBJECT_MANAGER_INCLUDE_OLD = """#include "gfxstream/host/external_object_manager.h"

#include <utility>
"""
OBJECT_MANAGER_INCLUDE_NEW = """#include "gfxstream/host/external_object_manager.h"

#include <utility>

// AHB-CRASH-TRACE-INCLUDE-OBJECT-MANAGER-BEGIN
#include "../vulkan/ahb_crash_trace.h"
// AHB-CRASH-TRACE-INCLUDE-OBJECT-MANAGER-END
"""
edit(OBJECT_MANAGER, OBJECT_MANAGER_INCLUDE_OLD, OBJECT_MANAGER_INCLUDE_NEW,
     "AHB-CRASH-TRACE-INCLUDE-OBJECT-MANAGER")

# ---------------------------------------------------------------------------
# 2. The explicit ColorBuffer AHB allocation. allocAhb() is the only place that
#    turns a guest image shape into AHardwareBuffer_allocate().
# ---------------------------------------------------------------------------
ALLOC_OLD = """    AHardwareBuffer* ahb = nullptr;
    int ahbRes = AHardwareBuffer_allocate(&desc, &ahb);
"""
ALLOC_NEW = """    AHardwareBuffer* ahb = nullptr;
    // AHB-CRASH-TRACE-ALLOC-BEGIN
    GFXSTREAM_AHB_TRACE("AHB-LIFE alloc enter", nullptr,
                        (static_cast<unsigned long long>(imageCreateInfo->extent.width) << 32) |
                            imageCreateInfo->extent.height);
    // AHB-CRASH-TRACE-ALLOC-END
    int ahbRes = AHardwareBuffer_allocate(&desc, &ahb);
    // AHB-CRASH-TRACE-ALLOC-RETURN-BEGIN
    GFXSTREAM_AHB_TRACE("AHB-LIFE alloc return", ahb,
                        static_cast<unsigned long long>(ahbRes));
    // AHB-CRASH-TRACE-ALLOC-RETURN-END
"""
edit(OPS, ALLOC_OLD, ALLOC_NEW, "AHB-CRASH-TRACE-ALLOC")

# ---------------------------------------------------------------------------
# 3. The ColorBuffer allocation imports the AHB into the host driver; the
#    driver calls AHardwareBuffer_acquire() on this pointer.
# ---------------------------------------------------------------------------
IMPORT_CB_OLD = """                importAhbInfo.buffer = ahb;
                vk_append_struct(&allocInfoChain, &importAhbInfo);
"""
IMPORT_CB_NEW = """                // AHB-CRASH-TRACE-IMPORT-CB-BEGIN
                if (!ahb) {
                    GFXSTREAM_AHB_TRACE("AHB-LIFE cb-import skip-null", nullptr,
                                        allocInfo.memoryTypeIndex);
                    break;
                }
                GFXSTREAM_AHB_TRACE("AHB-LIFE cb-import", ahb, allocInfo.memoryTypeIndex);
                // AHB-CRASH-TRACE-IMPORT-CB-END
                importAhbInfo.buffer = ahb;
                // AHB-CRASH-TRACE-IMPORT-CB-BUFFER-BEGIN
                GFXSTREAM_AHB_TRACE("AHB-LIFE cb-import buffer", importAhbInfo.buffer, 0);
                // AHB-CRASH-TRACE-IMPORT-CB-BUFFER-END
                vk_append_struct(&allocInfoChain, &importAhbInfo);
"""
edit(OPS, IMPORT_CB_OLD, IMPORT_CB_NEW, "AHB-CRASH-TRACE-IMPORT-CB")

# ---------------------------------------------------------------------------
# 4. AHB release at the two ColorBuffer-import failure exits.
# ---------------------------------------------------------------------------
RELEASE_PROPS_OLD = """                        string_VkResult(propsRes));
                    AHardwareBuffer_release(ahb);
"""
RELEASE_PROPS_NEW = """                        string_VkResult(propsRes));
                    // AHB-CRASH-TRACE-RELEASE-PROPS-BEGIN
                    GFXSTREAM_AHB_TRACE("AHB-LIFE release", ahb, 0);
                    // AHB-CRASH-TRACE-RELEASE-PROPS-END
                    AHardwareBuffer_release(ahb);
"""
edit(OPS, RELEASE_PROPS_OLD, RELEASE_PROPS_NEW, "AHB-CRASH-TRACE-RELEASE-PROPS")

RELEASE_SIZE_OLD = """                        (unsigned long long)cbInfoPtr->imageMemReqs.size);
                    AHardwareBuffer_release(ahb);
"""
RELEASE_SIZE_NEW = """                        (unsigned long long)cbInfoPtr->imageMemReqs.size);
                    // AHB-CRASH-TRACE-RELEASE-SIZE-BEGIN
                    GFXSTREAM_AHB_TRACE("AHB-LIFE release", ahb, 0);
                    // AHB-CRASH-TRACE-RELEASE-SIZE-END
                    AHardwareBuffer_release(ahb);
"""
edit(OPS, RELEASE_SIZE_OLD, RELEASE_SIZE_NEW, "AHB-CRASH-TRACE-RELEASE-SIZE")

# ---------------------------------------------------------------------------
# 5. The buffer-alias importer takes its own reference before the driver sees
#    the AHB. A null handle must never reach AHardwareBuffer_acquire(): the
#    diagnostic build records the skip and returns instead.
# ---------------------------------------------------------------------------
ALIAS_OLD = """                    importedAhb = reinterpret_cast<AHardwareBuffer*>(handleInfo->handle);
                    if (!importedAhb) return false;
                    AHardwareBuffer_acquire(importedAhb);
"""
ALIAS_NEW = """                    importedAhb = reinterpret_cast<AHardwareBuffer*>(handleInfo->handle);
                    // AHB-CRASH-TRACE-ALIAS-BEGIN
                    if (!importedAhb) {
                        GFXSTREAM_AHB_TRACE("AHB-LIFE alias-acquire skip-null", nullptr,
                                            handleInfo->streamHandleType);
                        return false;
                    }
                    GFXSTREAM_AHB_TRACE("AHB-LIFE alias-acquire", importedAhb,
                                        handleInfo->streamHandleType);
                    // AHB-CRASH-TRACE-ALIAS-END
                    AHardwareBuffer_acquire(importedAhb);
"""
edit(OPS, ALIAS_OLD, ALIAS_NEW, "AHB-CRASH-TRACE-ALIAS", required=False)

# The alias reference is dropped again when the driver rejects the allocation.
ALIAS_RELEASE_OLD = """    if (res != VK_SUCCESS && importedAhb) AHardwareBuffer_release(importedAhb);
"""
ALIAS_RELEASE_NEW = """    if (res != VK_SUCCESS && importedAhb) {
        // AHB-CRASH-TRACE-ALIAS-RELEASE-BEGIN
        GFXSTREAM_AHB_TRACE("AHB-LIFE release", importedAhb, 0);
        // AHB-CRASH-TRACE-ALIAS-RELEASE-END
        AHardwareBuffer_release(importedAhb);
    }
"""
edit(OPS, ALIAS_RELEASE_OLD, ALIAS_RELEASE_NEW, "AHB-CRASH-TRACE-ALIAS-RELEASE",
     required=False)

# ---------------------------------------------------------------------------
# 6. freeExternalMemoryLocked(): the AHB release is the other half of every
#    acquire, so record the pointer and tid both before and after it.
# ---------------------------------------------------------------------------
FREE_OLD = """                if (info->handleInfo->handle != 0) {
                    AHardwareBuffer_release(static_cast<AHardwareBuffer*>(
                        reinterpret_cast<void*>(info->handleInfo->handle)));
                }
"""
FREE_NEW = """                if (info->handleInfo->handle != 0) {
                    // AHB-CRASH-TRACE-FREE-PRE-BEGIN
                    GFXSTREAM_AHB_TRACE(
                        "AHB-LIFE free pre",
                        reinterpret_cast<void*>(info->handleInfo->handle),
                        info->handleInfo->streamHandleType);
                    // AHB-CRASH-TRACE-FREE-PRE-END
                    AHardwareBuffer_release(static_cast<AHardwareBuffer*>(
                        reinterpret_cast<void*>(info->handleInfo->handle)));
                    // AHB-CRASH-TRACE-FREE-POST-BEGIN
                    GFXSTREAM_AHB_TRACE(
                        "AHB-LIFE free post",
                        reinterpret_cast<void*>(info->handleInfo->handle),
                        info->handleInfo->streamHandleType);
                    // AHB-CRASH-TRACE-FREE-POST-END
                } else {
                    // AHB-CRASH-TRACE-FREE-SKIP-BEGIN
                    GFXSTREAM_AHB_TRACE("AHB-LIFE free skip-null", nullptr,
                                        info->handleInfo->streamHandleType);
                    // AHB-CRASH-TRACE-FREE-SKIP-END
                }
"""
edit(OPS, FREE_OLD, FREE_NEW, "AHB-CRASH-TRACE-FREE-PRE")

# ---------------------------------------------------------------------------
# 7. Decoder-side ColorBuffer/Buffer AHB import (guest handle duplicated by
#    gfxstream and placed in VkImportAndroidHardwareBufferInfoANDROID).
# ---------------------------------------------------------------------------
DECODER_CB_OLD = """                    importInfo.buffer = static_cast<AHardwareBuffer*>(
                        reinterpret_cast<void*>(dupHandleInfo->handle));
"""
DECODER_CB_NEW = """                    // AHB-CRASH-TRACE-DECODER-CB-BEGIN
                    GFXSTREAM_AHB_TRACE("AHB-LIFE import buffer",
                                        reinterpret_cast<void*>(dupHandleInfo->handle), 0);
                    // AHB-CRASH-TRACE-DECODER-CB-END
                    importInfo.buffer = static_cast<AHardwareBuffer*>(
                        reinterpret_cast<void*>(dupHandleInfo->handle));
"""
edit(DECODER, DECODER_CB_OLD, DECODER_CB_NEW, "AHB-CRASH-TRACE-DECODER-CB", required=False)

# ---------------------------------------------------------------------------
# 8. Decoder-side guest handle import, after the null-handle guard. The trace
#    records the exact handle the driver is about to acquire, and then the
#    buffer the driver will actually read from the import struct.
# ---------------------------------------------------------------------------
DECODER_HANDLE_OLD = """                    if (handleInfo.handle == 0) {
                        GFXSTREAM_ERROR(
                            "AHB-NULL-GUARD: refusing to import a null AHardwareBuffer handle");
                        return false;
                    }
                    importInfo.buffer = reinterpret_cast<AHardwareBuffer*>(handleInfo.handle);
                    vk_append_struct(&structChainIter, &importInfo);
"""
DECODER_HANDLE_NEW = """                    // AHB-CRASH-TRACE-DECODER-HANDLE-BEGIN
                    GFXSTREAM_AHB_TRACE("AHB-LIFE import handle",
                                        reinterpret_cast<void*>(handleInfo.handle),
                                        handleInfo.streamHandleType);
                    // AHB-CRASH-TRACE-DECODER-HANDLE-END
                    if (handleInfo.handle == 0) {
                        GFXSTREAM_ERROR(
                            "AHB-NULL-GUARD: refusing to import a null AHardwareBuffer handle");
                        return false;
                    }
                    importInfo.buffer = reinterpret_cast<AHardwareBuffer*>(handleInfo.handle);
                    // AHB-CRASH-TRACE-DECODER-IMPORT-BUFFER-BEGIN
                    GFXSTREAM_AHB_TRACE("AHB-LIFE import buffer", importInfo.buffer, 0);
                    // AHB-CRASH-TRACE-DECODER-IMPORT-BUFFER-END
                    vk_append_struct(&structChainIter, &importInfo);
"""
edit(DECODER, DECODER_HANDLE_OLD, DECODER_HANDLE_NEW, "AHB-CRASH-TRACE-DECODER-HANDLE",
     required=True)

# ---------------------------------------------------------------------------
# 9. A guest-supplied VkImportAndroidHardwareBufferInfoANDROID in the pNext
#    chain is forwarded to the driver untouched. Record its buffer, including
#    the null case, so a later driver-side acquire can be tied to it.
# ---------------------------------------------------------------------------
GUEST_AHB_OLD = """#ifdef __ANDROID__
        const auto* diagnosticAhbInfo = vk_find_struct<VkImportAndroidHardwareBufferInfoANDROID>(&localAllocInfo);
        if (diagnosticAhbInfo) {
            diagnosticAhb = diagnosticAhbInfo->buffer;
            diagnosticStreamType = STREAM_HANDLE_TYPE_PLATFORM_AHB;
        }
#endif
"""
GUEST_AHB_NEW = """#ifdef __ANDROID__
        const auto* diagnosticAhbInfo = vk_find_struct<VkImportAndroidHardwareBufferInfoANDROID>(&localAllocInfo);
        if (diagnosticAhbInfo) {
            diagnosticAhb = diagnosticAhbInfo->buffer;
            diagnosticStreamType = STREAM_HANDLE_TYPE_PLATFORM_AHB;
            // AHB-CRASH-TRACE-GUEST-AHB-BEGIN
            if (diagnosticAhbInfo->buffer != nullptr) {
                GFXSTREAM_AHB_TRACE("AHB-LIFE guest-import", diagnosticAhbInfo->buffer, 0);
            } else {
                GFXSTREAM_AHB_TRACE("AHB-LIFE guest-import null", nullptr,
                                    STREAM_HANDLE_TYPE_PLATFORM_AHB);
            }
            // AHB-CRASH-TRACE-GUEST-AHB-END
        }
#endif
"""
edit(DECODER, GUEST_AHB_OLD, GUEST_AHB_NEW, "AHB-CRASH-TRACE-GUEST-AHB", required=False)

# ---------------------------------------------------------------------------
# 10. AHB-first buffer allocation (runtime-disabled by GFXSTREAM_AHB_FIRST=0,
#     but trace it so an accidental enable is visible in the ring).
# ---------------------------------------------------------------------------
FIRST_OLD = """        AHardwareBuffer* ahb = nullptr;
        if (AHardwareBuffer_allocate(&desc, &ahb) != 0 || !ahb) {
            return nullptr;
        }
        return ahb;
"""
FIRST_NEW = """        AHardwareBuffer* ahb = nullptr;
        // AHB-CRASH-TRACE-FIRST-BEGIN
        GFXSTREAM_AHB_TRACE("AHB-LIFE alloc-ret enter", nullptr,
                            static_cast<unsigned long long>(size));
        // AHB-CRASH-TRACE-FIRST-END
        int ahbRes = AHardwareBuffer_allocate(&desc, &ahb);
        GFXSTREAM_AHB_TRACE("AHB-LIFE alloc-ret", ahb,
                            static_cast<unsigned long long>(ahbRes));
        if (ahbRes != 0 || !ahb) {
            return nullptr;
        }
        return ahb;
"""
edit(DECODER, FIRST_OLD, FIRST_NEW, "AHB-CRASH-TRACE-FIRST")

FIRST_IMPORT_OLD = """                        importInfo.buffer = ahb;
                        vk_append_struct(&structChainIter, &importInfo);
"""
FIRST_IMPORT_NEW = """                        importInfo.buffer = ahb;
                        // AHB-CRASH-TRACE-FIRST-IMPORT-BEGIN
                        GFXSTREAM_AHB_TRACE("AHB-LIFE ahbfirst-import buffer",
                                            importInfo.buffer, ahbBytes);
                        // AHB-CRASH-TRACE-FIRST-IMPORT-END
                        vk_append_struct(&structChainIter, &importInfo);
"""
edit(DECODER, FIRST_IMPORT_OLD, FIRST_IMPORT_NEW, "AHB-CRASH-TRACE-FIRST-IMPORT")

FIRST_RELEASE_OLD = """                        if (ahb) AHardwareBuffer_release(ahb);
"""
FIRST_RELEASE_NEW = """                        // AHB-CRASH-TRACE-FIRST-RELEASE-BEGIN
                        if (ahb) {
                            GFXSTREAM_AHB_TRACE("AHB-LIFE release", ahb, 0);
                            AHardwareBuffer_release(ahb);
                        } else {
                            GFXSTREAM_AHB_TRACE("AHB-LIFE release skip-null", nullptr, 0);
                        }
                        // AHB-CRASH-TRACE-FIRST-RELEASE-END
"""
edit(DECODER, FIRST_RELEASE_OLD, FIRST_RELEASE_NEW, "AHB-CRASH-TRACE-FIRST-RELEASE")

# ---------------------------------------------------------------------------
# 11. AHB-first teardown: the driver has released the memory that imported the
#     AHB; dropping the reference here must be visible in the ring.
# ---------------------------------------------------------------------------
DESTROY_RELEASE_OLD = """        if (memoryInfo.ahbBacking) {
            AHardwareBuffer_release(reinterpret_cast<AHardwareBuffer*>(memoryInfo.ahbBacking));
"""
DESTROY_RELEASE_NEW = """        if (memoryInfo.ahbBacking) {
            // AHB-CRASH-TRACE-DESTROY-RELEASE-BEGIN
            GFXSTREAM_AHB_TRACE("AHB-LIFE release",
                                reinterpret_cast<void*>(memoryInfo.ahbBacking), 0);
            // AHB-CRASH-TRACE-DESTROY-RELEASE-END
            AHardwareBuffer_release(reinterpret_cast<AHardwareBuffer*>(memoryInfo.ahbBacking));
"""
edit(DECODER, DESTROY_RELEASE_OLD, DESTROY_RELEASE_NEW, "AHB-CRASH-TRACE-DESTROY-RELEASE")

# ---------------------------------------------------------------------------
# 12. Blob descriptor teardown in host/common can own a PLATFORM_AHB handle.
# ---------------------------------------------------------------------------
OBJECT_MANAGER_RELEASE_OLD = """        case STREAM_HANDLE_TYPE_PLATFORM_AHB:
            AHardwareBuffer_release(
                reinterpret_cast<AHardwareBuffer*>(descriptorInfo.handle));
"""
OBJECT_MANAGER_RELEASE_NEW = """        case STREAM_HANDLE_TYPE_PLATFORM_AHB:
            // AHB-CRASH-TRACE-OBJECT-MANAGER-RELEASE-BEGIN
            GFXSTREAM_AHB_TRACE("AHB-LIFE release",
                                reinterpret_cast<void*>(descriptorInfo.handle), 0);
            // AHB-CRASH-TRACE-OBJECT-MANAGER-RELEASE-END
            AHardwareBuffer_release(
                reinterpret_cast<AHardwareBuffer*>(descriptorInfo.handle));
"""
edit(OBJECT_MANAGER, OBJECT_MANAGER_RELEASE_OLD, OBJECT_MANAGER_RELEASE_NEW,
     "AHB-CRASH-TRACE-OBJECT-MANAGER-RELEASE")

print("AHB lifecycle tracer installed (default off; GFXSTREAM_CRASH_TRACE=1 enables)")
