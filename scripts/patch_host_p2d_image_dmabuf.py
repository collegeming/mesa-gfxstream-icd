#!/usr/bin/env python3
"""D1 host patch: give ColorBuffers a dma-buf image/memory contract instead of the
AndroidAHB external type, so Mali never treats a plain VkDeviceMemory as an
AHardwareBuffer at vkBindImageMemory time.

Applied after patch_host_ahb_import_guard.py (P2c/P3) on top of the pinned DroidVM
gfxstream tree.  Semantics, all host-only:

  1. createVkColorBufferLocked(): a per-ColorBuffer mode selected from
     GFXSTREAM_CB_EXTERNAL_MODE (dmabuf = default, ahb = P2c behaviour, none = no
     external type/handle).  In dmabuf mode the ColorBuffer image declares
     VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT (0x200) instead of the platform
     default (AHB, 0x400).  If the driver rejects that image, or reports zero image
     memory requirements, the image is recreated without an external type (none
     mode) instead of failing ColorBuffer creation.
  2. allocExternalMemory(): dmabuf ColorBuffers skip the AHB allocate/import arm and
     allocate plain memory with VkExportMemoryAllocateInfo{DMA_BUF}; none
     ColorBuffers skip the AHB arm and never append an export struct.
  3. The export section calls vkGetMemoryFdKHR(DMA_BUF), stores
     handleInfo={fd, STREAM_HANDLE_TYPE_MEM_DMABUF}, and returns success even when
     the export fails (handleInfo stays empty -> D2 degradation, no fatal).
  4. CIB1: classicImageEnabled() also accepts OpaqueFd external mode, the native
     image-format query uses the handle type the guest asked for, and the bridge
     image accepts a MEM_DMABUF ColorBuffer handle and declares DMA_BUF.
  5. rejectZeroSizeAllocation(): a zero-size guest import of a ColorBuffer with a
     non-empty MEM_DMABUF handle is allowed (the handle is an fd, not an AHB).

Every anchor must occur exactly once; all post-checks must pass; a mismatch aborts
the build and leaves every file untouched.
"""

import sys
from pathlib import Path

OPS = Path("host/vulkan/vk_common_operations.cpp")
OPS_H = Path("host/vulkan/vk_common_operations.h")
DEC = Path("host/vulkan/vk_decoder_global_state.cpp")

# ------------------------------------------------------------------ header field

H_OLD = b"""        bool glExported = false;
        bool externalMemoryCompatible = false;

        VulkanMode vulkanMode = VulkanMode::Default;
    };
"""

H_NEW = b"""        bool glExported = false;
        bool externalMemoryCompatible = false;

        // D1: 0 = AndroidAHB contract (P2c), 1 = dma-buf image/memory, 2 = no external
        // type and no handle. Set once per ColorBuffer in createVkColorBufferLocked()
        // from GFXSTREAM_CB_EXTERNAL_MODE.
        uint8_t p2dExternalMode = 0;

        VulkanMode vulkanMode = VulkanMode::Default;
    };
"""

# --------------------------------------------------------- per-ColorBuffer mode

MODE_OLD = b"""    mColorBuffers[colorBufferHandle] = res;
    auto infoPtr = &mColorBuffers[colorBufferHandle];

    VkImageTiling tiling = (infoPtr->memoryProperty & VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT)
"""

MODE_NEW = b"""    mColorBuffers[colorBufferHandle] = res;
    auto infoPtr = &mColorBuffers[colorBufferHandle];

    // D1: pick the ColorBuffer memory contract once per process. "dmabuf" (default) makes
    // the image declare a dma-buf external type and the allocation export an fd, so Mali
    // never reads the backing memory as an AHardwareBuffer. "ahb" restores the P2c
    // experiment; "none" drops the external type and the handle entirely.
    static const int p2dExternalModeDefault = [] {
        const std::string value =
            gfxstream::base::getEnvironmentVariable("GFXSTREAM_CB_EXTERNAL_MODE");
        if (value == "ahb") return 0;
        if (value == "none") return 2;
        if (!value.empty() && value != "dmabuf") {
            GFXSTREAM_WARNING(
                "GFXDIAG: D1 unknown GFXSTREAM_CB_EXTERNAL_MODE=%s, using dmabuf",
                value.c_str());
        }
        return 1;
    }();
    infoPtr->p2dExternalMode = static_cast<uint8_t>(p2dExternalModeDefault);
    if (infoPtr->p2dExternalMode == 1 &&
        (getExternalMemoryMode() != ExternalMemory::Mode::AndroidAHB ||
         !mDeviceInfo.supportsDmaBuf || !mDeviceInfo.supportsExternalMemoryExport)) {
        GFXSTREAM_WARNING(
            "GFXDIAG: D1 dmabuf unavailable externalMode=%d supportsDmaBuf=%d "
            "supportsExport=%d cb=%u, using ahb",
            (int)getExternalMemoryMode(), (int)mDeviceInfo.supportsDmaBuf,
            (int)mDeviceInfo.supportsExternalMemoryExport, colorBufferHandle);
        infoPtr->p2dExternalMode = 0;
    }

    VkImageTiling tiling = (infoPtr->memoryProperty & VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT)
"""

