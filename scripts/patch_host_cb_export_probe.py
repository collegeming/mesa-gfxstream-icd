#!/usr/bin/env python3
"""D4BX host patch: one bounded diagnostic round for the dma-buf ColorBuffer export.

Applied after patch_host_p2d_image_dmabuf.py (D1) and
patch_host_cb_dmabuf_modifier.py (D4b) on the pinned DroidVM gfxstream tree.
Host-only: no guest ICD, dispatcher or production library changes.

  E1'  Probe the exact ColorBuffer image shape with
       vkGetPhysicalDeviceImageFormatProperties2 for DMA_BUF and OPAQUE_FD, with
       and without DRM format modifier tiling, and log externalMemoryFeatures
       (EXPORTABLE_BIT = 0x2), exportFromImportedHandleTypes,
       compatibleHandleTypes and VkImageFormatProperties.  Also list every
       memory type in imageMemReqs.memoryTypeBits with its physical flags.

  E2   GFXSTREAM_CB_EXPORT_MODE=opaque selects
       VK_EXTERNAL_MEMORY_HANDLE_TYPE_OPAQUE_FD_BIT for the image, the
       allocation's VkExportMemoryAllocateInfo, the vkGetMemoryFdKHR request
       and the CIB1 alias image, and reports
       STREAM_HANDLE_TYPE_MEM_OPAQUE_FD to the guest.  Default stays dma-buf.

  E3'  GFXSTREAM_CB_MEMTYPE_INDEX=<n> forces the ColorBuffer memory type so a
       single binary can test every candidate.  Unset keeps D1/D4b selection.

  fd   Both export arms log the raw fd, the VkResult, whether the driver wrote
       the out parameter at all, handleInfo, memory type index and its flags.
       The fd starts at -12345, a value no driver returns, so "driver wrote -1"
       and "driver did not write" are distinguishable.

D1/D4b markers, the image modifier contract, the fatal device-lost path and the
zero-size import behavior of MEM_DMABUF are preserved.  Every anchor must occur
exactly once; all post-checks must pass; a mismatch aborts before any write.
"""

import sys
from pathlib import Path

OPS = Path("host/vulkan/vk_common_operations.cpp")
OPS_H = Path("host/vulkan/vk_common_operations.h")
DEC = Path("host/vulkan/vk_decoder_global_state.cpp")

# ------------------------------------------------- E1': probe helper before create()
PROBE_OLD = b"""// Ideally we would like to specify a memory type index directly from
// localAllocInfo.memoryTypeIndex when allocating color buffers in
"""

