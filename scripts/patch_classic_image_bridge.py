#!/usr/bin/env python3
"""CIB1 host core draft: default-off unbound classic ColorBuffer image aliases."""
import argparse
from pathlib import Path
p=argparse.ArgumentParser(description=__doc__);p.add_argument('root',type=Path)
root=p.parse_args().root.resolve()
def edit(rel,old,new):
    path=root/rel;text=path.read_text()
    if text.count(old)!=1:raise SystemExit(f'{rel}: expected one anchor ({text.count(old)}): {old[:90]}')
    path.write_text(text.replace(old,new))
d='host/vulkan/vk_decoder_global_state.cpp';s='host/vulkan/vk_decoder_internal_structs.h'
helper=Path(__file__).with_name('classic_image_bridge.h').read_text().replace('#pragma once\n','')
edit(d,'#include "vk_decoder_global_state.h"','#include "vk_decoder_global_state.h"\n'+helper)
edit('host/features/include/gfxstream/host/features.h','    BoolFeatureInfo VulkanAllocateHostMemory = {','''    BoolFeatureInfo ClassicImageBridge = {
        "ClassicImageBridge", "CIB1 dedicated non-CPU-mappable classic image export bridge", &map,
    };
    BoolFeatureInfo VulkanAllocateHostMemory = {''')
edit('host/virtgpu_gfxstream_protocol.h', '#include <stdint.h>', '#include <stdint.h>\n#include <stddef.h>')
edit('host/virtgpu_gfxstream_protocol.h','    uint32_t guestAllocSizeMb;','''    uint32_t guestAllocSizeMb;
    uint32_t classicImageBridgeVersion;
    uint32_t classicImageBridgeContract;''')
edit('host/virtgpu_gfxstream_protocol.h','struct glesCapset {','''static_assert(offsetof(vulkanCapset, classicImageBridgeVersion) == 120);
static_assert(offsetof(vulkanCapset, classicImageBridgeContract) == 124);
static_assert(sizeof(vulkanCapset) == 128);

struct glesCapset {''')
edit('host/vulkan/vk_emulated_physical_device_memory.cpp','if (gfxstream::base::getEnvironmentVariable("GFXSTREAM_DEVICE_LOCAL_MEMORY_TYPE") == "1") {','''if (features.ClassicImageBridge.enabled() ||
        gfxstream::base::getEnvironmentVariable("GFXSTREAM_DEVICE_LOCAL_MEMORY_TYPE") == "1") {''')
edit('host/vulkan/vk_emulated_physical_device_memory.cpp', '''            // Only shadow types the guest can actually reach.''', '''            // CIB1 needs only the actual renderer ColorBuffer physical type;
            // avoid exhausting Vulkan's 32-type limit by shadowing every type.
            if (features.ClassicImageBridge.enabled() &&
                gfxstream::base::getEnvironmentVariable("GFXSTREAM_DEVICE_LOCAL_MEMORY_TYPE") != "1" &&
                hostMemoryTypeIndex != hostColorBufferMemoryTypeIndex) continue;
            // Only shadow types the guest can actually reach.''')
edit('host/vulkan/vk_emulated_physical_device_memory.h','    uint32_t getGuestColorBufferMemoryTypeIndex() const { return mGuestColorBufferMemoryTypeIndex; }','''    uint32_t getGuestColorBufferMemoryTypeIndex() const { return mGuestColorBufferMemoryTypeIndex; }
    uint32_t classicImageShadow(uint32_t physical) const {
        if (physical >= mHostMemoryProperties.memoryTypeCount) return ~0u;
        const uint32_t shadow = mHostToGuestDeviceLocalShadowMap[physical];
        if (shadow >= mGuestMemoryProperties.memoryTypeCount || !mGuestMemoryTypeIsShadow[shadow] ||
            mGuestToHostMemoryTypeIndexMap[shadow] != physical ||
            mGuestMemoryProperties.memoryTypes[shadow].propertyFlags != VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT) return ~0u;
        return shadow;
    }''')
