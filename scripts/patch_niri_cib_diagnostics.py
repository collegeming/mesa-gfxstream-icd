#!/usr/bin/env python3
"""Bounded metadata-only CIB1 activation diagnostics; no behavior or driver changes."""
import argparse
from pathlib import Path
p = argparse.ArgumentParser(description=__doc__)
p.add_argument('root', type=Path)
root = p.parse_args().root.resolve()
def edit(rel, old, new):
    path = root/rel
    text = path.read_text()
    if text.count(old) != 1:
        raise SystemExit(f'{rel}: expected one anchor ({text.count(old)}): {old[:100]}')
    path.write_text(text.replace(old, new))

common = 'host/vulkan/vk_common_operations.cpp'
edit(common, '#include <algorithm>', '#include <algorithm>\n#include <atomic>')
edit(common, '''bool VkEmulation::classicImageBridgeReady() const {
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
}''', '''bool VkEmulation::classicImageBridgeReady() const {
#ifdef __ANDROID__
    const bool featureEnabled = mFeatures.ClassicImageBridge.enabled();
    const bool androidAhb = mDeviceInfo.externalMemoryMode == ExternalMemory::Mode::AndroidAHB;
    const bool importSupported = mDeviceInfo.supportsExternalMemoryImport;
    const bool dvkPresent = mDvk != nullptr;
    const bool ahbPropsFn = dvkPresent && mDvk->vkGetAndroidHardwareBufferPropertiesANDROID != nullptr;
    const bool allocateFn = dvkPresent && mDvk->vkAllocateMemory != nullptr;
    const uint32_t physical = mRepresentativeColorBufferMemoryTypeInfo.hostMemoryTypeIndex;
    const uint32_t typeCount = mDeviceInfo.memProps.memoryTypeCount;
    const bool physicalValid = physical < typeCount;
    VkMemoryPropertyFlags physicalFlags = 0;
    if (physicalValid) physicalFlags = mDeviceInfo.memProps.memoryTypes[physical].propertyFlags;
    const auto required = VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT | VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT;
    const bool physicalFlagsOk = physicalValid && (physicalFlags & required) == required;
    uint32_t candidates = 0;
    for (uint32_t i = 0; i < typeCount; ++i) {
        if ((mDeviceInfo.memProps.memoryTypes[i].propertyFlags & required) == required) ++candidates;
    }
    const bool room = typeCount < VK_MAX_MEMORY_TYPES;
    const bool rgba = mImageSupportInfo.GetSupportedInfo(VK_FORMAT_R8G8B8A8_UNORM) != nullptr;
    const bool bgra = mImageSupportInfo.GetSupportedInfo(VK_FORMAT_B8G8R8A8_UNORM) != nullptr;
    static std::atomic<uint32_t> diagnostics{0};
    if (gfxstream::base::getEnvironmentVariable("GFXSTREAM_NIRI_IMAGE_TRACE") == "1" &&
        diagnostics.fetch_add(1, std::memory_order_relaxed) < 8) {
        GFXSTREAM_INFO("CIB1-DIAG-READY: feature=%d ahb-mode=%d import=%d dvk=%d ahb-props-fn=%d alloc-fn=%d "
                       "physical=%u type-count=%u physical-valid=%d physical-flags=0x%x flags-ok=%d candidates=%u room=%d rgba=%d bgra=%d",
                       featureEnabled, androidAhb, importSupported, dvkPresent, ahbPropsFn, allocateFn,
                       physical, typeCount, physicalValid, (unsigned)physicalFlags, physicalFlagsOk,
                       candidates, room, rgba, bgra);
    }
    return featureEnabled && androidAhb && importSupported && dvkPresent && ahbPropsFn && allocateFn &&
        physicalFlagsOk && room && rgba && bgra;
#else
    return false;
#endif
}''')

