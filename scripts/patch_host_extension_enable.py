#!/usr/bin/env python3
"""Enable VK_EXT_external_memory_host on the internal emulation device only.

SystemBlob host-pointer imports need the extension enabled on the device that
gfxstream creates for its own Vulkan work. Without it the
vkGetMemoryHostPointerPropertiesEXT dispatch entry is null and the import is
illegal. This is the extension-enable half of patch_buffer_host_pointer.py,
extracted so the systemblob_only build does not need the buffer rewrite.

Guarded by the same anchor check as the buffer patch: if the block is already
present this fails rather than appending a duplicate.
"""
import argparse
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('root', type=Path)
root = parser.parse_args().root.resolve()

cpp = root / 'host/vulkan/vk_common_operations.cpp'
text = cpp.read_text(encoding='utf-8')

patched_block = '''#if defined(__linux__)
    if (emulation->mDeviceInfo.supportsDmaBuf) {
        selectedDeviceExtensionNames.emplace(VK_EXT_EXTERNAL_MEMORY_DMA_BUF_EXTENSION_NAME);
        if (vk_util::extensionSupported(emulation->mDeviceInfo.extensions,
                                        VK_KHR_EXTERNAL_MEMORY_FD_EXTENSION_NAME)) {
            selectedDeviceExtensionNames.emplace(VK_KHR_EXTERNAL_MEMORY_FD_EXTENSION_NAME);
        }
    }
#endif
'''
plain_block = '''#if defined(__linux__)
    if (emulation->mDeviceInfo.supportsDmaBuf) {
        selectedDeviceExtensionNames.emplace(VK_EXT_EXTERNAL_MEMORY_DMA_BUF_EXTENSION_NAME);
    }
#endif
'''
host_block = '''
    if (emulation->mDeviceInfo.supportsExternalMemoryHostProps) {
        selectedDeviceExtensionNames.emplace(VK_EXT_EXTERNAL_MEMORY_HOST_EXTENSION_NAME);
    }
'''
if host_block in text:
    raise SystemExit('VK_EXT_external_memory_host is already enabled')
if text.count(patched_block) == 1:
    text = text.replace(patched_block, patched_block + host_block, 1)
elif text.count(plain_block) == 1:
    text = text.replace(plain_block, plain_block + host_block, 1)
else:
    raise SystemExit('expected one device extension anchor (patched or plain)')
cpp.write_text(text, encoding='utf-8', newline='')
print('host-pointer extension enable patched')
