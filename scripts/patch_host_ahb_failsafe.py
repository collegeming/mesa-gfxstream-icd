#!/usr/bin/env python3
"""E1 host patch: AHB fail-safe with an image contract, plus AHB property probes.

Ported semantics from upstream gfxstream 5abb038f (AHB allocate + import) and the
P2 guard (release the AHB and refuse the import when the memoryTypeBits sets do not
intersect). Upstream then falls back to its export path; this driver cannot export
at all (D4BX) and its export entry point dereferences an unset AHB field, so the
fallback here ends at a plain, non-exportable ColorBuffer instead:

  1. probe the gralloc AHB for the image shape and print AHB-PROPS / AHB-IMAGE-BITS;
  2. when the sets are disjoint (or the AHB is unusable), print AHB-MEMTYPE-DISJOINT,
     destroy the image that declared the AHB external type and recreate it without
     any external declaration, then re-read its memory requirements;
  3. mark the ColorBuffer ahbImportUnavailable so allocExternalMemory allocates
     plain memory, skips the AHB allocation and skips the export switch.

Without step 2 the image keeps an AHB external type that no memory satisfies and
Mali faults inside vkBindImageMemory (P2c, SIGSEGV at AHardwareBuffer_acquire).

Every anchor must occur exactly once; an already patched tree is rejected.
"""

import sys
from pathlib import Path

CPP = Path("host/vulkan/vk_common_operations.cpp")
HDR = Path("host/vulkan/vk_common_operations.h")

# ---------------------------------------------------------------- header field

HDR_OLD = b'''        bool glExported = false;
        bool externalMemoryCompatible = false;

        VulkanMode vulkanMode = VulkanMode::Default;'''

HDR_NEW = b'''        bool glExported = false;
        bool externalMemoryCompatible = false;
        // AHB fail-safe: the image was recreated without the platform AHB external type
        // because no memory type is shared with the gralloc AHB. Allocation must stay on
        // the image's own requirements and no AHB/export call may run for this buffer.
        bool ahbImportUnavailable = false;

        VulkanMode vulkanMode = VulkanMode::Default;'''

# ------------------------------------------------- AHB branch skip (allocation)

ALLOC_OLD = b'''            if (colorBufferInfo) {
                auto cbInfoPtr = *colorBufferInfo;

                AHardwareBuffer* ahb = allocAhb(&cbInfoPtr->imageCreateInfoShallow);'''

ALLOC_NEW = b'''            if (colorBufferInfo) {
                auto cbInfoPtr = *colorBufferInfo;

                if (cbInfoPtr->ahbImportUnavailable) {
                    // AHB fail-safe: createVkColorBufferLocked() already found that the
                    // gralloc AHB and this image share no memory type, and recreated the
                    // image without the AHB external type. Allocate on the image's own
                    // requirements and do not ask gralloc for a second AHB.
                    GFXSTREAM_WARNING(
                        "AHB-FAILSAFE-SKIP cb=%u imageBits=0x%x imageSize=%llu memindex=%u",
                        cbInfoPtr->handle, cbInfoPtr->imageMemReqs.memoryTypeBits,
                        (unsigned long long)cbInfoPtr->imageMemReqs.size, info->typeIndex);
                    break;
                }

                AHardwareBuffer* ahb = allocAhb(&cbInfoPtr->imageCreateInfoShallow);'''

# --------------------------------------------- export skip (after allocation)

EXPORT_OLD = b'''    if (!mDeviceInfo.supportsExternalMemoryExport) {
        return true;
    }

    uint32_t streamHandleType = 0;'''

EXPORT_NEW = b'''    if (!mDeviceInfo.supportsExternalMemoryExport) {
        return true;
    }

    if (colorBufferInfo && (*colorBufferInfo)->ahbImportUnavailable) {
        // AHB fail-safe: this ColorBuffer has no AHB backing. Both export routes are
        // unusable here -- the AHB path because Mali dereferences the unset AHB field of
        // a plain allocation, and the fd path because Mali advertises no exportable
        // external format (D4BX). Report the non-exportable state and keep the buffer.
        GFXSTREAM_WARNING(
            "AHB-FAILSAFE-NO-EXPORT cb=%u memindex=%u size=%llu handleInfo=none",
            (*colorBufferInfo)->handle, info->typeIndex,
            (unsigned long long)info->size);
        return true;
    }

    uint32_t streamHandleType = 0;'''

# ------------------------------------------------------ probe + downgrade site

