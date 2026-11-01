#!/usr/bin/env python3
"""Bounded native allocation ownership increment. Raw publication remains disabled."""
import argparse
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('root', type=Path)
root = parser.parse_args().root.resolve()

def edit(rel, old, new):
    path = root / rel
    text = path.read_text()
    if text.count(old) != 1:
        raise SystemExit(f'{rel}: expected exactly one anchor ({text.count(old)}): {old[:80]}')
    path.write_text(text.replace(old, new))

manager_h = 'host/common/include/gfxstream/host/external_object_manager.h'
manager = 'host/common/external_object_manager.cpp'
structs = 'host/vulkan/vk_decoder_internal_structs.h'
decoder = 'host/vulkan/vk_decoder_global_state.cpp'
resource = 'host/virtio_gpu_resource.cpp'
resource_h = 'host/virtio_gpu_resource.h'
# Embed in a tracked shared header: build artifacts contain the complete helper.
helper = (Path(__file__).resolve().parent / 'native_backing_lease.h').read_text()
edit(manager_h, '#include <atomic>', helper.replace('#pragma once\n', '') + '\n#include <atomic>')
edit(structs, '#include <condition_variable>', '#include "gfxstream/host/external_object_manager.h"\n#include <condition_variable>')
edit(structs, '    bool needUnmap = false;', '    std::shared_ptr<native::Allocation> nativeOwner;\n    bool needUnmap = false;')
edit(structs, 'struct InstanceInfo {', 'struct InstanceInfo {\n    std::shared_ptr<native::Lifetime> nativeLifetime;')
edit(structs, 'struct DeviceInfo {', 'struct DeviceInfo {\n    std::shared_ptr<native::Lifetime> nativeLifetime;')
edit(manager_h, '    uint32_t caching;\n};\n\nstruct GenericDescriptorInfo', '''    uint32_t caching;
    std::shared_ptr<native::Allocation> owner;
    uint64_t generation = 0;
};

struct GenericDescriptorInfo''')
edit(manager_h, '    void addMapping(uint32_t ctx_id, uint64_t blobId, void* addr, uint32_t caching);', '''    bool addMapping(uint32_t ctx_id, uint64_t blobId,
                    const std::shared_ptr<native::Allocation>& owner, uint32_t caching);
    void revokeMappings(uint64_t generation);''')
edit(manager, '''void ExternalObjectManager::addMapping(uint32_t ctxId, uint64_t blobId, void* addr,
                                       uint32_t caching) {
    struct HostMemInfo info = {
        .addr = addr,
        .caching = caching,
    };

    auto key = std::make_pair(ctxId, blobId);
    std::lock_guard<std::mutex> lock(mMutex);
    mHostMemInfos.insert(std::make_pair(key, info));
}''', '''bool ExternalObjectManager::addMapping(uint32_t ctxId, uint64_t blobId,
                                        const std::shared_ptr<native::Allocation>& owner,
                                        uint32_t caching) {
    // This internal registry grants no VMM readiness. Decoder publication and
    // resource Map independently enforce the immutable fail-closed gate.
    if (!owner || !owner->generation()) return false;
    HostMemInfo info{owner->address(), caching, owner, owner->generation()};
    auto key = std::make_pair(ctxId, blobId);
    std::lock_guard<std::mutex> lock(mMutex);
    return mHostMemInfos.emplace(key, std::move(info)).second; // reject duplicate, never replace
}

void ExternalObjectManager::revokeMappings(uint64_t generation) {
    std::vector<HostMemInfo> retired;
    {
        std::lock_guard<std::mutex> lock(mMutex);
        for (auto it = mHostMemInfos.begin(); it != mHostMemInfos.end();) {
            if (it->second.generation == generation) {
                retired.push_back(std::move(it->second));
                it = mHostMemInfos.erase(it);
            } else ++it;
        }
    }
    // Final owner releases are outside the manager lock; destructors only enqueue.
}''')
edit(manager_h, '#include <unordered_map>', '#include <unordered_map>\n#include <vector>')
edit(manager, '        std::optional<HostMemInfo> ret = found->second;', '        std::optional<HostMemInfo> ret = std::move(found->second);')
edit(manager, '''void ExternalObjectManager::removeContextBlobDescriptorInfos(uint32_t ctxId) {
    std::lock_guard<std::mutex> lock(mMutex);''', '''void ExternalObjectManager::removeContextBlobDescriptorInfos(uint32_t ctxId) {
    std::vector<HostMemInfo> retired;
    std::lock_guard<std::mutex> lock(mMutex);
    for (auto it = mHostMemInfos.begin(); it != mHostMemInfos.end();) {
        if (it->first.first == ctxId) {
            retired.push_back(std::move(it->second));
            it = mHostMemInfos.erase(it);
        } else ++it;
    }''') # retired declared before lock => lock destroyed first

