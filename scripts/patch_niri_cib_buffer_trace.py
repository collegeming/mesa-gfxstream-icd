#!/usr/bin/env python3
"""Bounded buffer-create diagnostics plus default-off no-external experiment."""
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

buffer_cpp = 'host/buffer.cpp'
edit(buffer_cpp, '#include "buffer.h"\n', '#include "buffer.h"\n\n#include <atomic>\n')
edit(buffer_cpp, '#include "vulkan/buffer_vk.h"', '#include "gfxstream/system/System.h"\n#include "vulkan/buffer_vk.h"')
edit(buffer_cpp, '''std::unique_ptr<Buffer::Impl> Buffer::Impl::create(gl::EmulationGl* emulationGl,
                                                   vk::VkEmulation* emulationVk, uint64_t size,
                                                   HandleType handle) {
    std::unique_ptr<Buffer::Impl> buffer(new Buffer::Impl(handle, size));

#if GFXSTREAM_ENABLE_HOST_GLES
    if (emulationGl) {
        buffer->mBufferGl = emulationGl->createBuffer(size, handle);
        if (!buffer->mBufferGl) {''', '''std::unique_ptr<Buffer::Impl> Buffer::Impl::create(gl::EmulationGl* emulationGl,
                                                   vk::VkEmulation* emulationVk, uint64_t size,
                                                   HandleType handle) {
    std::unique_ptr<Buffer::Impl> buffer(new Buffer::Impl(handle, size));

    const bool cibBufDiag =
        gfxstream::base::getEnvironmentVariable("GFXSTREAM_NIRI_IMAGE_TRACE") == "1";
    if (cibBufDiag) {
        static std::atomic<uint32_t> diagnostics{0};
        if (diagnostics.fetch_add(1, std::memory_order_relaxed) < 16) {
            GFXSTREAM_INFO("CIB1-DIAG-BUF-CREATE: handle=%u size=%llu emulation-gl=%d emulation-vk=%d",
                           (unsigned)handle, (unsigned long long)size, emulationGl != nullptr,
                           emulationVk != nullptr);
        }
    }

#if GFXSTREAM_ENABLE_HOST_GLES
    if (emulationGl) {
        buffer->mBufferGl = emulationGl->createBuffer(size, handle);
        if (cibBufDiag) {
            static std::atomic<uint32_t> glDiagnostics{0};
            if (glDiagnostics.fetch_add(1, std::memory_order_relaxed) < 16) {
                GFXSTREAM_INFO("CIB1-DIAG-BUF-GL: handle=%u created=%d",
                               (unsigned)handle, buffer->mBufferGl != nullptr);
            }
        }
        if (!buffer->mBufferGl) {''')
edit(buffer_cpp, '''        buffer->mBufferVk = vk::BufferVk::create(*emulationVk, handle, size, vulkanOnly);
        if (!buffer->mBufferVk) {''', '''        buffer->mBufferVk = vk::BufferVk::create(*emulationVk, handle, size, vulkanOnly);
        if (cibBufDiag) {
            static std::atomic<uint32_t> vkDiagnostics{0};
            if (vkDiagnostics.fetch_add(1, std::memory_order_relaxed) < 16) {
                GFXSTREAM_INFO("CIB1-DIAG-BUF-VK: handle=%u created=%d vulkan-only=%d",
                               (unsigned)handle, buffer->mBufferVk != nullptr, vulkanOnly);
            }
        }
        if (!buffer->mBufferVk) {''')