edit(s,'struct ImageInfo {','''struct ImageInfo {
    std::shared_ptr<void> classicImageBacking;
    uint32_t classicImageResource = 0;
    VkMemoryRequirements classicImageRequirements{};
    uint32_t classicImagePhysicalType = ~0u;
    std::vector<VkFormat> classicImageViewFormats;
    bool classicImageBound = false;
    bool classicImageAllocationClaimed = false;''')
edit(s,'struct MemoryInfo {','''struct MemoryInfo {
    std::shared_ptr<void> classicImageBacking;
    uint32_t classicImageResource = 0;
    VkImage classicDedicatedImage = VK_NULL_HANDLE;''')
methods='''    bool classicImageEnabled() const {
        return getFeatures().ClassicImageBridge.enabled() && !getFeatures().VulkanSnapshots.enabled() &&
            m_vkEmulation->getExternalMemoryMode() == ExternalMemory::Mode::AndroidAHB &&
            m_vkEmulation->classicImageBridgeReady();
    }
    bool classicImageRequirementsLocked(VkDevice device, VkImage image, VkMemoryRequirements* out) REQUIRES(mMutex) {
        const auto* info = gfxstream::base::find(mImageInfo, image);
        if (!info || info->device != device || !info->classicImageResource) return false;
        *out = info->classicImageRequirements; return true;
    }
    VkResult createClassicImageLocked(VkDevice device, VulkanDispatch* vk, const VkImageCreateInfo* input,
                                      const VkNativeBufferANDROID* marker, VkImage* image) REQUIRES(mMutex) {
        if (!classicImageEnabled() || !marker || marker->usage2.consumer != cib::kConsumer ||
            marker->usage2.producer != 0 || marker->usage != 0 || !marker->handle || !cib::image(*input))
            return VK_ERROR_FORMAT_NOT_SUPPORTED;
        uint32_t stride = 0, stagingSize = 0;
        if (!cib::staging(input->extent.width, input->extent.height, stride, stagingSize) ||
            marker->stride != static_cast<int>(stride / 4) ||
            marker->format != (input->format == VK_FORMAT_R8G8B8A8_UNORM ? 1 : 5))
            return VK_ERROR_FORMAT_NOT_SUPPORTED;
        const uint32_t resource = *static_cast<const uint32_t*>(marker->handle);
        auto strong = FrameBuffer::getFB()->findColorBuffer(resource);
        void* backingAddress = strong.get();
        std::shared_ptr<void> backing(backingAddress, [held = std::move(strong)](void*) mutable {
            native::Reclaimer::get().post([lease = std::move(held)] {});
        });
        const auto cb = m_vkEmulation->getColorBufferInfo(resource);
        auto* dev = gfxstream::base::find(mDeviceInfo, device);
        auto* phys = dev ? gfxstream::base::find(mPhysdevInfo, dev->physicalDevice) : nullptr;
        if (!backingAddress || !cb || !phys || cb->width != input->extent.width || cb->height != input->extent.height ||
            cb->imageCreateInfoShallow.format != input->format || !cb->memory.handleInfo ||
            cb->memory.handleInfo->streamHandleType != STREAM_HANDLE_TYPE_PLATFORM_AHB ||
            !cb->memory.handleInfo->handle || !cb->memory.size || cb->memory.bindOffset != 0)
            return VK_ERROR_INVALID_EXTERNAL_HANDLE;
        const uint32_t shadow = phys->memoryPropertiesHelper->classicImageShadow(cb->memory.typeIndex);
        if (shadow >= VK_MAX_MEMORY_TYPES) return VK_ERROR_INCOMPATIBLE_DRIVER;
        VkImageCreateInfo ci = cb->imageCreateInfoShallow;
        if ((input->usage & ~ci.usage) || (input->flags & ~(ci.flags | VK_IMAGE_CREATE_ALIAS_BIT))) return VK_ERROR_FORMAT_NOT_SUPPORTED;
        // Preserve compatible view-format semantics; reject unrelated or unsupported pNexts.
        const VkImageFormatListCreateInfo* list = nullptr;
        for (auto* n = static_cast<const VkBaseInStructure*>(input->pNext); n; n = n->pNext) {
            if (n->sType == VK_STRUCTURE_TYPE_NATIVE_BUFFER_ANDROID) continue;
            if (n->sType != VK_STRUCTURE_TYPE_IMAGE_FORMAT_LIST_CREATE_INFO || list) return VK_ERROR_FORMAT_NOT_SUPPORTED;
            list = reinterpret_cast<const VkImageFormatListCreateInfo*>(n);
        }
        VkImageFormatListCreateInfo localList{};
        if (list) {
            if (!list->viewFormatCount || list->viewFormatCount > 2 || !list->pViewFormats)
                return VK_ERROR_FORMAT_NOT_SUPPORTED;
            if (list->viewFormatCount == 2 && list->pViewFormats[0] == list->pViewFormats[1]) return VK_ERROR_FORMAT_NOT_SUPPORTED;
            for (uint32_t i = 0; i < list->viewFormatCount; ++i)
                if (!cib::view(ci.format, list->pViewFormats[i])) return VK_ERROR_FORMAT_NOT_SUPPORTED;
            localList = *list; localList.pNext = nullptr;
        }
        VkExternalMemoryImageCreateInfo external{VK_STRUCTURE_TYPE_EXTERNAL_MEMORY_IMAGE_CREATE_INFO,
            list ? &localList : nullptr, VK_EXTERNAL_MEMORY_HANDLE_TYPE_ANDROID_HARDWARE_BUFFER_BIT_ANDROID};
        ci.pNext = &external;
        VkResult result = vk->vkCreateImage(device, &ci, nullptr, image);
        if (result != VK_SUCCESS) return result;
        ImageInfo bridge;
        bridge.device = device;
        bridge.imageCreateInfoShallow = vk_make_orphan_copy(*input);
        bridge.classicImageBacking = std::move(backing); // failures and success defer final CB release
        bridge.classicImageResource = resource;
        bridge.classicImagePhysicalType = cb->memory.typeIndex;
        bridge.classicImageRequirements = {cb->memory.size, 1, uint32_t(1u << shadow)};
        mImageInfo[*image] = std::move(bridge);
        *image = new_boxed_non_dispatchable_VkImage(*image);
        mImageInfo[unbox_VkImage(*image)].boxed = *image;
        GFXSTREAM_INFO("CIB1: image=%p resource=%u bytes=%llu physical-type=%u shadow=%u unbound=1",
            (void*)*image, resource, (unsigned long long)cb->memory.size, cb->memory.typeIndex, shadow);
        return VK_SUCCESS;
    }

'''
edit(d,'    VkResult on_vkCreateImage(gfxstream::base::BumpPool* pool, VkSnapshotApiCallHandle,',methods+'    VkResult on_vkCreateImage(gfxstream::base::BumpPool* pool, VkSnapshotApiCallHandle,')
edit(d,'''        // What the guest asks a scanout-capable image to be.''','''        const auto* classicMarker = vk_find_struct<VkNativeBufferANDROID>(pCreateInfo);
        if (classicMarker && classicMarker->usage2.consumer == cib::kConsumer)
            return createClassicImageLocked(device, vk, pCreateInfo, classicMarker, pImage);

        // What the guest asks a scanout-capable image to be.''')