frontend = 'host/virtio_gpu_frontend.cpp'
edit(frontend, '#include <cerrno>', '#include <atomic>\n#include <cerrno>')
edit(frontend, '''            if (mFeatures.ClassicImageBridge.enabled() && FrameBuffer::getFB()->hasEmulationVk() &&
                vk::VkDecoderGlobalState::get()->classicImageBridgeReady()) {
                capset->classicImageBridgeVersion = 1;
                capset->classicImageBridgeContract = 0x43494231;
            }''', '''            {
                const bool cibFeature = mFeatures.ClassicImageBridge.enabled();
                const bool cibEmulationVk = FrameBuffer::getFB()->hasEmulationVk();
                const bool cibReady = cibFeature && cibEmulationVk &&
                    vk::VkDecoderGlobalState::get()->classicImageBridgeReady();
                if (cibReady) {
                    capset->classicImageBridgeVersion = 1;
                    capset->classicImageBridgeContract = 0x43494231;
                }
                if (gfxstream::base::getEnvironmentVariable("GFXSTREAM_NIRI_IMAGE_TRACE") == "1") {
                    static std::atomic<uint32_t> diagnostics{0};
                    if (diagnostics.fetch_add(1, std::memory_order_relaxed) < 8) {
                        GFXSTREAM_INFO("CIB1-DIAG-CAPSET: feature=%d has-emulation-vk=%d snapshots=%d ready=%d "
                                       "wrote=%d version=%u contract=0x%x color-buffer-index=%u",
                                       cibFeature, cibEmulationVk, mFeatures.VulkanSnapshots.enabled(), cibReady,
                                       capset->classicImageBridgeVersion != 0, capset->classicImageBridgeVersion,
                                       (unsigned)capset->classicImageBridgeContract,
                                       capset->colorBufferMemoryIndex);
                    }
                }
            }''')

decoder = 'host/vulkan/vk_decoder_global_state.cpp'
edit(decoder, '''    bool classicImageEnabled() const {
        return getFeatures().ClassicImageBridge.enabled() && !getFeatures().VulkanSnapshots.enabled() &&
            m_vkEmulation->getExternalMemoryMode() == ExternalMemory::Mode::AndroidAHB &&
            m_vkEmulation->classicImageBridgeReady();
    }''', '''    bool classicImageEnabled() const {
        const bool feature = getFeatures().ClassicImageBridge.enabled();
        const bool snapshots = getFeatures().VulkanSnapshots.enabled();
        const bool ahbMode = m_vkEmulation->getExternalMemoryMode() == ExternalMemory::Mode::AndroidAHB;
        const bool emulationReady = m_vkEmulation->classicImageBridgeReady();
        static std::atomic<uint32_t> diagnostics{0};
        if (gfxstream::base::getEnvironmentVariable("GFXSTREAM_NIRI_IMAGE_TRACE") == "1" &&
            diagnostics.fetch_add(1, std::memory_order_relaxed) < 8) {
            GFXSTREAM_INFO("CIB1-DIAG-DECODER-READY: feature=%d snapshots=%d ahb-mode=%d emulation-ready=%d ready=%d",
                           feature, snapshots, ahbMode, emulationReady,
                           feature && !snapshots && ahbMode && emulationReady);
        }
        return feature && !snapshots && ahbMode && emulationReady;
    }''')

# Query marker/usage2/view-list metadata is recorded before the existing paired gate.
edit(decoder, '''        const auto* physicalInfo = gfxstream::base::find(mPhysdevInfo, physical);
        const auto* instanceInfo = physicalInfo ? gfxstream::base::find(mInstanceInfo, physicalInfo->instance) : nullptr;
        if (!paired || !instanceInfo || instanceInfo->applicationName != "niri" || !classicImageEnabled() || !ext ||''', '''        const auto* physicalInfo = gfxstream::base::find(mPhysdevInfo, physical);
        const auto* instanceInfo = physicalInfo ? gfxstream::base::find(mInstanceInfo, physicalInfo->instance) : nullptr;
        if (instanceInfo && instanceInfo->applicationName == "niri" && ext &&
            (ext->handleType == VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT ||
             ext->handleType == VK_EXTERNAL_MEMORY_HANDLE_TYPE_ANDROID_HARDWARE_BUFFER_BIT_ANDROID) &&
            gfxstream::base::getEnvironmentVariable("GFXSTREAM_NIRI_IMAGE_TRACE") == "1") {
            static std::atomic<uint32_t> diagnostics{0};
            if (diagnostics.fetch_add(1, std::memory_order_relaxed) < 8) {
                const auto* views = vk_find_struct<VkImageFormatListCreateInfo>(input);
                const uint32_t viewCount = views ? views->viewFormatCount : 0;
                const VkFormat* viewFormats = views ? views->pViewFormats : nullptr;
                GFXSTREAM_INFO("CIB1-DIAG-QUERY: handle=0x%x marker=%d state=%s handle-null=%d stride=%d "
                               "format=%d usage=%d consumer=0x%llx producer=0x%llx paired=%d cib-enabled=%d "
                               "view-count=%u view0=%u view1=%u view2=%u view3=%u",
                               (unsigned)ext->handleType, ack != nullptr,
                               !ack ? "absent" : (ack->usage2.consumer == cib::kConsumer ? "paired" : "foreign"),
                               ack != nullptr && !ack->handle, ack ? ack->stride : 0,
                               ack ? ack->format : 0, ack ? ack->usage : 0,
                               (unsigned long long)(ack ? ack->usage2.consumer : 0),
                               (unsigned long long)(ack ? ack->usage2.producer : 0),
                               paired, classicImageEnabled(), viewCount,
                               viewFormats && viewCount > 0 ? (unsigned)viewFormats[0] : 0xffffffffu,
                               viewFormats && viewCount > 1 ? (unsigned)viewFormats[1] : 0xffffffffu,
                               viewFormats && viewCount > 2 ? (unsigned)viewFormats[2] : 0xffffffffu,
                               viewFormats && viewCount > 3 ? (unsigned)viewFormats[3] : 0xffffffffu);
            }
        }
        if (!paired || !instanceInfo || instanceInfo->applicationName != "niri" || !classicImageEnabled() || !ext ||''')