ops = 'host/vulkan/vk_common_operations.cpp'
edit(ops, '''bool VkEmulation::setupVkBuffer(uint64_t size, uint32_t bufferHandle, bool vulkanOnly,
                                uint32_t memoryProperty) {
    if (vulkanOnly == false) {''', '''bool VkEmulation::setupVkBuffer(uint64_t size, uint32_t bufferHandle, bool vulkanOnly,
                                uint32_t memoryProperty) {
    const bool cibVkBufDiag =
        gfxstream::base::getEnvironmentVariable("GFXSTREAM_NIRI_IMAGE_TRACE") == "1";
    const bool cibExternalModeKnown =
        mDeviceInfo.externalMemoryMode != ExternalMemory::Mode::Unknown;
    const unsigned cibDefaultHandle =
        cibExternalModeKnown ? (unsigned)getDefaultExternalMemoryHandleType() : 0u;
    if (cibVkBufDiag) {
        static std::atomic<uint32_t> diagnostics{0};
        if (diagnostics.fetch_add(1, std::memory_order_relaxed) < 16) {
            GFXSTREAM_INFO("CIB1-DIAG-BUF-VK-ENTRY: handle=%u size=%llu vulkan-only=%d "
                           "memory-property=0x%x import=%d export=%d default-handle=0x%x",
                           bufferHandle, (unsigned long long)size, vulkanOnly, memoryProperty,
                           mDeviceInfo.supportsExternalMemoryImport,
                           mDeviceInfo.supportsExternalMemoryExport, cibDefaultHandle);
        }
    }
    if (vulkanOnly == false) {''')
edit(ops, '''    res.sharingMode = VK_SHARING_MODE_EXCLUSIVE;

    // Create the buffer. If external memory is supported, make it external.
    VkExternalMemoryBufferCreateInfo extBufferCi = {''', '''    res.sharingMode = VK_SHARING_MODE_EXCLUSIVE;

    if (cibVkBufDiag && cibExternalModeKnown) {
        static std::atomic<uint32_t> extPropsDiagnostics{0};
        if (extPropsDiagnostics.fetch_add(1, std::memory_order_relaxed) < 16) {
            VkPhysicalDeviceExternalBufferInfo extInfo = {
                VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_EXTERNAL_BUFFER_INFO,
                nullptr,
                res.createFlags,
                res.usageFlags,
                static_cast<VkExternalMemoryHandleTypeFlagBits>(cibDefaultHandle),
            };
            VkExternalBufferProperties extProps = {
                VK_STRUCTURE_TYPE_EXTERNAL_BUFFER_PROPERTIES,
                nullptr,
                {},
            };
            const bool available = vk->vkGetPhysicalDeviceExternalBufferProperties != nullptr;
            bool queried = false;
            if (available) {
                vk->vkGetPhysicalDeviceExternalBufferProperties(mPhysicalDevice, &extInfo, &extProps);
                queried = true;
            }
            GFXSTREAM_INFO("CIB1-DIAG-BUF-VK-EXTPROPS: handle=%u available=%d queried=%d "
                           "features=0x%x export=0x%x compatible=0x%x",
                           bufferHandle, available, queried,
                           (unsigned)extProps.externalMemoryProperties.externalMemoryFeatures,
                           (unsigned)extProps.externalMemoryProperties.exportFromImportedHandleTypes,
                           (unsigned)extProps.externalMemoryProperties.compatibleHandleTypes);
        }
    }

    // Create the buffer. If external memory is supported, make it external.
    VkExternalMemoryBufferCreateInfo extBufferCi = {''')
edit(ops, '''    void* extBufferCiPtr = nullptr;
    if (mDeviceInfo.supportsExternalMemoryImport || mDeviceInfo.supportsExternalMemoryExport) {
        extBufferCi.handleTypes =
            static_cast<VkExternalMemoryHandleTypeFlags>(getDefaultExternalMemoryHandleType());
        extBufferCiPtr = &extBufferCi;
    }''', '''    void* extBufferCiPtr = nullptr;
    const bool cibNoExternalBuffer =
        gfxstream::base::getEnvironmentVariable("GFXSTREAM_NIRI_BUFFER_NO_EXTERNAL") == "1";
    if (mDeviceInfo.supportsExternalMemoryImport || mDeviceInfo.supportsExternalMemoryExport) {
        if (cibNoExternalBuffer) {
            if (cibVkBufDiag) {
                static std::atomic<uint32_t> disabledDiagnostics{0};
                if (disabledDiagnostics.fetch_add(1, std::memory_order_relaxed) < 16) {
                    GFXSTREAM_INFO("CIB1-DIAG-BUF-VK-EXTERNAL-DISABLED: handle=%u "
                                   "default-handle=0x%x",
                                   bufferHandle, (unsigned)getDefaultExternalMemoryHandleType());
                }
            }
        } else {
            extBufferCi.handleTypes =
                static_cast<VkExternalMemoryHandleTypeFlags>(getDefaultExternalMemoryHandleType());
            extBufferCiPtr = &extBufferCi;
        }
    }''')