# Must intercept before old ANB transform discards external+native pNext and prebinds.
edit(d,'''            // If the VkImage is going to bind to a ColorBuffer''','''            if (pNativeBufferANDROID && pNativeBufferANDROID->usage2.consumer == cib::kConsumer) continue;
            // If the VkImage is going to bind to a ColorBuffer''')
edit(d,'''        vk->vkGetImageMemoryRequirements(device, image, pMemoryRequirements);
        std::lock_guard<std::mutex> lock(mMutex);''','''        { std::lock_guard<std::mutex> lock(mMutex);
          if (classicImageRequirementsLocked(device, image, pMemoryRequirements)) return; }
        vk->vkGetImageMemoryRequirements(device, image, pMemoryRequirements);
        std::lock_guard<std::mutex> lock(mMutex);''')
edit(d,'''        if ((physicalDeviceInfo->props.apiVersion >= VK_MAKE_VERSION(1, 1, 0)) &&
            vk->vkGetImageMemoryRequirements2) {''','''        if (classicImageRequirementsLocked(device, pInfo->image, &pMemoryRequirements->memoryRequirements)) {
            auto* dedicated = vk_find_struct<VkMemoryDedicatedRequirements>(pMemoryRequirements);
            if (dedicated) { dedicated->prefersDedicatedAllocation = VK_TRUE; dedicated->requiresDedicatedAllocation = VK_TRUE; }
            return;
        }
        if ((physicalDeviceInfo->props.apiVersion >= VK_MAKE_VERSION(1, 1, 0)) &&
            vk->vkGetImageMemoryRequirements2) {''')
