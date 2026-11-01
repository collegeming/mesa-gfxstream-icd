#include "native_backing_lease.h"
#include <cassert>
#include <chrono>
#include <future>
#include <iostream>
#include <vector>
using namespace gfxstream::host;
static std::mutex eventsMutex;
static std::vector<int> events;
static void record(int event) { std::lock_guard<std::mutex> lock(eventsMutex); events.push_back(event); }
static VKAPI_ATTR void VKAPI_CALL unmap(VkDevice, VkDeviceMemory) { record(1); }
static VKAPI_ATTR void VKAPI_CALL freeMemory(VkDevice, VkDeviceMemory, const VkAllocationCallbacks*) { record(2); }

static void lifetimeCase(bool mapped, bool logicalFreeFirst, unsigned resourceCount) {
    { std::lock_guard<std::mutex> lock(eventsMutex); events.clear(); }
    auto complete = std::make_shared<std::promise<void>>();
    auto finished = complete->get_future();
    auto instance = std::make_shared<native::Lifetime>();
    instance->retire([complete] { record(4); complete->set_value(); });
    auto device = std::make_shared<native::Lifetime>(instance);
    device->retire([] { record(3); });
    auto owner = std::make_shared<native::Allocation>(device, VK_NULL_HANDLE, VK_NULL_HANDLE,
                                                    8192, 42, unmap, freeMemory);
    if (mapped) owner->mapped(reinterpret_cast<void*>(uintptr_t(0x10000)));
    std::vector<std::shared_ptr<native::Allocation>> publications(3, owner);
    std::vector<std::shared_ptr<native::Allocation>> resources(resourceCount, owner);
    native::MapState map;
    if (mapped && resourceCount) assert(map.map(*resources[0], 4096, 4096));
    // Device/instance retire while allocations remain. These edges MUST keep them alive.
    device.reset(); instance.reset();
    if (logicalFreeFirst) owner.reset();
    publications.clear(); // revoke all unconsumed publication references
    if (mapped && resourceCount) {
        assert(!map.map(*resources[0], 4096, 4096));
        assert(map.unmap());
        assert(!map.unmap());
        { std::lock_guard<std::mutex> lock(eventsMutex); assert(events.empty()); }
        assert(map.map(*resources[0], 8192, 4096)); // remap after logical free
        assert(map.unmap());
    }
    while (!resources.empty()) resources.pop_back();
    if (!logicalFreeFirst) {
        { std::lock_guard<std::mutex> lock(eventsMutex); assert(events.empty()); }
        owner.reset(); // inverse order: resource destroy before Vulkan logical free
    }
    assert(finished.wait_for(std::chrono::seconds(10)) == std::future_status::ready);
    { std::lock_guard<std::mutex> lock(eventsMutex);
      assert(events == (mapped ? std::vector<int>{1,2,3,4} : std::vector<int>{2,3,4})); }
}
int main() {
    static_assert(!native::publicationReady(), "No paired VMM proof exists");
    for (bool mapped : {false, true})
        for (bool freeFirst : {false, true})
            for (unsigned count : {0u, 1u, 3u}) lifetimeCase(mapped, freeFirst, count);
    // Invalid extents never acquire an active map. Real allocation size, NOT VMA extent.
    auto done = std::make_shared<std::promise<void>>();
    auto finished = done->get_future();
    auto device = std::make_shared<native::Lifetime>();
    device->retire([done] { done->set_value(); });
    auto owner = std::make_shared<native::Allocation>(device, VK_NULL_HANDLE, VK_NULL_HANDLE,
                                                    8192, 99, unmap, freeMemory);
    owner->mapped(reinterpret_cast<void*>(uintptr_t(0x10000)));
    assert(native::validExtent(*owner, 4097, 4096));
    assert(!native::validExtent(*owner, 8193, 4096));
    assert(!native::validExtent(*owner, UINT64_MAX, 4096));
    assert(!native::validExtent(*owner, 0, 4096));
    assert(!native::validExtent(*owner, 4096, 0));
    assert(!native::validExtent(*owner, 4096, 3));
    owner->mapped(reinterpret_cast<void*>(uintptr_t(0x10001)));
    assert(!native::validExtent(*owner, 4096, 4096));
    owner->mapped(reinterpret_cast<void*>(UINTPTR_MAX - 4095));
    assert(!native::validExtent(*owner, 8192, 4096));
    owner.reset(); device.reset();
    assert(finished.wait_for(std::chrono::seconds(10)) == std::future_status::ready);
    std::cout << "PASS: shared production Allocation/MapState/Lifetime; 12 free/map/resource orderings, multiple owners, never-mapped free, unmap/remap, device-instance retirement, overflow/alignment; readiness false\n";
}
