#!/usr/bin/env python3
"""Source-only assertions run in Actions after applying the pinned patches."""
import sys
from pathlib import Path

source = Path(sys.argv[1])
scripts = Path(__file__).resolve().parent
decoder = (source / 'host/vulkan/vk_decoder_global_state.cpp').read_text(encoding='utf-8')
internal = (source / 'host/vulkan/vk_common_operations.cpp').read_text(encoding='utf-8')
structs = (source / 'host/vulkan/vk_decoder_internal_structs.h').read_text(encoding='utf-8')
helper = (scripts / 'host_memory_contract.h').read_text(encoding='utf-8').replace('#pragma once\n', '')
assert decoder.count(helper) == 1
assert internal.count(helper) == 1
assert 'getEnvironmentVariable' not in helper
assert 'free(' not in helper and 'close(' not in helper and 'unmap' not in helper.replace('frees/unmaps', '')
# The waiver is a named predicate with three inputs, not an inline flag test.
assert 'enum class EmulatedAllocationShape' in helper
assert 'inline bool emulationCoversThisAllocation(' in helper
assert 'inline VkResult validateHostCoherentContractEmulated(' in helper
assert 'VK_MEMORY_PROPERTY_HOST_CACHED_BIT' in helper
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
coherent = allocation.index('const VkResult coherentContract = validateHostCoherentContractEmulated(')
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
# Alias host-index regression: the ColorBuffer/Buffer stored index is a HOST index and is never
# fed back into the guest index space, while the guest flags keep the guest's own index.
assert 'getHostMemoryInfoFromGuestMemoryTypeIndex(aliasHostIndex)' not in allocation
assert 'getHostMemoryInfoFromGuestMemoryTypeIndex(\n                aliasHostIndex)' not in allocation
assert '? helper.getHostMemoryInfoFromHostMemoryTypeIndex(aliasHostIndex)' in allocation
assert ': helper.getHostMemoryInfoFromGuestMemoryTypeIndex(guestIndex);' in allocation
assert 'hasAliasHostIndex ? aliasHostIndex : hostMemoryInfo.index' in allocation
assert 'getHostMemoryInfoFromHostMemoryTypeIndex(selected->index)' in allocation[:coherent]
assert 'guest.memoryTypes[guestIndex].propertyFlags' in allocation[:coherent]
assert 'selectedGuestIndex' not in allocation
# Both checks go through the coverage-aware contract, and the coverage inputs are the physical
# flags plus a shape the decoder tracks.
assert allocation.count('validateHostCoherentContractEmulated(') == 2
assert allocation.count('emulationCoversThisAllocation(') == 2
assert allocation.count('VulkanDisableCoherentMemoryAndEmulate.enabled()') == 2
assert 'physical->memoryType.propertyFlags, emulatedShape)' in allocation
assert 'physicalHostMemory->memoryType.propertyFlags, emulatedShape)' in allocation
assert 'emulatedShape = EmulatedAllocationShape::kImageRange;' in allocation
assert 'emulatedShape = EmulatedAllocationShape::kBufferRange;' in allocation
assert 'EmulatedAllocationShape emulatedShape = EmulatedAllocationShape::kNone;' in allocation
assert 'vk_find_struct<VkMemoryDedicatedAllocateInfo>(pAllocateInfo)' in allocation
# The waiver never rewrites the request: no coherent bit is added and no index is replaced.
assert 'memoryPropertyFlags |= VK_MEMORY_PROPERTY_HOST_COHERENT_BIT' not in allocation
assert 'memoryType.propertyFlags |= VK_MEMORY_PROPERTY_HOST_COHERENT_BIT' not in allocation
assert 'memoryType.propertyFlags &=' not in allocation
assert 'localAllocInfo.memoryTypeIndex = aliasHostIndex' not in allocation
assert 'guestMemoryPropertyFlags, physicalHostMemory->memoryType.propertyFlags' in allocation
# Optional staging diagnostics remain unnegotiated and can never waive the guard.
assert 'staging::Readiness readiness{};' in allocation
assert 'readiness.callbacks =' not in allocation
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
# Image ranges are tracked next to buffer ranges, in both bind entry points and on destroy.
assert 'std::unordered_map<VkBuffer, BoundMemoryRange> bufferMemoryRanges;' in structs
assert 'std::unordered_map<VkImage, BoundMemoryRange> imageMemoryRanges;' in structs
assert 'void* importedHostPointer = nullptr;' in structs
assert 'VkDeviceSize importedHostPointerSize = 0;' in structs
assert decoder.count('bindToImageLocked(') == 3  # definition + both bind paths
assert decoder.count('unbindFromImageLocked(') == 2  # definition + destroyImageLocked
assert 'bindToImageLocked(memoryInfo, image, memoryOffset, VK_WHOLE_SIZE);' in decoder
assert 'bindToImageLocked(memoryInfo, pBindInfos[i].image,' in decoder
assert 'unbindFromImageLocked(memoryInfo, image);' in decoder
# Range bookkeeping is unconditional: the host-pointer path needs the extents with the coherent
# feature switched off, which is the configuration the device runs.
for bind_site in [
    'bindToBufferLocked(memoryInfo, buffer, memoryOffset, bufferInfo->size);',
    'unbindFromBufferLocked(memoryInfo, buffer);',
    'bindToImageLocked(memoryInfo, image, memoryOffset, VK_WHOLE_SIZE);',
]:
    assert bind_site in decoder