# Dedicated CIB allocation bypasses legacy alias guest-index heuristics, but
# validates the exact advertised shadow -> actual CB physical type explicitly.
allocation = '''    VkResult allocateClassicImageLocked(VkDevice device, VulkanDispatch* vk,
                                          const VkMemoryAllocateInfo* input, VkDeviceMemory* output) REQUIRES(mMutex) {
        const auto* dedicated = vk_find_struct<VkMemoryDedicatedAllocateInfo>(input);
        const auto* cbImport = vk_find_struct<VkImportColorBufferGOOGLE>(input);
        if (!dedicated || !dedicated->image || dedicated->buffer || !cbImport || !classicImageEnabled())
            return VK_ERROR_INVALID_EXTERNAL_HANDLE;
        for (auto* n = static_cast<const VkBaseInStructure*>(input->pNext); n; n = n->pNext) {
            if (n->sType != VK_STRUCTURE_TYPE_MEMORY_DEDICATED_ALLOCATE_INFO &&
                n->sType != VK_STRUCTURE_TYPE_IMPORT_COLOR_BUFFER_GOOGLE) return VK_ERROR_INVALID_EXTERNAL_HANDLE;
        }
        auto* image = gfxstream::base::find(mImageInfo, dedicated->image);
        auto* dev = gfxstream::base::find(mDeviceInfo, device);
        auto* phys = dev ? gfxstream::base::find(mPhysdevInfo, dev->physicalDevice) : nullptr;
        if (!image || !phys || image->device != device || !image->classicImageResource ||
            image->classicImageResource != cbImport->colorBuffer || image->classicImageBound || image->classicImageAllocationClaimed ||
            input->allocationSize != image->classicImageRequirements.size)
            return VK_ERROR_INVALID_EXTERNAL_HANDLE;
        const auto selected = phys->memoryPropertiesHelper->getHostMemoryInfoFromGuestMemoryTypeIndex(input->memoryTypeIndex);
        const auto& guest = phys->memoryPropertiesHelper->getGuestMemoryProperties();
        if (input->memoryTypeIndex >= guest.memoryTypeCount || !selected ||
            selected->index != image->classicImagePhysicalType ||
            guest.memoryTypes[input->memoryTypeIndex].propertyFlags != VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT ||
            !(image->classicImageRequirements.memoryTypeBits & (1u << input->memoryTypeIndex)))
            return VK_ERROR_INCOMPATIBLE_DRIVER;
        const auto cb = m_vkEmulation->getColorBufferInfo(image->classicImageResource);
        if (!cb || cb->memory.typeIndex != selected->index || cb->memory.size != input->allocationSize)
            return VK_ERROR_INVALID_EXTERNAL_HANDLE;
        VkMemoryDedicatedAllocateInfo realDedicated{VK_STRUCTURE_TYPE_MEMORY_DEDICATED_ALLOCATE_INFO,
            nullptr, dedicated->image, VK_NULL_HANDLE};
        VkDeviceMemory memory = VK_NULL_HANDLE;
        image->classicImageAllocationClaimed = true;
        if (!m_vkEmulation->importExternalMemory(vk, device, &cb->memory, &realDedicated, &memory)) {
            image->classicImageAllocationClaimed = false;
            return VK_ERROR_OUT_OF_DEVICE_MEMORY;
        }
        MemoryInfo info;
        info.device = device; info.size = cb->memory.size; info.memoryIndex = cb->memory.typeIndex;
        info.classicImageResource = image->classicImageResource;
        info.classicDedicatedImage = dedicated->image;
        info.classicImageBacking = image->classicImageBacking;
        info.boundColorBuffer = image->classicImageResource;
        mMemoryInfo[memory] = std::move(info);
        *output = new_boxed_non_dispatchable_VkDeviceMemory(memory);
        return VK_SUCCESS;
    }

'''
edit(d, '    VkResult on_vkAllocateMemory(gfxstream::base::BumpPool* pool, VkSnapshotApiCallHandle,', allocation + '    VkResult on_vkAllocateMemory(gfxstream::base::BumpPool* pool, VkSnapshotApiCallHandle,')
edit(d, '''        // Alias metadata lookup borrows existing backing; no dup/acquire/invalidation.''', '''        {
            std::lock_guard<std::mutex> lock(mMutex);
            const auto* dedicated = vk_find_struct<VkMemoryDedicatedAllocateInfo>(pAllocateInfo);
            const auto* image = dedicated ? gfxstream::base::find(mImageInfo, dedicated->image) : nullptr;
            if (image && image->classicImageResource)
                return allocateClassicImageLocked(device, vk, pAllocateInfo, pMemory);
        }
        // Alias metadata lookup borrows existing backing; no dup/acquire/invalidation.''')
