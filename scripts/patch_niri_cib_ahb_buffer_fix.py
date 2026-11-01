#!/usr/bin/env python3
"""Skip AHB external-memory attachment/export for plain VkBuffers; images unchanged."""
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

ops = 'host/vulkan/vk_common_operations.cpp'
edit(ops, '''    void* extBufferCiPtr = nullptr;
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
    }''', '''    void* extBufferCiPtr = nullptr;
    const bool cibNoExternalBuffer =
        gfxstream::base::getEnvironmentVariable("GFXSTREAM_NIRI_BUFFER_NO_EXTERNAL") == "1";
    const bool cibAhbMode = cibExternalModeKnown &&
        getExternalMemoryMode() == ExternalMemory::Mode::AndroidAHB;
    if (mDeviceInfo.supportsExternalMemoryImport || mDeviceInfo.supportsExternalMemoryExport) {
        // AHB external memory is an image/ColorBuffer contract, not a VkBuffer contract.
        if (cibAhbMode && cibVkBufDiag) {
            static std::atomic<uint32_t> ahbDiagnostics{0};
            if (ahbDiagnostics.fetch_add(1, std::memory_order_relaxed) < 16) {
                GFXSTREAM_INFO("CIB1-DIAG-BUF-VK-AHB-NO-EXTERNAL: handle=%u", bufferHandle);
            }
        }
        if (cibNoExternalBuffer && cibVkBufDiag) {
            static std::atomic<uint32_t> disabledDiagnostics{0};
            if (disabledDiagnostics.fetch_add(1, std::memory_order_relaxed) < 16) {
                GFXSTREAM_INFO("CIB1-DIAG-BUF-VK-EXTERNAL-DISABLED: handle=%u "
                               "default-handle=0x%x",
                               bufferHandle, (unsigned)getDefaultExternalMemoryHandleType());
            }
        }
        if (!cibNoExternalBuffer && !cibAhbMode) {
            extBufferCi.handleTypes =
                static_cast<VkExternalMemoryHandleTypeFlags>(getDefaultExternalMemoryHandleType());
            extBufferCiPtr = &extBufferCi;
        }
    }''')

edit(ops, '''        case ExternalMemory::Mode::AndroidAHB: {
#ifdef __ANDROID__
            // If the AHB was already allocated and imported (via AHardwareBuffer_allocate
            // in the pre-allocation switch above), handleInfo is already set. Skip export.
            if (info->handleInfo &&''', '''        case ExternalMemory::Mode::AndroidAHB: {
#ifdef __ANDROID__
            // AHB external memory is an image/ColorBuffer contract. Plain VkBuffers cannot
            // export it; vkGetMemoryAndroidHardwareBufferANDROID dereferences image-only
            // state on some drivers. Skip export, leave handleInfo unset, and keep the
            // allocation successful for the buffer path.
            if (!colorBufferInfo) {
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
            // If the AHB was already allocated and imported (via AHardwareBuffer_allocate
            // in the pre-allocation switch above), handleInfo is already set. Skip export.
            if (info->handleInfo &&''')
print('Skipped AHB external buffer create/export for plain VkBuffers; image/ColorBuffer path retained')
