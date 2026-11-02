#!/usr/bin/env python3
"""Back classic Vulkan buffers with a guest-mappable host memfd.

Guest-visible VkBuffers (staging, readback, sync) are created by gfxstream's
classic buffer path, whose memory is allocated with the device-wide external
memory mode. That mode is AndroidAHB here, so buffers get an AHB whose
duplication into the guest's VkDeviceMemory fails (AHB is an image contract on
this driver and the buffer external info was disabled). The guest then sees
VK_ERROR_OUT_OF_DEVICE_MEMORY for every small host-visible buffer, which is the
allocation zink/readback and vkcube fail on.

Instead: create the VkBuffer with its external memory info (Mali requires it to
accept a host-pointer import, verified by the device probe), allocate a memfd,
import that mapping with VK_EXT_external_memory_host into the driver's
host-pointer type, and keep the memfd as the memory's external handle. The
guest reuses the same host pointer for its VkDeviceMemory and the memfd for its
blob mapping, so GPU writes land in the pages the guest reads back.

Gated by GFXSTREAM_BUFFER_HOST_PTR=1.
"""
import argparse
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('root', type=Path)
root = parser.parse_args().root.resolve()

# 1) Header: host allocation bookkeeping and the buffer accessor.
h = root / 'host/vulkan/vk_common_operations.h'
text = h.read_text(encoding='utf-8')
OLD = '    void* getBufferHostPointer(uint32_t bufferHandle);\n'
NEW = ('    void* getBufferHostPointer(uint32_t bufferHandle);\n'
       '    // Host allocation recorded for a buffer, whatever the device-wide external\n'
       '    // memory mode is. Used to re-import a classic buffer by host pointer.\n'
       '    void* getBufferHostPointerAny(uint32_t bufferHandle);\n')
if text.count(OLD) != 1:
    raise SystemExit(f'expected one header accessor anchor, found {text.count(OLD)}')
text = text.replace(OLD, NEW)

OLD = '        void* hostAllocationPtr = nullptr;\n'
NEW = ('        void* hostAllocationPtr = nullptr;\n'
       '        // True when hostAllocationPtr is an mmap of the handleInfo memfd rather\n'
       '        // than aligned_alloc memory; free with munmap instead of free.\n'
       '        bool hostAllocationMapped = false;\n')
if text.count(OLD) != 1:
    raise SystemExit(f'expected one header allocation anchor, found {text.count(OLD)}')
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

# 2) Includes for memfd/mmap.
if '#include <sys/mman.h>' not in text:
    OLD = '#include <unistd.h>\n'
    NEW = ('#include <unistd.h>\n'
           '#include <sys/mman.h>\n'
           '#ifndef MFD_CLOEXEC\n'
           '#define MFD_CLOEXEC 0x0001U\n'
           '#endif\n')
    if text.count(OLD) != 1:
        raise SystemExit(f'expected one unistd include, found {text.count(OLD)}')
    text = text.replace(OLD, NEW, 1)

# 3) Implementation next to the mode-gated accessor.
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

# 4) Definition gains the explicit parameter.
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

# 5) Force the host-allocation branch for the classic buffer caller.
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

# 6) Allocate a memfd (guest-mappable) for buffer host memory.
OLD = '''#ifdef _WIN32
        void* hostAllocation = _aligned_malloc(alignedSize, alignment);
#else
        void* hostAllocation = aligned_alloc(alignment, alignedSize);
#endif
        if (!hostAllocation) {
            GFXSTREAM_ERROR("%s: Could not allocated host memory with size %llu, alignment %llu",
                            __func__, alignedSize, alignment);
            return false;
        }'''
NEW = '''        void* hostAllocation = nullptr;
        if (hostPointerBacked) {
            // A memfd gives the guest a handle it can map; the host imports the same
            // mapping with VK_EXT_external_memory_host. The driver accepts host
            // pointer memory for an external VkBuffer even though the type is outside
            // the buffer's advertised memoryTypeBits (measured on Mali).
            int memfd = memfd_create("gfxstream-buffer-hostptr", MFD_CLOEXEC);
            if (memfd < 0 || ftruncate(memfd, (off_t)alignedSize) != 0) {
                if (memfd >= 0) close(memfd);
                GFXSTREAM_ERROR("%s: memfd for buffer host memory failed", __func__);
                return false;
            }
            hostAllocation =
                mmap(nullptr, alignedSize, PROT_READ | PROT_WRITE, MAP_SHARED, memfd, 0);
            if (hostAllocation == MAP_FAILED) {
                close(memfd);
                GFXSTREAM_ERROR("%s: mmap for buffer host memory failed", __func__);
                return false;
            }
            info->handleInfo = ExternalHandleInfo{
                .handle = static_cast<ExternalHandleType>(memfd),
                .streamHandleType = STREAM_HANDLE_TYPE_MEM_SHM,
            };
            info->hostAllocationMapped = true;
            GFXSTREAM_WARNING("BUF-HOSTPTR: memfd=%d size=%llu fd-map=%p", memfd,
                              (unsigned long long)alignedSize, hostAllocation);
        } else {
#ifdef _WIN32
            hostAllocation = _aligned_malloc(alignedSize, alignment);
#else
            hostAllocation = aligned_alloc(alignment, alignedSize);
#endif
        }
        if (!hostAllocation) {
            GFXSTREAM_ERROR("%s: Could not allocated host memory with size %llu, alignment %llu",
                            __func__, alignedSize, alignment);
            return false;
        }'''
