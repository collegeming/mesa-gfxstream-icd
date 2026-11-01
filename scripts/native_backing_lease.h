#pragma once
// Production native-allocation lifetime primitives. No wire-readiness assertion API.
#include <vulkan/vulkan.h>
#include <condition_variable>
#include <cstdint>
#include <cstddef>
#include <cstring>
#include <atomic>
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
        { std::lock_guard<std::mutex> lock(mMutex); ++mPending; mWork.push(std::move(work)); }
        mCv.notify_one();
    }
    // Only after producers/decoder threads have stopped, never under decoder or
    // manager locks. Includes child finalizers enqueued while parents retire.
    bool idle() { std::lock_guard<std::mutex> lock(mMutex); return mPending == 0; }
    void drain() {
        if (onWorker()) std::terminate(); // self-drain cannot make progress
        std::unique_lock<std::mutex> lock(mMutex);
        mIdle.wait(lock, [this] { return mPending == 0; });
    }
private:
    static bool& onWorker() { static thread_local bool value = false; return value; }
    Reclaimer() { std::thread([this] {
        onWorker() = true;
        for (;;) {
            std::function<void()> work;
            {
                std::unique_lock<std::mutex> lock(mMutex);
                mCv.wait(lock, [this] { return !mWork.empty(); });
                work = std::move(mWork.front()); mWork.pop();
            }
            work();
            work = {}; // release captured parents BEFORE declaring queue idle
            { std::lock_guard<std::mutex> lock(mMutex); --mPending; }
            mIdle.notify_all();
        }
    }).detach(); }
    std::mutex mMutex;
    std::condition_variable mCv;
    std::condition_variable mIdle;
    uint64_t mPending = 0;
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

inline constexpr uint64_t kLeaseParam = 0x44564d4c;
inline constexpr uint64_t kDetachMask = 0x0f;
struct LeaseDescriptor {
    uint32_t size;
    uint32_t version;
    uint64_t offered;
    uint64_t required;
    uint64_t hostSupported;
    uint64_t accepted;
    uint64_t epoch;
    uint8_t contractId[16];
};
static_assert(sizeof(LeaseDescriptor) == 64, "Native lease ABI size");
static_assert(offsetof(LeaseDescriptor, size) == 0 && offsetof(LeaseDescriptor, version) == 4 &&
    offsetof(LeaseDescriptor, offered) == 8 && offsetof(LeaseDescriptor, required) == 16 &&
    offsetof(LeaseDescriptor, hostSupported) == 24 && offsetof(LeaseDescriptor, accepted) == 32 &&
    offsetof(LeaseDescriptor, epoch) == 40 && offsetof(LeaseDescriptor, contractId) == 48,
    "Native lease ABI offsets");
inline constexpr uint8_t kContractId[16] = {
    0x44,0x56,0x4d,0x4e,0x4c,0x45,0x41,0x53,0x45,0,0,0,0,0,0,1};
class Offer {
public:
    bool parse(uint64_t key, uint64_t value) {
        if (key != kLeaseParam) return true;
        if (mSeen) return false;
        mSeen = true;
        if (!value || value % alignof(LeaseDescriptor)) return false;
        mOutput = reinterpret_cast<LeaseDescriptor*>(static_cast<uintptr_t>(value));
        if (mOutput->size != sizeof(LeaseDescriptor) || mOutput->version != 1) return false;
        const auto d = *mOutput; // borrowed only during synchronous init
        mValid = d.size == sizeof(d) && d.version == 1 &&
            d.offered == kDetachMask && d.required == kDetachMask &&
            !d.hostSupported && !d.accepted && !d.epoch &&
            std::memcmp(d.contractId, kContractId, sizeof(kContractId)) == 0;
        return mValid;
    }
    bool valid() const { return mValid; }
    void acknowledge(uint64_t epoch) {
        if (mValid) {
            mOutput->hostSupported = kDetachMask;
            mOutput->epoch = epoch;
            mOutput->accepted = kDetachMask; // last: publish completed acknowledgement
        }
    }
private:
    bool mSeen = false;
    bool mValid = false;
    LeaseDescriptor* mOutput = nullptr; // Offer is stack-local in stream_renderer_init
};
class Session {
public:
    static Session& get() { static auto* s = new Session; return *s; }
    std::mutex lifecycle;
    bool begin() {
        if (mInitialized || mAllocations.load() || !Reclaimer::get().idle()) return false;
        mInitialized = true;
        const auto previous = mEpoch.fetch_add(1);
        if (previous == UINT64_MAX) std::terminate();
        return true;
    }
    uint64_t epoch() const { return mEpoch.load(); }
    void commit() { mNativeNegotiated = true; mReady.store(true); }
    bool snapshotBlocked() const {
        return mNativeNegotiated || mAllocations.load() || !Reclaimer::get().idle();
    }
    void revoke() { mReady.store(false); }
    void stopped() { mReady.store(false); mInitialized = false; mNativeNegotiated = false; }
    bool ready(uint64_t epoch) const { return epoch && mReady.load() && epoch == mEpoch.load(); }
    void allocated() { mAllocations.fetch_add(1); }
    void freed() { mAllocations.fetch_sub(1); }
private:
    bool mInitialized = false; // lifecycle lock
    bool mNativeNegotiated = false; // remains true after reset revocation until full stop
    std::atomic<uint64_t> mEpoch{0};
    std::atomic<uint64_t> mAllocations{0};
    std::atomic<bool> mReady{false};
};
inline bool publicationReady() { return Session::get().ready(Session::get().epoch()); }
class Initialization {
public:
    Initialization() : mLock(Session::get().lifecycle), mStarted(Session::get().begin()) {}
    ~Initialization() { if (mStarted && !mSuccess) Session::get().stopped(); }
    bool started() const { return mStarted; }
    void success(Offer& offer) {
        if (offer.valid()) { Session::get().commit(); offer.acknowledge(Session::get().epoch()); }
        mSuccess = true;
    }
private:
    std::unique_lock<std::mutex> mLock;
    bool mStarted;
    bool mSuccess = false;
};

