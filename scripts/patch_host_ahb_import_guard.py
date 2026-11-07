#!/usr/bin/env python3
"""P2c host patch: refuse an AHB import with empty memory-type intersection, but
keep the ColorBuffer alive on the image's own memory type, and add print-only
P3 bind diagnostics.

P2c semantics (host/vulkan/vk_common_operations.cpp, allocExternalMemory()):

  * When ahbProps.memoryTypeBits & imageMemReqs.memoryTypeBits == 0, the AHB is
    still rejected (released, never imported), but the allocation continues on
    the image's own memory type (getValidMemoryTypeIndex(imageMemReqs.memoryTypeBits))
    instead of falling into the non-exportable failure path.  Mali reports
    imageMemReqs.size == 0 for these images, so ahbProps.allocationSize is used
    as the allocation size.
  * The export switch is skipped explicitly for that allocation.  AndroidAHB
    mode has no VkExportMemoryAllocateInfo, so vkGetMemoryAndroidHardwareBufferANDROID
    on a memory without AHB backing dereferences a null AHB field in Mali.  A
    plain `break` out of the pre-allocation switch is not enough: the export
    switch runs after vkAllocateMemory() and would fail the ColorBuffer.
  * handleInfo stays empty, so the ColorBuffer is created without a guest-visible
    handle (the intended P2c observable).

P3 (print-only, gated by GFXSTREAM_BIND_DIAG=1, no return-value change):

  * createVkColorBufferLocked(): before vkBindImageMemory, print the image
    requirements bits, final host memory type index and size.
  * performBindImageMemory() in vk_decoder_global_state.cpp: query
    vkGetImageMemoryRequirements once per bind and print image bits, the memory
    type bit/index the guest memory was allocated with, import stream type, AHB
    pointer and sizes.  This answers whether a guest modifier image is bound to
    a memory type its own requirements contain.

Every anchor must occur exactly once and all post-checks must pass; a mismatch
aborts the build and leaves both files untouched.
"""

import sys
from pathlib import Path

OPS = Path("host/vulkan/vk_common_operations.cpp")
DEC = Path("host/vulkan/vk_decoder_global_state.cpp")

# ---------------------------------------------------------------- P2c: flag

FLAG_OLD = b"""    auto allocInfoChain = vk_make_chain_iterator(&allocInfo);
"""

FLAG_NEW = b"""    auto allocInfoChain = vk_make_chain_iterator(&allocInfo);

    // P2c: set when an AHB import is refused (empty memory-type intersection) but the
    // ColorBuffer is still allocated on the image's own memory type. Such an allocation
    // has no AHB backing, so the export switch below must be skipped, not failed.
    bool p2cNonExportable = false;
"""

# ------------------------------------------------- P2c: empty-intersection arm

AHB_OLD = b'''                // Find a memory type that satisfies both the image and AHB requirements
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

AHB_NEW = b'''                // Find a memory type that satisfies both the image and AHB requirements
                uint32_t combinedBits = ahbProps.memoryTypeBits &
                                        cbInfoPtr->imageMemReqs.memoryTypeBits;
                if (!combinedBits) {
                    // P2c: never bind an image to an AHB whose memory types do not
                    // intersect the image requirements. Unlike the upstream fallback, keep
                    // the ColorBuffer alive by allocating on the image's own memory type:
                    // Mali reports imageMemReqs.size == 0 here, so the AHB allocationSize is
                    // the only usable size. handleInfo is deliberately left empty and the
                    // export switch is skipped below (there is no AHB backing to export).
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
                    p2cNonExportable = true;
                    info->typeIndex = getValidMemoryTypeIndex(
                        cbInfoPtr->imageMemReqs.memoryTypeBits, cbInfoPtr->memoryProperty);
                    allocInfo.memoryTypeIndex = info->typeIndex;
                    info->size = ahbProps.allocationSize;
                    allocInfo.allocationSize = ahbProps.allocationSize;
                    GFXSTREAM_WARNING(
                        "GFXDIAG: P2C non-exportable alloc cb=%u imageBits=0x%x ahbBits=0x%x "
                        "memindex=%u size=%llu imageSize=%llu",
                        cbInfoPtr->handle, cbInfoPtr->imageMemReqs.memoryTypeBits,
                        ahbProps.memoryTypeBits, info->typeIndex,
                        (unsigned long long)info->size,
                        (unsigned long long)cbInfoPtr->imageMemReqs.size);
                    break;
                }