assert 'if (m_vkEmulation->getFeatures().VulkanDisableCoherentMemoryAndEmulate.enabled()) {\n            bindToBufferLocked(' not in decoder
assert 'if (m_vkEmulation->getFeatures().VulkanDisableCoherentMemoryAndEmulate.enabled()) {\n            bindToImageLocked(' not in decoder
# The host-pointer import is recorded from the final chain, so SystemBlob, both aliases and the
# private allocation are all covered by the same field.
assert 'if (actualHostImport && actualHostImport->pHostPointer) {' in decoder
assert 'memoryInfo.importedHostPointer = actualHostImport->pHostPointer;' in decoder
assert 'memoryInfo.importedHostPointerSize = localAllocInfo.allocationSize;' in decoder
assert (allocation.index('memoryInfo.memoryIndex = localAllocInfo.memoryTypeIndex;') <
        allocation.index('memoryInfo.importedHostPointer = actualHostImport->pHostPointer;'))
# The cache-maintenance helper is embedded exactly once and drives the CPU caches directly.
cache_helper = (scripts / 'host_pointer_cache.h').read_text(encoding='utf-8').replace('#pragma once\n', '')
assert decoder.count(cache_helper) == 1
assert 'host_pointer_cache::invalidate(range.base, range.size);' in decoder
assert 'dc civac' in cache_helper and 'dc cvac' in cache_helper
assert 'mrs %0, ctr_el0' in cache_helper
# The helper never issues a Vulkan mapped-range call: these pages are not mapped by this host.
assert 'vk->vkFlushMappedMemoryRanges' not in cache_helper
assert 'vk->vkInvalidateMappedMemoryRanges' not in cache_helper
assert '__builtin___clear_cache' in cache_helper  # Portable fallback for non-AArch64 hosts.
# Only memory this host mapped itself may be driven through the Vulkan mapped-range calls.
assert 'if (!info.needUnmap || !info.ptr) continue;' in decoder
assert 'TODO(b/424729656): this logic should run only for emulated memory' not in decoder
assert 'TODO(b/424729656): Invalidate might be necessary around a fence.' not in decoder
flush_collect = decoder[decoder.index('    std::vector<VkMappedMemoryRange> collectVulkanFlushRangesLocked('):]
flush_collect = flush_collect[:flush_collect.index('    // Appends the maintenance owed for one memory.')]
assert 'importedHostPointer' not in flush_collect
assert 'if (!info.needUnmap || !info.ptr) continue;' in flush_collect
# Completion records are fence-keyed and bounded, and the invalidate never runs under mMutex.
assert decoder.count('recordEmulatedCompletion(') == 2  # definition + submit
assert decoder.count('invalidateEmulatedCompletionsAfterWait(') == 3  # definition + 2 wait hooks
assert decoder.count('drainSignalledEmulatedCompletions(') == 2  # definition + vkWaitSemaphores
assert decoder.count('takeEmulatedCompletionMaintenanceLocked(') == 3
assert decoder.count('takeEmulatedCompletionMaintenanceForQueueLocked(') == 2
assert decoder.count('collectCacheMaintenanceMemoriesLocked(') == 2
assert decoder.count('collectVulkanFlushRangesLocked(') == 2
assert decoder.count('applyCacheMaintenance(') == 4  # definition + wait + queue-idle + drain
assert decoder.count('vkInvalidateMappedMemoryRanges(') == 1  # only inside applyCacheMaintenance
assert 'static constexpr size_t kMaxEmulatedCompletions = 64;' in decoder
assert 'if (mEmulatedCompletions.size() >= kMaxEmulatedCompletions) {' in decoder
assert 'if (it == mMemoryInfo.end()) return; // Freed memory never reaches the driver.' in decoder
assert 'return VkMappedMemoryRange{VK_STRUCTURE_TYPE_MAPPED_MEMORY_RANGE, nullptr, memory, 0,' in decoder
# Neither direction is gated on the coherent feature: the guest is told the cached type is
# coherent, so the host owes the invalidation in every configuration.
apply = decoder[decoder.index('    void applyCacheMaintenance(VulkanDispatch* vk,'):]
apply = apply[:apply.index('    // Consumes only fences the driver reports signalled.')]
assert 'VulkanDisableCoherentMemoryAndEmulate' not in apply
assert 'vk->vkInvalidateMappedMemoryRanges(' in apply
assert apply.index('host_pointer_cache::invalidate(') > apply.index(
    'vk->vkInvalidateMappedMemoryRanges(')