PROBE_NEW = b"""namespace {

// D4BX E1': read-only exportability probe for the exact image shape a dma-buf
// ColorBuffer creates.  D1/D4b proved the pNext chain reaches the driver and that
// vkGetMemoryFdKHR returns VK_SUCCESS without a usable fd, but nothing ever asked
// Mali whether it considers this shape exportable.  These queries separate a
// handle-type problem (DMA_BUF vs OPAQUE_FD) from a shape/modifier problem.
struct P2dExportProbe {
    VkResult dmaBufModifierRes = VK_ERROR_UNKNOWN;
    VkResult opaqueFdModifierRes = VK_ERROR_UNKNOWN;
    VkResult dmaBufOptimalRes = VK_ERROR_UNKNOWN;
    VkResult opaqueFdOptimalRes = VK_ERROR_UNKNOWN;
    VkExternalMemoryFeatureFlags dmaBufModifierFeatures = 0;
    VkExternalMemoryFeatureFlags opaqueFdModifierFeatures = 0;
    VkExternalMemoryFeatureFlags dmaBufOptimalFeatures = 0;
    VkExternalMemoryFeatureFlags opaqueFdOptimalFeatures = 0;
};

P2dExportProbe p2dProbeExternalImageFormat(VulkanDispatch* vk, VkPhysicalDevice physdev,
                                           const VkImageCreateInfo& ci, uint32_t cb) {
    P2dExportProbe out;
    static const uint64_t kLinearModifier = 0;

    auto runQuery = [&](VkImageTiling tiling, VkExternalMemoryHandleTypeFlagBits handleType,
                        VkResult* result, VkExternalMemoryFeatureFlags* features) {
        VkPhysicalDeviceImageDrmFormatModifierInfoEXT modifierInfo{
            VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_IMAGE_DRM_FORMAT_MODIFIER_INFO_EXT,
            nullptr,
            kLinearModifier,
            VK_SHARING_MODE_EXCLUSIVE,
            0,
            nullptr,
        };
        VkPhysicalDeviceExternalImageFormatInfo externalInfo{
            VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_EXTERNAL_IMAGE_FORMAT_INFO,
            tiling == VK_IMAGE_TILING_DRM_FORMAT_MODIFIER_EXT ? &modifierInfo : nullptr,
            handleType,
        };
        VkPhysicalDeviceImageFormatInfo2 formatInfo{
            VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_IMAGE_FORMAT_INFO_2,
            &externalInfo,
            ci.format,
            ci.imageType,
            tiling,
            ci.usage,
            ci.flags,
        };
        VkExternalImageFormatProperties externalProps{
            VK_STRUCTURE_TYPE_EXTERNAL_IMAGE_FORMAT_PROPERTIES};
        VkImageFormatProperties2 formatProps{VK_STRUCTURE_TYPE_IMAGE_FORMAT_PROPERTIES_2,
                                            &externalProps};
        VkResult res = VK_ERROR_EXTENSION_NOT_PRESENT;
        if (vk->vkGetPhysicalDeviceImageFormatProperties2) {
            res = vk->vkGetPhysicalDeviceImageFormatProperties2(physdev, &formatInfo, &formatProps);
        } else if (vk->vkGetPhysicalDeviceImageFormatProperties2KHR) {
            res = vk->vkGetPhysicalDeviceImageFormatProperties2KHR(physdev, &formatInfo,
                                                                   &formatProps);
        }
        *result = res;
        *features = externalProps.externalMemoryProperties.externalMemoryFeatures;
        GFXSTREAM_WARNING(
            "GFXDIAG: D4BX EXTPROPS cb=%u handle=0x%x tiling=%d res=%d features=0x%x "
            "exportable=%d importable=%d from-imported=0x%x compatible=0x%x "
            "maxExtent=%ux%ux%u mip=%u layers=%u samples=0x%x maxRes=%llu",
            cb, (unsigned)handleType, (int)tiling, (int)res, (unsigned)*features,
            (*features & VK_EXTERNAL_MEMORY_FEATURE_EXPORTABLE_BIT) ? 1 : 0,
            (*features & VK_EXTERNAL_MEMORY_FEATURE_IMPORTABLE_BIT) ? 1 : 0,
            (unsigned)externalProps.externalMemoryProperties.exportFromImportedHandleTypes,
            (unsigned)externalProps.externalMemoryProperties.compatibleHandleTypes,
            formatProps.imageFormatProperties.maxExtent.width,
            formatProps.imageFormatProperties.maxExtent.height,
            formatProps.imageFormatProperties.maxExtent.depth,
            formatProps.imageFormatProperties.maxMipLevels,
            formatProps.imageFormatProperties.maxArrayLayers,
            (unsigned)formatProps.imageFormatProperties.sampleCounts,
            (unsigned long long)formatProps.imageFormatProperties.maxResourceSize);
    };

    runQuery(VK_IMAGE_TILING_DRM_FORMAT_MODIFIER_EXT,
             VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT, &out.dmaBufModifierRes,
             &out.dmaBufModifierFeatures);
    runQuery(VK_IMAGE_TILING_DRM_FORMAT_MODIFIER_EXT,
             VK_EXTERNAL_MEMORY_HANDLE_TYPE_OPAQUE_FD_BIT, &out.opaqueFdModifierRes,
             &out.opaqueFdModifierFeatures);
    runQuery(VK_IMAGE_TILING_OPTIMAL, VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT,
             &out.dmaBufOptimalRes, &out.dmaBufOptimalFeatures);
    runQuery(VK_IMAGE_TILING_OPTIMAL, VK_EXTERNAL_MEMORY_HANDLE_TYPE_OPAQUE_FD_BIT,
             &out.opaqueFdOptimalRes, &out.opaqueFdOptimalFeatures);
    return out;
}

}  // namespace

// Ideally we would like to specify a memory type index directly from
// localAllocInfo.memoryTypeIndex when allocating color buffers in
"""

# ------------------------------------------------------ E1' probe call + MEMTYPES
BITS_OLD = b"""    if (infoPtr->p2dExternalMode == 1) {
        GFXSTREAM_WARNING(
            "GFXDIAG: D4B P2D-IMAGE-BITS cb=%u fmt=%d %ux%u size=%llu bits=0x%x tiling=%d",
            colorBufferHandle, (int)vkFormat, width, height,
            (unsigned long long)infoPtr->imageMemReqs.size,
            (unsigned)infoPtr->imageMemReqs.memoryTypeBits,
            (int)infoPtr->imageCreateInfoShallow.tiling);
    }

    // Currently we only care about two memory properties: DEVICE_LOCAL
"""