'''

# ------------------------------------------------------- P2c: skip the export

EXPORT_OLD = b"""    if (!mDeviceInfo.supportsExternalMemoryExport) {
        return true;
    }

    uint32_t streamHandleType = 0;
"""

EXPORT_NEW = b"""    if (!mDeviceInfo.supportsExternalMemoryExport) {
        return true;
    }

    if (p2cNonExportable) {
        // P2c: the AHB import was refused and this memory is a plain local allocation
        // with no AHB backing. Do not enter the export switch: Mali's
        // vkGetMemoryAndroidHardwareBufferANDROID dereferences the memory's AHB field,
        // which is only written by an AHB import/export. handleInfo stays empty on
        // purpose, and ColorBuffer creation keeps its success result.
        GFXSTREAM_WARNING("GFXDIAG: P2C skip-export memindex=%u size=%llu handleInfo=none",
                          info->typeIndex, (unsigned long long)info->size);
        return true;
    }

    uint32_t streamHandleType = 0;
"""

# ------------------------------------------------------- P3: host ColorBuffer bind

CB_BIND_OLD = b"""    VkResult bindImageMemoryRes = vk->vkBindImageMemory(
        mDevice, infoPtr->image, infoPtr->memory.memory, infoPtr->memory.bindOffset);
"""

CB_BIND_NEW = b"""    // P3 (print-only, gated by the bind-diagnostic env switch): image requirements next
    // to the final host memory type and allocation size, for correlating ColorBuffer
    // binds with the P2c refusal lines. No return value or driver call is affected.
    static const bool p3BindDiag = [] {
        const std::string value = gfxstream::base::getEnvironmentVariable("GFXSTREAM_BIND_DIAG");
        return !value.empty() && value != "0";
    }();
    if (p3BindDiag) {
        GFXSTREAM_WARNING(
            "GFXDIAG: P3 CB-BIND image=%p imageBits=0x%x memindex=%u size=%llu "
            "externalTypes=0x%x handle=%lld",
            (void*)infoPtr->image, infoPtr->imageMemReqs.memoryTypeBits,
            infoPtr->memory.typeIndex, (unsigned long long)infoPtr->memory.size,
            extImageCi.handleTypes,
            infoPtr->memory.handleInfo ? (long long)infoPtr->memory.handleInfo->handle : 0LL);
    }
    VkResult bindImageMemoryRes = vk->vkBindImageMemory(
        mDevice, infoPtr->image, infoPtr->memory.memory, infoPtr->memory.bindOffset);
"""

# --------------------------------------------------- P3: guest bind (decoder)

GUEST_HEAD_OLD = b"""        VALIDATE_REQUIRED_HANDLE(memory);
        {
            std::lock_guard<std::mutex> diagnosticLock(mMutex);
"""

GUEST_HEAD_NEW = b"""        VALIDATE_REQUIRED_HANDLE(memory);

        // P3 (print-only, gated by the bind-diagnostic env switch): query the image's
        // requirements once so the bind diagnostic can compare them with the memory type
        // the guest memory was allocated with. No driver state changes and no return value
        // is affected.
        static const bool p3BindDiag = [] {
            const std::string value =
                gfxstream::base::getEnvironmentVariable("GFXSTREAM_BIND_DIAG");
            return !value.empty() && value != "0";
        }();
        VkMemoryRequirements p3ImageReqs{};
        if (p3BindDiag) {
            vk->vkGetImageMemoryRequirements(device, image, &p3ImageReqs);
        }

        {
            std::lock_guard<std::mutex> diagnosticLock(mMutex);
"""

GUEST_TAIL_OLD = b"""                    diagnosticMemory->boundColorBuffer ? *diagnosticMemory->boundColorBuffer : 0,
                    (long long)diagnosticMemory->poolOffset);
            }
"""

GUEST_TAIL_NEW = b"""                    diagnosticMemory->boundColorBuffer ? *diagnosticMemory->boundColorBuffer : 0,
                    (long long)diagnosticMemory->poolOffset);
                if (p3BindDiag) {
                    const uint32_t memIndex = diagnosticMemory->memoryIndex;
                    const uint32_t memBit =
                        memIndex < 32 ? (uint32_t{1} << memIndex) : 0u;
                    const bool legal =
                        memIndex < 32 && (p3ImageReqs.memoryTypeBits & memBit) != 0;
                    GFXSTREAM_WARNING(
                        "GFXDIAG: P3 GUEST-BIND image=%p mem=%p imageBits=0x%x reqSize=%llu "
                        "memindex=%u memBit=0x%x importType=0x%x ahb=%p size=%llu %s",
                        (void*)image, (void*)memory, p3ImageReqs.memoryTypeBits,
                        (unsigned long long)p3ImageReqs.size, memIndex, memBit,
                        diagnosticMemory->diagnosticStreamType, diagnosticMemory->diagnosticAhb,
                        (unsigned long long)diagnosticMemory->size,
                        legal ? "LEGAL" : "MISMATCH");
                }
            }
"""

MARKERS = [
    b"GFXDIAG: reject AHB import",
    b"GFXDIAG: P2C non-exportable alloc",
    b"GFXDIAG: P2C skip-export",
    b"GFXDIAG: P3 CB-BIND",
    b"GFXDIAG: P3 GUEST-BIND",
]


def fail(message):
    print(f"patch_host_ahb_import_guard: FAIL: {message}", file=sys.stderr)
    raise SystemExit(1)


def replace_once(data, old, new, label):
    count = data.count(old)
    if count != 1:
        fail(f"anchor '{label}' count is {count}, expected exactly 1")
    return data.replace(old, new, 1)


def main():
    if len(sys.argv) != 2:
        fail("usage: patch_host_ahb_import_guard.py <source-root>")

    root = Path(sys.argv[1])
    ops_path = root / OPS
    dec_path = root / DEC
    for path in (ops_path, dec_path):
        if not path.is_file():
            fail(f"missing {path}")

    ops = ops_path.read_bytes()
    dec = dec_path.read_bytes()

    for marker in MARKERS:
        for name, data in (("vk_common_operations.cpp", ops),
                           ("vk_decoder_global_state.cpp", dec)):
            if data.count(marker) != 0:
                fail(f"{name} already carries '{marker.decode()}'; refusing to patch twice")

    if ops.count(b"Using AHB memory type bits for import.") != 1:
        fail("expected exactly one 'Using AHB memory type bits for import.' before patching")
    if ops.count(b"combinedBits = ahbProps.memoryTypeBits;") != 1:
        fail("expected exactly one forced-AHB-bits assignment before patching")

    ops = replace_once(ops, FLAG_OLD, FLAG_NEW, "allocInfoChain")
    ops = replace_once(ops, AHB_OLD, AHB_NEW, "AHB empty-intersection arm")
    ops = replace_once(ops, EXPORT_OLD, EXPORT_NEW, "export switch prologue")
    ops = replace_once(ops, CB_BIND_OLD, CB_BIND_NEW, "ColorBuffer bind")
    dec = replace_once(dec, GUEST_HEAD_OLD, GUEST_HEAD_NEW, "guest bind prologue")
    dec = replace_once(dec, GUEST_TAIL_OLD, GUEST_TAIL_NEW, "guest bind diagnostic tail")

    checks = [
        (ops.count(b"GFXDIAG: reject AHB import"), 1, "reject-import diagnostics"),
        (ops.count(b"GFXDIAG: P2C non-exportable alloc"), 1, "P2c allocation diagnostics"),
        (ops.count(b"GFXDIAG: P2C skip-export"), 1, "P2c export-skip diagnostics"),
        (ops.count(b"GFXDIAG: P3 CB-BIND"), 1, "P3 ColorBuffer bind diagnostics"),
        (ops.count(b"p2cNonExportable"), 3, "P2c flag declaration/set/skip"),
        (ops.count(b"No common memory type for AHB"), 1,
         "'No common memory type for AHB' kept exactly once"),
        (ops.count(b"Falling back to non-exportable allocation."), 2,
         "non-exportable fallback string occurrences (AHB size guard + P2c)"),
        (ops.count(b"Using AHB memory type bits for import."), 0,
         "forced-AHB-memory-type tail removed"),
        (ops.count(b"combinedBits = ahbProps.memoryTypeBits;"), 0,
         "no forced AHB memoryTypeBits assignment left"),
        (dec.count(b"GFXDIAG: P3 GUEST-BIND"), 1, "P3 guest bind diagnostics"),
        (dec.count(b"GFXSTREAM_BIND_DIAG"), 1, "P3 guest bind diagnostics gate"),
        (ops.count(b"GFXSTREAM_BIND_DIAG"), 1, "P3 ColorBuffer bind diagnostics gate"),
    ]
    for actual, expected, description in checks:
        if actual != expected:
            fail(f"post-check {description}: count {actual}, expected {expected}")

    if ops == ops_path.read_bytes() or dec == dec_path.read_bytes():
        fail("replacement produced no change")

    ops_path.write_bytes(ops)
    dec_path.write_bytes(dec)

    ops_line = ops[: ops.index(b"GFXDIAG: reject AHB import")].count(b"\n") + 1
    dec_line = dec[: dec.index(b"GFXDIAG: P3 GUEST-BIND")].count(b"\n") + 1
    print(f"patch_host_ahb_import_guard: patched {ops_path} at line {ops_line} and "
          f"{dec_path} at line {dec_line}")


if __name__ == "__main__":
    main()
