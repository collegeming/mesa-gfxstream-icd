#!/usr/bin/env python3
"""Install the diagnostic-only AHB crash stack tracer.

The tracer is a header-only signal handler and breadcrumb ring in
host/vulkan/ahb_crash_trace.h. It is compiled into libgfxstream_backend.so by
including it from host/vulkan/vk_common_operations.cpp. Behaviour is unchanged
unless the process environment contains exactly GFXSTREAM_CRASH_TRACE=1; the
handler then prints raw PCs with module load offsets and the last AHB
breadcrumbs to stderr (or GFXSTREAM_CRASH_TRACE_FILE), and chains to the
previously installed handler so crosvm's own crash dump and _exit(128+sig)
still happen.

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
assert "GFXSTREAM_CRASH_TRACE" in header_text
assert "_Unwind_Backtrace" in header_text
if HEADER_TARGET.is_file():
    if HEADER_TARGET.read_text(encoding="utf-8") != header_text:
        raise SystemExit(f"{HEADER_TARGET}: exists but does not match the tracer header")
    print("already copied host/vulkan/ahb_crash_trace.h")
else:
    HEADER_TARGET.write_text(header_text, encoding="utf-8")
    print("copied host/vulkan/ahb_crash_trace.h")

# ---------------------------------------------------------------------------
# 1. Include the header from the one translation unit that already carries the
#    AHB code. The killswitch patch puts this include block at global scope.
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

# The decoder defines its own AHB import breadcrumbs, so it needs the same
# header. Every definition in the header is inline or function-local; the two
# translation units therefore share one handler, one maps table and one ring.
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

# ---------------------------------------------------------------------------
# 2. The explicit ColorBuffer AHB allocation. allocAhb() is the only place that
#    turns a guest image shape into AHardwareBuffer_allocate().
# ---------------------------------------------------------------------------
ALLOC_OLD = """    AHardwareBuffer* ahb = nullptr;
    int ahbRes = AHardwareBuffer_allocate(&desc, &ahb);
"""
ALLOC_NEW = """    AHardwareBuffer* ahb = nullptr;
    // AHB-CRASH-TRACE-ALLOC-BEGIN
    GFXSTREAM_AHB_TRACE("allocAhb.enter", nullptr,
                        (static_cast<unsigned long long>(imageCreateInfo->extent.width) << 32) |
                            imageCreateInfo->extent.height);
    // AHB-CRASH-TRACE-ALLOC-END
    int ahbRes = AHardwareBuffer_allocate(&desc, &ahb);
    GFXSTREAM_AHB_TRACE("allocAhb.result", ahb, static_cast<unsigned long long>(ahbRes));