SITE_OLD = b'''        vk->vkGetImageMemoryRequirements(mDevice, infoPtr->image, &infoPtr->imageMemReqs);
    }

    // Currently we only care about two memory properties: DEVICE_LOCAL'''

SITE_NEW = b'''        vk->vkGetImageMemoryRequirements(mDevice, infoPtr->image, &infoPtr->imageMemReqs);
    }

#ifdef __ANDROID__
    // AHB fail-safe (E1). An AHardwareBuffer whose memoryTypeBits do not intersect this
    // image's requirements can never be imported, and binding the image to the AHB's own
    // memory type anyway is what loses the Mali device. Upstream gfxstream detaches the
    // AHB and then re-exports the memory; this driver cannot export, so the fallback has
    // to end at a plain ColorBuffer. The decision belongs here: the image can still be
    // recreated without the external declaration, before any memory exists.
    if (getExternalMemoryMode() == ExternalMemory::Mode::AndroidAHB &&
        extImageCi.handleTypes != 0) {
        probeAhbShapeMatrix(vk, mDevice);

        AHardwareBuffer* probeAhb = allocAhb(imageCi.get());
        VkAndroidHardwareBufferPropertiesANDROID probeProps = {
            .sType = VK_STRUCTURE_TYPE_ANDROID_HARDWARE_BUFFER_PROPERTIES_ANDROID,
        };
        VkResult probeRes = VK_ERROR_INITIALIZATION_FAILED;
        if (probeAhb != nullptr) {
            probeRes = vk->vkGetAndroidHardwareBufferPropertiesANDROID(mDevice, probeAhb,
                                                                       &probeProps);
        }
        AHardwareBuffer_Desc probeDesc = {};
        if (probeAhb != nullptr) {
            AHardwareBuffer_describe(probeAhb, &probeDesc);
        }
        // AFBC is not part of the AHB API. gralloc appends whole 4 KiB header pages to the
        // pixel payload, so a stride*height payload at least one page short of
        // allocationSize is reported as compressed. Both values are printed so the guess
        // can be checked against gralloc's own "afbc header page num" line.
        // allocAhb() only ever requests RGBA_8888/BGRA_8888, so bytes per pixel is 4.
        const uint64_t probeLinear =
            static_cast<uint64_t>(probeDesc.stride) * probeDesc.height * 4u;
        const uint64_t probePad =
            (probeProps.allocationSize > probeLinear) ? probeProps.allocationSize - probeLinear
                                                      : 0u;
        GFXSTREAM_WARNING(
            "AHB-PROPS cb=%u format=0x%x usage=0x%llx stride=%u layers=%u afbc=%d bits=0x%x "
            "size=%llu imageBits=0x%x imageSize=%llu pad=%llu",
            colorBufferHandle, probeDesc.format, (unsigned long long)probeDesc.usage,
            probeDesc.stride, probeDesc.layers, (int)(probeAhb != nullptr && probePad >= 4096u),
            probeProps.memoryTypeBits, (unsigned long long)probeProps.allocationSize,
            infoPtr->imageMemReqs.memoryTypeBits,
            (unsigned long long)infoPtr->imageMemReqs.size, (unsigned long long)probePad);
        GFXSTREAM_WARNING(
            "AHB-IMAGE-BITS cb=%u fmt=%d %ux%u tiling=%d bits=0x%x size=%llu ahbRes=%d",
            colorBufferHandle, (int)vkFormat, width, height, (int)imageCi->tiling,
            infoPtr->imageMemReqs.memoryTypeBits,
            (unsigned long long)infoPtr->imageMemReqs.size, (int)probeRes);

        const bool probeUsable =
            probeAhb != nullptr && probeRes == VK_SUCCESS &&
            (probeProps.memoryTypeBits & infoPtr->imageMemReqs.memoryTypeBits) != 0;
        if (probeAhb != nullptr) {
            AHardwareBuffer_release(probeAhb);
        }
        if (!probeUsable) {
            // Two failures live here: the sets do not intersect, or the AHB itself could
            // not be allocated/queried (ahbRes reports the query). Both leave the image
            // unable to be backed by an AHB.
            GFXSTREAM_WARNING(
                "AHB-MEMTYPE-DISJOINT cb=%u ahbBits=0x%x imageBits=0x%x ahbSize=%llu "
                "imageSize=%llu fmt=%d %ux%u ahbRes=%d -> plain ColorBuffer",
                colorBufferHandle, probeProps.memoryTypeBits,
                infoPtr->imageMemReqs.memoryTypeBits,
                (unsigned long long)probeProps.allocationSize,
                (unsigned long long)infoPtr->imageMemReqs.size, (int)vkFormat, width, height,
                (int)probeRes);

            vk->vkDestroyImage(mDevice, infoPtr->image, nullptr);
            infoPtr->image = VK_NULL_HANDLE;
            extImageCi.handleTypes = 0;
            imageCi->pNext = nullptr;
            VkResult plainRes =
                vk->vkCreateImage(mDevice, imageCi.get(), nullptr, &infoPtr->image);
            if (plainRes != VK_SUCCESS) {
                GFXSTREAM_ERROR("AHB fail-safe: plain image recreate failed: %s",
                                string_VkResult(plainRes));
                return false;
            }
            vk->vkGetImageMemoryRequirements(mDevice, infoPtr->image, &infoPtr->imageMemReqs);
            infoPtr->imageCreateInfoShallow = vk_make_orphan_copy(*imageCi);
            infoPtr->ahbImportUnavailable = true;
        }
    }
#endif

    // Currently we only care about two memory properties: DEVICE_LOCAL'''