edit(d, '''        VALIDATE_REQUIRED_HANDLE(memory);
        VkResult result = vk->vkBindImageMemory(device, image, memory, memoryOffset);''', '''        {
            std::lock_guard<std::mutex> lock(mMutex);
            auto* bridge = gfxstream::base::find(mImageInfo, image);
            auto* allocation = gfxstream::base::find(mMemoryInfo, memory);
            if (allocation && allocation->classicImageResource && (!bridge || !bridge->classicImageResource))
                return VK_ERROR_INVALID_EXTERNAL_HANDLE;
            if (bridge && bridge->classicImageResource) {
                if (!allocation || allocation->device != device || memoryOffset != 0 || bridge->classicImageBound ||
                    allocation->classicDedicatedImage != image ||
                    allocation->classicImageResource != bridge->classicImageResource)
                    return VK_ERROR_INVALID_EXTERNAL_HANDLE;
            }
        }
        VALIDATE_REQUIRED_HANDLE(memory);
        VkResult result = vk->vkBindImageMemory(device, image, memory, memoryOffset);''')
edit(d, '''        imageInfo->memory = memory;''', '''        imageInfo->memory = memory;
        if (imageInfo->classicImageResource) imageInfo->classicImageBound = true;''')
# Marker image requests never enter the batch fast path; both bind2 names route
# through existing performBindImageMemory where dedicated identity is checked.
edit(d, '''                if (imageInfo->compressInfo) {
                    needEmulation = true;''', '''                const auto* classicAllocation = gfxstream::base::find(mMemoryInfo, pBindInfos[i].memory);
                if (imageInfo->classicImageResource || (classicAllocation && classicAllocation->classicImageResource)) {
                    needEmulation = true; break;
                }
                if (imageInfo->compressInfo) {
                    needEmulation = true;''')
