#!/usr/bin/env python3
"""Bounded image-view/memory lifetime diagnostics for the Mali destroyed-mutex abort.

Default off (GFXSTREAM_IV_DIAG=1 enables). Logs only handles and metadata the host
already tracks: no extra driver calls, no stores, no return-value changes.
"""
import argparse
from pathlib import Path

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('root', type=Path)
root = p.parse_args().root.resolve()


def edit(rel, old, new):
    path = root / rel
    text = path.read_text(encoding='utf-8')
    if text.count(old) != 1:
        raise SystemExit(f'{rel}: expected one anchor ({text.count(old)}): {old[:100]}')
    path.write_text(text.replace(old, new), encoding='utf-8', newline='')


d = 'host/vulkan/vk_decoder_global_state.cpp'

# 1) The image view creation that aborts inside the driver: what image, whose device,
#    which memory, and is that memory still tracked?
edit(d, '''        VkResult result = vk->vkCreateImageView(device, pCreateInfo, nullptr, pView);''',
     '''        if (gfxstream::base::getEnvironmentVariable("GFXSTREAM_IV_DIAG") == "1") {
            static std::atomic<uint32_t> sIvDiag{0};
            if (sIvDiag.fetch_add(1, std::memory_order_relaxed) < 512) {
                auto memIt = mMemoryInfo.find(imageInfo->memory);
                GFXSTREAM_WARNING(
                    "IV-DIAG-CREATE: device=%p boxed_device=%p image=%llu image_device=%p "
                    "image_mem=%llu mem_found=%d mem_device=%p mem_size=%llu mem_index=%u "
                    "mem_ptr=%p mem_blob=%llu fmt=%d tiling=%d usage=0x%x extent=%ux%u",
                    static_cast<void*>(device), static_cast<void*>(boxed_device),
                    (unsigned long long)pCreateInfo->image,
                    static_cast<void*>(imageInfo->device),
                    (unsigned long long)imageInfo->memory, (int)(memIt != mMemoryInfo.end()),
                    memIt != mMemoryInfo.end() ? static_cast<void*>(memIt->second.device) : nullptr,
                    memIt != mMemoryInfo.end() ? (unsigned long long)memIt->second.size : 0ull,
                    memIt != mMemoryInfo.end() ? memIt->second.memoryIndex : 0u,
                    memIt != mMemoryInfo.end() ? memIt->second.ptr : nullptr,
                    memIt != mMemoryInfo.end() ? (unsigned long long)memIt->second.blobId : 0ull,
                    (int)imageInfo->imageCreateInfoShallow.format,
                    (int)imageInfo->imageCreateInfoShallow.tiling,
                    (unsigned)imageInfo->imageCreateInfoShallow.usage,
                    imageInfo->imageCreateInfoShallow.extent.width,
                    imageInfo->imageCreateInfoShallow.extent.height);
            }
        }
        VkResult result = vk->vkCreateImageView(device, pCreateInfo, nullptr, pView);''')

# 2) Image destroy.
edit(d, '''        auto& imageInfo = imageInfoIt->second;

        destroyImageWithExclusiveInfo(device, deviceDispatch, image, imageInfo);''',
     '''        auto& imageInfo = imageInfoIt->second;

        if (gfxstream::base::getEnvironmentVariable("GFXSTREAM_IV_DIAG") == "1") {
            static std::atomic<uint32_t> sDiag{0};
            if (sDiag.fetch_add(1, std::memory_order_relaxed) < 512) {
                GFXSTREAM_WARNING(
                    "IV-DIAG-DESTROY-IMAGE: device=%p image=%llu image_device=%p image_mem=%llu",
                    static_cast<void*>(device), (unsigned long long)image,
                    static_cast<void*>(imageInfo.device),
                    (unsigned long long)imageInfo.memory);
            }
        }
        destroyImageWithExclusiveInfo(device, deviceDispatch, image, imageInfo);''')

# 3) Image view destroy.
edit(d, '''        auto& imageViewInfo = imageViewInfoIt->second;

        destroyImageViewWithExclusiveInfo(device, deviceDispatch, imageView, imageViewInfo);''',
     '''        auto& imageViewInfo = imageViewInfoIt->second;

        if (gfxstream::base::getEnvironmentVariable("GFXSTREAM_IV_DIAG") == "1") {
            static std::atomic<uint32_t> sDiag{0};
            if (sDiag.fetch_add(1, std::memory_order_relaxed) < 512) {
                GFXSTREAM_WARNING(
                    "IV-DIAG-DESTROY-VIEW: device=%p view=%llu view_device=%p",
                    static_cast<void*>(device), (unsigned long long)imageView,
                    static_cast<void*>(imageViewInfo.device));
            }
        }
        destroyImageViewWithExclusiveInfo(device, deviceDispatch, imageView, imageViewInfo);''')

# 4) Device destroy. Anchor on the body start, not the signature: the native backing
#    lease patch changes the return type of this function.
edit(d, '''        auto deviceInfoIt = mDeviceInfo.find(device);
        if (deviceInfoIt == mDeviceInfo.end()) {
            GFXSTREAM_WARNING("Could not find device:%p to destroy", device);''',
     '''        if (gfxstream::base::getEnvironmentVariable("GFXSTREAM_IV_DIAG") == "1") {
            static std::atomic<uint32_t> sDiag{0};
            if (sDiag.fetch_add(1, std::memory_order_relaxed) < 64) {
                GFXSTREAM_WARNING("IV-DIAG-DESTROY-DEVICE: device=%p",
                                  static_cast<void*>(device));
            }
        }
        auto deviceInfoIt = mDeviceInfo.find(device);
        if (deviceInfoIt == mDeviceInfo.end()) {
            GFXSTREAM_WARNING("Could not find device:%p to destroy", device);''')

# 5) Memory free.
edit(d, '''        std::lock_guard<std::mutex> lock(mMutex);
        freeMemoryLocked(device, deviceDispatch, memory);''',
     '''        std::lock_guard<std::mutex> lock(mMutex);
        if (gfxstream::base::getEnvironmentVariable("GFXSTREAM_IV_DIAG") == "1") {
            static std::atomic<uint32_t> sDiag{0};
            if (sDiag.fetch_add(1, std::memory_order_relaxed) < 512) {
                auto* memInfo = gfxstream::base::find(mMemoryInfo, memory);
                GFXSTREAM_WARNING(
                    "IV-DIAG-FREE-MEMORY: device=%p memory=%llu mem_found=%d mem_device=%p "
                    "mem_size=%llu mem_blob=%llu",
                    static_cast<void*>(device), (unsigned long long)memory,
                    (int)(memInfo != nullptr),
                    memInfo ? static_cast<void*>(memInfo->device) : nullptr,
                    memInfo ? (unsigned long long)memInfo->size : 0ull,
                    memInfo ? (unsigned long long)memInfo->blobId : 0ull);
            }
        }
        freeMemoryLocked(device, deviceDispatch, memory);''')

print('bounded image-view/memory lifetime diagnostics (GFXSTREAM_IV_DIAG=1)')
