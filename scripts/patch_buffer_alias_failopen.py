#!/usr/bin/env python3
"""Allocate a plain memory when a classic buffer's backing cannot be aliased.

VkImportBufferGOOGLE asks the host to return memory that aliases the buffer
resource's backing. On this driver that is impossible for a plain VkBuffer:
DMA_BUF export returns success with no fd (measured: features importable only,
exportable=0), and AHB export dereferences image-only state and crashes Mali.
The guest then sees VK_ERROR_OUT_OF_DEVICE_MEMORY and zink cannot create a
swapchain (observed on the 4096-byte swapchain buffer).

Instead of failing the allocation, allocate a normal host-visible memory at the
buffer's own host type (so vkBindBufferMemory accepts it) and let the guest bind
its VkBuffer to that. The buffer resource's own backing is left untouched; the
guest's mapping and the GPU then share this allocation, and the existing
pre-submit flush / completion invalidate track the buffer range. Logged, bounded.
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

# 1) The fallback flag must be declared before the import branch that sets it.
edit(d, '''        void* mappedPtr = nullptr;''',
     '''        void* mappedPtr = nullptr;
        // Set when a classic buffer's backing cannot be aliased and the guest gets a fresh
        // allocation at the buffer's own host type instead.
        bool bufferAliasPlainFallback = false;''')

# 2) Do not fail the allocation when the buffer has no exportable handle.
edit(d, '''                if (!dupHandleInfo) {
                    GFXSTREAM_ERROR(
                        "Failed to duplicate external memory handle/descriptor for Buffer object, "
                        "with internal handle: %d",
                        importBufferInfoPtr->buffer);
                    return VK_ERROR_OUT_OF_DEVICE_MEMORY;
                }

#if defined(_WIN32)''',
     '''                if (!dupHandleInfo) {
                    // This driver cannot export a plain VkBuffer's memory: DMA_BUF export
                    // reports success but returns no fd, and AHB export dereferences
                    // image-only state. A hard failure here breaks the guest's swapchain
                    // allocation, so allocate fresh memory at the buffer's own host type
                    // instead. The guest binds its VkBuffer to this allocation, which is what
                    // the GPU reads and the guest maps; the resource's own backing is unused.
                    static std::atomic<uint32_t> sAliasFallbackLogs{0};
                    const uint32_t n =
                        sAliasFallbackLogs.fetch_add(1, std::memory_order_relaxed);
                    if (n < 32) {
                        GFXSTREAM_WARNING(
                            "BUF-ALIAS-FALLBACK: buffer=%u size=%llu type=%u; allocating fresh "
                            "memory at the buffer's host type instead of failing",
                            importBufferInfoPtr->buffer,
                            (unsigned long long)localAllocInfo.allocationSize,
                            localAllocInfo.memoryTypeIndex);
                    }
                    bufferAliasPlainFallback = true;
                    opaqueFd = false;
                } else {
#if defined(_WIN32)''')

# 3) Close the new else around the existing dup handling.
edit(d, '''                if (!appendExternalMemoryImport(*dupHandleInfo)) {
                    GFXSTREAM_ERROR("Unsupported Buffer external memory handle type: %u",
                                    dupHandleInfo->streamHandleType);
                    return VK_ERROR_OUT_OF_DEVICE_MEMORY;
                }
#endif
            }''',
     '''                if (!appendExternalMemoryImport(*dupHandleInfo)) {
                    GFXSTREAM_ERROR("Unsupported Buffer external memory handle type: %u",
                                    dupHandleInfo->streamHandleType);
                    return VK_ERROR_OUT_OF_DEVICE_MEMORY;
                }
#endif
                }
            }''')

# 4) The allocation type: the guest's mapped shadow type normally, the buffer's own host
#    type for the fallback (the buffer's memoryTypeBits must contain it or the bind fails).
edit(d, '''            localAllocInfo.memoryTypeIndex = hostMemoryInfo.index;
            memoryPropertyFlags = hostMemoryInfo.memoryType.propertyFlags;''',
     '''            localAllocInfo.memoryTypeIndex =
                bufferAliasPlainFallback ? aliasHostIndex : hostMemoryInfo.index;
            memoryPropertyFlags = bufferAliasPlainFallback
                ? physicalHostMemory->memoryType.propertyFlags
                : hostMemoryInfo.memoryType.propertyFlags;''')

print('buffer alias fallback: fresh allocation at the buffer host type when no handle is exportable')