edit(d, '''        if (!info || !info->ptr) return VK_ERROR_MEMORY_MAP_FAILED;  // Invalid usage.''', '''        if (!info || info->classicImageResource || !info->ptr) return VK_ERROR_MEMORY_MAP_FAILED;  // CIB1 is not CPU mappable.''')
# Readiness uses actual initialized renderer functions/format support and reserves
# enough memory-type slots for truthful DEVICE_LOCAL-only shadows.
edit('host/vulkan/vk_common_operations.h', '    bool supportsDmaBuf() const;', '    bool supportsDmaBuf() const;\n    bool classicImageBridgeReady() const;')
edit('host/vulkan/vk_common_operations.cpp', 'VkExternalMemoryHandleTypeFlagBits VkEmulation::getDefaultExternalMemoryHandleType() {', '''bool VkEmulation::classicImageBridgeReady() const {
#ifdef __ANDROID__
    if (!mFeatures.ClassicImageBridge.enabled() ||
        mDeviceInfo.externalMemoryMode != ExternalMemory::Mode::AndroidAHB ||
        !mDeviceInfo.supportsExternalMemoryImport || !mDvk ||
        !mDvk->vkGetAndroidHardwareBufferPropertiesANDROID || !mDvk->vkAllocateMemory)
        return false;
    const uint32_t physical = mRepresentativeColorBufferMemoryTypeInfo.hostMemoryTypeIndex;
    if (physical >= mDeviceInfo.memProps.memoryTypeCount) return false;
    const auto flags = mDeviceInfo.memProps.memoryTypes[physical].propertyFlags;
    const auto required = VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT | VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT;
    return (flags & required) == required && mDeviceInfo.memProps.memoryTypeCount < VK_MAX_MEMORY_TYPES &&
        mImageSupportInfo.GetSupportedInfo(VK_FORMAT_R8G8B8A8_UNORM) &&
        mImageSupportInfo.GetSupportedInfo(VK_FORMAT_B8G8R8A8_UNORM);
#else
    return false;
#endif
}

VkExternalMemoryHandleTypeFlagBits VkEmulation::getDefaultExternalMemoryHandleType() {''')
# Capability readiness is renderer-backed and feature-gated, never guest env self-attestation.
edit('host/vulkan/vk_decoder_global_state.h', '    static void reset();', '''    static void reset();
    bool classicImageBridgeReady();''')
edit(d, 'VkDecoderGlobalState::~VkDecoderGlobalState() = default;', '''VkDecoderGlobalState::~VkDecoderGlobalState() = default;
bool VkDecoderGlobalState::classicImageBridgeReady() { return mImpl->classicImageEnabled(); }''')
edit('host/virtio_gpu_frontend.cpp', '''            capset->noRenderControlEnc = 1;''', '''            if (mFeatures.ClassicImageBridge.enabled() && FrameBuffer::getFB()->hasEmulationVk() &&
                vk::VkDecoderGlobalState::get()->classicImageBridgeReady()) {
                capset->classicImageBridgeVersion = 1;
                capset->classicImageBridgeContract = 0x43494231;
            }
            capset->noRenderControlEnc = 1;''')
