// Links the actual external_object_manager.cpp, not a replica of its registry.
#include "gfxstream/host/external_object_manager.h"
#include "vulkan_gfxstream.h"
#include "gfxstream/virtio-gpu-gfxstream-renderer.h"
#include <cassert>
#include <chrono>
#include <future>
#include <iostream>
using namespace gfxstream::host;
static VKAPI_ATTR void VKAPI_CALL unmap(VkDevice, VkDeviceMemory) {}
static VKAPI_ATTR void VKAPI_CALL freeMemory(VkDevice, VkDeviceMemory, const VkAllocationCallbacks*) {}
int main() {
    // Replay the paired guest's two distinct real ABI stages. The allocation
    // chain was memset-zeroed and only HOST3D/id were assigned before encoding.
    VkCreateBlobGOOGLE allocationBlob{};
    allocationBlob.blobMem = STREAM_BLOB_MEM_HOST3D;
    allocationBlob.blobId = 77;
    assert(allocationBlob.blobFlags == 0);
    assert(native::nativeBlob(true, allocationBlob.blobMem, allocationBlob.blobFlags));
    stream_renderer_create_blob resourceBlob{};
    resourceBlob.blob_mem = STREAM_BLOB_MEM_HOST3D;
    resourceBlob.blob_flags = STREAM_BLOB_FLAG_USE_MAPPABLE;
    resourceBlob.blob_id = allocationBlob.blobId;
    resourceBlob.size = 4096;
    assert(native::nativeResource(resourceBlob.blob_mem, resourceBlob.blob_flags));
    resourceBlob.blob_flags = 0;
    assert(!native::nativeResource(resourceBlob.blob_mem, resourceBlob.blob_flags));
    resourceBlob.blob_flags = STREAM_BLOB_FLAG_CREATE_GUEST_HANDLE;
    assert(!native::nativeResource(resourceBlob.blob_mem, resourceBlob.blob_flags));
    ExternalObjectManager manager;
    auto done = std::make_shared<std::promise<void>>();
    auto finished = done->get_future();
    auto lifetime = std::make_shared<native::Lifetime>();
    lifetime->retire([done] { done->set_value(); });
    auto first = std::make_shared<native::Allocation>(lifetime, VK_NULL_HANDLE, VK_NULL_HANDLE,
                                                    8192, 1, unmap, freeMemory);
    auto second = std::make_shared<native::Allocation>(lifetime, VK_NULL_HANDLE, VK_NULL_HANDLE,
                                                     4096, 2, unmap, freeMemory);
    first->mapped(reinterpret_cast<void*>(uintptr_t(0x10000)));
    second->mapped(reinterpret_cast<void*>(uintptr_t(0x20000)));
    assert(!manager.addMapping(1, 0, {}, 0));
    assert(manager.addMapping(1, 10, first, 1));
    assert(!manager.addMapping(1, 10, second, 2)); // duplicate cannot replace first
    assert(manager.addMapping(1, 11, first, 1));
    assert(manager.addMapping(2, 12, first, 1)); // revoke ALL contexts/keys of generation
    auto consumed = manager.removeMapping(1, 10);
    assert(consumed && consumed->owner == first && consumed->generation == 1);
    assert(!manager.removeMapping(1, 10));
    manager.revokeMappings(1);
    assert(!manager.removeMapping(1, 11));
    assert(!manager.removeMapping(2, 12));
    assert(consumed->owner == first); // resource lease survives publication revoke
    assert(manager.addMapping(1, 10, second, 2)); // same key, new generation
    manager.revokeMappings(1); // stale revoke cannot remove replacement
    auto replacement = manager.removeMapping(1, 10);
    assert(replacement && replacement->owner == second && replacement->generation == 2);
    assert(manager.addMapping(0, 15, first, 1)); // context zero is valid
    assert(manager.addMapping(3, 16, second, 2));
    manager.removeContextBlobDescriptorInfos(0);
    assert(!manager.removeMapping(0, 15));
    auto otherContext = manager.removeMapping(3, 16);
    assert(otherContext && otherContext->owner == second);
    first.reset(); second.reset(); lifetime.reset();
    consumed.reset(); replacement.reset(); otherContext.reset();
    assert(finished.wait_for(std::chrono::seconds(10)) == std::future_status::ready);
    std::cout << "PASS: actual production manager duplicate reject, generation revoke all keys, context cleanup, stale revoke, consumed resource retention\n";
}
