#!/usr/bin/env python3
"""Apply only the DroidVM Mali diagnostic host semantic fixes."""
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


# Do not advertise the command that crashes the guest Vulkan constructor.
edit(
    "host/virtio_gpu_frontend.cpp",
    "capset->hasSetMetadataCommand = 1;",
    "capset->hasSetMetadataCommand = 0;",
)

# For imported AHB allocations, the AHB properties supply size and memory types.
# Mali can report zero/disjoint pre-bind image requirements (0 or 0x1040 vs 0x2).
edit(
    "host/vulkan/vk_common_operations.cpp",
    """                if (!combinedBits) {
                    GFXSTREAM_WARNING(
                        "No common memory type for AHB (0x%x) and image (0x%x). "
                        "Falling back to non-exportable allocation.",
                        ahbProps.memoryTypeBits, cbInfoPtr->imageMemReqs.memoryTypeBits);
                    AHardwareBuffer_release(ahb);
                    break;
                }""",
    """                if (!combinedBits) {
                    // Mali may report zero/disjoint image requirements before AHB import.
                    // Use the imported AHB's authoritative memory types and allocation size.
                    GFXSTREAM_WARNING(
                        "No common memory type for AHB (0x%x) and image (0x%x). "
                        "Using AHB memory type bits for import.",
                        ahbProps.memoryTypeBits, cbInfoPtr->imageMemReqs.memoryTypeBits);
                    combinedBits = ahbProps.memoryTypeBits;
                }""",
)

# Preserve both explicit environment and renderer-feature overrides.
edit(
    "host/virtio_gpu_gfxstream_renderer.cpp",
    """    // On Android the platform default for external memory is AndroidAHB and nothing else --
    // calculateMode()'s Android list is one entry long, with no fd fallback. That is wrong for
    // this route twice over. The host driver here is Mesa turnip, whose exports are dma-buf fds;
    // and every host-visible blob, the GpuPool and the guest-alloc slice are dma-buf all the way
    // down, so a ColorBuffer exported as an AHardwareBuffer cannot be handed to any of them. A
    // host that picks AHB advertises external memory it cannot actually share, and the guest
    // renders into memory nothing scans out.
    //
    // OpaqueFd is the mode that means "fd", and it upgrades itself to
    // VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT wherever the driver reports
    // VK_EXT_external_memory_dma_buf, which turnip does. Set before the feature string is parsed,
    // so an explicit VulkanExternalMemoryMode:... there still wins; GFXSTREAM_EXTERNAL_MEMORY_MODE
    // is the same escape hatch for a launcher that cannot reach the feature string.
    //
    // On a host driver with no fd export -- the closed Adreno blob -- this resolves to
    // NotSupported rather than silently falling back to AHB. That is deliberate: the AHB route
    // cannot back host-visible coherent memory on this stack at all, so failing where the mode is
    // chosen is far easier to read than a black screen several layers down.
    {
        const std::string modeOverride =
            gfxstream::base::getEnvironmentVariable("GFXSTREAM_EXTERNAL_MEMORY_MODE");
        const std::string mode = modeOverride.empty() ? "OpaqueFd" : modeOverride;
        if (!features.processFeatureString("VulkanExternalMemoryMode:" + mode,
                                           "DroidVM default: the host driver exports dma-buf")) {""",
    """    // Match the Android platform default for the Mali system Vulkan driver.
    // Explicit environment and renderer-feature overrides still take precedence.
    {
        const std::string modeOverride =
            gfxstream::base::getEnvironmentVariable("GFXSTREAM_EXTERNAL_MEMORY_MODE");
        const std::string mode = modeOverride.empty() ? "AndroidAHB" : modeOverride;
        if (!features.processFeatureString("VulkanExternalMemoryMode:" + mode,
                                           "DroidVM Mali default: AndroidAHB")) {""",
)