edit('host/virtio_gpu_frontend.cpp', '#include "virtio_gpu_frontend.h"', '#include "virtio_gpu_frontend.h"\n#include "vulkan/vk_decoder_global_state.h"')
# The query validates real AHB-native optimal image capability with the same native
# format/usage/flags as the ColorBuffer factory. Returned external feature is an
# EXPORT of guest GEM, not a claim that the AHB is a native DMA_BUF export.
query = '''    std::optional<VkResult> classicImageQueryLocked(VkPhysicalDevice physical, VulkanDispatch* vk,
                                  const VkPhysicalDeviceImageFormatInfo2* input,
                                  VkImageFormatProperties2* output) REQUIRES(mMutex) {
        const auto* ext = vk_find_struct<VkPhysicalDeviceExternalImageFormatInfo>(input);
        const auto* ack = vk_find_struct<VkNativeBufferANDROID>(input);
        const bool paired = ack && !ack->handle && !ack->stride && !ack->format && !ack->usage &&
            ack->usage2.consumer == cib::kConsumer && ack->usage2.producer == 0;
        const auto* physicalInfo = gfxstream::base::find(mPhysdevInfo, physical);
        const auto* instanceInfo = physicalInfo ? gfxstream::base::find(mInstanceInfo, physicalInfo->instance) : nullptr;
        if (!paired || !instanceInfo || instanceInfo->applicationName != "niri" || !classicImageEnabled() || !ext ||
            !(ext->handleType == VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT ||
              ext->handleType == VK_EXTERNAL_MEMORY_HANDLE_TYPE_ANDROID_HARDWARE_BUFFER_BIT_ANDROID)) return std::nullopt;
        if (!cib::format(input->format) || input->type != VK_IMAGE_TYPE_2D ||
            input->tiling != VK_IMAGE_TILING_LINEAR || !input->usage || (input->usage & ~cib::kUsage) ||
            (input->flags & ~(VK_IMAGE_CREATE_MUTABLE_FORMAT_BIT | VK_IMAGE_CREATE_ALIAS_BIT)))
            return VK_ERROR_FORMAT_NOT_SUPPORTED;
        auto native = m_vkEmulation->generateColorBufferVkImageCreateInfo(input->format, 1, 1, VK_IMAGE_TILING_OPTIMAL, 1);
        if (!native || (input->usage & ~native->usage) || (input->flags & ~(native->flags | VK_IMAGE_CREATE_ALIAS_BIT))) return VK_ERROR_FORMAT_NOT_SUPPORTED;
        const VkImageFormatListCreateInfo* list = nullptr;
        for (auto* n = static_cast<const VkBaseInStructure*>(input->pNext); n; n = n->pNext) {
            if (n->sType == VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_EXTERNAL_IMAGE_FORMAT_INFO ||
                (n->sType == VK_STRUCTURE_TYPE_NATIVE_BUFFER_ANDROID && n == reinterpret_cast<const VkBaseInStructure*>(ack))) continue;
            if (n->sType != VK_STRUCTURE_TYPE_IMAGE_FORMAT_LIST_CREATE_INFO || list) return VK_ERROR_FORMAT_NOT_SUPPORTED;
            list = reinterpret_cast<const VkImageFormatListCreateInfo*>(n);
        }
        VkImageFormatListCreateInfo localList{};
        if (list) {
            if (!list->viewFormatCount || list->viewFormatCount > 2 || !list->pViewFormats) return VK_ERROR_FORMAT_NOT_SUPPORTED;
            if (list->viewFormatCount == 2 && list->pViewFormats[0] == list->pViewFormats[1]) return VK_ERROR_FORMAT_NOT_SUPPORTED;
            for (uint32_t i = 0; i < list->viewFormatCount; ++i)
                if (!cib::view(input->format, list->pViewFormats[i])) return VK_ERROR_FORMAT_NOT_SUPPORTED;
            localList = *list; localList.pNext = nullptr;
        }
        VkPhysicalDeviceExternalImageFormatInfo ahb{VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_EXTERNAL_IMAGE_FORMAT_INFO,
            list ? &localList : nullptr, VK_EXTERNAL_MEMORY_HANDLE_TYPE_ANDROID_HARDWARE_BUFFER_BIT_ANDROID};
        VkPhysicalDeviceImageFormatInfo2 actual = *input;
        actual.pNext = &ahb; actual.tiling = native->tiling; actual.flags = native->flags; actual.usage = native->usage;
        VkExternalImageFormatProperties external{VK_STRUCTURE_TYPE_EXTERNAL_IMAGE_FORMAT_PROPERTIES};
        VkImageFormatProperties2 real{VK_STRUCTURE_TYPE_IMAGE_FORMAT_PROPERTIES_2, &external};
        VkResult result = VK_ERROR_EXTENSION_NOT_PRESENT;
        if (vk->vkGetPhysicalDeviceImageFormatProperties2)
            result = vk->vkGetPhysicalDeviceImageFormatProperties2(physical, &actual, &real);
        else if (vk->vkGetPhysicalDeviceImageFormatProperties2KHR)
            result = vk->vkGetPhysicalDeviceImageFormatProperties2KHR(physical, &actual, &real);
        if (result != VK_SUCCESS) return result;
        if (!(external.externalMemoryProperties.externalMemoryFeatures & VK_EXTERNAL_MEMORY_FEATURE_IMPORTABLE_BIT))
            return VK_ERROR_FORMAT_NOT_SUPPORTED;
        output->imageFormatProperties = real.imageFormatProperties;
        output->imageFormatProperties.maxMipLevels = 1;
        output->imageFormatProperties.maxArrayLayers = 1;
        output->imageFormatProperties.sampleCounts = VK_SAMPLE_COUNT_1_BIT;
        auto* guestExternal = vk_find_struct<VkExternalImageFormatProperties>(output);
        if (guestExternal) guestExternal->externalMemoryProperties = {
            VK_EXTERNAL_MEMORY_FEATURE_EXPORTABLE_BIT | VK_EXTERNAL_MEMORY_FEATURE_DEDICATED_ONLY_BIT,
            0, ext->handleType == VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT ?
                VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT : VK_EXTERNAL_MEMORY_HANDLE_TYPE_OPAQUE_FD_BIT};
        return VK_SUCCESS;
    }

'''
edit(d, '    bool classicImageEnabled() const {', query + '    bool classicImageEnabled() const {')
edit(d, '''        // Pick by host dispatch-pointer availability; see on_vkGetPhysicalDeviceFeatures2.
        if (vk->vkGetPhysicalDeviceImageFormatProperties2) {''', '''        if (auto bridge = classicImageQueryLocked(physicalDevice, vk, pImageFormatInfo, pImageFormatProperties)) return *bridge;
        // Pick by host dispatch-pointer availability; see on_vkGetPhysicalDeviceFeatures2.
        if (vk->vkGetPhysicalDeviceImageFormatProperties2) {''')
