#!/usr/bin/env python3
"""Pick a host-pointer-importable memory type for SystemBlob allocations.

SystemBlob backs guest host-visible memory with a host memfd mapping and imports
that mapping with VK_EXT_external_memory_host. The physical memory type is chosen
from the guest-visible type before the import, but drivers need not accept host
pointers in every (or even their first) type: this Mali driver advertises
VK_EXT_external_memory_host and accepts image/buffer host-pointer imports only in
a subset of its types, returning VK_ERROR_INVALID_EXTERNAL_HANDLE for the rest.
The VulkanAllocateHostMemory branch already re-selects a supported type from
vkGetMemoryHostPointerPropertiesEXT; do the same here so SystemBlob is not
rejected for a type the driver never said it supports.
"""
import argparse
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('root', type=Path)
root = parser.parse_args().root.resolve()

path = root / 'host/vulkan/vk_decoder_global_state.cpp'
text = path.read_text(encoding='utf-8')

OLD = '''                    importHostInfo.pHostPointer = mappedPtr;
                    vk_append_struct(&structChainIter, &importHostInfo);
'''
NEW = '''                    importHostInfo.pHostPointer = mappedPtr;

                    // Not every driver memory type accepts host pointers, and the type
                    // picked for the guest need not be one of them. Ask before importing
                    // and move the physical index when the guest-derived one is rejected;
                    // without this the import fails with VK_ERROR_INVALID_EXTERNAL_HANDLE
                    // on drivers that support the extension (observed on Mali).
                    if (m_vkEmulation->supportsExternalMemoryHostProperties()) {
                        VkMemoryHostPointerPropertiesEXT hostPointerProps = {
                            VK_STRUCTURE_TYPE_MEMORY_HOST_POINTER_PROPERTIES_EXT, nullptr, 0};
                        vk->vkGetMemoryHostPointerPropertiesEXT(
                            device, VK_EXTERNAL_MEMORY_HANDLE_TYPE_HOST_ALLOCATION_BIT_EXT,
                            mappedPtr, &hostPointerProps);
                        if (hostPointerProps.memoryTypeBits == 0) {
                            GFXSTREAM_ERROR(
                                "SystemBlob: driver accepts no host-pointer memory type for %p",
                                mappedPtr);
                            return VK_ERROR_INCOMPATIBLE_DRIVER;
                        }
                        if (((1u << localAllocInfo.memoryTypeIndex) &
                             hostPointerProps.memoryTypeBits) == 0) {
                            const uint32_t requestedType = localAllocInfo.memoryTypeIndex;
                            for (uint32_t i = 0; i <= 31; ++i) {
                                if (hostPointerProps.memoryTypeBits & (1u << i)) {
                                    localAllocInfo.memoryTypeIndex = i;
                                    break;
                                }
                            }
                            static std::atomic<unsigned> sSystemBlobTypeSwitches{0};
                            if (sSystemBlobTypeSwitches.fetch_add(1) < 8) {
                                GFXSTREAM_WARNING(
                                    "SYSBLOB-HOSTPTR: memoryTypeIndex %u -> %u bits=0x%x "
                                    "size=%llu",
                                    requestedType, localAllocInfo.memoryTypeIndex,
                                    hostPointerProps.memoryTypeBits,
                                    (unsigned long long)localAllocInfo.allocationSize);
                            }
                        }
                    }

                    vk_append_struct(&structChainIter, &importHostInfo);
'''
if text.count(OLD) != 1:
    raise SystemExit(f'expected one SystemBlob host-pointer anchor, found {text.count(OLD)}')
text = text.replace(OLD, NEW)
path.write_text(text, encoding='utf-8', newline='')
print('SystemBlob host-pointer memory type selection patched')
