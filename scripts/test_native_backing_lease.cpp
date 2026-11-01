#include "native_backing_lease.h"
#include <cassert>
#include <atomic>
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
    assert(native::nativeBlob(false, 0, 0));
    assert(native::nativeBlob(true, 2, 1)); // deferred native HOST3D
    assert(native::nativeBlob(true, 2, 3));
    assert(!native::nativeBlob(true, 1, 9)); // GUEST imported handle
    assert(!native::nativeBlob(true, 3, 1)); // HOST3D_GUEST not supported
    assert(!native::nativeBlob(true, 2, 9)); // CREATE_GUEST_HANDLE never native
    assert(!native::nativeBlob(true, 2, 5)); // CROSS_DEVICE not supported
    assert(!native::nativeBlob(true, 2, 0));
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
    native::Reclaimer::get().drain();
    // drain includes callbacks posted by callbacks, not just a FIFO sentinel.
    std::atomic<unsigned> drained{0};
    native::Reclaimer::get().post([&drained] {
        native::Reclaimer::get().post([&drained] { drained.fetch_add(1); });
        drained.fetch_add(1);
    });
    native::Reclaimer::get().drain();
    assert(drained.load() == 2);
    auto held = std::make_shared<native::Allocation>(nullptr, VK_NULL_HANDLE, VK_NULL_HANDLE,
                                                   4096, 100, unmap, freeMemory);
    held->mapped(reinterpret_cast<void*>(uintptr_t(0x10000)));
    native::MapState active;
    assert(active.map(*held, 4096, 4096));
    native::MapState moved(std::move(active));
    assert(!active.active() && moved.active());
    std::weak_ptr<native::Allocation> quarantined = held;
    native::quarantine(std::move(held));
    native::Reclaimer::get().drain();
    assert(!quarantined.expired()); // forced drop without detach never frees backing
    std::cout << "PASS: shared production Allocation/MapState/Lifetime; 12 free/map/resource orderings, multiple owners, never-mapped free, unmap/remap, device-instance retirement, overflow/alignment, nested drain and forced-drop quarantine; readiness false\n";
}