edit('host/virtio_gpu_frontend.cpp', '''        mRenderer->stop(/*wait*/ true);
        mRenderer.reset();''', '''        mRenderer->stop(/*wait*/ true);
        native::Reclaimer::get().drain(); // CIB leases may release VkEmulation-backed ColorBuffers.
        mRenderer.reset();''')
layout = '''    bool classicImageLayout(VkImage image, const VkImageSubresource* sub, VkSubresourceLayout* out) {
        std::lock_guard<std::mutex> lock(mMutex);
        const auto* info = gfxstream::base::find(mImageInfo, image);
        if (!info || !info->classicImageResource) return false;
        *out = {};
        if (!sub || sub->mipLevel || sub->arrayLayer ||
            !(sub->aspectMask == VK_IMAGE_ASPECT_COLOR_BIT || sub->aspectMask == VK_IMAGE_ASPECT_MEMORY_PLANE_0_BIT_EXT)) return true;
        uint32_t stride = 0, size = 0;
        if (!cib::staging(info->imageCreateInfoShallow.extent.width, info->imageCreateInfoShallow.extent.height,
                          stride, size)) return true;
        // Layout describes guest classic GEM transfer staging, NOT native AHB layout.
        *out = {0, size, stride, size, size};
        return true;
    }

'''
edit(d, '    bool classicImageEnabled() const {', layout + '    bool classicImageEnabled() const {')
edit('host/vulkan/vk_decoder_global_state.h', '    bool classicImageBridgeReady();', '''    bool classicImageBridgeReady();
    bool classicImageLayout(VkImage image, const VkImageSubresource* sub, VkSubresourceLayout* out);''')
edit(d, 'bool VkDecoderGlobalState::classicImageBridgeReady() { return mImpl->classicImageEnabled(); }', '''bool VkDecoderGlobalState::classicImageBridgeReady() { return mImpl->classicImageEnabled(); }
bool VkDecoderGlobalState::classicImageLayout(VkImage image, const VkImageSubresource* sub, VkSubresourceLayout* out) {
    return mImpl->classicImageLayout(image, sub, out);
}''')
edit('host/vulkan/vk_decoder.cpp', '''                    vk->vkGetImageSubresourceLayout(unboxed_device, image, pSubresource, pLayout);''', '''                    if (!m_state->classicImageLayout(image, pSubresource, pLayout))
                        vk->vkGetImageSubresourceLayout(unboxed_device, image, pSubresource, pLayout);''')
print('Applied CIB1 export-only classic image bridge draft; pending source review, no publication authorization implied')
