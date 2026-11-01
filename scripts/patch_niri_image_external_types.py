#!/usr/bin/env python3
"""Preserve explicit external image handle types across query and create."""
import argparse
from pathlib import Path
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('root', type=Path)
root = parser.parse_args().root.resolve()
path = root / 'host/vulkan/vk_decoder_global_state.cpp'
text = path.read_text()
query = '''        if (extImageFormatInfo &&
            extImageFormatInfo->handleType == VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT) {
            const_cast<VkPhysicalDeviceExternalImageFormatInfo*>(extImageFormatInfo)->handleType =
                m_vkEmulation->getDefaultExternalMemoryHandleType();
        }
'''
create = '''            if (pExternalMemoryImageCi && pExternalMemoryImageCi->handleTypes &
                                              VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT) {
                pExternalMemoryImageCi->handleTypes |=
                    m_vkEmulation->getDefaultExternalMemoryHandleType();
            }
'''
for old, replacement in [
    (query, '''        // Explicit DMA_BUF capability queries must query DMA_BUF, not the
        // platform-default AHB type. The existing external-handle codec handles
        // opaque/platform aliases separately; do not change explicit fd semantics.
        (void)extImageFormatInfo;
'''),
    (create, '''            // Preserve explicit DMA_BUF creation exactly. Adding the platform
            // default AHB bit here changes the external-memory contract and was
            // inconsistent with the already-computed importAndroidHardwareBuffer.
'''),
]:
    if text.count(old) != 1: raise SystemExit(f'Expected one external image anchor, found {text.count(old)}')
    text = text.replace(old, replacement)
path.write_text(text)
print('Preserved explicit DMA_BUF image query/create types; opaque/AHB behavior and allocation guards unchanged')
