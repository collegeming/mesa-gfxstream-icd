#!/usr/bin/env python3
"""Fail-closed source invariants, run only in Actions against patched pinned source."""
from pathlib import Path
import sys
root = Path(sys.argv[1])
def read(rel): return (root / rel).read_text()
d = read('host/vulkan/vk_decoder_global_state.cpp')
m = read('host/common/external_object_manager.cpp')
h = read('host/common/include/gfxstream/host/external_object_manager.h')
r = read('host/virtio_gpu_resource.cpp')
s = read('host/vulkan/vk_decoder_internal_structs.h')
helper = Path(__file__).with_name('native_backing_lease.h').read_text().replace('#pragma once\n', '')
assert helper in h
assert 'std::shared_ptr<native::Allocation> nativeOwner;' in s
assert 'std::shared_ptr<native::Allocation> owner;' in h
assert 'std::make_shared<native::Allocation>' in d
assert 'nativeDevice->nativeLifetime, device, *pMemory, memoryInfo.size,' in d
assert 'generation, vk->vkUnmapMemory, vk->vkFreeMemory' in d
assert 'native::publicationReady() || !info->nativeOwner' in d
assert 'NATIVE-LEASE-PUBLISH-REJECT: ready=%d owner=%d owner-epoch=%llu' in d
assert 'NATIVE-LEASE-ALLOC: eligible=%d ready=%d host-visible=%d virtio-next=%d' in d
assert r.count('native::nativeResource(createBlobArgs->blob_mem, createBlobArgs->blob_flags)') == 2
assert 'NATIVE-LEASE-CREATE-REJECT:' in r
assert 'if (!owner || !owner->generation()) return false;' in m
assert 'poolOffset < 0 && folioCharge.bytes() == 0 &&' in d
assert 'native::nativeBlob(createBlobInfoPtr != nullptr,' in d
assert 'createBlobInfoPtr ? createBlobInfoPtr->blobMem : 0,' in d
assert 'createBlobInfoPtr ? createBlobInfoPtr->blobFlags : 0);' in d
assert 'if (info->needUnmap && !m_vkEmulation->getFeatures().ExternalBlob.enabled())' in d
assert 'if (info->nativeOwner) info->nativeOwner->mapped(info->ptr);' in d
frontend = read('host/virtio_gpu_frontend.cpp')
assert 'if (resource.Destroy() != 0)' in frontend
assert frontend.index('if (resource.Destroy() != 0)') < frontend.index('mResources.erase(resourceIt);')
assert '!native::Session::get().ready(memory.owner->epoch())' in r
assert '!native::Session::get().ready(info->nativeOwner->epoch())' in d
assert 'getEnvironmentVariable' not in helper
assert 'class Initialization' in helper and 'class Offer' in helper
renderer = read('host/virtio_gpu_gfxstream_renderer.cpp')
assert 'if (!nativeOffer.parse(param.key, param.value)) return -EINVAL;' in renderer
assert renderer.index('const int initRes = sFrontend()->init(') < renderer.index('nativeInitialization.success(nativeOffer)')
assert 'return sFrontend()->createBlob(ctx_id, res_handle, create_blob, handle);' in renderer
assert 'NATIVE-LEASE-CONTRACT: version=1 accepted=0x%llx epoch=%llu native-host3d-only' in renderer
assert 'NATIVE-LEASE-MAP-REJECT: res=%u owner=%d epoch-current=%d active=%d extent-valid=%d' in r
assert 'const auto destroy = deviceDispatch->vkDestroyDevice;' in d
assert 'const auto destroy = dispatch->vkDestroyInstance;' in d
assert '[device, destroy, boxed]' in d and 'debugContext]' in d
assert 'return mHostMemInfos.emplace(key, std::move(info)).second;' in m
assert 'it->second.generation == generation' in m
assert 'retired.push_back(std::move(it->second));' in m
assert 'std::optional<HostMemInfo> ret = std::move(found->second);' in m
unmap = r[r.index('int VirtioGpuResource::Unmap()'):r.index('int VirtioGpuResource::GetInfo(')]
assert 'mNativeMapState.unmap()' in unmap and '.reset()' not in unmap
assert 'mNativeMapState.active()) return -EBUSY' in r
assert 'info->nativeOwner, info->caching' in d
assert 'native::validExtent(*info->nativeOwner, info->size, nativePage)' in d
assert 'native::Lifetime>(nativeInstance->nativeLifetime)' in d
assert 'no native owner/VMM contract' in d
# Existing descriptor-producing paths still exist, and import ABI guards remain.
for marker in ['STREAM_HANDLE_TYPE_MEM_POOL', 'STREAM_HANDLE_TYPE_MEM_SHM',
               'exportMemoryHandle(deviceInfo, vk, device, memory)',
               'validateHostPointerImport(', 'validateHostVisibleDmaBufImport(']:
    assert marker in d
assert 'native::quarantine(std::move(std::get<ExternalMemoryMapping>(*mBlobMemory).owner))' in r
assert 'VirtioGpuResource::~VirtioGpuResource()' in r
assert 'this->~VirtioGpuResource();' in r
assert 'Native reset rejected: resource %u still mapped' in frontend
assert 'if (destroyVirtioGpuObjects() != 0) return;' in frontend
assert 'int ret = destroyVirtioGpuObjects();\n    if (ret) return ret;' in frontend
assert frontend.index('mRenderer->stop(/*wait*/ true);') < frontend.index('native::Reclaimer::get().drain();')
assert 'if (resource.HasActiveNativeMap()) return -EBUSY;' in frontend
loader = read('host/vulkan/vulkan_dispatch.cpp')
assert 'static VulkanDispatchImpl* impl = new VulkanDispatchImpl;' in loader
assert 'std::vector<SharedLibrary*> mLibs;' in loader
assert '~SharedLibraries() = default;' in loader
assert 'dlclose(' not in loader
print('PASS: production owner propagation, device/instance finalizers, all-generation revocation, retained unmap/remap, reset rejection, forced drop quarantine, post-stop drain, fail-closed raw native/imported paths; descriptor paths retained')
assert 'new InstanceObjects::DeviceObjects(std::move(deviceObjects))' in d
assert 'std::lock_guard<std::recursive_mutex> resourceLock(mResourceOperations);' in frontend
assert 'if (mNativeRetiring) return -EBUSY;' in frontend
assert 'if (native::Session::get().snapshotBlocked()) return -EBUSY;' in frontend
assert 'return mNativeNegotiated || mAllocations.load() || !Reclaimer::get().idle();' in helper
assert '} // release resource operation lock before context cleanup/decoder waits' in frontend
print('BOUNDARY: exact paired init contract and native owner epoch required; old VMM rejected; failed GPU idle quarantines native children; no guest/PFN/cache/GPU validation')
