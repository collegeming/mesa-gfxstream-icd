#include "native_backing_lease.h"
#include <cassert>
#include <chrono>
#include <future>
#include <iostream>
#include <vector>
using namespace gfxstream::host;
static std::mutex eventsMutex;
static std::vector<int> events;
static std::promise<void> complete;
static void record(int event) { std::lock_guard<std::mutex> lock(eventsMutex); events.push_back(event); }
static VKAPI_ATTR void VKAPI_CALL unmap(VkDevice, VkDeviceMemory) { record(1); }
static VKAPI_ATTR void VKAPI_CALL freeMemory(VkDevice, VkDeviceMemory, const VkAllocationCallbacks*) { record(2); }
int main() {
    static_assert(!native::publicationReady(), "No paired VMM proof exists");
    auto finished = complete.get_future();
    auto instance = std::make_shared<native::Lifetime>();
    instance->retire([] { record(4); complete.set_value(); });
    auto device = std::make_shared<native::Lifetime>(instance);
    device->retire([] { record(3); });
    auto owner = std::make_shared<native::Allocation>(device, VK_NULL_HANDLE, VK_NULL_HANDLE,
                                                    8192, 42, unmap, freeMemory);
    owner->mapped(reinterpret_cast<void*>(uintptr_t(0x10000)));
    assert(owner->generation() == 42 && owner->size() == 8192);
    assert(native::validExtent(*owner, 4097, 4096));
    assert(!native::validExtent(*owner, 8193, 4096));
    assert(!native::validExtent(*owner, UINT64_MAX, 4096));
    assert(!native::validExtent(*owner, 0, 4096));
    assert(!native::validExtent(*owner, 4096, 0));
    assert(!native::validExtent(*owner, 4096, 3));
    owner->mapped(reinterpret_cast<void*>(uintptr_t(0x10001)));
    assert(!native::validExtent(*owner, 4096, 4096));
    owner->mapped(reinterpret_cast<void*>(uintptr_t(0x10000)));
    auto publication = owner;
    auto resource = publication;
    native::MapState map;
    assert(map.map(*resource, 4096, 4096));
    assert(!map.map(*resource, 4096, 4096));
    // logical free, publication revoke, device and instance retire before resource
    owner.reset(); publication.reset(); device.reset(); instance.reset();
    assert(map.unmap());
    assert(!map.unmap());
    { std::lock_guard<std::mutex> lock(eventsMutex); assert(events.empty()); }
    // Unmap deliberately keeps the resource owner: remap after Vulkan free is safe.
    assert(map.map(*resource, 8192, 4096));
    assert(map.unmap());
    resource.reset();
    assert(finished.wait_for(std::chrono::seconds(10)) == std::future_status::ready);
    { std::lock_guard<std::mutex> lock(eventsMutex); assert((events == std::vector<int>{1,2,3,4})); }
    std::cout << "PASS: real production Allocation/MapState/Lifetime; unmap-free-device-instance order, retained remap and overflow/alignment failures; readiness false\n";
}