# Append the runtime override after launcher-provided features, as in the local
# mali-compat reference. This leaves the kernel 6.6 default policy unchanged.
edit(
    "host/virtio_gpu_gfxstream_renderer.cpp",
    '    for (const std::string& rendererFeature : gfxstream::Split(rendererFeatures, ",;")) {',
    '''    // Allow explicit bring-up overrides on kernels outside the default policy gate.
    std::string effectiveRendererFeatures = rendererFeatures;
    {
        const std::string envFeatures =
            gfxstream::base::getEnvironmentVariable("GFXSTREAM_RENDERER_FEATURES");
        if (!envFeatures.empty()) {
            if (!effectiveRendererFeatures.empty()) {
                effectiveRendererFeatures += ",";
            }
            effectiveRendererFeatures += envFeatures;
            GFXSTREAM_INFO("Applying renderer features from GFXSTREAM_RENDERER_FEATURES: %s",
                           envFeatures.c_str());
        }
    }

    for (const std::string& rendererFeature : gfxstream::Split(effectiveRendererFeatures, ",;")) {''',
)

# NDK CMake uses the syscall branch. Both entry points must create sealable
# memfds; seal only shared memory, after sizing it, not arbitrary file backing.
edit(
    "common/base/SharedMemory_posix.cpp",
    '#include "gfxstream/EintrWrapper.h"',
    '''#if defined(__linux__) || defined(__ANDROID__)
#ifndef MFD_CLOEXEC
#define MFD_CLOEXEC 0x0001U
#endif
#ifndef MFD_ALLOW_SEALING
#define MFD_ALLOW_SEALING 0x0002U
#endif
#endif

#include "gfxstream/EintrWrapper.h"''',
)
edit(
    "common/base/SharedMemory_posix.cpp",
    'mFd = syscall(__NR_memfd_create, mName.c_str(), FD_CLOEXEC);',
    'mFd = syscall(__NR_memfd_create, mName.c_str(), MFD_CLOEXEC | MFD_ALLOW_SEALING);',
)
edit(
    "common/base/SharedMemory_posix.cpp",
    '''#if defined(HAVE_MEMFD_CREATE)
        // DroidVM: seal against SHRINK only (protects existing mappings from SIGBUS), NOT GROW/SEAL.
        // The VMM (crosvm) grows a host-visible blob's shmem to a 2MB multiple to fold it into
        // order-9 folios (host-visible blob backing; see HostVisibleFolio.h). Only crosvm holds the
        // fd -- the guest reaches the memory through a BAR mapping, never the fd -- so allowing grow
        // does not widen the guest's authority. Growing is additive and never invalidates the
        // original-size mappings other consumers hold.
        if (fcntl(mFd, F_ADD_SEALS, F_SEAL_SHRINK) == -1) {
            err = -errno;
            close();
            return err;
        }
#endif''',
    '''#if defined(__linux__) || defined(__ANDROID__)
        // udmabuf requires SHRINK sealing. Keep mappings writable and avoid SEAL_SEAL.
        if (mShareType == ShareType::SHARED_MEMORY &&
            fcntl(mFd, F_ADD_SEALS, F_SEAL_SHRINK | F_SEAL_GROW) == -1) {
            err = -errno;
            close();
            return err;
        }
#endif''',
)

# Log RingBlob export results without changing handle ownership or return values.
edit(
    "host/virtio_gpu_resource.cpp",
    '''            ShouldPinRingBlobsForGunyah() ? memory->dupHandle() : memory->releaseHandle();
#ifdef _WIN32''',
    '''            ShouldPinRingBlobsForGunyah() ? memory->dupHandle() : memory->releaseHandle();
        const int ringExportErrno = errno;
#ifdef _WIN32''',
)
edit(
    "host/virtio_gpu_resource.cpp",
    '''        outHandle->handle_type = STREAM_HANDLE_TYPE_MEM_SHM;
        return 0;''',
    '''        outHandle->handle_type = STREAM_HANDLE_TYPE_MEM_SHM;
        GFXSTREAM_WARNING(
            "RINGBLOB-EXPORT: res=%u type=0x%x fd=%lld errno=%d",
            mId, outHandle->handle_type, (long long)outHandle->os_handle,
            handle == gfxstream::base::SharedMemory::invalidHandle() ? ringExportErrno : 0);
        errno = ringExportErrno;
        return 0;''',
)
edit(
    "host/virtio_gpu_resource.cpp",
    '''        if (!memory->isExportable()) {
            return -EINVAL;
        }''',
    '''        if (!memory->isExportable()) {
            const int savedErrno = errno;
            GFXSTREAM_WARNING(
                "RINGBLOB-EXPORT: res=%u type=0x%x fd=-1 ret=%d errno=%d not-exportable",
                mId, STREAM_HANDLE_TYPE_MEM_SHM, -EINVAL, savedErrno);
            errno = savedErrno;
            return -EINVAL;
        }''',
)