BITS_NEW = b"""    if (infoPtr->p2dExternalMode == 1) {
        GFXSTREAM_WARNING(
            "GFXDIAG: D4B P2D-IMAGE-BITS cb=%u fmt=%d %ux%u size=%llu bits=0x%x tiling=%d",
            colorBufferHandle, (int)vkFormat, width, height,
            (unsigned long long)infoPtr->imageMemReqs.size,
            (unsigned)infoPtr->imageMemReqs.memoryTypeBits,
            (int)infoPtr->imageCreateInfoShallow.tiling);
    }

    // D4BX E1': ask the driver whether this exact image shape is exportable, and list
    // every candidate memory type.  Read-only: no allocation, bind or export changes.
    P2dExportProbe p2dExportProbe;
    if (infoPtr->p2dExternalMode == 1) {
        p2dExportProbe = p2dProbeExternalImageFormat(vk, mPhysicalDevice,
                                                    infoPtr->imageCreateInfoShallow,
                                                    colorBufferHandle);
        for (uint32_t p2dType = 0; p2dType < mDeviceInfo.memProps.memoryTypeCount; ++p2dType) {
            if ((infoPtr->imageMemReqs.memoryTypeBits & (1u << p2dType)) == 0) continue;
            GFXSTREAM_WARNING(
                "GFXDIAG: D4BX MEMTYPES cb=%u bits=0x%x idx=%u flags=0x%x heap=%u "
                "imageExportable=%d",
                colorBufferHandle, (unsigned)infoPtr->imageMemReqs.memoryTypeBits, p2dType,
                (unsigned)mDeviceInfo.memProps.memoryTypes[p2dType].propertyFlags,
                (unsigned)mDeviceInfo.memProps.memoryTypes[p2dType].heapIndex,
                (p2dExportProbe.dmaBufModifierFeatures &
                 VK_EXTERNAL_MEMORY_FEATURE_EXPORTABLE_BIT) ? 1 : 0);
        }
    }

    // Currently we only care about two memory properties: DEVICE_LOCAL
"""

# ------------------------------------------------- E2/E3': env switches + handle type
MODE_OLD = b"""        infoPtr->p2dExternalMode = 0;
    }

    VkImageTiling tiling = (infoPtr->memoryProperty & VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT)
"""

MODE_NEW = b"""        infoPtr->p2dExternalMode = 0;
    }

    // D4BX E2: handle type switch.  Default keeps the D1/D4b dma-buf contract; "opaque"
    // declares VK_EXTERNAL_MEMORY_HANDLE_TYPE_OPAQUE_FD_BIT and reports
    // STREAM_HANDLE_TYPE_MEM_OPAQUE_FD to the guest instead.
    static const int p2dExportModeDefault = [] {
        const std::string value =
            gfxstream::base::getEnvironmentVariable("GFXSTREAM_CB_EXPORT_MODE");
        if (value.empty() || value == "dma_buf" || value == "dmabuf") return 0;
        if (value == "opaque" || value == "opaque_fd") return 1;
        GFXSTREAM_WARNING("GFXDIAG: D4BX unknown GFXSTREAM_CB_EXPORT_MODE=%s, using dma_buf",
                          value.c_str());
        return 0;
    }();
    // D4BX E3': GFXSTREAM_CB_MEMTYPE_INDEX=<n> forces the ColorBuffer memory type so one
    // binary can test every candidate the image advertises.  Unset keeps D1/D4b.
    static const int p2dMemTypeOverride = [] {
        const std::string value =
            gfxstream::base::getEnvironmentVariable("GFXSTREAM_CB_MEMTYPE_INDEX");
        if (value.empty()) return -1;
        return atoi(value.c_str());
    }();
    infoPtr->p2dHandleType =
        (infoPtr->p2dExternalMode == 1 && p2dExportModeDefault == 1)
            ? static_cast<uint32_t>(VK_EXTERNAL_MEMORY_HANDLE_TYPE_OPAQUE_FD_BIT)
            : static_cast<uint32_t>(VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT);
    GFXSTREAM_WARNING(
        "GFXDIAG: D4BX EXPORT-MODE mode=%s handleType=0x%x memtypeOverride=%d cb=%u",
        p2dExportModeDefault == 1 ? "opaque" : "dma_buf", (unsigned)infoPtr->p2dHandleType,
        p2dMemTypeOverride, colorBufferHandle);

    VkImageTiling tiling = (infoPtr->memoryProperty & VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT)
"""

# ------------------------------------------------------- E2: image handle type
EXT_TYPE_OLD = b"""        extImageCi.handleTypes =
            infoPtr->p2dExternalMode == 1
                ? static_cast<VkExternalMemoryHandleTypeFlags>(
                      VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT)
                : static_cast<VkExternalMemoryHandleTypeFlags>(
                      getDefaultExternalMemoryHandleType());
"""

EXT_TYPE_NEW = b"""        extImageCi.handleTypes =
            infoPtr->p2dExternalMode == 1
                ? static_cast<VkExternalMemoryHandleTypeFlags>(infoPtr->p2dHandleType)
                : static_cast<VkExternalMemoryHandleTypeFlags>(
                      getDefaultExternalMemoryHandleType());
"""

# ---------------------------------------------------------- E3': memory type pick
MEMTYPE_OLD = b"""    // Determine memory type.
    infoPtr->memory.typeIndex =
        getValidMemoryTypeIndex(infoPtr->imageMemReqs.memoryTypeBits, infoPtr->memoryProperty);

    const VkFormat imageVkFormat = infoPtr->imageCreateInfoShallow.format;
"""

