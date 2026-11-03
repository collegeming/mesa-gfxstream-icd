#!/usr/bin/env python3
"""Bounded image-view/memory lifetime diagnostics for the Mali destroyed-mutex abort.

Default off (GFXSTREAM_IV_DIAG=1 enables). Writes to fd 2 instead of logcat: the
abort kills the process inside the driver call, and logcat can lose the last
buffered line, while the stderr pipe has already handed it to the VMM log.
Logs only handles and metadata the host already tracks: no extra driver calls,
no stores, no return-value changes.
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

# stdio for the direct-to-fd-2 diagnostics.
edit(d, '#include <cerrno>\n', '#include <cerrno>\n#include <cstdio>\n')

# 1) The image view creation that aborts inside the driver: what image, whose device,
#    which memory, is that memory still tracked, and what view is being asked for.
#    A matching IV-DIAG-CREATED line follows a driver call that returned.
edit(d, '''        VkResult result = vk->vkCreateImageView(device, pCreateInfo, nullptr, pView);''',
     '''        if (gfxstream::base::getEnvironmentVariable("GFXSTREAM_IV_DIAG") == "1") {
            static std::atomic<uint32_t> sIvDiag{0};
            if (sIvDiag.fetch_add(1, std::memory_order_relaxed) < 512) {
                auto memIt = mMemoryInfo.find(imageInfo->memory);
                fprintf(stderr,
                        "IV-DIAG-CREATE: device=%p boxed_device=%p image=%llu "
                        "image_device=%p image_mem=%llu mem_found=%d mem_device=%p "
                        "mem_size=%llu mem_index=%u mem_ptr=%p mem_blob=%llu "
                        "fmt=%d tiling=%d usage=0x%x extent=%ux%u view_fmt=%d "
                        "view_components=%d,%d,%d,%d cb=%d\\n",
                        static_cast<void*>(device), static_cast<void*>(boxed_device),
                        (unsigned long long)pCreateInfo->image,
                        static_cast<void*>(imageInfo->device),
                        (unsigned long long)imageInfo->memory, (int)(memIt != mMemoryInfo.end()),
                        memIt != mMemoryInfo.end()
                            ? static_cast<void*>(memIt->second.device) : nullptr,
                        memIt != mMemoryInfo.end()
                            ? (unsigned long long)memIt->second.size : 0ull,
                        memIt != mMemoryInfo.end() ? memIt->second.memoryIndex : 0u,
                        memIt != mMemoryInfo.end() ? memIt->second.ptr : nullptr,
                        memIt != mMemoryInfo.end()
                            ? (unsigned long long)memIt->second.blobId : 0ull,
                        (int)imageInfo->imageCreateInfoShallow.format,
                        (int)imageInfo->imageCreateInfoShallow.tiling,
                        (unsigned)imageInfo->imageCreateInfoShallow.usage,
                        imageInfo->imageCreateInfoShallow.extent.width,
                        imageInfo->imageCreateInfoShallow.extent.height,
                        (int)pCreateInfo->format, (int)pCreateInfo->components.r,
                        (int)pCreateInfo->components.g, (int)pCreateInfo->components.b,
                        (int)pCreateInfo->components.a,
                        imageInfo->boundColorBuffer ? (int)*imageInfo->boundColorBuffer : -1);
                fflush(stderr);
            }
        }
        VkResult result = vk->vkCreateImageView(device, pCreateInfo, nullptr, pView);
        if (gfxstream::base::getEnvironmentVariable("GFXSTREAM_IV_DIAG") == "1") {
            static std::atomic<uint32_t> sIvDiagDone{0};
            if (sIvDiagDone.fetch_add(1, std::memory_order_relaxed) < 512) {
                fprintf(stderr, "IV-DIAG-CREATED: image=%llu result=%d view=%llu\\n",
                        (unsigned long long)pCreateInfo->image, (int)result,
                        (unsigned long long)*pView);
                fflush(stderr);
            }
        }''')

# 1b) The end of the same function: a view that reaches here completed its bookkeeping.
edit(d, '''        *pView = new_boxed_non_dispatchable_VkImageView(*pView);
        imageViewInfo.boxed = *pView;
        return result;''',
     '''        *pView = new_boxed_non_dispatchable_VkImageView(*pView);
        imageViewInfo.boxed = *pView;
        if (gfxstream::base::getEnvironmentVariable("GFXSTREAM_IV_DIAG") == "1") {
            static std::atomic<uint32_t> sIvDone{0};
            if (sIvDone.fetch_add(1, std::memory_order_relaxed) < 512) {
                fprintf(stderr, "IV-DIAG-DONE: view=%llu cb=%d\\n",
                        (unsigned long long)*pView,
                        imageViewInfo.boundColorBuffer ? (int)*imageViewInfo.boundColorBuffer : -1);
                fflush(stderr);
            }
        }
        return result;''')

# 1c) The second driver call in the same function: the debug label.
edit(d, '''        if (imageViewInfo.boundColorBuffer) {
            deviceInfo->debugUtilsHelper.addDebugLabel(*pView, "ColorBuffer:%d",
                                                       *imageViewInfo.boundColorBuffer);
        }''',
     '''        if (imageViewInfo.boundColorBuffer) {
            if (gfxstream::base::getEnvironmentVariable("GFXSTREAM_IV_DIAG") == "1") {
                static std::atomic<uint32_t> sIvLabel{0};
                if (sIvLabel.fetch_add(1, std::memory_order_relaxed) < 512) {
                    fprintf(stderr, "IV-DIAG-LABEL: view=%llu cb=%d enabled=%d\\n",
                            (unsigned long long)*pView, (int)*imageViewInfo.boundColorBuffer,
                            (int)deviceInfo->debugUtilsHelper.isEnabled());
                    fflush(stderr);
                }
            }
            deviceInfo->debugUtilsHelper.addDebugLabel(*pView, "ColorBuffer:%d",
                                                       *imageViewInfo.boundColorBuffer);
        }''')

# 2) Image destroy.
edit(d, '''        auto& imageInfo = imageInfoIt->second;

        destroyImageWithExclusiveInfo(device, deviceDispatch, image, imageInfo);''',
     '''        auto& imageInfo = imageInfoIt->second;

        if (gfxstream::base::getEnvironmentVariable("GFXSTREAM_IV_DIAG") == "1") {
            static std::atomic<uint32_t> sDiag{0};
            if (sDiag.fetch_add(1, std::memory_order_relaxed) < 512) {
                fprintf(stderr,
                        "IV-DIAG-DESTROY-IMAGE: device=%p image=%llu image_device=%p "
                        "image_mem=%llu\\n",
                        static_cast<void*>(device), (unsigned long long)image,
                        static_cast<void*>(imageInfo.device),
                        (unsigned long long)imageInfo.memory);
                fflush(stderr);
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
                fprintf(stderr, "IV-DIAG-DESTROY-VIEW: device=%p view=%llu view_device=%p\\n",
                        static_cast<void*>(device), (unsigned long long)imageView,
                        static_cast<void*>(imageViewInfo.device));
                fflush(stderr);
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
                fprintf(stderr, "IV-DIAG-DESTROY-DEVICE: device=%p\\n",
                        static_cast<void*>(device));
                fflush(stderr);
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
                fprintf(stderr,
                        "IV-DIAG-FREE-MEMORY: device=%p memory=%llu mem_found=%d mem_device=%p "
                        "mem_size=%llu mem_blob=%llu\\n",
                        static_cast<void*>(device), (unsigned long long)memory,
                        (int)(memInfo != nullptr),
                        memInfo ? static_cast<void*>(memInfo->device) : nullptr,
                        memInfo ? (unsigned long long)memInfo->size : 0ull,
                        memInfo ? (unsigned long long)memInfo->blobId : 0ull);
                fflush(stderr);
            }
        }
        freeMemoryLocked(device, deviceDispatch, memory);''')

print('bounded image-view/memory lifetime diagnostics to fd 2 (GFXSTREAM_IV_DIAG=1)')
