#!/usr/bin/env python3
"""Source-only assertions run in Actions after applying the pinned patches."""
import sys
from pathlib import Path

source = Path(sys.argv[1])
scripts = Path(__file__).resolve().parent
decoder = (source / 'host/vulkan/vk_decoder_global_state.cpp').read_text()
internal = (source / 'host/vulkan/vk_common_operations.cpp').read_text()
helper = (scripts / 'host_memory_contract.h').read_text().replace('#pragma once\n', '')
assert decoder.count(helper) == 1
assert internal.count(helper) == 1
assert 'getEnvironmentVariable' not in helper
assert 'free(' not in helper and 'close(' not in helper and 'unmap' not in helper.replace('frees/unmaps', '')
start = decoder.index('    VkResult on_vkAllocateMemory(')
end = decoder.index('    void destroyMemoryWithExclusiveInfo(', start)
allocation = decoder[start:end]
driver = allocation.index('        VkResult result = vk->vkAllocateMemory(')
guard = allocation.index('        const auto* actualHostImport =')
assert allocation.count('validateHostPointerImport(') == 1
assert allocation.count('vk->vkGetMemoryHostPointerPropertiesEXT') == 1
assert 'vk_find_struct<VkImportMemoryHostPointerInfoEXT>(&localAllocInfo)' in allocation[guard:driver]
assert 'actualHostImport ? actualHostImport->pHostPointer : nullptr' in allocation[guard:driver]
assert 'device, actualHostImport, localAllocInfo.memoryTypeIndex,' in allocation[guard:driver]
assert '*pMemory = VK_NULL_HANDLE;' in allocation[guard:driver]
assert 'close(importFdInfo.fd);' in allocation[guard:driver]
assert 'rejectedPool->free(' in allocation[guard:driver]
assert 'sharedMemory/privateMemory release by RAII only' in allocation[guard:driver]
assert 'actualHostImport && (diagnosticFdInfo || poolOffset >= 0)' in allocation[guard:driver]
assert 'Detected memoryTypeIndex violation' not in decoder
assert 'Look for the first available supported memory index' not in decoder
assert 'localAllocInfo.memoryTypeIndex = i;' not in allocation
assert 'memoryHostPointerProperties' not in allocation
for producer in [
    'm_vkEmulation->getColorBufferHostPointer(',
    'm_vkEmulation->getBufferHostPointer(',
    'mappedPtr = memory.get();',
    'mappedPtr = privateMemory->getAddr();',
]:
    pos = allocation.index(producer)
    append = allocation.index('vk_append_struct(&structChainIter, &importHostInfo);', pos)
    assert pos < append < guard < driver
# The only pointer producers are the two aliases, SystemBlob, and PrivateMemory.
assert allocation.count('vk_append_struct(&structChainIter, &importHostInfo);') == 4
assert allocation.count('vk_append_struct(&structChainIter, &importFdInfo);') == 5
system = allocation[allocation.index('} else if (m_vkEmulation->getFeatures().SystemBlob.enabled())'):]
system = system[:system.index('sharedMemory = std::make_optional')]
assert 'importFdInfo.fd =' not in system and 'poolOffset =' not in system
private_start = allocation.index('} else if (m_vkEmulation->getFeatures().VulkanAllocateHostMemory.enabled()')
private_end = allocation.index('vk_append_struct(&structChainIter, &importHostInfo);', private_start)
private = allocation[private_start:private_end]
assert 'importFdInfo.fd =' not in private and 'poolOffset =' not in private
assert 'if (poolOffset < 0) {' in allocation
assert 'poolEligible =\n                    udmabufEnabled' in allocation
# Early unconditional preflight precedes synthetic reimport, aliases and all acquisitions.
coherent = allocation.index('const VkResult coherentContract = validateHostCoherentContract(')
assert coherent < allocation.index('dupGuestBlobResourceDescriptor(')
assert coherent < allocation.index('dupColorBufferExtMemoryHandle(')
assert coherent < allocation.index('dupBufferExtMemoryHandle(')
assert coherent < allocation.index('invalidateColorBuffer(')
assert coherent < allocation.index('"GFXSTREAM_EXPERIMENTAL_STAGING_CONTRACT"')
assert coherent < allocation.index('HostVisibleFolioCharge folioCharge;')
assert coherent < allocation.index('removeBlobDescriptorInfo(')
assert coherent < allocation.index('hvPool->alloc(')
assert coherent < allocation.index('memory.create(')
assert '*pMemory = VK_NULL_HANDLE;' in allocation[:coherent]
assert 'selectedGuestIndex);' in allocation[:coherent]
assert 'getHostMemoryInfoFromHostMemoryTypeIndex(selected->index)' in allocation[:coherent]
assert 'physical->memoryType.propertyFlags' in allocation[coherent:coherent + 300]
assert allocation.count('validateHostCoherentContract(') == 2
assert 'guestMemoryPropertyFlags, physicalHostMemory->memoryType.propertyFlags' in allocation
# Optional staging diagnostics remain unnegotiated and can never waive the guard.
assert 'staging::Readiness readiness{};' in allocation
assert 'readiness.callbacks =' not in allocation
assert 'memoryPropertyFlags |= VK_MEMORY_PROPERTY_HOST_COHERENT_BIT' not in allocation
alias = internal[internal.index('bool VkEmulation::importExternalMemory('):]
alias = alias[:alias.index('bool VkEmulation::getColorBufferShareInfo(')]
assert alias.index('*out = VK_NULL_HANDLE;') < alias.index('switch (mDeviceInfo.externalMemoryMode)')
assert 'vk_find_struct<VkImportMemoryHostPointerInfoEXT>(&allocInfo)' in alias
assert alias.index('validateHostPointerImport(') < alias.index('vk->vkAllocateMemory(')
assert 'if (res == VK_SUCCESS) {\n        res = vk->vkAllocateMemory(' in alias
host_alias = alias[alias.index('case ExternalMemory::Mode::HostAllocation:'):]
host_alias = host_alias[:host_alias.index('default:')]
assert 'importedFd =' not in host_alias and 'AHardwareBuffer_acquire' not in host_alias
assert 'if (res != VK_SUCCESS && importedFd >= 0) close(importedFd);' in alias
print('PASS: exact tested helper in both TUs; four final-chain pointer producers guarded once; old fallback absent')
print('PASS: unconditional physical coherent guard before fd/pool/backing acquisition; aliases and transformed selection checked; native coherent not disabled')
print('PASS: exclusive pointer/fd/pool producers asserted; defensive mixed-chain owned cleanup; borrowed aliases never freed')