# Android AHB is not an fd. Observe its actual stream type and opaque value;
# do not dup, acquire, release, reject, or reinterpret it as an fd here.
edit(
    "host/virtio_gpu_resource.cpp",
    '''    auto rawDescriptor = descriptorInfo.handle;
    switch (descriptorInfo.streamHandleType) {''',
    '''    auto rawDescriptor = descriptorInfo.handle;
    const int entryErrno = errno;
    GFXSTREAM_WARNING("FILL-EXPORT: type=0x%x input_handle=%lld errno=%d (AHB is not fd)",
                      descriptorInfo.streamHandleType, (long long)rawDescriptor, entryErrno);
    errno = entryErrno;
    switch (descriptorInfo.streamHandleType) {''',
)
edit(
    "host/virtio_gpu_resource.cpp",
    '''            if (static_cast<int>(rawDescriptor) < 0) {
                return -EINVAL;
            }''',
    '''            if (static_cast<int>(rawDescriptor) < 0) {
                const int savedErrno = errno;
                GFXSTREAM_WARNING("FILL-EXPORT: type=0x%x fd=%lld ret=%d errno=%d",
                                  descriptorInfo.streamHandleType, (long long)rawDescriptor,
                                  -EINVAL, savedErrno);
                errno = savedErrno;
                return -EINVAL;
            }''',
)
edit(
    "host/virtio_gpu_resource.cpp",
    '''    outHandle->handle_type = descriptorInfo.streamHandleType;
    return 0;''',
    '''    outHandle->handle_type = descriptorInfo.streamHandleType;
#ifdef __ANDROID__
    const int exportErrno = errno;
    GFXSTREAM_WARNING("FILL-EXPORT: type=0x%x output_handle=%lld ret=0 errno=0",
                      outHandle->handle_type, (long long)outHandle->os_handle);
    errno = exportErrno;
#endif
    return 0;''',
)