# ------------------------------------------------------------ image external type

EXT_OLD = b"""    VkExternalMemoryImageCreateInfo extImageCi = {
        VK_STRUCTURE_TYPE_EXTERNAL_MEMORY_IMAGE_CREATE_INFO
    };
    if (mDeviceInfo.supportsExternalMemoryExport || mDeviceInfo.supportsExternalMemoryImport) {
        // If external memory is supported (either by import or export), then append
        // VkExternalMemoryImageCreateInfo unconditionally, as it may be backed by external memory.
        extImageCi.handleTypes =
            static_cast<VkExternalMemoryHandleTypeFlags>(getDefaultExternalMemoryHandleType());

        imageCi->pNext = &extImageCi;
    }

    auto vk = mDvk;
"""

EXT_NEW = b"""    VkExternalMemoryImageCreateInfo extImageCi = {
        VK_STRUCTURE_TYPE_EXTERNAL_MEMORY_IMAGE_CREATE_INFO
    };
    const char* p2dModeName = infoPtr->p2dExternalMode == 1
        ? "dmabuf"
        : (infoPtr->p2dExternalMode == 2 ? "none" : "ahb");
    if (infoPtr->p2dExternalMode != 2 &&
        (mDeviceInfo.supportsExternalMemoryExport || mDeviceInfo.supportsExternalMemoryImport)) {
        // If external memory is supported (either by import or export), then append
        // VkExternalMemoryImageCreateInfo unconditionally, as it may be backed by external memory.
        // D1: a dma-buf ColorBuffer declares the dma-buf contract instead of the platform
        // default (AHB on this build), which is what made Mali interpret a plain
        // VkDeviceMemory as an AHardwareBuffer at bind time.
        extImageCi.handleTypes =
            infoPtr->p2dExternalMode == 1
                ? static_cast<VkExternalMemoryHandleTypeFlags>(
                      VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT)
                : static_cast<VkExternalMemoryHandleTypeFlags>(
                      getDefaultExternalMemoryHandleType());

        imageCi->pNext = &extImageCi;
    }
    GFXSTREAM_WARNING(
        "GFXDIAG: D1 P2D-IMAGE-EXT mode=%s handleTypes=0x%x supportsDmaBuf=%d cb=%u "
        "externalMode=%d",
        p2dModeName, (unsigned)extImageCi.handleTypes, (int)mDeviceInfo.supportsDmaBuf,
        colorBufferHandle, (int)getExternalMemoryMode());

    auto vk = mDvk;
"""

# ------------------------------------------------------- image create fallback

CREATE_OLD = b"""    VkResult createRes = vk->vkCreateImage(mDevice, imageCi.get(), nullptr, &infoPtr->image);
    if (createRes != VK_SUCCESS) {
        GFXSTREAM_ERROR("Failed to create Vulkan image for ColorBuffer %d, error: %s",
                        colorBufferHandle, string_VkResult(createRes));
        return false;
    }
"""