MEMTYPE_NEW = b"""    // Determine memory type.
    infoPtr->memory.typeIndex =
        getValidMemoryTypeIndex(infoPtr->imageMemReqs.memoryTypeBits, infoPtr->memoryProperty);
    if (infoPtr->p2dExternalMode == 1 && p2dMemTypeOverride >= 0) {
        const uint32_t p2dRequestedType = static_cast<uint32_t>(p2dMemTypeOverride);
        const bool p2dInRange = p2dRequestedType < mDeviceInfo.memProps.memoryTypeCount;
        const bool p2dInBits =
            p2dInRange && ((infoPtr->imageMemReqs.memoryTypeBits & (1u << p2dRequestedType)) != 0);
        GFXSTREAM_WARNING(
            "GFXDIAG: D4BX MEMTYPE-OVERRIDE cb=%u requested=%u inRange=%d inBits=%d "
            "bits=0x%x previous=%u",
            colorBufferHandle, p2dRequestedType, (int)p2dInRange, (int)p2dInBits,
            (unsigned)infoPtr->imageMemReqs.memoryTypeBits, infoPtr->memory.typeIndex);
        if (p2dInRange) infoPtr->memory.typeIndex = p2dRequestedType;
    }
    if (infoPtr->p2dExternalMode == 1) {
        const uint32_t p2dChosenType = infoPtr->memory.typeIndex;
        const bool p2dChosenInRange = p2dChosenType < mDeviceInfo.memProps.memoryTypeCount;
        GFXSTREAM_WARNING(
            "GFXDIAG: D4BX MEMTYPE-PICK cb=%u idx=%u flags=0x%x heap=%u imageExportable=%d "
            "handleType=0x%x",
            colorBufferHandle, p2dChosenType,
            p2dChosenInRange
                ? (unsigned)mDeviceInfo.memProps.memoryTypes[p2dChosenType].propertyFlags : 0u,
            p2dChosenInRange
                ? (unsigned)mDeviceInfo.memProps.memoryTypes[p2dChosenType].heapIndex : 0u,
            (p2dExportProbe.dmaBufModifierFeatures &
             VK_EXTERNAL_MEMORY_FEATURE_EXPORTABLE_BIT) ? 1 : 0,
            (unsigned)infoPtr->p2dHandleType);
    }

    const VkFormat imageVkFormat = infoPtr->imageCreateInfoShallow.format;
"""

# --------------------------------------------------- E2: allocation handle type
ALLOC_OLD = b"""    if (colorBufferInfo && (*colorBufferInfo)->p2dExternalMode == 1 &&
        mDeviceInfo.supportsExternalMemoryExport) {
        exportAi.handleTypes = VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT;
        vk_append_struct(&allocInfoChain, &exportAi);
        GFXSTREAM_WARNING("GFXDIAG: D1 ALLOC-DMABUF cb=%u exportTypes=0x%x size=%llu",
                          (*colorBufferInfo)->handle, (unsigned)exportAi.handleTypes,
                          (unsigned long long)allocInfo.allocationSize);
    }
"""

ALLOC_NEW = b"""    if (colorBufferInfo && (*colorBufferInfo)->p2dExternalMode == 1 &&
        mDeviceInfo.supportsExternalMemoryExport) {
        // D4BX E2: declare the ColorBuffer's own handle type (DMA_BUF by default,
        // OPAQUE_FD under GFXSTREAM_CB_EXPORT_MODE=opaque) instead of a fixed dma-buf bit.
        exportAi.handleTypes =
            static_cast<VkExternalMemoryHandleTypeFlags>((*colorBufferInfo)->p2dHandleType);
        vk_append_struct(&allocInfoChain, &exportAi);
        GFXSTREAM_WARNING("GFXDIAG: D1 ALLOC-DMABUF cb=%u exportTypes=0x%x size=%llu",
                          (*colorBufferInfo)->handle, (unsigned)exportAi.handleTypes,
                          (unsigned long long)allocInfo.allocationSize);
        const uint32_t p2dAllocType = allocInfo.memoryTypeIndex;
        GFXSTREAM_WARNING(
            "GFXDIAG: D4BX ALLOC cb=%u memTypeIndex=%u flags=0x%x size=%llu exportTypes=0x%x "
            "dedicated=%d",
            (*colorBufferInfo)->handle, p2dAllocType,
            p2dAllocType < mDeviceInfo.memProps.memoryTypeCount
                ? (unsigned)mDeviceInfo.memProps.memoryTypes[p2dAllocType].propertyFlags : 0u,
            (unsigned long long)allocInfo.allocationSize, (unsigned)exportAi.handleTypes,
            (bufferForDedicatedAllocation.hasValue() ||
             imageForDedicatedAllocation.hasValue()) ? 1 : 0);
    }
"""