edit(ops, '''    VkResult createRes = vk->vkCreateBuffer(mDevice, &bufferCi, nullptr, &res.buffer);

    if (createRes != VK_SUCCESS) {''', '''    VkResult createRes = vk->vkCreateBuffer(mDevice, &bufferCi, nullptr, &res.buffer);
    if (cibVkBufDiag) {
        static std::atomic<uint32_t> createDiagnostics{0};
        if (createDiagnostics.fetch_add(1, std::memory_order_relaxed) < 16) {
            GFXSTREAM_INFO("CIB1-DIAG-BUF-VK-CREATE: handle=%u result=%d ext-ci=%d",
                           bufferHandle, (int)createRes, extBufferCiPtr != nullptr);
        }
    }

    if (createRes != VK_SUCCESS) {''')
edit(ops, '''    res.memory.typeIndex = getValidMemoryTypeIndex(memReqs.memoryTypeBits, memoryProperty);

    GFXSTREAM_DEBUG(''', '''    res.memory.typeIndex = getValidMemoryTypeIndex(memReqs.memoryTypeBits, memoryProperty);
    if (cibVkBufDiag) {
        static std::atomic<uint32_t> reqDiagnostics{0};
        if (reqDiagnostics.fetch_add(1, std::memory_order_relaxed) < 16) {
            GFXSTREAM_INFO("CIB1-DIAG-BUF-VK-REQ: handle=%u size=%llu alignment=%llu bits=0x%x "
                           "type-index=%u dedicated=%d",
                           bufferHandle, (unsigned long long)memReqs.size,
                           (unsigned long long)memReqs.alignment, memReqs.memoryTypeBits,
                           res.memory.typeIndex, useDedicated);
        }
    }

    GFXSTREAM_DEBUG(''')
edit(ops, '''    bool allocRes = allocExternalMemory(vk, &res.memory, deviceAlignment, dedicated_buffer);

    if (!allocRes) {''', '''    bool allocRes = allocExternalMemory(vk, &res.memory, deviceAlignment, dedicated_buffer);
    if (cibVkBufDiag) {
        static std::atomic<uint32_t> allocDiagnostics{0};
        if (allocDiagnostics.fetch_add(1, std::memory_order_relaxed) < 16) {
            GFXSTREAM_INFO("CIB1-DIAG-BUF-VK-ALLOC: handle=%u result=%d memory=%llu",
                           bufferHandle, allocRes, (unsigned long long)(uintptr_t)res.memory.memory);
        }
    }

    if (!allocRes) {''')
edit(ops, '''    VkResult bindBufferMemoryRes =
        vk->vkBindBufferMemory(mDevice, res.buffer, res.memory.memory, 0);

    if (bindBufferMemoryRes != VK_SUCCESS) {''', '''    VkResult bindBufferMemoryRes =
        vk->vkBindBufferMemory(mDevice, res.buffer, res.memory.memory, 0);
    if (cibVkBufDiag) {
        static std::atomic<uint32_t> bindDiagnostics{0};
        if (bindDiagnostics.fetch_add(1, std::memory_order_relaxed) < 16) {
            GFXSTREAM_INFO("CIB1-DIAG-BUF-VK-BIND: handle=%u result=%d",
                           bufferHandle, (int)bindBufferMemoryRes);
        }
    }

    if (bindBufferMemoryRes != VK_SUCCESS) {''')
print('Added bounded buffer create diagnostics and default-off external-buffer experiment')
