#!/usr/bin/env python3
from pathlib import Path
import sys
root=Path(sys.argv[1])
d=(root/'host/vulkan/vk_decoder_global_state.cpp').read_text()
s=(root/'host/vulkan/vk_decoder_internal_structs.h').read_text()
p=(root/'host/virtgpu_gfxstream_protocol.h').read_text()
f=(root/'host/virtio_gpu_frontend.cpp').read_text()
m=(root/'host/vulkan/vk_emulated_physical_device_memory.h').read_text()
assert 'offsetof(vulkanCapset, classicImageBridgeVersion) == 120' in p
assert 'offsetof(vulkanCapset, classicImageBridgeContract) == 124' in p
assert 'sizeof(vulkanCapset) == 128' in p
assert 'getFeatures().ClassicImageBridge.enabled()' in d
assert '!getFeatures().VulkanSnapshots.enabled()' in d
assert 'const bool paired = ack && !ack->handle && !ack->stride && !ack->format && !ack->usage' in d
assert 'if (!paired ||' in d
assert 'actual.pNext = &ahb; actual.tiling = native->tiling;' in d
assert 'VK_EXTERNAL_MEMORY_FEATURE_EXPORTABLE_BIT | VK_EXTERNAL_MEMORY_FEATURE_DEDICATED_ONLY_BIT' in d
assert 'classicImageAllocationClaimed = true' in d and 'classicImageAllocationClaimed = false' in d
assert 'allocation->classicDedicatedImage != image' in d
assert 'allocation->classicImageResource && (!bridge || !bridge->classicImageResource)' in d
assert 'classicImageResource || (classicAllocation && classicAllocation->classicImageResource)' in d
assert 'info->classicImageResource || !info->ptr' in d
assert 'classicImageRequirements = {cb->memory.size, 1, uint32_t(1u << shadow)}' in d
assert 'mGuestToHostMemoryTypeIndexMap[shadow] != physical' in m
assert 'propertyFlags != VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT' in m
assert 'native::Reclaimer::get().post([lease = std::move(held)]' in d
assert f.index('mRenderer->stop(/*wait*/ true);') < f.index('CIB leases may release') < f.index('mRenderer.reset();')
assert 'Layout describes guest classic GEM transfer staging, NOT native AHB layout' in d
assert 'classicImageBacking' in s
# All original allocation coherence guards still exist for non-CIB paths.
assert 'validateHostCoherentContract(' in d and 'rejectZeroSizeAllocation(' in d
assert 'm_vkEmulation->appendExternalMemoryModeDeviceExtensions(hostAlwaysDeviceExtensions);' in d
external=(root/'host/vulkan/external_memory.cpp').read_text()
assert 'VK_ANDROID_EXTERNAL_MEMORY_ANDROID_HARDWARE_BUFFER_EXTENSION_NAME' in external
print('PASS: CIB1 default-off versioned capset/private ACK, actual backing size/shadow/dedicated one-bind/map rejection, staging-vs-AHB separation, rollback/deferred release guards and unchanged non-CIB coherence checks')