# ------------------------------------------- E2 + fd raw value: export request
EXPORT_OLD = b"""        int p2dExportFd = -1;
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
"""

EXPORT_NEW = b"""        // D4BX: -12345 is never a real fd, so an out parameter still equal to it after
        // VK_SUCCESS means the driver did not write it at all; any other value (including
        // a literal -1) means it did.
        const int p2dFdUnwritten = -12345;
        int p2dExportFd = p2dFdUnwritten;
        const VkExternalMemoryHandleTypeFlagBits p2dGetFdHandleType =
            static_cast<VkExternalMemoryHandleTypeFlagBits>((*colorBufferInfo)->p2dHandleType);
        const uint32_t p2dStreamHandleType =
            (*colorBufferInfo)->p2dHandleType ==
                    static_cast<uint32_t>(VK_EXTERNAL_MEMORY_HANDLE_TYPE_OPAQUE_FD_BIT)
                ? static_cast<uint32_t>(STREAM_HANDLE_TYPE_MEM_OPAQUE_FD)
                : static_cast<uint32_t>(STREAM_HANDLE_TYPE_MEM_DMABUF);
        VkResult p2dExportRes = VK_ERROR_EXTENSION_NOT_PRESENT;
        if (vk->vkGetMemoryFdKHR) {
            VkMemoryGetFdInfoKHR p2dGetFdInfo = {
                VK_STRUCTURE_TYPE_MEMORY_GET_FD_INFO_KHR,
                nullptr,
                info->memory,
                p2dGetFdHandleType,
            };
            p2dExportRes = vk->vkGetMemoryFdKHR(mDevice, &p2dGetFdInfo, &p2dExportFd);
        }
        const bool p2dExported = (p2dExportRes == VK_SUCCESS) && (p2dExportFd >= 0);
        const bool p2dFdWritten = p2dExportFd != p2dFdUnwritten;
        const uint32_t p2dMemFlags =
            info->typeIndex < mDeviceInfo.memProps.memoryTypeCount
                ? static_cast<uint32_t>(
                      mDeviceInfo.memProps.memoryTypes[info->typeIndex].propertyFlags)
                : 0u;
"""

# ------------------------------------------------ export success: handle + D4BX log
EXPORT_OK_OLD = b"""        if (p2dExported) {
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
            GFXSTREAM_WARNING(
                "GFXDIAG: D4B P2D-DMABUF-EXPORT tiling=%d mod=DRM_FORMAT_MOD_LINEAR(0x0) "
                "exportTypes=0x%x ok=1 fd=%d cb=%u",
                (int)(*colorBufferInfo)->imageCreateInfoShallow.tiling,
                (unsigned)VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT, p2dExportFd,
                (*colorBufferInfo)->handle);
"""

EXPORT_OK_NEW = b"""        if (p2dExported) {
            info->handleInfo = ExternalHandleInfo{
                .handle = static_cast<ExternalHandleType>(p2dExportFd),
                .streamHandleType = p2dStreamHandleType,
            };
            (*colorBufferInfo)->externalMemoryCompatible = true;
            GFXSTREAM_WARNING(
                "GFXDIAG: D1 P2D-DMABUF-EXPORT ok=1 fd=%d cb=%u memindex=%u size=%llu "
                "handleInfo=0x%x",
                p2dExportFd, (*colorBufferInfo)->handle, info->typeIndex,
                (unsigned long long)info->size, (unsigned)p2dStreamHandleType);
            GFXSTREAM_WARNING(
                "GFXDIAG: D4B P2D-DMABUF-EXPORT tiling=%d mod=DRM_FORMAT_MOD_LINEAR(0x0) "
                "exportTypes=0x%x ok=1 fd=%d cb=%u",
                (int)(*colorBufferInfo)->imageCreateInfoShallow.tiling,
                (unsigned)p2dGetFdHandleType, p2dExportFd,
                (*colorBufferInfo)->handle);
            GFXSTREAM_WARNING(
                "GFXDIAG: D4BX EXPORT ok=1 fd=%d result=%d written=%d handleInfo=0x%llx "
                "memTypeIndex=%u flags=0x%x cb=%u mem=%p getFdHandleType=0x%x",
                p2dExportFd, (int)p2dExportRes, p2dFdWritten ? 1 : 0,
                (unsigned long long)p2dStreamHandleType, info->typeIndex, p2dMemFlags,
                (*colorBufferInfo)->handle, (void*)info->memory, (unsigned)p2dGetFdHandleType);
"""

# -------------------------------------------------- export failure: fd raw value
EXPORT_FAIL_OLD = b"""            GFXSTREAM_WARNING(
                "GFXDIAG: D1 P2D-DMABUF-EXPORT ok=0 err=%d cb=%u memindex=%u size=%llu "
                "handleInfo=0x0",
                (int)p2dExportRes, (*colorBufferInfo)->handle, info->typeIndex,
                (unsigned long long)info->size);
"""

