#pragma once
// Production native-allocation lifetime primitives. No wire-readiness assertion API.
#include <vulkan/vulkan.h>
#include <condition_variable>
#include <cstdint>
#include <functional>
#include <limits>
#include <memory>
#include <mutex>
#include <queue>
#include <thread>
#include <utility>

namespace gfxstream { namespace host { namespace native {
// The process-lifetime worker never waits for leases on a decoder command thread.
// Queued work owns all captured state; it cannot refer to decoder/MemoryInfo storage.
class Reclaimer {
public:
    static Reclaimer& get() { static auto* q = new Reclaimer; return *q; }
    void post(std::function<void()> work) {
        { std::lock_guard<std::mutex> lock(mMutex); mWork.push(std::move(work)); }
        mCv.notify_one();
    }
private:
    Reclaimer() { std::thread([this] {
        for (;;) {
            std::function<void()> work;
            {
                std::unique_lock<std::mutex> lock(mMutex);
                mCv.wait(lock, [this] { return !mWork.empty(); });
                work = std::move(mWork.front()); mWork.pop();
            }
            work();
        }
    }).detach(); }
    std::mutex mMutex;
    std::condition_variable mCv;
    std::queue<std::function<void()>> mWork;
};

// Device -> instance and allocation -> device strong edges. Destruction work is
// installed only at logical retirement, after ordinary device children are gone.
class Lifetime {
public:
    explicit Lifetime(std::shared_ptr<Lifetime> parent = {}) : mParent(std::move(parent)) {}
    void retire(std::function<void()> destroy) { mDestroy = std::move(destroy); }
    ~Lifetime() {
        auto parent = std::move(mParent);
        auto destroy = std::move(mDestroy);
        Reclaimer::get().post([parent = std::move(parent), destroy = std::move(destroy)] {
            if (destroy) destroy();
            // parent is released only AFTER the child's physical destruction.
        });
    }
private:
    std::shared_ptr<Lifetime> mParent;
    std::function<void()> mDestroy;
};

class Allocation {
public:
    Allocation(std::shared_ptr<Lifetime> deviceLifetime, VkDevice device, VkDeviceMemory memory,
               uint64_t size, uint64_t generation, PFN_vkUnmapMemory unmap, PFN_vkFreeMemory free)
        : mDeviceLifetime(std::move(deviceLifetime)), mDevice(device), mMemory(memory),
          mSize(size), mGeneration(generation), mUnmap(unmap), mFree(free) {}
    Allocation(const Allocation&) = delete;
    Allocation& operator=(const Allocation&) = delete;
    void mapped(void* address) { mAddress = address; }
    void* address() const { return mAddress; }
    uint64_t size() const { return mSize; }
    uint64_t generation() const { return mGeneration; }
    ~Allocation() {
        const auto device = mDevice;
        const auto memory = mMemory;
        const auto unmap = mUnmap;
        const auto free = mFree;
        const bool mapped = mAddress != nullptr;
        Reclaimer::get().post([lifetime = std::move(mDeviceLifetime), device, memory,
                               unmap, free, mapped] {
            if (mapped) unmap(device, memory);
            free(device, memory, nullptr);
        });
    }
private:
    std::shared_ptr<Lifetime> mDeviceLifetime;
    VkDevice mDevice;
    VkDeviceMemory mMemory;
    uint64_t mSize;
    uint64_t mGeneration;
    PFN_vkUnmapMemory mUnmap;
    PFN_vkFreeMemory mFree;
    void* mAddress = nullptr;
};

inline bool validExtent(const Allocation& owner, uint64_t requested, uint64_t page) {
    if (!owner.address() || !requested || !page || (page & (page - 1))) return false;
    const auto address = reinterpret_cast<uintptr_t>(owner.address());
    if (address % page || requested > std::numeric_limits<uint64_t>::max() - (page - 1))
        return false;
    const uint64_t rounded = (requested + page - 1) & ~(page - 1);
    return rounded <= owner.size() && rounded <= std::numeric_limits<uintptr_t>::max() - address;
}

// Not negotiated: existing renderer unmap has no trustworthy unregister proof.
// No environment variable, callback invocation or CPU test can turn this true.
inline constexpr bool publicationReady() { return false; }

// Stable virtio blob ABI values: HOST3D=2, MAPPABLE=1, SHAREABLE=2.
// Guest-handle imports, cross-device and unknown flags are not native backing.
inline bool nativeBlob(bool hasBlob, uint32_t memory, uint32_t flags) {
    return !hasBlob || (memory == 2 && (flags & 1) && !(flags & ~uint32_t(3)));
}

class MapState {
public:
    bool map(const Allocation& owner, uint64_t requested, uint64_t page) {
        if (mActive || !validExtent(owner, requested, page)) return false;
        mActive = true; return true;
    }
    bool unmap() { if (!mActive) return false; mActive = false; return true; }
    bool active() const { return mActive; }
private:
    bool mActive = false;
};
}}} // namespace gfxstream::host::native