# Scope: real native host-visible allocation only. Imported aliases/fd/AHB,
# shared/private memory, pools, exports and legacy GPA mapping are not converted.
edit(decoder, '        memoryInfo.memoryIndex = localAllocInfo.memoryTypeIndex;', '''        memoryInfo.memoryIndex = localAllocInfo.memoryTypeIndex;
        const bool nativeAllocation = hostVisible && usingVirtioGpuNext() &&
            !m_vkEmulation->getFeatures().ExternalBlob.enabled() &&
            !importEmulatedExternalMemory && !diagnosticFdInfo && !diagnosticAhb &&
            !diagnosticHostPtr && !sharedMemory.has_value() && !privateMemory &&
            poolOffset < 0 && folioCharge.bytes() == 0 &&
            native::nativeBlob(createBlobInfoPtr != nullptr,
                createBlobInfoPtr ? createBlobInfoPtr->blobMem : 0,
                createBlobInfoPtr ? createBlobInfoPtr->blobFlags : 0);
        if (nativeAllocation) {
            auto* nativeDevice = gfxstream::base::find(mDeviceInfo, device);
            auto* nativeInstance = nativeDevice ?
                gfxstream::base::find(mInstanceInfo, nativeDevice->parentInstance) : nullptr;
            if (!nativeInstance) {
                freeMemoryLocked(device, vk, *pMemory);
                *pMemory = VK_NULL_HANDLE;
                return VK_ERROR_DEVICE_LOST;
            }
            if (!nativeInstance->nativeLifetime)
                nativeInstance->nativeLifetime = std::make_shared<native::Lifetime>();
            if (!nativeDevice->nativeLifetime)
                nativeDevice->nativeLifetime =
                    std::make_shared<native::Lifetime>(nativeInstance->nativeLifetime);
            static std::atomic<uint64_t> nextNativeGeneration{1};
            const uint64_t generation = nextNativeGeneration.fetch_add(1);
            // Exhaustion must never recycle a generation (practically unreachable).
            if (!generation || generation == UINT64_MAX) std::abort();
            memoryInfo.nativeOwner = std::make_shared<native::Allocation>(
                nativeDevice->nativeLifetime, device, *pMemory, memoryInfo.size,
                generation, vk->vkUnmapMemory, vk->vkFreeMemory);
        }''')
# usingVirtioGpuNext is checked by source invariant against actual feature below.
edit(decoder, 'hostVisible && usingVirtioGpuNext() &&', 'hostVisible && m_vkEmulation->getFeatures().VirtioGpuNext.enabled() &&')
edit(decoder, '''        *pMemory = new_boxed_non_dispatchable_VkDeviceMemory(*pMemory);

        return result;''', '''        if (memoryInfo.nativeOwner) memoryInfo.nativeOwner->mapped(memoryInfo.ptr);
        *pMemory = new_boxed_non_dispatchable_VkDeviceMemory(*pMemory);

        return result;''')
edit(decoder, '''        if (memoryInfo.directMapped) {''', '''        if (memoryInfo.nativeOwner) {
            // Logical Vulkan free revokes EVERY unconsumed key of this generation.
            // Consumed resource leases are deliberately not revoked.
            ExternalObjectManager::get()->revokeMappings(memoryInfo.nativeOwner->generation());
            memoryInfo.nativeOwner.reset(); // actual unmap/free executes on reclaimer
            memoryInfo.ptr = nullptr;
            memoryInfo.needUnmap = false;
            return;
        }
        if (memoryInfo.directMapped) {''')
# Block raw publication before mapping fallback imports. Export success unchanged.
edit(decoder, '''            if (!exportedMemoryOpt) {
                // Memory''', '''            if (!exportedMemoryOpt) {
                GFXSTREAM_ERROR("NATIVE-LEASE-REJECT: imported raw fallback has no native owner/VMM contract");
                return VK_ERROR_FEATURE_NOT_PRESENT;
                // Memory''')
edit(decoder, '''        } else if (!info->needUnmap) {
            VkResult mapResult = vk->vkMapMemory''', '''        } else if (!native::publicationReady() || !info->nativeOwner) {
            GFXSTREAM_ERROR("NATIVE-LEASE-REJECT: VMM detach acknowledgement not negotiated");
            return VK_ERROR_FEATURE_NOT_PRESENT;
        } else if (!info->needUnmap) {
            VkResult mapResult = vk->vkMapMemory''')
edit(decoder, '''        if (info->needUnmap) {
            uint64_t hva''', '''        if (info->needUnmap && !m_vkEmulation->getFeatures().ExternalBlob.enabled()) {
            if (info->nativeOwner) info->nativeOwner->mapped(info->ptr);
            uint64_t hva''')