EXPORT_FAIL_NEW = b"""            GFXSTREAM_WARNING(
                "GFXDIAG: D1 P2D-DMABUF-EXPORT ok=0 err=%d cb=%u memindex=%u size=%llu "
                "handleInfo=0x0",
                (int)p2dExportRes, (*colorBufferInfo)->handle, info->typeIndex,
                (unsigned long long)info->size);
            GFXSTREAM_WARNING(
                "GFXDIAG: D4BX EXPORT ok=0 fd=%d result=%d written=%d handleInfo=0x%llx "
                "memTypeIndex=%u flags=0x%x cb=%u mem=%p getFdHandleType=0x%x",
                p2dExportFd, (int)p2dExportRes, p2dFdWritten ? 1 : 0, 0ULL, info->typeIndex,
                p2dMemFlags, (*colorBufferInfo)->handle, (void*)info->memory,
                (unsigned)p2dGetFdHandleType);
"""

# ------------------------------------ E1' owner: enable the modifier extension too
EXTEN_OLD = b"""#if defined(__linux__)
    if (emulation->mDeviceInfo.supportsDmaBuf) {
        selectedDeviceExtensionNames.emplace(VK_EXT_EXTERNAL_MEMORY_DMA_BUF_EXTENSION_NAME);
        if (vk_util::extensionSupported(emulation->mDeviceInfo.extensions,
                                        VK_KHR_EXTERNAL_MEMORY_FD_EXTENSION_NAME)) {
            selectedDeviceExtensionNames.emplace(VK_KHR_EXTERNAL_MEMORY_FD_EXTENSION_NAME);
        }
    }
#endif
"""

EXTEN_NEW = b"""#if defined(__linux__)
    if (emulation->mDeviceInfo.supportsDmaBuf) {
        selectedDeviceExtensionNames.emplace(VK_EXT_EXTERNAL_MEMORY_DMA_BUF_EXTENSION_NAME);
        if (vk_util::extensionSupported(emulation->mDeviceInfo.extensions,
                                        VK_KHR_EXTERNAL_MEMORY_FD_EXTENSION_NAME)) {
            selectedDeviceExtensionNames.emplace(VK_KHR_EXTERNAL_MEMORY_FD_EXTENSION_NAME);
        }
        // D4BX: the ColorBuffer image is created with
        // VK_IMAGE_TILING_DRM_FORMAT_MODIFIER_EXT, so this emulation device should enable
        // the extension that defines it.  Enumerate first: enabling an extension the ICD
        // does not advertise would fail vkCreateDevice.
        if (vk_util::extensionSupported(emulation->mDeviceInfo.extensions,
                                        VK_EXT_IMAGE_DRM_FORMAT_MODIFIER_EXTENSION_NAME)) {
            selectedDeviceExtensionNames.emplace(VK_EXT_IMAGE_DRM_FORMAT_MODIFIER_EXTENSION_NAME);
        } else {
            GFXSTREAM_WARNING("GFXDIAG: D4BX device extension absent name=%s",
                              VK_EXT_IMAGE_DRM_FORMAT_MODIFIER_EXTENSION_NAME);
        }
    }
#endif
"""

# ------------------------------------------------------- header: handle type field
H_OLD = b"""        uint8_t p2dExternalMode = 0;
"""

H_NEW = b"""        uint8_t p2dExternalMode = 0;

        // D4BX: external handle type declared when p2dExternalMode == 1.  Default is the
        // D1/D4b dma-buf contract; GFXSTREAM_CB_EXPORT_MODE=opaque switches to an opaque
        // fd.  The matching stream handle type is STREAM_HANDLE_TYPE_MEM_DMABUF (0x2) or
        // STREAM_HANDLE_TYPE_MEM_OPAQUE_FD (0x1).
        uint32_t p2dHandleType = static_cast<uint32_t>(
            VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT);
"""

# ------------------------------------------------- decoder: zero-size import whitelist
ZERO_OLD = b"""                     cb->memory.handleInfo->streamHandleType == STREAM_HANDLE_TYPE_MEM_DMABUF)) {
                    // D1: a dma-buf ColorBuffer handle is an fd, not an AHardwareBuffer.
                    if (cb->memory.handleInfo->streamHandleType == STREAM_HANDLE_TYPE_MEM_DMABUF) {
"""

ZERO_NEW = b"""                     cb->memory.handleInfo->streamHandleType == STREAM_HANDLE_TYPE_MEM_DMABUF ||
                     cb->memory.handleInfo->streamHandleType == STREAM_HANDLE_TYPE_MEM_OPAQUE_FD)) {
                    // D1: a dma-buf ColorBuffer handle is an fd, not an AHardwareBuffer.
                    // D4BX: an opaque-fd ColorBuffer handle is the same fd shape.
                    if (cb->memory.handleInfo->streamHandleType == STREAM_HANDLE_TYPE_MEM_DMABUF ||
                        cb->memory.handleInfo->streamHandleType == STREAM_HANDLE_TYPE_MEM_OPAQUE_FD) {
"""