CREATE_NEW = b"""    VkResult createRes = vk->vkCreateImage(mDevice, imageCi.get(), nullptr, &infoPtr->image);
    if (createRes != VK_SUCCESS && infoPtr->p2dExternalMode == 1) {
        // D1: an unsupported dma-buf external image must not fail ColorBuffer creation.
        // Recreate without the external declaration (the "none" shape) and continue.
        GFXSTREAM_WARNING(
            "GFXDIAG: D1 IMAGE-CREATE-FALLBACK reason=dmabuf err=%d cb=%u fmt=%d %ux%u "
            "handleTypes=0x%x",
            (int)createRes, colorBufferHandle, (int)vkFormat, width, height,
            (unsigned)extImageCi.handleTypes);
        extImageCi.handleTypes = 0;
        imageCi->pNext = nullptr;
        infoPtr->p2dExternalMode = 2;
        p2dModeName = "none";
        infoPtr->image = VK_NULL_HANDLE;
        createRes = vk->vkCreateImage(mDevice, imageCi.get(), nullptr, &infoPtr->image);
    }
    if (createRes != VK_SUCCESS) {
        GFXSTREAM_ERROR("Failed to create Vulkan image for ColorBuffer %d, error: %s",
                        colorBufferHandle, string_VkResult(createRes));
        return false;
    }
"""

# --------------------------------------------------------- zero-size size guard

SIZE_OLD = b"""    } else {
        vk->vkGetImageMemoryRequirements(mDevice, infoPtr->image, &infoPtr->imageMemReqs);
    }

    // Currently we only care about two memory properties: DEVICE_LOCAL
"""

SIZE_NEW = b"""    } else {
        vk->vkGetImageMemoryRequirements(mDevice, infoPtr->image, &infoPtr->imageMemReqs);
    }

    if (infoPtr->p2dExternalMode == 1 && infoPtr->imageMemReqs.size == 0) {
        // D1: Mali reported zero image requirements for the AHB image in P2c. A dma-buf
        // image should report a real size; if it does not, recreate without an external
        // type instead of allocating zero bytes.
        GFXSTREAM_WARNING(
            "GFXDIAG: D1 IMAGE-SIZE-ZERO cb=%u fmt=%d %ux%u imageBits=0x%x, recreating "
            "without external type",
            colorBufferHandle, (int)vkFormat, width, height,
            (unsigned)infoPtr->imageMemReqs.memoryTypeBits);
        vk->vkDestroyImage(mDevice, infoPtr->image, nullptr);
        infoPtr->image = VK_NULL_HANDLE;
        extImageCi.handleTypes = 0;
        imageCi->pNext = nullptr;
        infoPtr->p2dExternalMode = 2;
        p2dModeName = "none";
        VkResult p2dRecreateRes =
            vk->vkCreateImage(mDevice, imageCi.get(), nullptr, &infoPtr->image);
        if (p2dRecreateRes != VK_SUCCESS) {
            GFXSTREAM_ERROR("Failed to recreate Vulkan image for ColorBuffer %d, error: %s",
                            colorBufferHandle, string_VkResult(p2dRecreateRes));
            return false;
        }
        infoPtr->imageCreateInfoShallow = vk_make_orphan_copy(*imageCi);
        vk->vkGetImageMemoryRequirements(mDevice, infoPtr->image, &infoPtr->imageMemReqs);
    }

    // Currently we only care about two memory properties: DEVICE_LOCAL
"""

# ------------------------------------------------------- export struct for dmabuf

ALLOC_OLD = b"""    bool p2cNonExportable = false;

    // HostAllocation mode uses host side allocation and should not add VkExportMemoryAllocateInfo.
"""

ALLOC_NEW = b"""    bool p2cNonExportable = false;

    // D1: a dma-buf ColorBuffer is allocated as plain memory that declares a dma-buf
    // export. AndroidAHB mode adds no VkExportMemoryAllocateInfo (its path imports an
    // AHB), so this is where the dma-buf contract is declared for that mode.
    if (colorBufferInfo && (*colorBufferInfo)->p2dExternalMode == 1 &&
        mDeviceInfo.supportsExternalMemoryExport) {
        exportAi.handleTypes = VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT;
        vk_append_struct(&allocInfoChain, &exportAi);
        GFXSTREAM_WARNING("GFXDIAG: D1 ALLOC-DMABUF cb=%u exportTypes=0x%x size=%llu",
                          (*colorBufferInfo)->handle, (unsigned)exportAi.handleTypes,
                          (unsigned long long)allocInfo.allocationSize);
    }

    // HostAllocation mode uses host side allocation and should not add VkExportMemoryAllocateInfo.
"""

# ------------------------------------------------------------ skip AHB allocation

AHB_OLD = b"""        case ExternalMemory::Mode::AndroidAHB: {
#ifdef __ANDROID__
            // Prefer allocating the AHardwareBuffer via Gralloc and import to work around
"""