"""
edit(OPS, ALLOC_OLD, ALLOC_NEW, "AHB-CRASH-TRACE-ALLOC")

# ---------------------------------------------------------------------------
# 3. The deferred-layout AHB is the second owner of an AHB reference and calls
#    AHardwareBuffer_acquire() from gfxstream itself.
# ---------------------------------------------------------------------------
STASH_OLD = """void VkEmulation::stashPendingDeferredLayoutAhb(const VkImageCreateInfo* imageCreateInfo,
                                                AHardwareBuffer* ahb) {
    AHardwareBuffer_acquire(ahb);
"""
STASH_NEW = """void VkEmulation::stashPendingDeferredLayoutAhb(const VkImageCreateInfo* imageCreateInfo,
                                                AHardwareBuffer* ahb) {
    // AHB-CRASH-TRACE-STASH-BEGIN
    GFXSTREAM_AHB_TRACE("stash.acquire", ahb, 0);
    // AHB-CRASH-TRACE-STASH-END
    AHardwareBuffer_acquire(ahb);
"""
edit(OPS, STASH_OLD, STASH_NEW, "AHB-CRASH-TRACE-STASH", required=False)

# ---------------------------------------------------------------------------
# 4. The ColorBuffer allocation imports the AHB into the host driver; the
#    driver calls AHardwareBuffer_acquire() on this pointer.
# ---------------------------------------------------------------------------
IMPORT_CB_OLD = """                importAhbInfo.buffer = ahb;
                vk_append_struct(&allocInfoChain, &importAhbInfo);
"""
IMPORT_CB_NEW = """                // AHB-CRASH-TRACE-IMPORT-CB-BEGIN
                GFXSTREAM_AHB_TRACE("allocExternalMemory.import", ahb,
                                    allocInfo.memoryTypeIndex);
                // AHB-CRASH-TRACE-IMPORT-CB-END
                importAhbInfo.buffer = ahb;
                vk_append_struct(&allocInfoChain, &importAhbInfo);
"""
edit(OPS, IMPORT_CB_OLD, IMPORT_CB_NEW, "AHB-CRASH-TRACE-IMPORT-CB")

# ---------------------------------------------------------------------------
# 5. The buffer-alias importer takes its own reference before the driver sees
#    the AHB. This hunk only exists after patch_buffer_alias_failopen.py.
# ---------------------------------------------------------------------------
ALIAS_OLD = """                    importedAhb = reinterpret_cast<AHardwareBuffer*>(handleInfo->handle);
                    if (!importedAhb) return false;
                    AHardwareBuffer_acquire(importedAhb);
"""
ALIAS_NEW = """                    importedAhb = reinterpret_cast<AHardwareBuffer*>(handleInfo->handle);
                    if (!importedAhb) return false;
                    // AHB-CRASH-TRACE-ALIAS-BEGIN
                    GFXSTREAM_AHB_TRACE("importExternalMemory.importedAhb", importedAhb,
                                        handleInfo->streamHandleType);
                    // AHB-CRASH-TRACE-ALIAS-END
                    AHardwareBuffer_acquire(importedAhb);
"""
edit(OPS, ALIAS_OLD, ALIAS_NEW, "AHB-CRASH-TRACE-ALIAS", required=False)

# ---------------------------------------------------------------------------
# 6. Decoder-side ColorBuffer/Buffer AHB import (guest handle duplicated by
#    gfxstream and placed in VkImportAndroidHardwareBufferInfoANDROID).
# ---------------------------------------------------------------------------
DECODER_CB_OLD = """                    importInfo.buffer = static_cast<AHardwareBuffer*>(
                        reinterpret_cast<void*>(dupHandleInfo->handle));
"""
DECODER_CB_NEW = """                    // AHB-CRASH-TRACE-DECODER-CB-BEGIN
                    GFXSTREAM_AHB_TRACE("decoder.import.dupHandle",
                                        reinterpret_cast<void*>(dupHandleInfo->handle), 0);
                    // AHB-CRASH-TRACE-DECODER-CB-END
                    importInfo.buffer = static_cast<AHardwareBuffer*>(
                        reinterpret_cast<void*>(dupHandleInfo->handle));
"""
edit(DECODER, DECODER_CB_OLD, DECODER_CB_NEW, "AHB-CRASH-TRACE-DECODER-CB", required=False)

# ---------------------------------------------------------------------------
# 7. Decoder-side guest handle import, after the null-handle guard. The trace
#    records the exact handle the driver is about to acquire.
# ---------------------------------------------------------------------------
DECODER_HANDLE_OLD = """                    if (handleInfo.handle == 0) {
                        GFXSTREAM_ERROR(
                            "AHB-NULL-GUARD: refusing to import a null AHardwareBuffer handle");
                        return false;
                    }
                    importInfo.buffer = reinterpret_cast<AHardwareBuffer*>(handleInfo.handle);
"""
DECODER_HANDLE_NEW = """                    // AHB-CRASH-TRACE-DECODER-HANDLE-BEGIN
                    GFXSTREAM_AHB_TRACE("decoder.import.handleInfo",
                                        reinterpret_cast<void*>(handleInfo.handle),
                                        handleInfo.streamHandleType);
                    // AHB-CRASH-TRACE-DECODER-HANDLE-END
                    if (handleInfo.handle == 0) {
                        GFXSTREAM_ERROR(
                            "AHB-NULL-GUARD: refusing to import a null AHardwareBuffer handle");
                        return false;
                    }
                    importInfo.buffer = reinterpret_cast<AHardwareBuffer*>(handleInfo.handle);
"""
edit(DECODER, DECODER_HANDLE_OLD, DECODER_HANDLE_NEW, "AHB-CRASH-TRACE-DECODER-HANDLE",
     required=True)

# ---------------------------------------------------------------------------
# 7b. A guest-supplied VkImportAndroidHardwareBufferInfoANDROID in the pNext
#     chain is forwarded to the driver untouched. This is the one AHB import
#     site the null-handle guards do not cover; record its buffer value.
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
            GFXSTREAM_AHB_TRACE("decoder.guestAhbImport", diagnosticAhbInfo->buffer, 0);
            // AHB-CRASH-TRACE-GUEST-AHB-END
        }
#endif
"""
edit(DECODER, GUEST_AHB_OLD, GUEST_AHB_NEW, "AHB-CRASH-TRACE-GUEST-AHB", required=False)

# ---------------------------------------------------------------------------
# 8. AHB-first buffer allocation (runtime-disabled by GFXSTREAM_AHB_FIRST=0,
#    but trace it so an accidental enable is visible in the ring).
# ---------------------------------------------------------------------------
FIRST_OLD = """        AHardwareBuffer* ahb = nullptr;
        if (AHardwareBuffer_allocate(&desc, &ahb) != 0 || !ahb) {
            return nullptr;
        }
        return ahb;
"""
FIRST_NEW = """        AHardwareBuffer* ahb = nullptr;
        // AHB-CRASH-TRACE-FIRST-BEGIN
        GFXSTREAM_AHB_TRACE("allocateAhbFirstMemory.enter", nullptr,
                            static_cast<unsigned long long>(size));
        // AHB-CRASH-TRACE-FIRST-END
        if (AHardwareBuffer_allocate(&desc, &ahb) != 0 || !ahb) {
            return nullptr;
        }
        GFXSTREAM_AHB_TRACE("allocateAhbFirstMemory.result", ahb,
                            static_cast<unsigned long long>(size));
        return ahb;
"""
edit(DECODER, FIRST_OLD, FIRST_NEW, "AHB-CRASH-TRACE-FIRST", required=True)

print("AHB crash stack tracer installed (default off; GFXSTREAM_CRASH_TRACE=1 enables)")