# ------------------------------------------------- decoder: CIB1 alias handle type
CIB_OLD = b"""        VkExternalMemoryImageCreateInfo external{VK_STRUCTURE_TYPE_EXTERNAL_MEMORY_IMAGE_CREATE_INFO,
            list ? static_cast<const void*>(&localList)
                 : (p2dBridgeModifierTiling
                        ? static_cast<const void*>(&p2dBridgeModifierList)
                        : nullptr),
            p2dBridgeDmaBuf ? VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT
                            : VK_EXTERNAL_MEMORY_HANDLE_TYPE_ANDROID_HARDWARE_BUFFER_BIT_ANDROID};
"""

CIB_NEW = b"""        // D4BX E2: the alias image must declare the ColorBuffer's actual fd handle type.
        const VkExternalMemoryHandleTypeFlagBits p2dBridgeHandleType =
            p2dBridgeDmaBuf
                ? static_cast<VkExternalMemoryHandleTypeFlagBits>(cb->p2dHandleType)
                : VK_EXTERNAL_MEMORY_HANDLE_TYPE_ANDROID_HARDWARE_BUFFER_BIT_ANDROID;
        VkExternalMemoryImageCreateInfo external{VK_STRUCTURE_TYPE_EXTERNAL_MEMORY_IMAGE_CREATE_INFO,
            list ? static_cast<const void*>(&localList)
                 : (p2dBridgeModifierTiling
                        ? static_cast<const void*>(&p2dBridgeModifierList)
                        : nullptr),
            p2dBridgeHandleType};
"""

MARKERS = [
    b"GFXDIAG: D4BX EXTPROPS",
    b"GFXDIAG: D4BX MEMTYPES",
    b"GFXDIAG: D4BX EXPORT",
    b"GFXDIAG: D4BX MEMTYPE-OVERRIDE",
    b"GFXDIAG: D4BX EXPORT-MODE",
    b"GFXSTREAM_CB_EXPORT_MODE",
    b"GFXSTREAM_CB_MEMTYPE_INDEX",
    b"GFXDIAG: D4BX ALLOC",
]


def fail(message):
    print(f"patch_host_cb_export_probe: FAIL: {message}", file=sys.stderr)
    raise SystemExit(1)


def replace_once(data, old, new, label):
    count = data.count(old)
    if count != 1:
        fail(f"anchor '{label}' count is {count}, expected exactly 1")
    return data.replace(old, new, 1)


