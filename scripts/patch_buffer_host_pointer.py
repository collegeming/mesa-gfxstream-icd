#!/usr/bin/env python3
"""Back classic Vulkan buffers with host-pointer memory for SystemBlob hosts.

Guest-visible VkBuffers (staging, readback, sync) are created by gfxstream's
classic buffer path, whose memory is allocated with the device-wide external
memory mode. On this host that mode is AndroidAHB, so buffers get an AHB whose
duplication into the guest's VkDeviceMemory fails (buffers are deliberately not
AHB-exported, and Mali rejects AHB imports here anyway). The guest then sees
VK_ERROR_OUT_OF_DEVICE_MEMORY for every small host-visible buffer, which is the
allocation zink/readback and vkcube fail on.

Allocate buffer memory the same way SystemBlob allocates blob memory instead:
host memory imported with VK_EXT_external_memory_host. The guest's
VkImportBufferGOOGLE then reuses that very host pointer, so the buffer never
needs an AHB or dma-buf handle. Gated by GFXSTREAM_BUFFER_HOST_PTR=1.
"""
import argparse
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('root', type=Path)
root = parser.parse_args().root.resolve()

# 1) Header: expose the buffer's host allocation regardless of the global mode.
h = root / 'host/vulkan/vk_common_operations.h'
text = h.read_text(encoding='utf-8')
OLD = '    void* getBufferHostPointer(uint32_t bufferHandle);\n'
NEW = ('    void* getBufferHostPointer(uint32_t bufferHandle);\n'
       '    // Host allocation recorded for a buffer, whatever the device-wide external\n'
       '    // memory mode is. Used to re-import a classic buffer by host pointer.\n'
       '    void* getBufferHostPointerAny(uint32_t bufferHandle);\n')
if text.count(OLD) != 1:
    raise SystemExit(f'expected one header anchor, found {text.count(OLD)}')
text = text.replace(OLD, NEW)

OLD = ('        gfxstream::base::Optional<VkImage> imageForDedicatedAllocation = gfxstream::base::kNullopt,\n'
       '        gfxstream::base::Optional<ColorBufferInfo*> colorBufferInfo = gfxstream::base::kNullopt);\n')
NEW = ('        gfxstream::base::Optional<VkImage> imageForDedicatedAllocation = gfxstream::base::kNullopt,\n'
       '        gfxstream::base::Optional<ColorBufferInfo*> colorBufferInfo = gfxstream::base::kNullopt,\n'
       '        bool hostPointerBacked = false);\n')
if text.count(OLD) != 1:
    raise SystemExit(f'expected one declaration anchor, found {text.count(OLD)}')
text = text.replace(OLD, NEW)
h.write_text(text, encoding='utf-8', newline='')

cpp = root / 'host/vulkan/vk_common_operations.cpp'
text = cpp.read_text(encoding='utf-8')

# 2) Implementation next to the mode-gated accessor.
OLD = '''void* VkEmulation::getBufferHostPointer(uint32_t bufferHandle) {
    if (getExternalMemoryMode() != ExternalMemory::Mode::HostAllocation) {
        GFXSTREAM_ERROR("%s: external memory mode doesn't use host pointers!", __func__);
        return nullptr;
    }
'''
NEW = '''void* VkEmulation::getBufferHostPointerAny(uint32_t bufferHandle) {
    std::lock_guard<std::mutex> lock(mMutex);
    auto infoPtr = gfxstream::base::find(mBuffers, bufferHandle);
    return infoPtr ? infoPtr->memory.hostAllocationPtr : nullptr;
}

void* VkEmulation::getBufferHostPointer(uint32_t bufferHandle) {
    if (getExternalMemoryMode() != ExternalMemory::Mode::HostAllocation) {
        GFXSTREAM_ERROR("%s: external memory mode doesn't use host pointers!", __func__);
        return nullptr;
    }
'''
if text.count(OLD) != 1:
    raise SystemExit(f'expected one accessor anchor, found {text.count(OLD)}')
text = text.replace(OLD, NEW)

# 3) Definition gains the explicit parameter (no shared mutable state).
OLD = '''bool VkEmulation::allocExternalMemory(VulkanDispatch* vk, VkEmulation::ExternalMemoryInfo* info,
                                      Optional<uint64_t> deviceAlignment,
                                      Optional<VkBuffer> bufferForDedicatedAllocation,
                                      Optional<VkImage> imageForDedicatedAllocation,
                                      Optional<ColorBufferInfo*> colorBufferInfo) {'''
