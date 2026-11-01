#pragma once
#include <cstdint>
#include <exception>
#include <limits>
#include <vulkan/vulkan.h>

namespace gfxstream::host::vk::staging {
// Bounded, externally serialized, one-range/one-epoch contract; not a wire protocol
// implementation. No fd import, cache syscall, Vulkan submission or fence is invented here.
struct Range { VkDeviceSize offset = 0, size = 0; };
struct Backing {
    // Negotiated object identity + generation, NOT an fd number or virtual address.
    uint64_t object = 0, generation = 0;
    VkDeviceSize allocationSize = 0, mappedOffset = 0, mappedSize = 0;
};
inline bool sameBacking(const Backing& a, const Backing& b) {
    return a.object == b.object && a.generation == b.generation &&
        a.allocationSize == b.allocationSize && a.mappedOffset == b.mappedOffset &&
        a.mappedSize == b.mappedSize;
}
inline bool validBacking(const Backing& b) {
    return b.object && b.generation && b.allocationSize &&
        b.allocationSize != VK_WHOLE_SIZE && b.mappedSize &&
        b.mappedOffset < b.allocationSize &&
        b.mappedSize <= b.allocationSize - b.mappedOffset;
}
inline bool contains(Range outer, Range inner) {
    return inner.offset >= outer.offset && inner.size <= outer.size &&
        inner.offset - outer.offset <= outer.size - inner.size;
}
// Allocation-relative ranges. Expand to nonCoherentAtomSize without overflowing;
// a non-multiple size is legal only at the actual allocation tail, not a map tail.
inline VkResult normalizeRange(VkDeviceSize allocation, VkDeviceSize atom,
                               VkDeviceSize maximum, VkDeviceSize offset,
                               VkDeviceSize size, Range* out) {
    if (!out || !allocation || allocation == VK_WHOLE_SIZE || !atom ||
        (atom & (atom - 1)) || !maximum || offset >= allocation) {
        return VK_ERROR_VALIDATION_FAILED_EXT;
    }
    if (size == VK_WHOLE_SIZE) size = allocation - offset;
    if (!size || size > allocation - offset) return VK_ERROR_VALIDATION_FAILED_EXT;
    const VkDeviceSize end = offset + size; // Proven <= allocation above.
    const VkDeviceSize start = offset - offset % atom;
    VkDeviceSize alignedEnd = end;
    const VkDeviceSize remainder = end % atom;
    if (remainder) {
        const VkDeviceSize padding = atom - remainder;
        alignedEnd = padding > allocation - end ? allocation : end + padding;
    }
    if (alignedEnd - start > maximum) return VK_ERROR_VALIDATION_FAILED_EXT;
    *out = {start, alignedEnd - start};
    return VK_SUCCESS;
}
struct Completion {
    uint64_t epoch = 0;
    Range coverage{};
    bool complete = false;
};
using Transfer = VkResult (*)(void*, uint64_t, Range);
using Complete = VkResult (*)(void*, uint64_t, Range, Completion*);
struct Callbacks {
    void* context = nullptr;
    // Upload: staging copy -> flush actual GPU backing -> permit submission.
    // Download: proven GPU completion -> invalidate actual GPU backing -> copy
    // into staging -> return success -> permit guest publication/readability.
    // Never copy to guest-visible bytes before invalidating the GPU backing.
    // A backend without these operations must leave callbacks null / return error.
    Transfer uploadAndFlush = nullptr;
    Transfer submitGpu = nullptr;
    Complete completion = nullptr;
    Transfer downloadAndInvalidate = nullptr;
};
// Coverage bits are self-attestation by a future paired implementation, NOT
// runtime proof that interception is complete, and cannot be enabled by env.
// Production currently supplies no attestation/callbacks and rejects opt-in use.
// Every guest write, submission, fence poll/wait, queue/device idle, guest read/map,
// cross-queue/timeline/local-sync-fd wait, persistent CPU read and destruction must
// be intercepted/proven. A single fence callback is insufficient.
enum Coverage : uint32_t {
    GuestWrites = 1, Submissions = 2, FencePollAndWait = 4, QueueIdle = 8,
    DeviceIdle = 16, GuestReadsAndMaps = 32, Destruction = 64,
    CrossQueueAndTimeline = 128, GuestLocalSyncFd = 256, PersistentMappedReads = 512,
    AllCoverage = 1023
};
struct Readiness {
    uint32_t hostProtocol = 0, guestProtocol = 0, coverage = 0;
    uint64_t hostPair = 0, guestPair = 0;
    bool directMapping = false;
    Backing staging{}, expectedStaging{}, gpu{}, expectedGpu{};
    VkDeviceSize atom = 0, maximumRange = 0;
    Callbacks callbacks{};
};
inline VkResult checkReadiness(const Readiness& r, const char** reason = nullptr) {
    auto fail = [&](const char* why) {
        if (reason) *reason = why;
        return VK_ERROR_FEATURE_NOT_PRESENT;
    };
    if (r.hostProtocol != 1 || r.guestProtocol != 1 || !r.hostPair ||
        r.hostPair != r.guestPair) return fail("paired protocol v1 not negotiated");
    if (r.directMapping) return fail("direct guest mapping bypasses staging interception");
    if (r.coverage != AllCoverage) return fail("completion/read/write/lifetime coverage incomplete");
    if (!validBacking(r.staging) || !validBacking(r.gpu) ||
        !sameBacking(r.staging, r.expectedStaging) || !sameBacking(r.gpu, r.expectedGpu) ||
        r.staging.object == r.gpu.object ||
        r.staging.allocationSize != r.gpu.allocationSize) {
        return fail("staging/GPU backing identity or mapping not proven");
    }
    if (!r.atom || (r.atom & (r.atom - 1)) || !r.maximumRange) {
        return fail("atom/range bounds unavailable");
    }
    if (!r.callbacks.uploadAndFlush || !r.callbacks.submitGpu || !r.callbacks.completion ||
        !r.callbacks.downloadAndInvalidate) return fail("paired backend callbacks unavailable");
    if (reason) *reason = "ready";
    return VK_SUCCESS;
}
inline VkResult allocationGate(bool enabled, bool emulated, bool guestCoherent,
                               const Readiness& r, const char** reason = nullptr) {
    if (!enabled || !emulated || !guestCoherent) return VK_SUCCESS;
    return checkReadiness(r, reason);
}
struct Ownership {
    void* context = nullptr;
    int fd = -1;
    bool poolReserved = false;
    uint64_t poolOffset = 0, poolSize = 0;
    VkResult (*closeFd)(void*, int) = nullptr;
    VkResult (*releasePool)(void*, uint64_t, uint64_t) = nullptr;
};
class Contract {
public:
    enum class State { Idle, GuestDirty, Uploaded, GpuPending, Completed, Readable, Failed, Retired };
    // Ownership transfers here even when readiness fails. Cleanup callbacks are
    // required for any owned resource. No copying/moving or implicit fd transfer.
    Contract(Readiness readiness, Ownership ownership = {}) : mReady(readiness), mOwn(ownership) {}
    Contract(const Contract&) = delete;
    Contract& operator=(const Contract&) = delete;
    ~Contract() {
        // Explicit retire() rejects in-flight destruction. Silently freeing or
        // leaking a pending backing in the C++ destructor would hide a caller bug.
        if (mPending) std::terminate();
        retire();
    }
    State state() const { return mState; }
    uint64_t epoch() const { return mEpoch; }
    Range range() const { return mRange; }
    bool inFlight() const { return mPending; }
    VkResult guestWrite(const Backing& identity, VkDeviceSize offset, VkDeviceSize size) {
        if (mState != State::Idle && mState != State::Readable) return invalid();
        if (checkReadiness(mReady) != VK_SUCCESS) return VK_ERROR_FEATURE_NOT_PRESENT;
        if (!sameBacking(identity, mReady.staging)) return invalid();
        Range range;
        const VkResult result = normalizeRange(mReady.gpu.allocationSize, mReady.atom,
                                               mReady.maximumRange, offset, size, &range);
        if (result != VK_SUCCESS) return result;
        if (!contains({mReady.staging.mappedOffset, mReady.staging.mappedSize}, range) ||
            !contains({mReady.gpu.mappedOffset, mReady.gpu.mappedSize}, range) ||
            mEpoch == std::numeric_limits<uint64_t>::max()) return invalid();
        mRange = range;
        ++mEpoch;
        mState = State::GuestDirty; // Revokes readability BEFORE guest writes.
        return VK_SUCCESS;
    }
    VkResult upload(uint64_t epoch) {
        if (!matches(epoch, State::GuestDirty)) return invalid();
        return transfer(mReady.callbacks.uploadAndFlush, State::Uploaded);
    }
    VkResult submit(uint64_t epoch) {
        if (!matches(epoch, State::Uploaded)) return invalid();
        // A failed submit may still have queued work. Only completion can drain it.
        mPending = true;
        return transfer(mReady.callbacks.submitGpu, State::GpuPending);
    }
    VkResult complete(uint64_t epoch) {
        if (!mPending || epoch != mEpoch) return invalid();
        Completion proof{};
        const VkResult result = mReady.callbacks.completion
            ? mReady.callbacks.completion(mReady.callbacks.context, epoch, mRange, &proof)
            : VK_ERROR_FEATURE_NOT_PRESENT;
        if (result == VK_NOT_READY || result == VK_TIMEOUT) return result;
        if (result != VK_SUCCESS) { mState = State::Failed; return result; }
        if (!proof.complete || proof.epoch != epoch || !proof.coverage.size ||
            proof.coverage.offset >= mReady.gpu.allocationSize ||
            proof.coverage.size > mReady.gpu.allocationSize - proof.coverage.offset ||
            !contains(proof.coverage, mRange)) return VK_NOT_READY;
        mPending = false;
        if (mState == State::Failed) return VK_ERROR_INITIALIZATION_FAILED; // Drained, poisoned.
        mState = State::Completed;
        return VK_SUCCESS;
    }
    VkResult download(uint64_t epoch) {
        if (!matches(epoch, State::Completed)) return invalid();
        return transfer(mReady.callbacks.downloadAndInvalidate, State::Readable);
    }
    bool guestReadable(uint64_t epoch) const { return matches(epoch, State::Readable); }
    VkResult retire() {
        if (mPending) return VK_NOT_READY;
        if (mState == State::Retired) return mRetireResult;
        mState = State::Retired; // Consume ownership BEFORE invoking either cleanup.
        if (mOwn.fd >= 0) {
            const int fd = mOwn.fd;
            mOwn.fd = -1;
            mRetireResult = mOwn.closeFd ? mOwn.closeFd(mOwn.context, fd)
                                        : VK_ERROR_INITIALIZATION_FAILED;
        }
        if (mOwn.poolReserved) {
            mOwn.poolReserved = false;
            const VkResult result = mOwn.releasePool
                ? mOwn.releasePool(mOwn.context, mOwn.poolOffset, mOwn.poolSize)
                : VK_ERROR_INITIALIZATION_FAILED;
            if (mRetireResult == VK_SUCCESS) mRetireResult = result;
        }
        return mRetireResult;
    }
private:
    static VkResult invalid() { return VK_ERROR_VALIDATION_FAILED_EXT; }
    bool matches(uint64_t epoch, State state) const {
        return epoch && epoch == mEpoch && mState == state;
    }
    VkResult transfer(Transfer callback, State success) {
        const VkResult result = callback
            ? callback(mReady.callbacks.context, mEpoch, mRange) : VK_ERROR_FEATURE_NOT_PRESENT;
        // Any non-success poisons this epoch; never expose partially copied data.
        mState = result == VK_SUCCESS ? success : State::Failed;
        return result;
    }
    Readiness mReady;
    Ownership mOwn;
    State mState = State::Idle;
    uint64_t mEpoch = 0;
    Range mRange{};
    bool mPending = false;
    VkResult mRetireResult = VK_SUCCESS;
};
} // namespace gfxstream::host::vk::staging