# ------------------------------------------------------------ matrix helper

MATRIX_OLD = b'''bool VkEmulation::createVkColorBufferLocked(uint32_t width, uint32_t height,
                                            GfxstreamFormat format,
                                            uint32_t colorBufferHandle, bool vulkanOnly,
                                            uint32_t memoryProperty, uint32_t mipLevels) {'''

MATRIX_NEW = b'''#ifdef __ANDROID__
// E1 AHB shape matrix. gralloc picks the heap -- and therefore the memoryTypeBits -- from
// format, usage and extent, so "no AHB can be imported" may be specific to the ColorBuffer
// shapes this project happens to use. Before concluding that, ask gralloc for every GPU
// usage the ColorBuffer path can generate, at the two extents already observed on device.
// Bounded: 2 formats x 5 usages x 2 extents, allocated and released one at a time, and only
// when GFXSTREAM_AHB_PROBE_MATRIX=1. The switch is read once, on the first ColorBuffer.
static void probeAhbShapeMatrix(VulkanDispatch* vk, VkDevice device) {
    static std::atomic<bool> sMatrixDone{false};
    if (sMatrixDone.exchange(true)) {
        return;
    }
    if (gfxstream::base::getEnvironmentVariable("GFXSTREAM_AHB_PROBE_MATRIX") != "1") {
        return;
    }
    struct ProbeShape {
        uint32_t width;
        uint32_t height;
    };
    const ProbeShape kShapes[] = {{64, 64}, {1148, 1463}};
    const uint64_t kUsages[] = {
        AHARDWAREBUFFER_USAGE_GPU_FRAMEBUFFER,
        AHARDWAREBUFFER_USAGE_GPU_SAMPLED_IMAGE,
        AHARDWAREBUFFER_USAGE_GPU_FRAMEBUFFER | AHARDWAREBUFFER_USAGE_GPU_SAMPLED_IMAGE,
        AHARDWAREBUFFER_USAGE_GPU_FRAMEBUFFER | AHARDWAREBUFFER_USAGE_GPU_SAMPLED_IMAGE |
            AHARDWAREBUFFER_USAGE_CPU_READ_OFTEN,
        AHARDWAREBUFFER_USAGE_GPU_DATA_BUFFER,
    };
    const uint32_t kFormats[] = {1, 5};
    for (uint32_t matrixFormat : kFormats) {
        for (uint64_t matrixUsage : kUsages) {
            for (const ProbeShape& shape : kShapes) {
                AHardwareBuffer_Desc desc = {};
                desc.width = shape.width;
                desc.height = shape.height;
                desc.layers = 1;
                desc.format = matrixFormat;
                desc.usage = matrixUsage;
                AHardwareBuffer* ahb = nullptr;
                const int allocRes = AHardwareBuffer_allocate(&desc, &ahb);
                VkAndroidHardwareBufferPropertiesANDROID props = {
                    .sType = VK_STRUCTURE_TYPE_ANDROID_HARDWARE_BUFFER_PROPERTIES_ANDROID,
                };
                VkResult propsRes = VK_ERROR_INITIALIZATION_FAILED;
                AHardwareBuffer_Desc got = {};
                if (allocRes == 0 && ahb != nullptr) {
                    propsRes =
                        vk->vkGetAndroidHardwareBufferPropertiesANDROID(device, ahb, &props);
                    AHardwareBuffer_describe(ahb, &got);
                    AHardwareBuffer_release(ahb);
                }
                GFXSTREAM_WARNING(
                    "AHB-MATRIX fmt=%u usage=0x%llx %ux%u allocRes=%d propsRes=%d "
                    "gotFormat=0x%x gotUsage=0x%llx stride=%u bits=0x%x size=%llu",
                    matrixFormat, (unsigned long long)matrixUsage, shape.width, shape.height,
                    allocRes, (int)propsRes, got.format, (unsigned long long)got.usage,
                    got.stride, props.memoryTypeBits, (unsigned long long)props.allocationSize);
            }
        }
    }
}
#endif

bool VkEmulation::createVkColorBufferLocked(uint32_t width, uint32_t height,
                                            GfxstreamFormat format,
                                            uint32_t colorBufferHandle, bool vulkanOnly,
                                            uint32_t memoryProperty, uint32_t mipLevels) {'''