NEW = '''bool VkEmulation::allocExternalMemory(VulkanDispatch* vk, VkEmulation::ExternalMemoryInfo* info,
                                      Optional<uint64_t> deviceAlignment,
                                      Optional<VkBuffer> bufferForDedicatedAllocation,
                                      Optional<VkImage> imageForDedicatedAllocation,
                                      Optional<ColorBufferInfo*> colorBufferInfo,
                                      bool hostPointerBacked) {'''
if text.count(OLD) != 1:
    raise SystemExit(f'expected one definition anchor, found {text.count(OLD)}')
text = text.replace(OLD, NEW)

# 4) Force the host-allocation branch for the classic buffer caller.
OLD = '''    switch (getExternalMemoryMode()) {
        // For host-allocation external memory mode, allocate host side memory first, then import
        case ExternalMemory::Mode::HostAllocation: {'''
NEW = '''    const bool bufferHostPointer = hostPointerBacked && supportsExternalMemoryHostProperties();
    switch (bufferHostPointer ? ExternalMemory::Mode::HostAllocation : getExternalMemoryMode()) {
        // For host-allocation external memory mode, allocate host side memory first, then import
        case ExternalMemory::Mode::HostAllocation: {'''
if text.count(OLD) != 1:
    raise SystemExit(f'expected one switch anchor, found {text.count(OLD)}')
text = text.replace(OLD, NEW)

# 5) Move the physical type to one the driver accepts host pointers in.
OLD = '''        // Update allocation size
        info->size = alignedSize;
        allocInfo.allocationSize = alignedSize;
        info->hostAllocationPtr = hostAllocation;'''
NEW = '''        // Not every driver type accepts host pointers; move to one that does.
        if (memoryHostPointerProperties.memoryTypeBits != 0 &&
            ((1u << allocInfo.memoryTypeIndex) & memoryHostPointerProperties.memoryTypeBits) == 0) {
            for (uint32_t i = 0; i <= 31; ++i) {
                if (memoryHostPointerProperties.memoryTypeBits & (1u << i)) {
                    GFXSTREAM_WARNING(
                        "BUF-HOSTPTR: memoryTypeIndex %u -> %u (bits=0x%x size=%llu)",
                        allocInfo.memoryTypeIndex, i, memoryHostPointerProperties.memoryTypeBits,
                        (unsigned long long)alignedSize);
                    allocInfo.memoryTypeIndex = i;
                    info->typeIndex = i;
                    break;
                }
            }
        }

        // Update allocation size
        info->size = alignedSize;
        allocInfo.allocationSize = alignedSize;
        info->hostAllocationPtr = hostAllocation;'''
if text.count(OLD) != 1:
    raise SystemExit(f'expected one host-allocation anchor, found {text.count(OLD)}')
text = text.replace(OLD, NEW)

# 6) setupVkBuffer opts in explicitly.
OLD = '    bool allocRes = allocExternalMemory(vk, &res.memory, deviceAlignment, dedicated_buffer);'
NEW = ('    bool allocRes = allocExternalMemory(\n'
       '        vk, &res.memory, deviceAlignment, dedicated_buffer, kNullopt, kNullopt,\n'
       '        gfxstream::base::getEnvironmentVariable("GFXSTREAM_BUFFER_HOST_PTR") == "1");')
if text.count(OLD) != 1:
    raise SystemExit(f'expected one buffer alloc anchor, found {text.count(OLD)}')
text = text.replace(OLD, NEW)
cpp.write_text(text, encoding='utf-8', newline='')

# 7) Guest buffer import: prefer the recorded host pointer over any fd/AHB handle.
d = root / 'host/vulkan/vk_decoder_global_state.cpp'
text = d.read_text(encoding='utf-8')
OLD = '''            if (opaqueFd && m_vkEmulation->supportsExternalMemoryImport()) {
                auto dupHandleInfo =
                    m_vkEmulation->dupBufferExtMemoryHandle(importBufferInfoPtr->buffer);'''
NEW = '''            if (opaqueFd) {
                if (void* bufferHostPtr =
                        m_vkEmulation->getBufferHostPointerAny(importBufferInfoPtr->buffer)) {
                    importHostInfo.pHostPointer = bufferHostPtr;
                    vk_append_struct(&structChainIter, &importHostInfo);
                    opaqueFd = false;
                }
            }

            if (opaqueFd && m_vkEmulation->supportsExternalMemoryImport()) {
                auto dupHandleInfo =
                    m_vkEmulation->dupBufferExtMemoryHandle(importBufferInfoPtr->buffer);'''
if text.count(OLD) != 1:
    raise SystemExit(f'expected one import anchor, found {text.count(OLD)}')
text = text.replace(OLD, NEW)
d.write_text(text, encoding='utf-8', newline='')

print('classic buffer memory forced to host-pointer import')