AHB_NEW = b"""        case ExternalMemory::Mode::AndroidAHB: {
#ifdef __ANDROID__
            if (colorBufferInfo && (*colorBufferInfo)->p2dExternalMode != 0) {
                // D1: dma-buf and no-external ColorBuffers must not allocate or import an
                // AHardwareBuffer. Keep the image's own size and memory type.
                GFXSTREAM_WARNING(
                    "GFXDIAG: D1 SKIP-AHB cb=%u mode=%d size=%llu memindex=%u",
                    (*colorBufferInfo)->handle, (int)(*colorBufferInfo)->p2dExternalMode,
                    (unsigned long long)allocInfo.allocationSize, allocInfo.memoryTypeIndex);
                break;
            }
            // Prefer allocating the AHardwareBuffer via Gralloc and import to work around
"""

# ------------------------------------------------------------- export the fd

EXPORT_OLD = b"""        GFXSTREAM_WARNING("GFXDIAG: P2C skip-export memindex=%u size=%llu handleInfo=none",
                          info->typeIndex, (unsigned long long)info->size);
        return true;
    }

    uint32_t streamHandleType = 0;
"""

EXPORT_NEW = b"""        GFXSTREAM_WARNING("GFXDIAG: P2C skip-export memindex=%u size=%llu handleInfo=none",
                          info->typeIndex, (unsigned long long)info->size);
        return true;
    }

    if (colorBufferInfo && (*colorBufferInfo)->p2dExternalMode != 0) {
        if ((*colorBufferInfo)->p2dExternalMode == 2) {
            GFXSTREAM_WARNING(
                "GFXDIAG: D1 CB-EXTERNAL-NONE cb=%u memindex=%u size=%llu skip-export=1",
                (*colorBufferInfo)->handle, info->typeIndex, (unsigned long long)info->size);
            return true;
        }
        int p2dExportFd = -1;
        VkResult p2dExportRes = VK_ERROR_EXTENSION_NOT_PRESENT;
        if (vk->vkGetMemoryFdKHR) {
            VkMemoryGetFdInfoKHR p2dGetFdInfo = {
                VK_STRUCTURE_TYPE_MEMORY_GET_FD_INFO_KHR,
                nullptr,
                info->memory,
                VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT,
            };
            p2dExportRes = vk->vkGetMemoryFdKHR(mDevice, &p2dGetFdInfo, &p2dExportFd);
        }
        const bool p2dExported = (p2dExportRes == VK_SUCCESS) && (p2dExportFd >= 0);
        if (p2dExported) {
            info->handleInfo = ExternalHandleInfo{
                .handle = static_cast<ExternalHandleType>(p2dExportFd),
                .streamHandleType = STREAM_HANDLE_TYPE_MEM_DMABUF,
            };
            (*colorBufferInfo)->externalMemoryCompatible = true;
            GFXSTREAM_WARNING(
                "GFXDIAG: D1 P2D-DMABUF-EXPORT ok=1 fd=%d cb=%u memindex=%u size=%llu "
                "handleInfo=0x%x",
                p2dExportFd, (*colorBufferInfo)->handle, info->typeIndex,
                (unsigned long long)info->size, (unsigned)STREAM_HANDLE_TYPE_MEM_DMABUF);
        } else {
            GFXSTREAM_WARNING(
                "GFXDIAG: D1 P2D-DMABUF-EXPORT ok=0 err=%d cb=%u memindex=%u size=%llu "
                "handleInfo=0x0",
                (int)p2dExportRes, (*colorBufferInfo)->handle, info->typeIndex,
                (unsigned long long)info->size);
        }
        // D1: never fail ColorBuffer creation on export. A handle-less ColorBuffer is the
        // D2 degradation (the guest import fails), not a startup failure.
        return true;
    }

    uint32_t streamHandleType = 0;
"""

# ---------------------------------------------------- zero-size import whitelist

ZERO_OLD = b"""                if (cb && cb->memory.handleInfo && cb->memory.handleInfo->handle &&
                    cb->memory.handleInfo->streamHandleType == STREAM_HANDLE_TYPE_PLATFORM_AHB) {
                    return false;
                }
"""