if text.count(OLD) != 1:
    raise SystemExit(f'expected one host allocation anchor, found {text.count(OLD)}')
text = text.replace(OLD, NEW)

# 7) Move the physical type to one the driver accepts host pointers in.
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
    raise SystemExit(f'expected one host-allocation size anchor, found {text.count(OLD)}')
text = text.replace(OLD, NEW)

# 8) Free the mapping with munmap, the memfd via handleInfo.
OLD = '''    if (info->hostAllocationPtr) {
#ifdef _WIN32
        _aligned_free(info->hostAllocationPtr);
#else
        free(info->hostAllocationPtr);
#endif
        info->hostAllocationPtr = nullptr;
    }'''
NEW = '''    if (info->hostAllocationPtr) {
        if (info->hostAllocationMapped) {
#if !defined(_WIN32)
            munmap(info->hostAllocationPtr, info->size);
#endif
        } else {
#ifdef _WIN32
            _aligned_free(info->hostAllocationPtr);
#else
            free(info->hostAllocationPtr);
#endif
        }
        info->hostAllocationPtr = nullptr;
        info->hostAllocationMapped = false;
    }'''
if text.count(OLD) != 1:
    raise SystemExit(f'expected one free anchor, found {text.count(OLD)}')
text = text.replace(OLD, NEW)

# 9) setupVkBuffer opts in explicitly.
OLD = '    bool allocRes = allocExternalMemory(vk, &res.memory, deviceAlignment, dedicated_buffer);'
NEW = ('    bool allocRes = allocExternalMemory(\n'
       '        vk, &res.memory, deviceAlignment, dedicated_buffer, kNullopt, kNullopt,\n'
       '        gfxstream::base::getEnvironmentVariable("GFXSTREAM_BUFFER_HOST_PTR") == "1");')
if text.count(OLD) != 1:
    raise SystemExit(f'expected one buffer alloc anchor, found {text.count(OLD)}')
text = text.replace(OLD, NEW)
cpp.write_text(text, encoding='utf-8', newline='')

# 10) Keep the external buffer info attached in host-pointer mode (Mali needs it
#     to accept the host-pointer import), while AHB export stays disabled.
text = cpp.read_text(encoding='utf-8')
OLD = '''        if (!cibNoExternalBuffer && !cibAhbMode) {
            extBufferCi.handleTypes =
                static_cast<VkExternalMemoryHandleTypeFlags>(getDefaultExternalMemoryHandleType());
            extBufferCiPtr = &extBufferCi;
        }'''
NEW = '''        const bool bufferHostPtrMode =
            gfxstream::base::getEnvironmentVariable("GFXSTREAM_BUFFER_HOST_PTR") == "1";
        if (!cibNoExternalBuffer && (!cibAhbMode || bufferHostPtrMode)) {
            extBufferCi.handleTypes =
                static_cast<VkExternalMemoryHandleTypeFlags>(getDefaultExternalMemoryHandleType());
            extBufferCiPtr = &extBufferCi;
        }'''
if text.count(OLD) != 1:
    raise SystemExit(f'expected one external-buffer-info anchor, found {text.count(OLD)}')
text = text.replace(OLD, NEW)
cpp.write_text(text, encoding='utf-8', newline='')

# 11) Enable VK_EXT_external_memory_host on the emulation device. Without it the
#     dispatch entry for vkGetMemoryHostPointerPropertiesEXT is null (observed as a
#     pc=0 crash inside allocExternalMemory) and host-pointer imports are illegal.
text = cpp.read_text(encoding='utf-8')
OLD = '''#if defined(__linux__)
    if (emulation->mDeviceInfo.supportsDmaBuf) {
        selectedDeviceExtensionNames.emplace(VK_EXT_EXTERNAL_MEMORY_DMA_BUF_EXTENSION_NAME);
    }
#endif
'''
NEW = OLD + '''
    if (emulation->mDeviceInfo.supportsExternalMemoryHostProps) {
        selectedDeviceExtensionNames.emplace(VK_EXT_EXTERNAL_MEMORY_HOST_EXTENSION_NAME);
    }
'''
if text.count(OLD) != 1:
    raise SystemExit(f'expected one device extension anchor, found {text.count(OLD)}')
text = text.replace(OLD, NEW)
cpp.write_text(text, encoding='utf-8', newline='')

# 12) Guest buffer import: prefer the recorded host pointer over any fd/AHB handle.
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

print('classic buffers: memfd host-pointer memory with guest-mappable handle')