assert 'if (!m_vkEmulation->getFeatures().VulkanDisableCoherentMemoryAndEmulate.enabled()) return;' not in decoder
# Positive evidence that the path ran, bounded like every other diagnostic here.
assert 'HOSTPTR-INVALIDATE: call=%u ranges=%zu bytes=%llu first=%p' in decoder
assert 'if (n < 8) {' in apply
assert apply.index('HOSTPTR-INVALIDATE: call=%u') < apply.index(
    'host_pointer_cache::invalidate(range.base, range.size);')
submit = decoder[decoder.index('    VkResult on_vkQueueSubmit('):]
submit = submit[:submit.index('    VkResult on_vkQueueWaitIdle(')]
flush = submit.index('vkFlushMappedMemoryRanges(')
fence = submit.index('usedFence = builder.CreateFenceForOp();')
record = submit.index('recordEmulatedCompletion(device, queue, usedFence);')
dispatch = submit.index('auto result = dispatchVkQueueSubmit(vk, queue, submitCount, pSubmits, usedFence')
assert flush < fence < record < dispatch
assert 'collectVulkanFlushRangesLocked(device);' in submit
# The record is taken whether or not the coherent feature is enabled.
record_block = submit[submit.rindex('usedFence = builder.CreateFenceForOp();'):record]
assert 'VulkanDisableCoherentMemoryAndEmulate' not in record_block
wait_fences = decoder[decoder.index('    VkResult on_vkWaitForFences('):]
wait_fences = wait_fences[:wait_fences.index('    VkResult on_vkResetFences(')]
assert (wait_fences.index('waitForFences(device, vk, fenceCount, pFences, waitAll, timeout, false)') <
        wait_fences.index('invalidateEmulatedCompletionsAfterWait(') <
        wait_fences.index('return waitResult;'))