# Create-side marker and view-list metadata, including why a CIB create did not happen.
edit(decoder, '''        const auto* classicMarker = vk_find_struct<VkNativeBufferANDROID>(pCreateInfo);
        if (classicMarker && classicMarker->usage2.consumer == cib::kConsumer)''', '''        const auto* classicMarker = vk_find_struct<VkNativeBufferANDROID>(pCreateInfo);
        {
            const auto* cibExternal = vk_find_struct<VkExternalMemoryImageCreateInfo>(pCreateInfo);
            const bool cibRelevant = classicMarker != nullptr ||
                (cibExternal && (cibExternal->handleTypes &
                    (VK_EXTERNAL_MEMORY_HANDLE_TYPE_ANDROID_HARDWARE_BUFFER_BIT_ANDROID |
                     VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT)));
            const auto* cibInstance = gfxstream::base::find(mInstanceInfo, deviceInfo->parentInstance);
            if (cibRelevant && cibInstance && cibInstance->applicationName == "niri" &&
                gfxstream::base::getEnvironmentVariable("GFXSTREAM_NIRI_IMAGE_TRACE") == "1") {
                static std::atomic<uint32_t> diagnostics{0};
                if (diagnostics.fetch_add(1, std::memory_order_relaxed) < 8) {
                    const auto* views = vk_find_struct<VkImageFormatListCreateInfo>(pCreateInfo);
                    const uint32_t viewCount = views ? views->viewFormatCount : 0;
                    const VkFormat* viewFormats = views ? views->pViewFormats : nullptr;
                    GFXSTREAM_INFO("CIB1-DIAG-CREATE: fmt=%u usage=0x%x flags=0x%x external=0x%x marker=%d "
                                   "state=%s handle-null=%d marker-stride=%d marker-format=%d marker-usage=%d "
                                   "consumer=0x%llx producer=0x%llx cib-enabled=%d view-count=%u "
                                   "view0=%u view1=%u view2=%u view3=%u pNext=%s",
                                   (unsigned)pCreateInfo->format, (unsigned)pCreateInfo->usage,
                                   (unsigned)pCreateInfo->flags,
                                   cibExternal ? (unsigned)cibExternal->handleTypes : 0u,
                                   classicMarker != nullptr,
                                   !classicMarker ? "absent" :
                                       (classicMarker->usage2.consumer == cib::kConsumer ? "paired" : "foreign"),
                                   classicMarker != nullptr && !classicMarker->handle,
                                   classicMarker ? classicMarker->stride : 0,
                                   classicMarker ? classicMarker->format : 0,
                                   classicMarker ? classicMarker->usage : 0,
                                   (unsigned long long)(classicMarker ? classicMarker->usage2.consumer : 0),
                                   (unsigned long long)(classicMarker ? classicMarker->usage2.producer : 0),
                                   classicImageEnabled(), viewCount,
                                   viewFormats && viewCount > 0 ? (unsigned)viewFormats[0] : 0xffffffffu,
                                   viewFormats && viewCount > 1 ? (unsigned)viewFormats[1] : 0xffffffffu,
                                   viewFormats && viewCount > 2 ? (unsigned)viewFormats[2] : 0xffffffffu,
                                   viewFormats && viewCount > 3 ? (unsigned)viewFormats[3] : 0xffffffffu,
                                   niriImagePNext(pCreateInfo->pNext).c_str());
                }
            }
        }
        if (classicMarker && classicMarker->usage2.consumer == cib::kConsumer)''')

print('Applied bounded CIB1 metadata diagnostics; capset, readiness, query and create behavior unchanged')