edit(decoder, '''            ExternalObjectManager::get()->addMapping(virtioGpuContextId, hostBlobId,
                                                     (void*)(uintptr_t)alignedHva, info->caching);''', '''            const long nativePage = sysconf(_SC_PAGESIZE);
            if (!native::publicationReady() || !info->nativeOwner || nativePage <= 0 ||
                !native::validExtent(*info->nativeOwner, info->size, nativePage) ||
                !ExternalObjectManager::get()->addMapping(virtioGpuContextId, hostBlobId,
                                                          info->nativeOwner, info->caching)) {
                return VK_ERROR_FEATURE_NOT_PRESENT;
            }''')

# Native device physical finalizer captures ONLY values and retained boxed handle.
# Existing non-native fd/AHB/pool destruction remains synchronous and unchanged.
edit(decoder, '''        // Run the underlying API call.
        {
            AutoLock lock(*graphicsDriverLock());''', '''        if (deviceInfo.nativeLifetime) {
            const auto destroy = deviceDispatch->vkDestroyDevice;
            const auto boxed = deviceInfo.boxed;
            deviceInfo.nativeLifetime->retire([device, destroy, boxed] {
                { AutoLock lock(*graphicsDriverLock()); destroy(device, nullptr); }
                delete_VkDevice(boxed); // dispatch table remains alive through final allocation
            });
            deviceInfo.nativeLifetime.reset();
            return;
        }
        // Run the underlying API call.
        {
            AutoLock lock(*graphicsDriverLock());''')
edit(decoder, '''        if (instanceInfo.debugMessenger != VK_NULL_HANDLE) {''', '''        if (instanceInfo.nativeLifetime) {
            const auto boxed = instanceInfo.boxed;
            auto* dispatch = dispatch_VkInstance(boxed);
            const auto destroy = dispatch->vkDestroyInstance;
            const auto destroyMessenger = dispatch->vkDestroyDebugUtilsMessengerEXT;
            const auto messenger = instanceInfo.debugMessenger;
            // VVL callback userdata must also outlive the debug messenger.
            std::shared_ptr<VVLContext> debugContext(std::move(instanceInfo.debugContext));
            instanceInfo.nativeLifetime->retire(
                [instance, boxed, destroy, destroyMessenger, messenger, debugContext] {
                    if (messenger && destroyMessenger)
                        destroyMessenger(instance, messenger, nullptr);
                    if (destroy) destroy(instance, nullptr);
                    delete_VkInstance(boxed);
                });
#ifdef CONFIG_AEMU
            m_vkEmulation->getCallbacks().unregisterVulkanInstance((uint64_t)instance);
#endif
            instanceInfo.nativeLifetime.reset();
            return;
        }
        if (instanceInfo.debugMessenger != VK_NULL_HANDLE) {''')
# Remove preexisting explicit destroy-device wait from mMutex (no lease wait).
edit(decoder, '''        destroyDeviceObjects(deviceObjects);

        mDeviceInfo.erase(device);
    }

    void on_vkDestroyDevice''', '''        // Caller performs physical object cleanup after dropping mMutex.
        return deviceObjects;
    }

    void on_vkDestroyDevice''')
edit(decoder, '    void destroyDeviceLocked(VkDevice device) REQUIRES(mMutex) {', '    InstanceObjects::DeviceObjects destroyDeviceLocked(VkDevice device) REQUIRES(mMutex) {')
# unique anchor in modified function
edit(decoder, '''            GFXSTREAM_WARNING("Could not find device:%p to destroy", device);
            return;''', '''            GFXSTREAM_WARNING("Could not find device:%p to destroy", device);
            return {};''')
edit(decoder, '''        std::lock_guard<std::mutex> lock(mMutex);

        destroyDeviceLocked(device);''', '''        InstanceObjects::DeviceObjects objects;
        {
            std::lock_guard<std::mutex> lock(mMutex);
            objects = destroyDeviceLocked(device);
        }
        if (!objects.device.empty()) destroyDeviceObjects(objects);''')

# Resource holds owner until Destroy, NOT until Unmap. This state machine does
# not assert VMM detach and cannot enable raw publication.
edit(resource_h, '    int Map(void** outAddress, uint64_t* outSize);', '    int Map(void** outAddress, uint64_t* outSize);\n    int Unmap();')
edit(resource_h, '    std::optional<BlobMemory> mBlobMemory;', '    std::optional<BlobMemory> mBlobMemory;\n    native::MapState mNativeMapState;')
edit(resource, '''        hva = memory.addr;
        hvaSize = mCreateBlobArgs->size;''', '''        const long page = sysconf(_SC_PAGESIZE);
        if (!native::publicationReady() || !memory.owner ||
            memory.generation != memory.owner->generation() || page <= 0 ||
            !mNativeMapState.map(*memory.owner, mCreateBlobArgs->size, page)) {
            if (outAddress) *outAddress = nullptr;
            if (outSize) *outSize = 0;
            return -EINVAL;
        }
        hva = memory.owner->address();
        hvaSize = mCreateBlobArgs->size;''')