def main():
    if len(sys.argv) != 2:
        fail("usage: patch_host_cb_export_probe.py <source-root>")

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

    # D1 + D4b must be applied first: the anchors below pin their output.
    if ops.count(b"GFXDIAG: D4B P2D-IMAGE-BITS") != 1:
        fail("expected one D4B P2D-IMAGE-BITS trace before patching (D4b applied?)")
    if ops.count(b"GFXDIAG: D4B P2D-DMABUF-EXPORT") != 1:
        fail("expected one D4B P2D-DMABUF-EXPORT trace before patching (D4b applied?)")
    if ops.count(b"GFXDIAG: D1 ALLOC-DMABUF") != 1:
        fail("expected one D1 ALLOC-DMABUF trace before patching (D1 applied?)")
    if ops_h.count(b"uint8_t p2dExternalMode = 0;") != 1:
        fail("expected one p2dExternalMode field before patching (D1 applied?)")
    if dec.count(b"p2dBridgeDmaBuf") != 4:
        fail("expected four p2dBridgeDmaBuf decisions before patching (D1 applied?)")

    ops = replace_once(ops, PROBE_OLD, PROBE_NEW, "E1 probe helper")
    ops = replace_once(ops, BITS_OLD, BITS_NEW, "E1 probe call and memory types")
    ops = replace_once(ops, MODE_OLD, MODE_NEW, "export mode and memory type switches")
    ops = replace_once(ops, EXT_TYPE_OLD, EXT_TYPE_NEW, "image handle type")
    ops = replace_once(ops, MEMTYPE_OLD, MEMTYPE_NEW, "memory type override")
    ops = replace_once(ops, ALLOC_OLD, ALLOC_NEW, "allocation export handle type")
    ops = replace_once(ops, EXPORT_OLD, EXPORT_NEW, "export request handle type and fd seed")
    ops = replace_once(ops, EXPORT_OK_OLD, EXPORT_OK_NEW, "export success trace")
    ops = replace_once(ops, EXPORT_FAIL_OLD, EXPORT_FAIL_NEW, "export failure trace")
    ops = replace_once(ops, EXTEN_OLD, EXTEN_NEW, "modifier device extension")
    ops_h = replace_once(ops_h, H_OLD, H_NEW, "ColorBuffer handle type field")
    dec = replace_once(dec, ZERO_OLD, ZERO_NEW, "zero-size import whitelist")
    dec = replace_once(dec, CIB_OLD, CIB_NEW, "CIB1 alias handle type")

    checks = [
        (ops.count(b"GFXDIAG: D4BX EXTPROPS"), 1, "E1 external properties trace"),
        (ops.count(b"GFXDIAG: D4BX MEMTYPES"), 1, "E1 memory type trace"),
        (ops.count(b"GFXDIAG: D4BX EXPORT-MODE"), 1, "E2 export mode trace"),
        (ops.count(b"GFXDIAG: D4BX MEMTYPE-OVERRIDE"), 1, "E3 override trace"),
        (ops.count(b"GFXDIAG: D4BX MEMTYPE-PICK"), 1, "E3 chosen type trace"),
        (ops.count(b"GFXDIAG: D4BX EXPORT ok=1"), 1, "export success raw fd trace"),
        (ops.count(b"GFXDIAG: D4BX EXPORT ok=0"), 1, "export failure raw fd trace"),
        (ops.count(b"GFXDIAG: D4BX ALLOC"), 1, "allocation trace"),
        (ops.count(b"GFXSTREAM_CB_EXPORT_MODE"), 3, "E2 environment variable"),
        (ops.count(b"GFXSTREAM_CB_MEMTYPE_INDEX"), 2, "E3 environment variable"),
        (ops.count(b"VK_EXTERNAL_MEMORY_HANDLE_TYPE_OPAQUE_FD_BIT"), 9,
         "opaque fd handle type uses"),
        (ops.count(b"VK_EXT_IMAGE_DRM_FORMAT_MODIFIER_EXTENSION_NAME"), 3,
         "modifier extension name uses"),
        (ops.count(b"STREAM_HANDLE_TYPE_MEM_OPAQUE_FD"), 5, "opaque fd stream handle type"),
        (ops.count(b"GFXDIAG: D4B P2D-IMAGE-BITS"), 1, "D4B image requirements preserved"),
        (ops.count(b"GFXDIAG: D4B P2D-DMABUF-EXPORT"), 1, "D4B export trace preserved"),
        (ops.count(b"GFXDIAG: D4B P2D-IMAGE-MOD"), 1, "D4B image modifier preserved"),
        (ops.count(b"GFXDIAG: D1 P2D-DMABUF-EXPORT"), 2, "D1 export traces preserved"),
        (ops.count(b"GFXDIAG: D1 ALLOC-DMABUF"), 1, "D1 allocation trace preserved"),
        (ops.count(b"GFXDIAG: D1 SKIP-AHB"), 1, "D1 AHB skip preserved"),
        (ops.count(b"imageCi->tiling = VK_IMAGE_TILING_OPTIMAL"), 2,
         "both fallback tiling resets preserved"),
        (ops_h.count(b"p2dHandleType"), 1, "header handle type field"),
        (dec.count(b"GFXDIAG: D4B CIB1-IMAGE-MOD"), 1, "D4B CIB1 modifier trace preserved"),
        (dec.count(b"GFXDIAG: D1 ZERO-SIZE-ALLOW"), 1, "D1 zero-size trace preserved"),
        (dec.count(b"STREAM_HANDLE_TYPE_MEM_OPAQUE_FD"), 5, "decoder opaque fd uses"),
        (dec.count(b"p2dBridgeDmaBuf"), 4, "D1 CIB1 dma-buf decisions preserved"),
        (dec.count(b"p2dBridgeHandleType"), 2, "CIB1 alias handle type"),
    ]
    for actual, expected, description in checks:
        if actual != expected:
            fail(f"post-check {description}: count {actual}, expected {expected}")
    if ops.count(b"STREAM_HANDLE_TYPE_MEM_DMABUF") < 5:
        fail("post-check D1 MEM_DMABUF handle occurrences: fewer than 5")

    if ops == ops_path.read_bytes():
        fail("vk_common_operations.cpp replacement produced no change")
    if dec == dec_path.read_bytes():
        fail("vk_decoder_global_state.cpp replacement produced no change")
    if ops_h == ops_h_path.read_bytes():
        fail("vk_common_operations.h replacement produced no change")

    ops_path.write_bytes(ops)
    ops_h_path.write_bytes(ops_h)
    dec_path.write_bytes(dec)

    ops_line = ops[: ops.index(b"GFXDIAG: D4BX EXTPROPS")].count(b"\n") + 1
    dec_line = dec[: dec.index(b"p2dBridgeHandleType")].count(b"\n") + 1
    print(f"patch_host_cb_export_probe: patched {ops_path} at line {ops_line}, "
          f"{ops_h_path} and {dec_path} at line {dec_line}")


if __name__ == "__main__":
    main()