# The shared waitForFences() helper keeps its original return statement: the roundtrip-trace
# patch anchors on it, and the guest-visible boundary is the handler above.
helper_wait = decoder[decoder.index('    VkResult waitForFences('):]
helper_wait = helper_wait[:helper_wait.index('    VkResult waitForFence(VkFence fence, uint64_t timeout)')]
assert 'invalidateEmulatedCompletionsAfterWait(' not in helper_wait
fence_status = decoder[decoder.index('    VkResult on_vkGetFenceStatus('):]
fence_status = fence_status[:fence_status.index('    VkResult on_vkWaitForFences(')]
assert 'if (status == VK_SUCCESS) {' in fence_status
assert (fence_status.index('vk->vkGetFenceStatus(device, fence)') <
        fence_status.index('invalidateEmulatedCompletionsAfterWait('))
queue_idle = decoder[decoder.index('    VkResult on_vkQueueWaitIdle('):]
queue_idle = queue_idle[:queue_idle.index('    VkResult on_vkResetCommandBuffer(')]
assert 'const VkResult idleResult = vk->vkQueueWaitIdle(queue);' in queue_idle
assert queue_idle.index('vk->vkQueueWaitIdle(queue)') < queue_idle.index('applyCacheMaintenance(')
assert 'VulkanDisableCoherentMemoryAndEmulate' not in queue_idle
semaphores = decoder[decoder.index('    VkResult on_vkWaitSemaphores('):]
semaphores = semaphores[:semaphores.index('    VkResult onSemaphoreSignalledOnSharedQueue(')]
assert 'if (waitResult == VK_SUCCESS) {' in semaphores
assert semaphores.index('vkWaitSemaphores(device, pWaitInfo, timeout)') < semaphores.index(
    'drainSignalledEmulatedCompletions(')
# An unconfirmed fence is never consumed by a wait that did not prove completion.
assert 'const bool allListed = waitAll && waitResult == VK_SUCCESS;' in decoder
assert 'vk->vkGetFenceStatus(device, fence) == VK_SUCCESS' in decoder
# Bounded evidence for the guest's own cache maintenance, at both opcodes.
wire = (source / 'host/vulkan/vk_decoder.cpp').read_text(encoding='utf-8')
assert wire.count('GUEST-CACHE-OP') == 2
assert 'opcode=vkFlushMappedMemoryRanges' in wire
assert 'opcode=vkInvalidateMappedMemoryRanges' in wire
assert wire.count('if (call < 8) {') == 2  # Bounded: eight lines each, then silence.
assert '#include <atomic>' in wire
flush_opcode = wire[wire.index('case OP_vkFlushMappedMemoryRanges: {'):]
flush_opcode = flush_opcode[:flush_opcode.index('case OP_vkInvalidateMappedMemoryRanges: {')]
assert 'GUEST-CACHE-OP: opcode=vkFlushMappedMemoryRanges' in flush_opcode
assert flush_opcode.index('memoryRangeCount') < flush_opcode.index('GUEST-CACHE-OP')
invalidate_opcode = wire[wire.index('case OP_vkInvalidateMappedMemoryRanges: {'):]
assert 'GUEST-CACHE-OP: opcode=vkInvalidateMappedMemoryRanges' in invalidate_opcode
assert 'vk->vkInvalidateMappedMemoryRanges(unboxed_device' in invalidate_opcode
print('PASS: exact tested helper in both TUs; four final-chain pointer producers guarded once; old fallback absent')
print('PASS: unconditional physical coherent guard before fd/pool/backing acquisition; alias host index never re-mapped; emulation waiver bounded to cached+tracked shapes; native coherent not disabled')
print('PASS: exclusive pointer/fd/pool producers asserted; defensive mixed-chain owned cleanup; borrowed aliases never freed')
print('PASS: image ranges tracked on both bind paths and dropped on destroy; flush covers buffers and images; invalidate only after a confirmed completion')
print('PASS: host-pointer imports recorded from the final chain, maintained with dc civac at every completion boundary regardless of the coherent feature; Vulkan mapped-range calls stay behind the feature and off host-pointer memory')
print('PASS: guest vkFlushMappedMemoryRanges/vkInvalidateMappedMemoryRanges opcodes counted with an eight-line bound')
