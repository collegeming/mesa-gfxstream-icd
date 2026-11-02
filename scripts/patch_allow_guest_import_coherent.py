#!/usr/bin/env python3
"""Allow a guest-owned import to keep the guest's HOST_COHERENT request.

The fail-closed coherent contract rejects any allocation whose guest type advertises
HOST_COHERENT while the mapped physical host type does not. That is right for memory the
host allocates: the guest would trust a guarantee nobody provides. An import of the guest's
own buffer is different -- the pages are the guest's, the guest's gfxstream flush/invalidate
paths are the coherence contract, and the alternative is refusing every dma-buf image import
outright. Keep the reject for host-allocated memory; exempt only the alias/fd import chains.
"""
import argparse
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('root', type=Path)
root = parser.parse_args().root.resolve()

path = root / 'host/vulkan/vk_decoder_global_state.cpp'
text = path.read_text()

OLD = '''            const VkResult coherentContract = validateHostCoherentContract(
                guest.memoryTypes[pAllocateInfo->memoryTypeIndex].propertyFlags,
                physical->memoryType.propertyFlags);
'''
NEW = '''            // A guest-owned import (ColorBuffer/Buffer alias or an explicit fd import) is
            // coherent on the guest's terms: its pages are the guest's, and the guest's
            // flush/invalidate paths own the ordering. Host-allocated memory keeps the reject.
            const bool guestOwnedImport = preflightCb != nullptr || preflightBuffer != nullptr ||
                vk_find_struct<VkImportMemoryFdInfoKHR>(pAllocateInfo) != nullptr;
            if (guestOwnedImport) {
                static std::atomic<unsigned> coherentImportLogs{0};
                if (coherentImportLogs.fetch_add(1, std::memory_order_relaxed) < 8) {
                    GFXSTREAM_WARNING("COHERENT-DOWNGRADE-ALLOW-IMPORT: guestIndex=%u hostIndex=%u "
                                      "guestFlags=0x%x physicalFlags=0x%x (guest-owned pages)",
                                      pAllocateInfo->memoryTypeIndex, physical->index,
                                      guest.memoryTypes[pAllocateInfo->memoryTypeIndex].propertyFlags,
                                      physical->memoryType.propertyFlags);
                }
            }
            const VkResult coherentContract = guestOwnedImport ? VK_SUCCESS :
                validateHostCoherentContract(
                    guest.memoryTypes[pAllocateInfo->memoryTypeIndex].propertyFlags,
                    physical->memoryType.propertyFlags);
'''
if text.count(OLD) != 1:
    raise SystemExit(f'expected one coherent-contract anchor, found {text.count(OLD)}')
text = text.replace(OLD, NEW)

if '#include <atomic>' not in text:
    anchor = '#include <cstdint>'
    if text.count(anchor) != 1:
        raise SystemExit(f'expected one cstdint include, found {text.count(anchor)}')
    text = text.replace(anchor, '#include <atomic>\n#include <cstdint>', 1)

path.write_text(text, encoding='utf-8', newline='')
print('guest-owned imports exempt from the coherent downgrade reject')