# The alias importer must choose an import struct from the actual stream type.
# PLATFORM_AHB carries a pointer, never an integer fd. Keep an acquired AHB
# reference until vkAllocateMemory returns; a successful fd import consumes its dup.
edit(
    "host/vulkan/vk_common_operations.cpp",
    "    VkImportMemoryFdInfoKHR importInfoFd;",
    '''    VkImportMemoryFdInfoKHR importInfoFd;
    int importedFd = -1;
#ifdef __ANDROID__
    AHardwareBuffer* importedAhb = nullptr;
    VkImportAndroidHardwareBufferInfoANDROID importInfoAhb = {};
#endif''',
)
edit(
    "host/vulkan/vk_common_operations.cpp",
    '''            auto dupHandle = dupExternalMemory(handleInfo);
            if (!dupHandle) {
                GFXSTREAM_ERROR(
                    "importExternalMemory: Failed to duplicate handleInfo.handle: 0x%x, "
                    "streamHandleType: %d",
                    handleInfo->handle, handleInfo->streamHandleType);
                return false;
            }
            importInfoFd = {
                VK_STRUCTURE_TYPE_IMPORT_MEMORY_FD_INFO_KHR,
                dedicatedAllocInfoPtr,
                VK_EXTERNAL_MEMORY_HANDLE_TYPE_OPAQUE_FD_BIT,
                static_cast<int>(dupHandle->handle),
            };
            importInfoPtr = &importInfoFd;
            break;''',
    '''            if (!handleInfo) {
                GFXSTREAM_ERROR("importExternalMemory: missing external handle info");
                return false;
            }
            switch (handleInfo->streamHandleType) {
#ifdef __ANDROID__
                case STREAM_HANDLE_TYPE_PLATFORM_AHB:
                    importedAhb = reinterpret_cast<AHardwareBuffer*>(handleInfo->handle);
                    if (!importedAhb) return false;
                    AHardwareBuffer_acquire(importedAhb);
                    importInfoAhb = {
                        VK_STRUCTURE_TYPE_IMPORT_ANDROID_HARDWARE_BUFFER_INFO_ANDROID,
                        dedicatedAllocInfoPtr,
                        importedAhb,
                    };
                    importInfoPtr = &importInfoAhb;
                    break;
#endif
                case STREAM_HANDLE_TYPE_MEM_DMABUF:
                case STREAM_HANDLE_TYPE_MEM_OPAQUE_FD: {
                    auto dupHandle = dupExternalMemory(handleInfo);
                    if (!dupHandle || dupHandle->getFd() < 0) {
                        GFXSTREAM_ERROR("importExternalMemory: fd duplication failed type=0x%x",
                                        handleInfo->streamHandleType);
                        return false;
                    }
                    importedFd = dupHandle->getFd();
                    importInfoFd = {
                        VK_STRUCTURE_TYPE_IMPORT_MEMORY_FD_INFO_KHR,
                        dedicatedAllocInfoPtr,
                        handleInfo->streamHandleType == STREAM_HANDLE_TYPE_MEM_DMABUF
                            ? VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT
                            : VK_EXTERNAL_MEMORY_HANDLE_TYPE_OPAQUE_FD_BIT,
                        importedFd,
                    };
                    importInfoPtr = &importInfoFd;
                    break;
                }
                default:
                    GFXSTREAM_ERROR("importExternalMemory: unsupported stream type=0x%x (SHM is not AHB)",
                                    handleInfo->streamHandleType);
                    return false;
            }
            break;''',
)
edit(
    "host/vulkan/vk_common_operations.cpp",
    '''    VkResult res = vk->vkAllocateMemory(targetDevice, &allocInfo, nullptr, out);

    if (res != VK_SUCCESS) {''',
    '''    GFXSTREAM_WARNING(
        "ALIAS-ALLOC: before-driver type=0x%x handle=%lld fd=%d hostptr=%p size=%llu "
        "memindex=%u dedicatedImage=%p dedicatedBuffer=%p importSType=%u",
        handleInfo ? handleInfo->streamHandleType : 0,
        handleInfo ? (long long)handleInfo->handle : 0LL, importedFd, info->hostAllocationPtr,
        (unsigned long long)allocInfo.allocationSize, allocInfo.memoryTypeIndex,
        dedicatedAllocInfoPtr ? (void*)dedicatedAllocInfoPtr->image : nullptr,
        dedicatedAllocInfoPtr ? (void*)dedicatedAllocInfoPtr->buffer : nullptr,
        importInfoPtr ? (unsigned)((const VkBaseInStructure*)importInfoPtr)->sType : 0);
    VkResult res = vk->vkAllocateMemory(targetDevice, &allocInfo, nullptr, out);
#ifdef __ANDROID__
    if (importedAhb) AHardwareBuffer_release(importedAhb);
#endif
#ifndef _WIN32
    if (res != VK_SUCCESS && importedFd >= 0) close(importedFd);
#endif
    GFXSTREAM_WARNING("ALIAS-ALLOC: after-driver result=%d memory=%p", (int)res,
                      res == VK_SUCCESS ? (void*)*out : nullptr);

    if (res != VK_SUCCESS) {''',
)
edit(
    "host/vulkan/vk_common_operations.cpp",
    "    bool useDedicated = mUseDedicatedAllocations;",
    '''    bool useDedicated = mUseDedicatedAllocations ||
                        getExternalMemoryMode() == ExternalMemory::Mode::AndroidAHB;''',
)
edit(
    "host/vulkan/vk_android_native_buffer_operations.cpp",
    "        if (importedColorBufferMemoryInfo.dedicatedAllocation) {",
    '''        if (importedColorBufferMemoryInfo.dedicatedAllocation ||
            (importedColorBufferMemoryInfo.handleInfo &&
             importedColorBufferMemoryInfo.handleInfo->streamHandleType == STREAM_HANDLE_TYPE_PLATFORM_AHB)) {''',
)