ZERO_NEW = b"""                if (cb && cb->memory.handleInfo && cb->memory.handleInfo->handle &&
                    (cb->memory.handleInfo->streamHandleType == STREAM_HANDLE_TYPE_PLATFORM_AHB ||
                     cb->memory.handleInfo->streamHandleType == STREAM_HANDLE_TYPE_MEM_DMABUF)) {
                    // D1: a dma-buf ColorBuffer handle is an fd, not an AHardwareBuffer.
                    if (cb->memory.handleInfo->streamHandleType == STREAM_HANDLE_TYPE_MEM_DMABUF) {
                        GFXSTREAM_WARNING(
                            "GFXDIAG: D1 ZERO-SIZE-ALLOW type=0x%x handle=%lld cb=%u stage=%s",
                            cb->memory.handleInfo->streamHandleType,
                            (long long)cb->memory.handleInfo->handle, cb->handle, stage);
                    }
                    return false;
                }
"""

# ------------------------------------------------------------- CIB1 enable gate

CIB_ENABLE_OLD = b"""        const bool ahbMode = m_vkEmulation->getExternalMemoryMode() == ExternalMemory::Mode::AndroidAHB;
"""

CIB_ENABLE_NEW = b"""        // D1: OpaqueFd also exports a dma-buf fd on this driver; the bridge accepts a
        // MEM_DMABUF ColorBuffer handle below and still fails closed for other types.
        const auto p2dExternalMode = m_vkEmulation->getExternalMemoryMode();
        const bool ahbMode =
            p2dExternalMode == ExternalMemory::Mode::AndroidAHB ||
            p2dExternalMode == ExternalMemory::Mode::OpaqueFd;
"""

# ---------------------------------------------------------- CIB1 native query

CIB_QUERY_OLD = b"""        VkPhysicalDeviceExternalImageFormatInfo ahb{VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_EXTERNAL_IMAGE_FORMAT_INFO,
            list ? &localList : nullptr, VK_EXTERNAL_MEMORY_HANDLE_TYPE_ANDROID_HARDWARE_BUFFER_BIT_ANDROID};
        VkPhysicalDeviceImageFormatInfo2 actual = *input;
"""

CIB_QUERY_NEW = b"""        // D1: query the driver for the handle type the guest asked for. A dma-buf
        // ColorBuffer's memory is an fd, so AHB capabilities are the wrong contract.
        const VkExternalMemoryHandleTypeFlagBits p2dNativeExternalType =
            ext->handleType == VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT
                ? VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT
                : VK_EXTERNAL_MEMORY_HANDLE_TYPE_ANDROID_HARDWARE_BUFFER_BIT_ANDROID;
        VkPhysicalDeviceExternalImageFormatInfo ahb{VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_EXTERNAL_IMAGE_FORMAT_INFO,
            list ? &localList : nullptr, p2dNativeExternalType};
        VkPhysicalDeviceImageFormatInfo2 actual = *input;
"""

# -------------------------------------------------------- CIB1 handle accept

CIB_HANDLE_OLD = b"""        if (!cb->memory.handleInfo) return fail("handle-info", VK_ERROR_INVALID_EXTERNAL_HANDLE);
        if (cb->memory.handleInfo->streamHandleType != STREAM_HANDLE_TYPE_PLATFORM_AHB)
            return fail("handle-type", VK_ERROR_INVALID_EXTERNAL_HANDLE);
        if (!cb->memory.handleInfo->handle) return fail("cb-handle-null", VK_ERROR_INVALID_EXTERNAL_HANDLE);
"""

CIB_HANDLE_NEW = b"""        if (!cb->memory.handleInfo) return fail("handle-info", VK_ERROR_INVALID_EXTERNAL_HANDLE);
        // D1: a dma-buf ColorBuffer has an fd handle; the bridge imports it through the
        // fd path and creates the alias image with the matching dma-buf external type.
        const bool p2dBridgeDmaBuf =
            cb->memory.handleInfo->streamHandleType == STREAM_HANDLE_TYPE_MEM_DMABUF;
        if (cb->memory.handleInfo->streamHandleType != STREAM_HANDLE_TYPE_PLATFORM_AHB &&
            !p2dBridgeDmaBuf)
            return fail("handle-type", VK_ERROR_INVALID_EXTERNAL_HANDLE);
        if (!cb->memory.handleInfo->handle) return fail("cb-handle-null", VK_ERROR_INVALID_EXTERNAL_HANDLE);
        GFXSTREAM_WARNING(
            "GFXDIAG: D1 CIB1-IMAGE-EXT cb=%u streamType=0x%x bridgeTypes=0x%x size=%llu",
            resource, cb->memory.handleInfo->streamHandleType,
            (unsigned)(p2dBridgeDmaBuf
                           ? VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT
                           : VK_EXTERNAL_MEMORY_HANDLE_TYPE_ANDROID_HARDWARE_BUFFER_BIT_ANDROID),
            (unsigned long long)cb->memory.size);
"""