EDITS = [
    (HDR, HDR_OLD, HDR_NEW, "ColorBufferInfo::ahbImportUnavailable"),
    (CPP, ALLOC_OLD, ALLOC_NEW, "AHB allocation skip"),
    (CPP, EXPORT_OLD, EXPORT_NEW, "export skip"),
    (CPP, SITE_OLD, SITE_NEW, "AHB probe + image downgrade"),
    (CPP, MATRIX_OLD, MATRIX_NEW, "AHB shape matrix probe"),
]

MARKERS = [
    b"AHB-PROPS cb=%u",
    b"AHB-IMAGE-BITS cb=%u",
    b"AHB-MEMTYPE-DISJOINT cb=%u",
    b"AHB-FAILSAFE-SKIP cb=%u",
    b"AHB-FAILSAFE-NO-EXPORT cb=%u",
    b"AHB-MATRIX fmt=%u",
    b"GFXSTREAM_AHB_PROBE_MATRIX",
]


def fail(message):
    print(f"patch_host_ahb_failsafe: FAIL: {message}", file=sys.stderr)
    raise SystemExit(1)


def main():
    if len(sys.argv) != 2:
        fail("usage: patch_host_ahb_failsafe.py <source-root>")

    root = Path(sys.argv[1])
    for relative, _, _, _ in EDITS:
        if not (root / relative).is_file():
            fail(f"missing {root / relative}")

    for relative, _, _, _ in EDITS:
        data = (root / relative).read_bytes()
        for marker in MARKERS:
            if marker in data:
                fail(f"{root / relative} already carries '{marker.decode()}'; "
                     f"refusing to patch twice")

    patched = {}
    for relative, old, new, description in EDITS:
        target = root / relative
        data = patched.get(target, target.read_bytes())
        count = data.count(old)
        if count != 1:
            fail(f"anchor for {description}: count {count}, expected exactly 1 in {target}")
        patched[target] = data.replace(old, new, 1)

    cpp = patched[root / CPP]
    hdr = patched[root / HDR]

    checks = [
        (hdr.count(b"bool ahbImportUnavailable = false;"), 1, "header flag"),
        (cpp.count(b"AHB-PROPS cb=%u"), 1, "AHB-PROPS probe"),
        (cpp.count(b"AHB-IMAGE-BITS cb=%u"), 1, "AHB-IMAGE-BITS probe"),
        (cpp.count(b"AHB-MEMTYPE-DISJOINT cb=%u"), 1, "AHB-MEMTYPE-DISJOINT marker"),
        (cpp.count(b"AHB-FAILSAFE-SKIP cb=%u"), 1, "allocation skip marker"),
        (cpp.count(b"AHB-FAILSAFE-NO-EXPORT cb=%u"), 1, "export skip marker"),
        (cpp.count(b"AHB-MATRIX fmt=%u"), 1, "shape matrix marker"),
        (cpp.count(b"infoPtr->ahbImportUnavailable = true;"), 1, "downgrade set"),
        (cpp.count(b"extImageCi.handleTypes = 0;"), 1, "external declaration removed"),
        (cpp.count(b"combinedBits = ahbProps.memoryTypeBits;"), 0,
         "no forced AHB memoryTypeBits assignment"),
        (cpp.count(b"Using AHB memory type bits for import."), 0, "forced-bits log gone"),
    ]
    for actual, expected, description in checks:
        if actual != expected:
            fail(f"post-check {description}: count {actual}, expected {expected}")

    for target, data in patched.items():
        target.write_bytes(data)

    line = cpp[: cpp.index(b"AHB-PROPS cb=%u")].count(b"\n") + 1
    print(f"patch_host_ahb_failsafe: patched {root / CPP} at line {line} "
          f"({len(EDITS)} edits)")


if __name__ == "__main__":
    main()