# Preserve diagnostic provenance as values only, never as retained handles or
# dereferenceable pointers: the imported fd is consumed on successful allocation.
edit(
    "host/vulkan/vk_decoder_internal_structs.h",
    "    uint32_t memoryIndex = 0;",
    '''    uint32_t memoryIndex = 0;
    uint32_t diagnosticStreamType = 0;
    int diagnosticImportFd = -1;
    const void* diagnosticAhb = nullptr;
    const void* diagnosticHostPtr = nullptr;''',
)
edit(
    "host/vulkan/vk_decoder_internal_structs.h",
    "    VkImageCreateInfo imageCreateInfoShallow;",
    '''    VkImageCreateInfo imageCreateInfoShallow;
    std::string diagnosticCreatePNextTypes;
    VkExternalMemoryHandleTypeFlags diagnosticExternalHandleTypes = 0;''',
)
edit(
    "host/vulkan/vk_decoder_global_state.cpp",
    "        imageInfo.imageCreateInfoShallow = vk_make_orphan_copy(*pCreateInfo);",
    '''        imageInfo.imageCreateInfoShallow = vk_make_orphan_copy(*pCreateInfo);
        const auto* diagnosticExternalCi = vk_find_struct<VkExternalMemoryImageCreateInfo>(pCreateInfo);
        imageInfo.diagnosticExternalHandleTypes = diagnosticExternalCi ? diagnosticExternalCi->handleTypes : 0;
        const auto* diagnosticNext = reinterpret_cast<const VkBaseInStructure*>(pCreateInfo->pNext);
        for (unsigned depth = 0; diagnosticNext && depth < 32; ++depth) {
            imageInfo.diagnosticCreatePNextTypes += std::to_string((unsigned)diagnosticNext->sType) + ",";
            diagnosticNext = diagnosticNext->pNext;
        }''',
)
edit(
    "host/vulkan/vk_decoder_global_state.cpp",
    '''        VALIDATE_REQUIRED_HANDLE(memory);
        VkResult result = vk->vkBindImageMemory(device, image, memory, memoryOffset);''',
    '''        VALIDATE_REQUIRED_HANDLE(memory);
        {
            std::lock_guard<std::mutex> diagnosticLock(mMutex);
            const auto* diagnosticImage = gfxstream::base::find(mImageInfo, image);
            const auto* diagnosticMemory = gfxstream::base::find(mMemoryInfo, memory);
            if (diagnosticImage) {
                const auto& ci = diagnosticImage->imageCreateInfoShallow;
                GFXSTREAM_WARNING(
                    "BIND-BEFORE: image=%p memory=%p offset=%llu fmt=%d flags=0x%x usage=0x%x "
                    "tiling=%d extent=%ux%ux%u createPNext=%s externalTypes=0x%x",
                    (void*)image, (void*)memory, (unsigned long long)memoryOffset,
                    (int)ci.format, ci.flags, ci.usage, (int)ci.tiling,
                    ci.extent.width, ci.extent.height, ci.extent.depth,
                    diagnosticImage->diagnosticCreatePNextTypes.c_str(),
                    diagnosticImage->diagnosticExternalHandleTypes);
            }
            if (diagnosticMemory) {
                GFXSTREAM_WARNING(
                    "BIND-BEFORE-MEM: memory=%p type=0x%x importFd=%d ahb=%p hostptr=%p "
                    "mapped=%p size=%llu memindex=%u blobId=%llu cb=%u poolOff=%lld",
                    (void*)memory, diagnosticMemory->diagnosticStreamType,
                    diagnosticMemory->diagnosticImportFd, diagnosticMemory->diagnosticAhb,
                    diagnosticMemory->diagnosticHostPtr, diagnosticMemory->ptr,
                    (unsigned long long)diagnosticMemory->size, diagnosticMemory->memoryIndex,
                    (unsigned long long)diagnosticMemory->blobId,
                    diagnosticMemory->boundColorBuffer ? *diagnosticMemory->boundColorBuffer : 0,
                    (long long)diagnosticMemory->poolOffset);
            }
            for (auto* next = reinterpret_cast<const VkBaseInStructure*>(bimi->pNext);
                 next; next = next->pNext) {
                GFXSTREAM_WARNING("BIND-BEFORE-PNEXT: image=%p sType=%u",
                                  (void*)image, (unsigned)next->sType);
            }
        }
        VkResult result = vk->vkBindImageMemory(device, image, memory, memoryOffset);''',
)
edit(
    "host/vulkan/vk_decoder_global_state.cpp",
    '''        auto appendExternalMemoryImport = [&](const ExternalHandleInfo& handleInfo) -> bool {
            switch''',
    '''        auto appendExternalMemoryImport = [&](const ExternalHandleInfo& handleInfo) -> bool {
            GFXSTREAM_WARNING(
                "DECODER-IMPORT: type=0x%x handle=%lld size=%llu memindex=%u dedicated=%d image=%p buffer=%p",
                handleInfo.streamHandleType, (long long)handleInfo.handle,
                (unsigned long long)localAllocInfo.allocationSize, localAllocInfo.memoryTypeIndex,
                (int)shouldUseDedicatedAllocInfo, (void*)localDedicatedAllocInfo.image,
                (void*)localDedicatedAllocInfo.buffer);
            switch''',
)
edit(
    "host/vulkan/vk_decoder_global_state.cpp",
    "        VkResult result = vk->vkAllocateMemory(device, &localAllocInfo, nullptr, pMemory);",
    '''        uint32_t diagnosticStreamType = 0;
        int diagnosticImportFd = -1;
        const void* diagnosticAhb = nullptr;
        const auto* diagnosticFdInfo = vk_find_struct<VkImportMemoryFdInfoKHR>(&localAllocInfo);
        if (diagnosticFdInfo) {
            diagnosticImportFd = diagnosticFdInfo->fd;
            diagnosticStreamType = diagnosticFdInfo->handleType == VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT
                ? STREAM_HANDLE_TYPE_MEM_DMABUF : STREAM_HANDLE_TYPE_MEM_OPAQUE_FD;
        }
#ifdef __ANDROID__
        const auto* diagnosticAhbInfo = vk_find_struct<VkImportAndroidHardwareBufferInfoANDROID>(&localAllocInfo);
        if (diagnosticAhbInfo) {
            diagnosticAhb = diagnosticAhbInfo->buffer;
            diagnosticStreamType = STREAM_HANDLE_TYPE_PLATFORM_AHB;
        }
#endif
        const auto* diagnosticHostInfo = vk_find_struct<VkImportMemoryHostPointerInfoEXT>(&localAllocInfo);
        const void* diagnosticHostPtr = diagnosticHostInfo ? diagnosticHostInfo->pHostPointer : nullptr;
        const auto* diagnosticDedicated = vk_find_struct<VkMemoryDedicatedAllocateInfo>(&localAllocInfo);
        GFXSTREAM_WARNING(
            "DECODER-ALLOC-BEFORE: type=0x%x fd=%d ahb=%p hostptr=%p size=%llu memindex=%u "
            "dedicatedImage=%p dedicatedBuffer=%p",
            diagnosticStreamType, diagnosticImportFd, diagnosticAhb, diagnosticHostPtr,
            (unsigned long long)localAllocInfo.allocationSize, localAllocInfo.memoryTypeIndex,
            diagnosticDedicated ? (void*)diagnosticDedicated->image : nullptr,
            diagnosticDedicated ? (void*)diagnosticDedicated->buffer : nullptr);
        VkResult result = vk->vkAllocateMemory(device, &localAllocInfo, nullptr, pMemory);''',
)
edit(
    "host/vulkan/vk_decoder_global_state.cpp",
    "        memoryInfo.memoryIndex = localAllocInfo.memoryTypeIndex;",
    '''        memoryInfo.memoryIndex = localAllocInfo.memoryTypeIndex;
        memoryInfo.diagnosticStreamType = diagnosticStreamType;
        memoryInfo.diagnosticImportFd = diagnosticImportFd;
        memoryInfo.diagnosticAhb = diagnosticAhb;
        memoryInfo.diagnosticHostPtr = diagnosticHostPtr;''',
)
edit(
    "host/vulkan/vk_decoder_global_state.cpp",
    '''            if (pExternalMemoryImageCi && pExternalMemoryImageCi->handleTypes &
                                              VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT) {''',
    '''            GFXSTREAM_WARNING(
                "IMAGE-TRANSFORM: input fmt=%d flags=0x%x usage=0x%x tiling=%d externalTypes=0x%x ahbRequested=%d",
                (int)imageCreateInfo.format, imageCreateInfo.flags, imageCreateInfo.usage,
                (int)imageCreateInfo.tiling,
                pExternalMemoryImageCi ? pExternalMemoryImageCi->handleTypes : 0,
                (int)importAndroidHardwareBuffer);
            if (pExternalMemoryImageCi && pExternalMemoryImageCi->handleTypes &
                                              VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT) {''',
)
edit(
    "host/vulkan/vk_decoder_global_state.cpp",
    '''            // If the VkImage is going to bind to a ColorBuffer, we have to make sure the VkImage''',
    '''            GFXSTREAM_WARNING("IMAGE-TRANSFORM: output externalTypes=0x%x ahbBranch=%d",
                              pExternalMemoryImageCi ? pExternalMemoryImageCi->handleTypes : 0,
                              (int)importAndroidHardwareBuffer);
            // If the VkImage is going to bind to a ColorBuffer, we have to make sure the VkImage''',
)