# ----------------------------------------------------- CIB1 bridge image type

CIB_IMAGE_OLD = b"""        VkExternalMemoryImageCreateInfo external{VK_STRUCTURE_TYPE_EXTERNAL_MEMORY_IMAGE_CREATE_INFO,
            list ? &localList : nullptr, VK_EXTERNAL_MEMORY_HANDLE_TYPE_ANDROID_HARDWARE_BUFFER_BIT_ANDROID};
        ci.pNext = &external;
"""

CIB_IMAGE_NEW = b"""        // D1: the alias must declare the same external contract as the ColorBuffer it
        // backs, or the bind reintroduces the AHB-vs-plain-memory mismatch.
        VkExternalMemoryImageCreateInfo external{VK_STRUCTURE_TYPE_EXTERNAL_MEMORY_IMAGE_CREATE_INFO,
            list ? &localList : nullptr,
            p2dBridgeDmaBuf ? VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT
                            : VK_EXTERNAL_MEMORY_HANDLE_TYPE_ANDROID_HARDWARE_BUFFER_BIT_ANDROID};
        ci.pNext = &external;
"""

MARKERS = [
    b"GFXDIAG: D1 P2D-IMAGE-EXT",
    b"GFXDIAG: D1 P2D-DMABUF-EXPORT",
    b"GFXDIAG: D1 CIB1-IMAGE-EXT",
    b"GFXDIAG: D1 ZERO-SIZE-ALLOW",
    b"p2dExternalMode",
]


def fail(message):
    print(f"patch_host_p2d_image_dmabuf: FAIL: {message}", file=sys.stderr)
    raise SystemExit(1)


def replace_once(data, old, new, label):
    count = data.count(old)
    if count != 1:
        fail(f"anchor '{label}' count is {count}, expected exactly 1")
    return data.replace(old, new, 1)


