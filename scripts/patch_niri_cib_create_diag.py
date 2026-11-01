#!/usr/bin/env python3
"""Per-check bounded diagnostics for createClassicImageLocked; no gate changes."""
import argparse
from pathlib import Path
p = argparse.ArgumentParser(description=__doc__)
p.add_argument('root', type=Path)
root = p.parse_args().root.resolve()
path = root/'host/vulkan/vk_decoder_global_state.cpp'
text = path.read_text()
old = '''    VkResult createClassicImageLocked(VkDevice device, VulkanDispatch* vk, const VkImageCreateInfo* input,
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
    }'''
new = '''    VkResult createClassicImageLocked(VkDevice device, VulkanDispatch* vk, const VkImageCreateInfo* input,
                                      const VkNativeBufferANDROID* marker, VkImage* image) REQUIRES(mMutex) {
        const bool diag = gfxstream::base::getEnvironmentVariable("GFXSTREAM_NIRI_IMAGE_TRACE") == "1";
        bool markerHandleAccepted = false;
        auto fail = [&](const char* reason, VkResult result) {
            static std::atomic<uint32_t> diagnostics{0};
            if (diag && diagnostics.fetch_add(1, std::memory_order_relaxed) < 16) {
                uint32_t stride = 0, stagingSize = 0;
                const bool stagingOk = input && cib::staging(input->extent.width, input->extent.height, stride, stagingSize);
                const bool strideOk = stagingOk && marker && marker->stride == static_cast<int>(stride / 4);
                const bool formatOk = input && marker &&
                    marker->format == (input->format == VK_FORMAT_R8G8B8A8_UNORM ? 1 : 5);
                const uint32_t resource = markerHandleAccepted && marker ? *static_cast<const uint32_t*>(marker->handle) : 0;
                const bool strongNonNull = resource && FrameBuffer::getFB()->findColorBuffer(resource) != nullptr;
                const auto cb = m_vkEmulation->getColorBufferInfo(resource);
                auto* dev = gfxstream::base::find(mDeviceInfo, device);
                auto* phys = dev ? gfxstream::base::find(mPhysdevInfo, dev->physicalDevice) : nullptr;
                uint32_t shadow = ~0u;
                if (cb && phys) shadow = phys->memoryPropertiesHelper->classicImageShadow(cb->memory.typeIndex);
                const VkImageFormatListCreateInfo* views = nullptr;
                for (const auto* node = input ? static_cast<const VkBaseInStructure*>(input->pNext) : nullptr;
                     node; node = node->pNext) {
                    if (node->sType == VK_STRUCTURE_TYPE_IMAGE_FORMAT_LIST_CREATE_INFO) {
                        views = reinterpret_cast<const VkImageFormatListCreateInfo*>(node);
                        break;
                    }
                }
                const uint32_t viewCount = views ? views->viewFormatCount : 0;
                const VkFormat* viewFormats = views ? views->pViewFormats : nullptr;
                const uint32_t usageExtra = cb && input
                    ? static_cast<uint32_t>(input->usage & ~cb->imageCreateInfoShallow.usage) : 0;
                const uint32_t flagsExtra = cb && input ? static_cast<uint32_t>(
                    input->flags & ~(cb->imageCreateInfoShallow.flags | VK_IMAGE_CREATE_ALIAS_BIT)) : 0;
                GFXSTREAM_INFO("CIB1-DIAG-CREATE-FAIL: reason=%s result=%d cib-enabled=%d marker=%d state=%s "
                               "marker-handle-null=%d marker-stride=%d marker-format=%d marker-usage=%d "
                               "consumer=0x%llx producer=0x%llx input-fmt=%u input-usage=0x%x input-flags=0x%x "
                               "extent=%ux%u image-ok=%d staging-ok=%d stride=%u staging-size=%u "
                               "expected-marker-stride=%d stride-ok=%d format-ok=%d",
                               reason, (int)result, classicImageEnabled(), marker != nullptr,
                               !marker ? "absent" : (marker->usage2.consumer == cib::kConsumer ? "paired" : "foreign"),
                               marker != nullptr && !marker->handle,
                               marker ? marker->stride : 0, marker ? marker->format : 0, marker ? marker->usage : 0,
                               (unsigned long long)(marker ? marker->usage2.consumer : 0),
                               (unsigned long long)(marker ? marker->usage2.producer : 0),
                               input ? (unsigned)input->format : 0, input ? (unsigned)input->usage : 0,
                               input ? (unsigned)input->flags : 0,
                               input ? input->extent.width : 0, input ? input->extent.height : 0,
                               input ? cib::image(*input) : 0, stagingOk, stride, stagingSize,
                               stagingOk ? (int)(stride / 4) : -1, strideOk, formatOk);
                GFXSTREAM_INFO("CIB1-DIAG-CREATE-STATE: reason=%s result=%d resource=%u resource-known=%d "
                               "strong=%d cb=%d dev=%d phys=%d cb-width=%u cb-height=%u cb-format=%u "
                               "cb-usage=0x%x cb-flags=0x%x handle-info=%d handle-type=%u handle-null=%d "
                               "size=%llu bind-offset=%llu type-index=%u shadow=%u usage-extra=0x%x "
                               "flags-extra=0x%x view-count=%u view0=%u view1=%u view2=%u view3=%u",
                               reason, (int)result, resource, markerHandleAccepted, strongNonNull,
                               cb.has_value(), dev != nullptr, phys != nullptr,
                               cb ? cb->width : 0, cb ? cb->height : 0,
                               cb ? (unsigned)cb->imageCreateInfoShallow.format : 0,
                               cb ? (unsigned)cb->imageCreateInfoShallow.usage : 0,
                               cb ? (unsigned)cb->imageCreateInfoShallow.flags : 0,
                               cb && cb->memory.handleInfo,
                               cb && cb->memory.handleInfo ? (unsigned)cb->memory.handleInfo->streamHandleType : 0u,
                               cb && cb->memory.handleInfo && !cb->memory.handleInfo->handle,
                               (unsigned long long)(cb ? cb->memory.size : 0),
                               (unsigned long long)(cb ? cb->memory.bindOffset : 0),
                               cb ? cb->memory.typeIndex : 0, shadow, usageExtra, flagsExtra, viewCount,
                               viewFormats && viewCount > 0 ? (unsigned)viewFormats[0] : 0xffffffffu,
                               viewFormats && viewCount > 1 ? (unsigned)viewFormats[1] : 0xffffffffu,
                               viewFormats && viewCount > 2 ? (unsigned)viewFormats[2] : 0xffffffffu,
                               viewFormats && viewCount > 3 ? (unsigned)viewFormats[3] : 0xffffffffu);
            }
            return result;
        };
        if (!classicImageEnabled()) return fail("not-enabled", VK_ERROR_FORMAT_NOT_SUPPORTED);
        if (!marker) return fail("marker-null", VK_ERROR_FORMAT_NOT_SUPPORTED);
        if (marker->usage2.consumer != cib::kConsumer) return fail("consumer", VK_ERROR_FORMAT_NOT_SUPPORTED);
        if (marker->usage2.producer != 0) return fail("producer", VK_ERROR_FORMAT_NOT_SUPPORTED);
        if (marker->usage != 0) return fail("marker-usage", VK_ERROR_FORMAT_NOT_SUPPORTED);
        if (!marker->handle) return fail("marker-handle-null", VK_ERROR_FORMAT_NOT_SUPPORTED);
        markerHandleAccepted = true;
        if (!cib::image(*input)) return fail("image-shape", VK_ERROR_FORMAT_NOT_SUPPORTED);
        uint32_t stride = 0, stagingSize = 0;
        if (!cib::staging(input->extent.width, input->extent.height, stride, stagingSize))
            return fail("staging", VK_ERROR_FORMAT_NOT_SUPPORTED);
        if (marker->stride != static_cast<int>(stride / 4))
            return fail("stride", VK_ERROR_FORMAT_NOT_SUPPORTED);
        if (marker->format != (input->format == VK_FORMAT_R8G8B8A8_UNORM ? 1 : 5))
            return fail("format", VK_ERROR_FORMAT_NOT_SUPPORTED);
        const uint32_t resource = *static_cast<const uint32_t*>(marker->handle);
        auto strong = FrameBuffer::getFB()->findColorBuffer(resource);
        void* backingAddress = strong.get();
        std::shared_ptr<void> backing(backingAddress, [held = std::move(strong)](void*) mutable {
            native::Reclaimer::get().post([lease = std::move(held)] {});
        });
        const auto cb = m_vkEmulation->getColorBufferInfo(resource);
        auto* dev = gfxstream::base::find(mDeviceInfo, device);
        auto* phys = dev ? gfxstream::base::find(mPhysdevInfo, dev->physicalDevice) : nullptr;
        if (!backingAddress) return fail("backing-null", VK_ERROR_INVALID_EXTERNAL_HANDLE);
        if (!cb) return fail("cb-null", VK_ERROR_INVALID_EXTERNAL_HANDLE);
        if (!phys) return fail("phys-null", VK_ERROR_INVALID_EXTERNAL_HANDLE);
        if (cb->width != input->extent.width) return fail("cb-width", VK_ERROR_INVALID_EXTERNAL_HANDLE);
        if (cb->height != input->extent.height) return fail("cb-height", VK_ERROR_INVALID_EXTERNAL_HANDLE);
        if (cb->imageCreateInfoShallow.format != input->format) return fail("cb-format", VK_ERROR_INVALID_EXTERNAL_HANDLE);
        if (!cb->memory.handleInfo) return fail("handle-info", VK_ERROR_INVALID_EXTERNAL_HANDLE);
        if (cb->memory.handleInfo->streamHandleType != STREAM_HANDLE_TYPE_PLATFORM_AHB)
            return fail("handle-type", VK_ERROR_INVALID_EXTERNAL_HANDLE);
        if (!cb->memory.handleInfo->handle) return fail("cb-handle-null", VK_ERROR_INVALID_EXTERNAL_HANDLE);
        if (!cb->memory.size) return fail("size-zero", VK_ERROR_INVALID_EXTERNAL_HANDLE);
        if (cb->memory.bindOffset != 0) return fail("bind-offset", VK_ERROR_INVALID_EXTERNAL_HANDLE);
        const uint32_t shadow = phys->memoryPropertiesHelper->classicImageShadow(cb->memory.typeIndex);
        if (shadow >= VK_MAX_MEMORY_TYPES) return fail("shadow", VK_ERROR_INCOMPATIBLE_DRIVER);
        VkImageCreateInfo ci = cb->imageCreateInfoShallow;
        if ((input->usage & ~ci.usage) || (input->flags & ~(ci.flags | VK_IMAGE_CREATE_ALIAS_BIT)))
            return fail("usage-flags", VK_ERROR_FORMAT_NOT_SUPPORTED);
        // Preserve compatible view-format semantics; reject unrelated or unsupported pNexts.
        const VkImageFormatListCreateInfo* list = nullptr;
        for (auto* n = static_cast<const VkBaseInStructure*>(input->pNext); n; n = n->pNext) {
            if (n->sType == VK_STRUCTURE_TYPE_NATIVE_BUFFER_ANDROID) continue;
            if (n->sType != VK_STRUCTURE_TYPE_IMAGE_FORMAT_LIST_CREATE_INFO || list)
                return fail("view-list-pnext", VK_ERROR_FORMAT_NOT_SUPPORTED);
            list = reinterpret_cast<const VkImageFormatListCreateInfo*>(n);
        }
        VkImageFormatListCreateInfo localList{};
        if (list) {
            if (!list->viewFormatCount || list->viewFormatCount > 2 || !list->pViewFormats)
                return fail("view-list-count", VK_ERROR_FORMAT_NOT_SUPPORTED);
            if (list->viewFormatCount == 2 && list->pViewFormats[0] == list->pViewFormats[1])
                return fail("view-list-duplicate", VK_ERROR_FORMAT_NOT_SUPPORTED);
            for (uint32_t i = 0; i < list->viewFormatCount; ++i)
                if (!cib::view(ci.format, list->pViewFormats[i]))
                    return fail("view-list-format", VK_ERROR_FORMAT_NOT_SUPPORTED);
            localList = *list; localList.pNext = nullptr;
        }
        VkExternalMemoryImageCreateInfo external{VK_STRUCTURE_TYPE_EXTERNAL_MEMORY_IMAGE_CREATE_INFO,
            list ? &localList : nullptr, VK_EXTERNAL_MEMORY_HANDLE_TYPE_ANDROID_HARDWARE_BUFFER_BIT_ANDROID};
        ci.pNext = &external;
        VkResult result = vk->vkCreateImage(device, &ci, nullptr, image);
        if (result != VK_SUCCESS) return fail("driver-create", result);
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
    }'''
if text.count(old) != 1:
    raise SystemExit(f'createClassicImageLocked anchor count {text.count(old)}')
path.write_text(text.replace(old, new))
print('Added bounded per-check createClassicImageLocked diagnostics')
