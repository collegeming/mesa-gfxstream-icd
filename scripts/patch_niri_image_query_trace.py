#!/usr/bin/env python3
"""Bounded metadata trace of niri's existing image queries/create/requirements."""
import argparse
from pathlib import Path
p = argparse.ArgumentParser(description=__doc__)
p.add_argument('root', type=Path)
root = p.parse_args().root.resolve()
def edit(rel, old, new):
    path = root/rel; text = path.read_text()
    if text.count(old) != 1: raise SystemExit(f'{rel}: expected one anchor ({text.count(old)}): {old[:80]}')
    path.write_text(text.replace(old,new))
d='host/vulkan/vk_decoder_global_state.cpp'
methods='''    uint64_t niriImageTraceLocked(VkInstance instance) REQUIRES(mMutex) {
        const auto* info = gfxstream::base::find(mInstanceInfo, instance);
        if (!info || info->applicationName != "niri" ||
            gfxstream::base::getEnvironmentVariable("GFXSTREAM_NIRI_IMAGE_TRACE") != "1") return 0;
        static std::atomic<uint64_t> records{0};
        uint64_t count = records.load(std::memory_order_relaxed);
        while (count < 64) {
            if (records.compare_exchange_weak(count, count + 1, std::memory_order_relaxed)) return count + 1;
        }
        return 0;
    }
    uint64_t niriDeviceImageTraceLocked(VkDevice device) REQUIRES(mMutex) {
        const auto* info = gfxstream::base::find(mDeviceInfo, device);
        return info ? niriImageTraceLocked(info->parentInstance) : 0;
    }
    void traceNiriImageQueryInput(VkPhysicalDevice physical, const VkPhysicalDeviceImageFormatInfo2* input) {
        std::lock_guard<std::mutex> lock(mMutex);
        if (!input) return;
        const auto* ext = vk_find_struct<VkPhysicalDeviceExternalImageFormatInfo>(input);
        if (!ext || !ext->handleType) return; // filter BEFORE consuming the shared budget
        const auto* info = gfxstream::base::find(mPhysdevInfo, physical);
        const uint64_t id = info ? niriImageTraceLocked(info->instance) : 0;
        if (!id) return;
        GFXSTREAM_INFO("NIRI-IMAGE-QUERY-WIRE: id=%llu fmt=%u type=%u tiling=%u usage=0x%x flags=0x%x external=0x%x pNext=%s phase=before-host-codec-transform",
            (unsigned long long)id, input->format, input->type, input->tiling, input->usage, input->flags,
            ext ? ext->handleType : 0, niriImagePNext(input->pNext).c_str());
    }
    static std::string niriImagePNext(const void* next) {
        std::string types;
        auto* node = static_cast<const VkBaseInStructure*>(next);
        for (unsigned i = 0; node && i < 8; ++i, node = node->pNext) {
            types += std::to_string(static_cast<unsigned>(node->sType)) + ",";
        }
        return types;
    }

'''
edit(d,'    VkResult on_vkGetPhysicalDeviceImageFormatProperties2(',methods+'    VkResult on_vkGetPhysicalDeviceImageFormatProperties2(')
# Snapshot original handler input before emulated-format changes, not extra native queries.
edit(d,'''        VkPhysicalDeviceImageFormatInfo2 imageFormatInfo;
        VkFormat format = pImageFormatInfo->format;''','''        const VkPhysicalDeviceImageFormatInfo2 niriOriginal = *pImageFormatInfo;
        const auto* niriOriginalExternal = vk_find_struct<VkPhysicalDeviceExternalImageFormatInfo>(pImageFormatInfo);
        const uint32_t niriOriginalHandle = niriOriginalExternal ? niriOriginalExternal->handleType : 0;
        VkPhysicalDeviceImageFormatInfo2 imageFormatInfo;
        VkFormat format = pImageFormatInfo->format;''')
edit(d,'''        // Pick by host dispatch-pointer availability; see on_vkGetPhysicalDeviceFeatures2.
        if (vk->vkGetPhysicalDeviceImageFormatProperties2) {''','''        const uint64_t niriQueryRecord = niriOriginalHandle ? niriImageTraceLocked(physdevInfo->instance) : 0;
        if (niriQueryRecord) GFXSTREAM_INFO(
            "NIRI-IMAGE-QUERY-BEGIN: id=%llu phys=%p fmt=%u type=%u tiling=%u usage=0x%x flags=0x%x external=0x%x pNext=%s "
            "native-fmt=%u native-tiling=%u native-usage=0x%x native-flags=0x%x native-external=0x%x native-pNext=%s",
            (unsigned long long)niriQueryRecord, (void*)physicalDevice, niriOriginal.format, niriOriginal.type,
            niriOriginal.tiling, niriOriginal.usage, niriOriginal.flags, niriOriginalHandle,
            niriImagePNext(niriOriginal.pNext).c_str(), pImageFormatInfo->format, pImageFormatInfo->tiling,
            pImageFormatInfo->usage, pImageFormatInfo->flags, extImageFormatInfo ? extImageFormatInfo->handleType : 0,
            niriImagePNext(pImageFormatInfo->pNext).c_str());
        // Pick by host dispatch-pointer availability; see on_vkGetPhysicalDeviceFeatures2.
        if (vk->vkGetPhysicalDeviceImageFormatProperties2) {''')
