#!/usr/bin/env python3
"""Export classic VkBuffer memory as a dma-buf so VkImportBufferGOOGLE can alias it.

The AHB skip made classic buffer allocations succeed, but the guest's
VkImportBufferGOOGLE then has no handle to duplicate: vkAllocateMemory returns
VK_ERROR_OUT_OF_DEVICE_MEMORY and zink reports "could not create swapchain"
(observed on noctalia's 4096-byte buffer, requirements bits 0x4100).

AHB is an image contract on this driver, but dma-buf is not. The device probe
imported a 4096-byte dma-buf into a buffer with these exact requirements at type
14 and GPU-filled it, so export the classic buffer's memory as a dma-buf and let
the existing dup/import path carry the same pages to the guest.

No memory type or flag is rewritten here: the buffer keeps its type and the
guest allocation keeps its own type. Only the external handle kind changes.
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


ops = 'host/vulkan/vk_common_operations.cpp'

# 1) The VkBuffer must be created for the fd handle type the memory will export.
edit(ops, '''        if (!cibNoExternalBuffer && !cibAhbMode) {
            extBufferCi.handleTypes =
                static_cast<VkExternalMemoryHandleTypeFlags>(getDefaultExternalMemoryHandleType());
            extBufferCiPtr = &extBufferCi;
        }
''', '''        if (!cibNoExternalBuffer && !cibAhbMode) {
            extBufferCi.handleTypes =
                static_cast<VkExternalMemoryHandleTypeFlags>(getDefaultExternalMemoryHandleType());
            extBufferCiPtr = &extBufferCi;
        }
        // AHB cannot leave this driver through a VkBuffer's memory, but a dma-buf can: the
        // device probe imported a 4096-byte dma-buf into a buffer with requirements bits
        // 0x4100 at type 14. Declare the fd handle type that allocExternalMemory actually
        // exports, so the guest's VkImportBufferGOOGLE has a handle to duplicate. The memfd
        // host-pointer experiment owns the buffer in its own mode and is left alone.
        if (cibAhbMode && !cibNoExternalBuffer && mDeviceInfo.supportsDmaBuf &&
            gfxstream::base::getEnvironmentVariable("GFXSTREAM_BUFFER_HOST_PTR") != "1") {
            extBufferCi.handleTypes = VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT;
            extBufferCiPtr = &extBufferCi;
        }
''')

# 2) The allocation must be created exportable for the same handle type.
edit(ops, '''        if (mDeviceInfo.supportsDmaBuf) {
            exportAi.handleTypes |= VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT;
        }

        vk_append_struct(&allocInfoChain, &exportAi);
    }
''', '''        if (mDeviceInfo.supportsDmaBuf) {
            exportAi.handleTypes |= VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT;
        }

        vk_append_struct(&allocInfoChain, &exportAi);
    }

    // Classic VkBuffers in AHB mode: the AHB export below is skipped for them, so the
    // allocation has to declare the dma-buf export it performs instead. Without this the
    // memory is not exportable, vkGetMemoryFdKHR cannot return a handle, and the guest's
    // VkImportBufferGOOGLE alias fails with VK_ERROR_OUT_OF_DEVICE_MEMORY.
    if (getExternalMemoryMode() == ExternalMemory::Mode::AndroidAHB &&
        !colorBufferInfo.hasValue() && mDeviceInfo.supportsDmaBuf) {
        exportAi.handleTypes = VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT;
        vk_append_struct(&allocInfoChain, &exportAi);
    }
''')

# 3) Export the dma-buf in place of the skipped AHB export. The ColorBuffer/image AHB
#    path below is untouched, and a failed export keeps the old behavior (allocation
#    still succeeds; only the guest alias fails).
edit(ops, '''            if (!colorBufferInfo) {
                if (gfxstream::base::getEnvironmentVariable("GFXSTREAM_NIRI_IMAGE_TRACE") == "1") {
                    static std::atomic<uint32_t> ahbSkipDiagnostics{0};
                    if (ahbSkipDiagnostics.fetch_add(1, std::memory_order_relaxed) < 16) {
                        GFXSTREAM_INFO("CIB1-DIAG-BUF-VK-AHB-EXPORT-SKIPPED: size=%llu "
                                       "memory=%llu color-buffer=0",
                                       (unsigned long long)info->size,
                                       (unsigned long long)(uintptr_t)info->memory);
                    }
                }
                return true;
            }
''', '''            if (!colorBufferInfo) {
                // The guest's VkImportBufferGOOGLE duplicates this memory's handle. AHB
                // cannot produce one for a buffer on this driver, but a dma-buf can, and
                // the device probe proved the guest-visible import at the same shape.
                if (mDeviceInfo.supportsDmaBuf) {
                    VkMemoryGetFdInfoKHR getFdInfo = {
                        VK_STRUCTURE_TYPE_MEMORY_GET_FD_INFO_KHR,
                        nullptr,
                        info->memory,
                        VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT,
                    };
                    int exportFd = -1;
                    exportRes = vk->vkGetMemoryFdKHR(mDevice, &getFdInfo, &exportFd);
                    validHandle = (VK_SUCCESS == exportRes) && (-1 != exportFd);
                    if (validHandle) {
                        info->handleInfo = ExternalHandleInfo{
                            .handle = static_cast<ExternalHandleType>(exportFd),
                            .streamHandleType = STREAM_HANDLE_TYPE_MEM_DMABUF,
                        };
                    }
                }
                if (gfxstream::base::getEnvironmentVariable("GFXSTREAM_NIRI_IMAGE_TRACE") == "1") {
                    static std::atomic<uint32_t> ahbSkipDiagnostics{0};
                    if (ahbSkipDiagnostics.fetch_add(1, std::memory_order_relaxed) < 16) {
                        GFXSTREAM_INFO("CIB1-DIAG-BUF-VK-DMABUF-EXPORT: size=%llu memory=%llu "
                                       "result=%d handle=%d color-buffer=0",
                                       (unsigned long long)info->size,
                                       (unsigned long long)(uintptr_t)info->memory,
                                       (int)exportRes, validHandle ? (int)exportFd : -1);
                    }
                }
                return true;
            }
''')

print('classic buffers: dma-buf export for AHB mode, alias import path unchanged')