def main():
    if len(sys.argv) != 2:
        fail("usage: patch_host_p2d_image_dmabuf.py <source-root>")

    root = Path(sys.argv[1])
    ops_path = root / OPS
    ops_h_path = root / OPS_H
    dec_path = root / DEC
    for path in (ops_path, ops_h_path, dec_path):
        if not path.is_file():
            fail(f"missing {path}")

    ops = ops_path.read_bytes()
    ops_h = ops_h_path.read_bytes()
    dec = dec_path.read_bytes()

    for marker in MARKERS:
        for name, data in (("vk_common_operations.cpp", ops),
                           ("vk_common_operations.h", ops_h),
                           ("vk_decoder_global_state.cpp", dec)):
            if data.count(marker) != 0:
                fail(f"{name} already carries '{marker.decode()}'; refusing to patch twice")

    # P2c/P3 must be applied first: the D1 anchors below pin P2c strings.
    if ops.count(b"p2cNonExportable") != 3:
        fail("expected exactly 3 p2cNonExportable occurrences before patching (P2c applied?)")
    if ops.count(b"GFXDIAG: P2C skip-export") != 1:
        fail("expected exactly one P2c skip-export line before patching (P2c applied?)")

    ops_h = replace_once(ops_h, H_OLD, H_NEW, "ColorBufferInfo external mode field")
    ops = replace_once(ops, MODE_OLD, MODE_NEW, "per-ColorBuffer external mode")
    ops = replace_once(ops, EXT_OLD, EXT_NEW, "ColorBuffer image external type")
    ops = replace_once(ops, CREATE_OLD, CREATE_NEW, "ColorBuffer image create")
    ops = replace_once(ops, SIZE_OLD, SIZE_NEW, "ColorBuffer image size guard")
    ops = replace_once(ops, ALLOC_OLD, ALLOC_NEW, "dma-buf export allocate info")
    ops = replace_once(ops, AHB_OLD, AHB_NEW, "skip AHB allocation")
    ops = replace_once(ops, EXPORT_OLD, EXPORT_NEW, "dma-buf fd export")
    dec = replace_once(dec, ZERO_OLD, ZERO_NEW, "zero-size import whitelist")
    dec = replace_once(dec, CIB_ENABLE_OLD, CIB_ENABLE_NEW, "CIB1 enable gate")
    dec = replace_once(dec, CIB_QUERY_OLD, CIB_QUERY_NEW, "CIB1 native query handle type")
    dec = replace_once(dec, CIB_HANDLE_OLD, CIB_HANDLE_NEW, "CIB1 ColorBuffer handle accept")
    dec = replace_once(dec, CIB_IMAGE_OLD, CIB_IMAGE_NEW, "CIB1 bridge image external type")

    checks = [
        (ops_h.count(b"p2dExternalMode"), 1, "ColorBufferInfo D1 mode field"),
        (ops.count(b"GFXDIAG: D1 P2D-IMAGE-EXT"), 1, "D1 image external type trace"),
        (ops.count(b"GFXDIAG: D1 P2D-DMABUF-EXPORT"), 2, "D1 export ok/fail traces"),
        (ops.count(b"GFXDIAG: D1 ALLOC-DMABUF"), 1, "D1 allocation export info trace"),
        (ops.count(b"GFXDIAG: D1 SKIP-AHB"), 1, "D1 AHB skip trace"),
        (ops.count(b"GFXDIAG: D1 CB-EXTERNAL-NONE"), 1, "D1 no-external export skip trace"),
        (ops.count(b"GFXDIAG: D1 IMAGE-CREATE-FALLBACK"), 1, "D1 image create fallback trace"),
        (ops.count(b"GFXDIAG: D1 IMAGE-SIZE-ZERO"), 1, "D1 zero-size image guard trace"),
        (ops.count(b"GFXSTREAM_CB_EXTERNAL_MODE"), 2, "D1 mode switch and its log"),
        (ops.count(b"VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT"), 11,
         "dma-buf handle type occurrences in vk_common_operations.cpp"),
        (ops.count(b"STREAM_HANDLE_TYPE_MEM_DMABUF"), 7,
         "MEM_DMABUF occurrences in vk_common_operations.cpp"),
        (ops.count(b"p2dNonExportable") + ops.count(b"p2cNonExportable"), 3,
         "P2c flag occurrences preserved"),
        (ops.count(b"GFXDIAG: P2C skip-export"), 1, "P2c skip-export preserved"),
        (ops.count(b"GFXDIAG: reject AHB import"), 1, "P2c reject-import preserved"),
        (dec.count(b"GFXDIAG: D1 ZERO-SIZE-ALLOW"), 1, "D1 zero-size import whitelist trace"),
        (dec.count(b"GFXDIAG: D1 CIB1-IMAGE-EXT"), 1, "D1 CIB1 bridge trace"),
        (dec.count(b"p2dBridgeDmaBuf"), 4, "D1 CIB1 dma-buf decisions"),
        (dec.count(b"STREAM_HANDLE_TYPE_MEM_DMABUF"), 8,
         "MEM_DMABUF occurrences in vk_decoder_global_state.cpp"),
        (dec.count(b"GFXDIAG: P3 GUEST-BIND"), 1, "P3 guest bind diagnostics preserved"),
    ]
    for actual, expected, description in checks:
        if actual != expected:
            fail(f"post-check {description}: count {actual}, expected {expected}")

    if ops == ops_path.read_bytes():
        fail("vk_common_operations.cpp replacement produced no change")
    if ops_h == ops_h_path.read_bytes():
        fail("vk_common_operations.h replacement produced no change")
    if dec == dec_path.read_bytes():
        fail("vk_decoder_global_state.cpp replacement produced no change")

    ops_h_path.write_bytes(ops_h)
    ops_path.write_bytes(ops)
    dec_path.write_bytes(dec)

    ops_line = ops[: ops.index(b"GFXDIAG: D1 P2D-IMAGE-EXT")].count(b"\n") + 1
    dec_line = dec[: dec.index(b"GFXDIAG: D1 CIB1-IMAGE-EXT")].count(b"\n") + 1
    print(f"patch_host_p2d_image_dmabuf: patched {ops_h_path}, {ops_path} at line {ops_line} "
          f"and {dec_path} at line {dec_line}")


if __name__ == "__main__":
    main()