class Allocation {
public:
    Allocation(std::shared_ptr<Lifetime> deviceLifetime, VkDevice device, VkDeviceMemory memory,
               uint64_t size, uint64_t generation, PFN_vkUnmapMemory unmap, PFN_vkFreeMemory free)
        : mDeviceLifetime(std::move(deviceLifetime)), mDevice(device), mMemory(memory),
          mSize(size), mGeneration(generation), mUnmap(unmap), mFree(free),
          mEpoch(Session::get().epoch()) { Session::get().allocated(); }
    Allocation(const Allocation&) = delete;
    Allocation& operator=(const Allocation&) = delete;
    void mapped(void* address) { mAddress = address; }
    void* address() const { return mAddress; }
    uint64_t size() const { return mSize; }
    uint64_t generation() const { return mGeneration; }
    uint64_t epoch() const { return mEpoch; }
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
            Session::get().freed();
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
    uint64_t mEpoch;
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

// Allocation metadata and resource-create flags are different protocol stages.
// The paired guest leaves VkCreateBlobGOOGLE.blobFlags zero on deferred HOST3D
// allocation; MAPPABLE is supplied later in virtio RESOURCE_CREATE_BLOB at map.
// HOST3D=2, MAPPABLE=1, SHAREABLE=2. Never accept guest/cross-device/unknown bits.
inline bool nativeBlob(bool hasBlob, uint32_t memory, uint32_t flags) {
    return !hasBlob || (memory == 2 && !(flags & ~uint32_t(3)));
}
inline bool nativeResource(uint32_t memory, uint32_t flags) {
    return memory == 2 && (flags & 1) && !(flags & ~uint32_t(3));
}

class MapState {
public:
    MapState() = default;
    MapState(const MapState&) = delete;
    MapState& operator=(const MapState&) = delete;
    MapState(MapState&& other) noexcept : mActive(std::exchange(other.mActive, false)) {}
    MapState& operator=(MapState&& other) noexcept {
        if (mActive) std::terminate(); // destination must quarantine before replacement
        mActive = std::exchange(other.mActive, false); return *this;
    }
    bool map(const Allocation& owner, uint64_t requested, uint64_t page) {
        if (mActive || !validExtent(owner, requested, page)) return false;
        mActive = true; return true;
    }
    bool unmap() { if (!mActive) return false; mActive = false; return true; }
    bool active() const { return mActive; }
private:
    bool mActive = false;
};
// Forced renderer drop is not evidence of VMM detach. Preserve uncertain live
// backing for process lifetime; only a future proven detach token may reclaim it.
inline void quarantine(std::shared_ptr<Allocation> owner) {
    if (!owner) return;
    struct Held { std::shared_ptr<Allocation> owner; Held* next; };
    static std::mutex* mutex = new std::mutex;
    static Held* head = nullptr;
    std::lock_guard<std::mutex> lock(*mutex);
    head = new Held{std::move(owner), head};
}
}}} // namespace gfxstream::host::native