edit(
    "host/vulkan/vk_common_operations.cpp",
    '''    VkResult bindImageMemoryRes = vk->vkBindImageMemory(
        mDevice, infoPtr->image, infoPtr->memory.memory, infoPtr->memory.bindOffset);''',
    '''    GFXSTREAM_WARNING(
        "CB-BIND-BEFORE: image=%p memory=%p offset=%llu fmt=%d flags=0x%x usage=0x%x tiling=%d "
        "externalTypes=0x%x type=0x%x handle=%lld hostptr=%p memindex=%u dedicated=%d",
        (void*)infoPtr->image, (void*)infoPtr->memory.memory,
        (unsigned long long)infoPtr->memory.bindOffset, (int)imageCi->format,
        imageCi->flags, imageCi->usage, (int)imageCi->tiling, extImageCi.handleTypes,
        infoPtr->memory.handleInfo ? infoPtr->memory.handleInfo->streamHandleType : 0,
        infoPtr->memory.handleInfo ? (long long)infoPtr->memory.handleInfo->handle : 0LL,
        infoPtr->memory.hostAllocationPtr, infoPtr->memory.typeIndex,
        (int)infoPtr->memory.dedicatedAllocation);
    VkResult bindImageMemoryRes = vk->vkBindImageMemory(
        mDevice, infoPtr->image, infoPtr->memory.memory, infoPtr->memory.bindOffset);''',
)
edit(
    "host/vulkan/vk_android_native_buffer_operations.cpp",
    '''    if (VK_SUCCESS != vk->vkBindImageMemory(device, out->mImage, out->mImageMemory, bindOffset)) {''',
    '''    GFXSTREAM_WARNING(
        "ANB-BIND-BEFORE: image=%p memory=%p offset=%llu fmt=%d flags=0x%x usage=0x%x tiling=%d",
        (void*)out->mImage, (void*)out->mImageMemory, (unsigned long long)bindOffset,
        (int)pCreateInfo->format, pCreateInfo->flags, pCreateInfo->usage, (int)pCreateInfo->tiling);
    if (VK_SUCCESS != vk->vkBindImageMemory(device, out->mImage, out->mImageMemory, bindOffset)) {''',
)

# external_memory.cpp already defaults to AndroidAHB: verify, do not modify it.
platform_default = (root / "host/vulkan/external_memory.cpp").read_text(encoding="utf-8")
if """#elif defined(__ANDROID__)
    std::array<Mode, 1> supportedModes = {
        Mode::AndroidAHB,
    };""" not in platform_default:
    raise SystemExit("Android platform default is not the expected AndroidAHB")
print("verified AndroidAHB platform default; done")