edit(resource, 'int VirtioGpuResource::GetInfo(', '''int VirtioGpuResource::Unmap() {
    if (mBlobMemory && std::holds_alternative<ExternalMemoryMapping>(*mBlobMemory)) {
        return mNativeMapState.unmap() ? 0 : -EINVAL;
    }
    return 0; // ring/fd/AHB paths unchanged
}

int VirtioGpuResource::GetInfo(''')
edit(resource, '''int VirtioGpuResource::Destroy() {
    if''', '''int VirtioGpuResource::Destroy() {
    if (mBlobMemory && std::holds_alternative<ExternalMemoryMapping>(*mBlobMemory)) {
        // Enabled production must require proven detach before this callback.
        // Readiness is hard false here, so old VMM can never acquire a native lease.
        if (mNativeMapState.active()) return -EBUSY;
        mBlobMemory.reset();
    }
    if''')
edit('host/virtio_gpu_frontend.cpp', '''    // TODO(lfy): Good place to run any registered cleanup callbacks.
    // No-op for now.
    return 0;''', '''    return it->second.Unmap();''')
edit('host/virtio_gpu_frontend.cpp', '    resource.Destroy();', '''    if (resource.Destroy() != 0) {
        GFXSTREAM_ERROR("Native resource still active: retaining resource %u", resourceId);
        return; // Never erase a resource whose destroy rejected active mapping.
    }''')
# Forced clear/drop cannot silently release a possibly KVM-visible native lease.
edit(resource_h, '    VirtioGpuResource() {}', '''    VirtioGpuResource() {}
    ~VirtioGpuResource();
    VirtioGpuResource(const VirtioGpuResource&) = delete;
    VirtioGpuResource& operator=(const VirtioGpuResource&) = delete;
    VirtioGpuResource(VirtioGpuResource&&) noexcept = default;
    VirtioGpuResource& operator=(VirtioGpuResource&& other) noexcept;
    bool HasActiveNativeMap() const { return mNativeMapState.active(); }''')
edit(resource, 'int VirtioGpuResource::Destroy() {', '''VirtioGpuResource::~VirtioGpuResource() {
    if (mNativeMapState.active() && mBlobMemory &&
        std::holds_alternative<ExternalMemoryMapping>(*mBlobMemory)) {
        native::quarantine(std::move(std::get<ExternalMemoryMapping>(*mBlobMemory).owner));
    }
}

VirtioGpuResource& VirtioGpuResource::operator=(VirtioGpuResource&& other) noexcept {
    if (this != &other) {
        this->~VirtioGpuResource(); // quarantine active destination, never silently release
        new (this) VirtioGpuResource(std::move(other));
    }
    return *this;
}

int VirtioGpuResource::Destroy() {''')
edit(resource, '#include <unistd.h>', '#include <unistd.h>\n#include <new>')
edit('host/virtio_gpu_frontend.cpp', '''int VirtioGpuFrontend::destroyVirtioGpuObjects() {
    {''', '''int VirtioGpuFrontend::destroyVirtioGpuObjects() {
    for (const auto& [id, resource] : mResources) {
        if (resource.HasActiveNativeMap()) {
            GFXSTREAM_ERROR("Native reset rejected: resource %u still mapped", id);
            return -EBUSY; // preserve resources AND contexts, no fake successful reset
        }
    }
    {''')
edit('host/virtio_gpu_frontend.cpp', '''void VirtioGpuFrontend::teardown() {
    destroyVirtioGpuObjects();''', '''void VirtioGpuFrontend::teardown() {
    if (destroyVirtioGpuObjects() != 0) return;''')
edit('host/virtio_gpu_frontend.cpp', '''        mRenderer.reset();
    }
}''', '''        mRenderer.reset();
    }
    native::Reclaimer::get().drain(); // producers stopped; no decoder/manager locks
}''')
edit('host/virtio_gpu_frontend.cpp', '''    mContexts.clear();
    mResources.clear();''', '''    for (const auto& [id, resource] : mResources) {
        if (resource.HasActiveNativeMap()) return -EBUSY;
    }
    mContexts.clear();
    mResources.clear();''')
edit('host/virtio_gpu_frontend.cpp', '''    destroyVirtioGpuObjects();

    int ret = restoreRenderer(directory);''', '''    int ret = destroyVirtioGpuObjects();
    if (ret) return ret;

    ret = restoreRenderer(directory);''')
print('Applied native allocation ownership; raw publication intentionally fail-closed')