edit(d,'''        if (res != VK_SUCCESS) {
            return res;
        }

        VkExternalImageFormatProperties* extImageFormatProps =''','''        if (niriQueryRecord) {
            const auto* nativeExternal = res == VK_SUCCESS ?
                vk_find_struct<VkExternalImageFormatProperties>(pImageFormatProperties) : nullptr;
            GFXSTREAM_INFO("NIRI-IMAGE-QUERY-END: id=%llu result=%d features=0x%x export-from-imported=0x%x compatible=0x%x max-extent=%ux%ux%u",
                (unsigned long long)niriQueryRecord, (int)res,
                nativeExternal ? nativeExternal->externalMemoryProperties.externalMemoryFeatures : 0,
                nativeExternal ? nativeExternal->externalMemoryProperties.exportFromImportedHandleTypes : 0,
                nativeExternal ? nativeExternal->externalMemoryProperties.compatibleHandleTypes : 0,
                res == VK_SUCCESS ? pImageFormatProperties->imageFormatProperties.maxExtent.width : 0,
                res == VK_SUCCESS ? pImageFormatProperties->imageFormatProperties.maxExtent.height : 0,
                res == VK_SUCCESS ? pImageFormatProperties->imageFormatProperties.maxExtent.depth : 0);
        }
        if (res != VK_SUCCESS) {
            return res;
        }

        VkExternalImageFormatProperties* extImageFormatProps =''')
edit(d,'''            createRes = vk->vkCreateImage(device, pCreateInfo, nullptr, pImage);''','''            const auto* niriExternal = vk_find_struct<VkExternalMemoryImageCreateInfo>(pCreateInfo);
            const uint64_t niriCreateRecord = niriExternal && niriExternal->handleTypes ? niriDeviceImageTraceLocked(device) : 0;
            if (niriCreateRecord) GFXSTREAM_INFO(
                "NIRI-IMAGE-CREATE-BEGIN: id=%llu device=%p fmt=%u type=%u extent=%ux%ux%u tiling=%u usage=0x%x flags=0x%x external=0x%x pNext=%s",
                (unsigned long long)niriCreateRecord, (void*)device, pCreateInfo->format, pCreateInfo->imageType,
                pCreateInfo->extent.width, pCreateInfo->extent.height, pCreateInfo->extent.depth,
                pCreateInfo->tiling, pCreateInfo->usage, pCreateInfo->flags,
                niriExternal ? niriExternal->handleTypes : 0, niriImagePNext(pCreateInfo->pNext).c_str());
            createRes = vk->vkCreateImage(device, pCreateInfo, nullptr, pImage);
            if (niriCreateRecord) GFXSTREAM_INFO("NIRI-IMAGE-CREATE-END: id=%llu result=%d image=%p",
                (unsigned long long)niriCreateRecord, (int)createRes, createRes == VK_SUCCESS ? (void*)*pImage : nullptr);''')
# Requirements are logged before existing size/index transformations, after real driver call.
req='''        const auto* niriImage = gfxstream::base::find(mImageInfo, NIRI_IMAGE);
        const uint64_t niriReqRecord = niriImage && niriImage->diagnosticExternalHandleTypes ? niriDeviceImageTraceLocked(device) : 0;
        if (niriReqRecord) {
            GFXSTREAM_INFO("NIRI-IMAGE-REQUIREMENTS: id=%llu device=%p image=%p fmt=%u size=%llu alignment=%llu bits=0x%x native-output=1",
                (unsigned long long)niriReqRecord, (void*)device, (void*)NIRI_IMAGE,
                niriImage ? niriImage->imageCreateInfoShallow.format : VK_FORMAT_UNDEFINED,
                (unsigned long long)NIRI_REQ.size, (unsigned long long)NIRI_REQ.alignment, NIRI_REQ.memoryTypeBits);
        }
'''
edit(d,'''        updateImageMemorySizeLocked(device, image, pMemoryRequirements);''',req.replace('NIRI_IMAGE','image').replace('NIRI_REQ','(*pMemoryRequirements)')+'''        updateImageMemorySizeLocked(device, image, pMemoryRequirements);''')
edit(d,'''        updateImageMemorySizeLocked(device, pInfo->image, &pMemoryRequirements->memoryRequirements);''',req.replace('NIRI_IMAGE','pInfo->image').replace('NIRI_REQ','pMemoryRequirements->memoryRequirements')+'''        updateImageMemorySizeLocked(device, pInfo->image, &pMemoryRequirements->memoryRequirements);''')
edit('host/vulkan/vk_decoder_global_state.h', '    static void reset();', '''    static void reset();
    void traceNiriImageQueryInput(VkPhysicalDevice physical, const VkPhysicalDeviceImageFormatInfo2* input);''')
edit(d, 'VkDecoderGlobalState::~VkDecoderGlobalState() = default;', '''VkDecoderGlobalState::~VkDecoderGlobalState() = default;
void VkDecoderGlobalState::traceNiriImageQueryInput(VkPhysicalDevice physical, const VkPhysicalDeviceImageFormatInfo2* input) {
    mImpl->traceNiriImageQueryInput(physical, input);
}''')
path = root/'host/vulkan/vk_decoder.cpp'
text = path.read_text()
old = '''                    transform_tohost_VkPhysicalDeviceImageFormatInfo2(
                        m_state, (VkPhysicalDeviceImageFormatInfo2*)(pImageFormatInfo));'''
assert text.count(old) == 2
text = text.replace(old, '''                    m_state->traceNiriImageQueryInput(unbox_VkPhysicalDevice(physicalDevice), pImageFormatInfo);
''' + old)
path.write_text(text)
print('Applied niri metadata-only bounded image query/create/requirements trace')
