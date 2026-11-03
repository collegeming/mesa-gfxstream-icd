#include "host_memory_contract.h"
#include <algorithm>
#include <cstddef>
#include <cstdlib>
#include <iostream>
#include <limits>
#include <map>
#include <memory>
#include <set>
#include <vector>

using namespace gfxstream::host::vk;
namespace {
// A failed expectation names its own source line; a silent abort costs a CI round trip.
#define check(ok)                                                         \
    do {                                                                  \
        if (!(ok)) {                                                      \
            std::cerr << "FAIL " << __FILE__ << ":" << __LINE__ << '\n';   \
            std::abort();                                                 \
        }                                                                 \
    } while (0)
int queries = 0, scenarios = 0;
uint32_t queryBits = 0;
VkResult queryResult = VK_SUCCESS;
int backing = 0;
VKAPI_ATTR VkResult VKAPI_CALL mockQuery(
    VkDevice, VkExternalMemoryHandleTypeFlagBits type, const void* ptr,
    VkMemoryHostPointerPropertiesEXT* props) {
    check(type == VK_EXTERNAL_MEMORY_HANDLE_TYPE_HOST_ALLOCATION_BIT_EXT);
    check(ptr == &backing);
    check(props->sType == VK_STRUCTURE_TYPE_MEMORY_HOST_POINTER_PROPERTIES_EXT);
    check(props->pNext == nullptr && props->memoryTypeBits == 0);
    ++queries;
    props->memoryTypeBits = queryBits;
    return queryResult;
}
void pointerCase(uint32_t bits, uint32_t index, VkResult expected,
                 PFN_vkGetMemoryHostPointerPropertiesEXT query = mockQuery,
                 const void* ptr = &backing,
                 VkExternalMemoryHandleTypeFlagBits type =
                     VK_EXTERNAL_MEMORY_HANDLE_TYPE_HOST_ALLOCATION_BIT_EXT,
                 VkResult result = VK_SUCCESS, bool appended = true) {
    ++scenarios;
    queries = 0;
    queryBits = bits;
    queryResult = result;
    VkImportMemoryHostPointerInfoEXT importInfo = {
        VK_STRUCTURE_TYPE_IMPORT_MEMORY_HOST_POINTER_INFO_EXT, nullptr, type,
        const_cast<void*>(ptr)};
    VkMemoryAllocateFlagsInfo flags = {
        VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_FLAGS_INFO, nullptr,
        VK_MEMORY_ALLOCATE_DEVICE_ADDRESS_BIT, 0};
    VkMemoryAllocateInfo allocation = {
        VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO, &flags, 4096, index};
    if (appended) flags.pNext = &importInfo; // Null pointer still is an appended import.
    const auto original = allocation;
    const auto originalFlags = flags.flags;
    VkDeviceMemory memory = reinterpret_cast<VkDeviceMemory>(uintptr_t{123});
    uint32_t reported = ~uint32_t{0};
    VkResult reportedResult = VK_SUCCESS;
    int driverCalls = 0;
    const auto validation = validateHostPointerImport(
        VK_NULL_HANDLE, appended ? &importInfo : nullptr, allocation.memoryTypeIndex,
        query, &reported, &reportedResult);
    if (validation == VK_SUCCESS) ++driverCalls;
    else memory = VK_NULL_HANDLE;
    check(validation == expected);
    check(driverCalls == (expected == VK_SUCCESS ? 1 : 0));
    if (expected != VK_SUCCESS) check(memory == VK_NULL_HANDLE);
    check(allocation.memoryTypeIndex == original.memoryTypeIndex);
    check(allocation.allocationSize == original.allocationSize);
    check(allocation.pNext == original.pNext && flags.flags == originalFlags);
    const bool queried = appended && query && ptr && index < 32 &&
        type == VK_EXTERNAL_MEMORY_HANDLE_TYPE_HOST_ALLOCATION_BIT_EXT;
    check(queries == (queried ? 1 : 0));
    check(reported == (queried ? bits : 0));
    check(reportedResult == (queried ? result : VK_ERROR_EXTENSION_NOT_PRESENT));
}
struct Owner {
    int& releases;
    explicit Owner(int& count) : releases(count) {}
    ~Owner() { ++releases; }
};
void borrowedLifetime(bool compatible, bool privateOwner) {
    ++scenarios;
    int releases = 0;
    {
        auto owner = std::make_shared<Owner>(releases);
        auto alias = owner; // No ownership is passed to the helper.
        VkImportMemoryHostPointerInfoEXT info = {
            VK_STRUCTURE_TYPE_IMPORT_MEMORY_HOST_POINTER_INFO_EXT, nullptr,
            VK_EXTERNAL_MEMORY_HANDLE_TYPE_HOST_ALLOCATION_BIT_EXT, &backing};
        queryBits = compatible ? 2 : 0;
        queryResult = VK_SUCCESS;
        uint32_t bits = 0;
        check(validateHostPointerImport(VK_NULL_HANDLE, &info, 1, mockQuery, &bits) ==
              (compatible ? VK_SUCCESS : VK_ERROR_INVALID_EXTERNAL_HANDLE));
        check(releases == 0 && owner.use_count() == 2);
        if (privateOwner) alias.reset(); // Private/shared-memory scope releases on return.
        else owner.reset(); // External alias retains its independent owner.
        check(releases == 0);
    }
    check(releases == 1);
}
void coherentCase(VkMemoryPropertyFlags guest, VkMemoryPropertyFlags physical,
                  VkResult expected, bool diagnosticEnabled) {
    ++scenarios;
    const auto originalGuest = guest;
    const auto originalPhysical = physical;
    int resources = 0, driverCalls = 0, diagnosticCalls = 0;
    VkDeviceMemory memory = reinterpret_cast<VkDeviceMemory>(uintptr_t{123});
    const VkResult result = validateHostCoherentContract(guest, physical);
    if (result == VK_SUCCESS) {
        // Production's optional diagnostic gate is strictly later. Never a bypass.
        if (diagnosticEnabled) ++diagnosticCalls;
        ++resources;
        ++driverCalls;
    } else memory = VK_NULL_HANDLE;
    check(result == expected);
    check(guest == originalGuest && physical == originalPhysical);
    check(resources == (expected == VK_SUCCESS ? 1 : 0));
    check(driverCalls == resources);
    if (expected != VK_SUCCESS) check(memory == VK_NULL_HANDLE && diagnosticCalls == 0);
}

// --- Emulated coherent coverage -------------------------------------------------------------
// The two index spaces the preflight must never mix: the guest index a vkAllocateMemory call
// carries, and the host index a ColorBuffer/Buffer stores. Modelled as plain arrays so the
// regression is the arithmetic and not the decoder.
constexpr uint32_t kTypes = 32;
struct IndexSpaces {
    int guestToHost[kTypes];
};

int physicalHostIndex(const IndexSpaces& spaces, bool hasAliasHostIndex, uint32_t aliasHostIndex,
                      uint32_t guestIndex) {
    // A stored alias index is already host space and must not be mapped a second time.
    if (hasAliasHostIndex) return aliasHostIndex < kTypes ? static_cast<int>(aliasHostIndex) : -1;
    if (guestIndex >= kTypes) return -1;
    return spaces.guestToHost[guestIndex];
}

// What the guard used to do: feed the stored host index back through the guest->host map.
int remappedHostIndex(const IndexSpaces& spaces, bool hasAliasHostIndex, uint32_t aliasHostIndex,
                      uint32_t guestIndex) {
    const uint32_t looked = hasAliasHostIndex ? aliasHostIndex : guestIndex;
    if (looked >= kTypes) return -1;
    return spaces.guestToHost[looked];
}

void aliasIndexSpaceCase() {
    ++scenarios;
    IndexSpaces spaces{};
    for (auto& entry : spaces.guestToHost) entry = -1;
    spaces.guestToHost[6] = 1;  // guest coherent type 6 is backed by host cached type 1
    spaces.guestToHost[1] = 0;  // guest type 1 routes somewhere else entirely
    const uint32_t aliasHostIndex = 1;  // what the ColorBuffer stored
    const uint32_t guestIndex = 6;      // what the guest asked for
    check(physicalHostIndex(spaces, true, aliasHostIndex, guestIndex) == 1);
    check(physicalHostIndex(spaces, false, aliasHostIndex, guestIndex) == 1);
    // The old lookup answers 0 for the same allocation -- a different physical type, and with
    // it a different verdict. Keeping the two apart is the whole point.
    check(remappedHostIndex(spaces, true, aliasHostIndex, guestIndex) == 0);
    check(physicalHostIndex(spaces, true, aliasHostIndex, guestIndex) !=
          remappedHostIndex(spaces, true, aliasHostIndex, guestIndex));
    // Out-of-range inputs stay rejected on both paths.
    check(physicalHostIndex(spaces, true, kTypes, guestIndex) == -1);
    check(physicalHostIndex(spaces, false, aliasHostIndex, kTypes) == -1);
}

void coverageCase(bool feature, VkMemoryPropertyFlags physical, EmulatedAllocationShape shape,
                  bool expected) {
    ++scenarios;
    const auto originalPhysical = physical;
    check(emulationCoversThisAllocation(feature, physical, shape) == expected);
    check(physical == originalPhysical); // The predicate never rewrites the physical flags.
}

void emulatedContractCase(VkMemoryPropertyFlags guest, VkMemoryPropertyFlags physical,
                          bool covered, VkResult expected) {
    ++scenarios;
    const auto originalGuest = guest;
    const auto originalPhysical = physical;
    check(validateHostCoherentContractEmulated(guest, physical, covered) == expected);
    check(guest == originalGuest && physical == originalPhysical);
    if (!covered) {
        // An uncovered allocation is byte-for-byte the old contract.
        check(validateHostCoherentContractEmulated(guest, physical, false) ==
              validateHostCoherentContract(guest, physical));
    }
}

// --- Image range bookkeeping ----------------------------------------------------------------
void checkSize(std::size_t actual, std::size_t expected) { check(actual == expected); }

// Mirrors MemoryInfo's two range maps: binding records a range, destroying the object removes
// it, and the memory only becomes cache-maintenance eligible while at least one range exists.
struct RangeBookkeeping {
    std::map<uint64_t, uint64_t> bufferRanges; // object -> offset
    std::map<uint64_t, uint64_t> imageRanges;
    bool tracked() const { return !bufferRanges.empty() || !imageRanges.empty(); }
    void bindBuffer(uint64_t object, uint64_t offset) { bufferRanges[object] = offset; }
    void bindImage(uint64_t object, uint64_t offset) { imageRanges[object] = offset; }
    void destroyBuffer(uint64_t object) { bufferRanges.erase(object); }
    void destroyImage(uint64_t object) { imageRanges.erase(object); }
};

void imageRangeCase() {
    ++scenarios;
    RangeBookkeeping book;
    check(!book.tracked());
    book.bindImage(7, 0);
    check(book.tracked() && book.bufferRanges.empty());
    checkSize(book.imageRanges.size(), 1);
    book.bindImage(8, 4096);
    checkSize(book.imageRanges.size(), 2);
    book.bindBuffer(3, 0);
    check(book.tracked());
    checkSize(book.imageRanges.size(), 2);
    checkSize(book.bufferRanges.size(), 1);
    book.destroyImage(7);
    check(book.tracked()); // The buffer keeps the memory eligible.
    checkSize(book.imageRanges.size(), 1);
    book.destroyImage(8);
    book.destroyBuffer(3);
    check(!book.tracked());
    // A rebind of the same object replaces, it does not accumulate.
    book.bindImage(9, 0);
    book.bindImage(9, 8192);
    checkSize(book.imageRanges.size(), 1);
    check(book.imageRanges[9] == uint64_t{8192});
}

// --- Flush/invalidate ordering --------------------------------------------------------------
// Models the production submit path: clean before dispatch, record against the fence the guest
// waits on, invalidate only once that fence is observed signalled. A record is never consumed
// by a wait that does not prove completion.
struct CompletionModel {
    bool feature = true;
    std::map<uint64_t, std::vector<uint64_t>> pending; // fence -> memories
    std::set<uint64_t> signalled;
    std::vector<uint64_t> invalidated;
    int premature = 0;
    int dropped = 0;
    std::size_t cap = 64;

    void submit(uint64_t fence, const std::vector<uint64_t>& memories, bool hostMapped,
                bool rangeBound) {
        if (!feature || !hostMapped || !rangeBound || memories.empty()) return;
        auto it = pending.find(fence);
        if (it == pending.end()) {
            if (pending.size() >= cap) {
                ++dropped; // Degrade to no invalidate; never invalidate unfinished work.
                return;
            }
            it = pending.emplace(fence, std::vector<uint64_t>{}).first;
        }
        for (uint64_t memory : memories) {
            if (std::find(it->second.begin(), it->second.end(), memory) == it->second.end()) {
                it->second.push_back(memory);
            }
        }
    }

    void signal(uint64_t fence) { signalled.insert(fence); }

    void waitAndInvalidate(const std::vector<uint64_t>& fences) {
        for (uint64_t fence : fences) {
            auto it = pending.find(fence);
            if (it == pending.end()) continue;
            if (!signalled.count(fence)) {
                ++premature; // Production asks the driver instead of assuming.
                continue;
            }
            invalidated.insert(invalidated.end(), it->second.begin(), it->second.end());
            pending.erase(it);
        }
    }

    void queueIdle() {
        for (auto it = pending.begin(); it != pending.end();) {
            invalidated.insert(invalidated.end(), it->second.begin(), it->second.end());
            it = pending.erase(it);
        }
    }
};

void completionOrderingCase() {
    ++scenarios;
    CompletionModel model;
    model.submit(1, {10, 11}, true, true);
    check(model.invalidated.empty()); // Nothing is invalidated at submit time.
    // A wait that has not observed completion must not invalidate.
    model.waitAndInvalidate({1});
    check(model.premature == 1 && model.invalidated.empty());
    checkSize(model.pending.count(1), 1);
    model.signal(1);
    model.waitAndInvalidate({1});
    check(model.invalidated == std::vector<uint64_t>({10, 11}));
    check(model.pending.empty());
    // The same fence reused after a reset merges instead of losing the earlier list.
    model.submit(2, {20}, true, true);
    model.submit(2, {21}, true, true);
    checkSize(model.pending[2].size(), 2);
    model.signal(2);
    model.waitAndInvalidate({2});
    checkSize(model.invalidated.size(), 4);
    // Unmapped or unbound memory is never recorded, so it can never be invalidated.
    model.submit(3, {30}, false, true);
    model.submit(4, {40}, true, false);
    model.signal(3);
    model.signal(4);
    model.waitAndInvalidate({3, 4});
    checkSize(model.invalidated.size(), 4);
    check(model.pending.empty());
    // The cap drops rather than invalidates early.
    CompletionModel bounded;
    bounded.cap = 2;
    bounded.submit(1, {1}, true, true);
    bounded.submit(2, {2}, true, true);
    bounded.submit(3, {3}, true, true);
    check(bounded.dropped == 1);
    checkSize(bounded.pending.size(), 2);
    bounded.waitAndInvalidate({1, 2, 3});
    check(bounded.premature == 2 && bounded.invalidated.empty());
    bounded.queueIdle();
    checkSize(bounded.invalidated.size(), 2);
    check(bounded.pending.empty());
}
}
int main() {
    for (uint32_t mask : {0x2u, 0x7e02u}) {
        pointerCase(mask, 8, VK_ERROR_INVALID_EXTERNAL_HANDLE);
        pointerCase(mask, 1, VK_SUCCESS);
        pointerCase(mask, 14, mask == 0x7e02 ? VK_SUCCESS : VK_ERROR_INVALID_EXTERNAL_HANDLE);
        pointerCase(mask, 31, VK_ERROR_INVALID_EXTERNAL_HANDLE);
        pointerCase(mask, 32, VK_ERROR_INVALID_EXTERNAL_HANDLE);
        pointerCase(mask, std::numeric_limits<uint32_t>::max(), VK_ERROR_INVALID_EXTERNAL_HANDLE);
    }
    pointerCase(0x80000000u, 31, VK_SUCCESS);
    pointerCase(0x80000000u, 32, VK_ERROR_INVALID_EXTERNAL_HANDLE);
    pointerCase(2, 1, VK_ERROR_INVALID_EXTERNAL_HANDLE, nullptr);
    pointerCase(2, 1, VK_ERROR_INVALID_EXTERNAL_HANDLE, mockQuery, nullptr);
    pointerCase(2, 1, VK_ERROR_INVALID_EXTERNAL_HANDLE, mockQuery, &backing,
                VK_EXTERNAL_MEMORY_HANDLE_TYPE_HOST_ALLOCATION_BIT_EXT, VK_ERROR_DEVICE_LOST);
    pointerCase(0, 1, VK_ERROR_INVALID_EXTERNAL_HANDLE);
    pointerCase(2, 1, VK_ERROR_INVALID_EXTERNAL_HANDLE, mockQuery, &backing,
                VK_EXTERNAL_MEMORY_HANDLE_TYPE_HOST_MAPPED_FOREIGN_MEMORY_BIT_EXT);
    pointerCase(2, 1, VK_ERROR_INVALID_EXTERNAL_HANDLE, mockQuery, &backing,
                VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT);
    pointerCase(0, 8, VK_SUCCESS, nullptr, nullptr,
                VK_EXTERNAL_MEMORY_HANDLE_TYPE_HOST_ALLOCATION_BIT_EXT, VK_SUCCESS, false);
    for (bool allowed : {false, true}) {
        borrowedLifetime(allowed, true);
        borrowedLifetime(allowed, false);
    }
    const VkMemoryPropertyFlags nc = VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT |
                                     VK_MEMORY_PROPERTY_HOST_CACHED_BIT;
    const VkMemoryPropertyFlags coherent = nc | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT;
    for (bool diagnostic : {false, true}) {
        coherentCase(coherent, nc, VK_ERROR_FEATURE_NOT_PRESENT, diagnostic);
        coherentCase(coherent, coherent, VK_SUCCESS, diagnostic);
        coherentCase(nc, nc, VK_SUCCESS, diagnostic);
        coherentCase(nc, coherent, VK_SUCCESS, diagnostic);
        coherentCase(VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT, nc, VK_SUCCESS, diagnostic);
    }

    // Alias host index regression: the stored host index is never re-mapped as a guest index.
    aliasIndexSpaceCase();

    // Coverage matrix: the waiver needs the feature, a HOST_CACHED physical type and a tracked
    // shape. Any one missing keeps the allocation rejected.
    const VkMemoryPropertyFlags cached = VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT |
                                         VK_MEMORY_PROPERTY_HOST_CACHED_BIT;
    const VkMemoryPropertyFlags cachedCoherent = cached | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT;
    // Coherent but not cached: the Mali type 0/3-8 family, which the waiver must never cover.
    const VkMemoryPropertyFlags uncachedCoherent =
        VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT;
    for (bool feature : {false, true}) {
        for (VkMemoryPropertyFlags physical : {cached, cachedCoherent, uncachedCoherent}) {
            for (EmulatedAllocationShape shape : {EmulatedAllocationShape::kNone,
                                                  EmulatedAllocationShape::kBufferRange,
                                                  EmulatedAllocationShape::kImageRange}) {
                const bool expected =
                    feature && (physical & VK_MEMORY_PROPERTY_HOST_CACHED_BIT) != 0 &&
                    shape != EmulatedAllocationShape::kNone;
                coverageCase(feature, physical, shape, expected);
            }
        }
    }
    // Emulation-covered allow: guest coherent, physical cached non-coherent, tracked shape.
    emulatedContractCase(coherent, cached, true, VK_SUCCESS);
    // Non-covered reject: the same allocation without coverage is the original -8.
    emulatedContractCase(coherent, cached, false, VK_ERROR_FEATURE_NOT_PRESENT);
    // The predicate never covers a type without HOST_CACHED, so no waiver exists for the
    // coherent-but-uncached family. Such a type still satisfies the guest on its own merits:
    // the contract is about the physical flags, not about which types the feature rewrites.
    check(!emulationCoversThisAllocation(true, uncachedCoherent,
                                         EmulatedAllocationShape::kImageRange));
    emulatedContractCase(coherent, uncachedCoherent, false, VK_SUCCESS);
    emulatedContractCase(coherent, coherent, false, VK_SUCCESS);
    // Uncached and non-coherent: no waiver, and the physical type does not satisfy the guest.
    const VkMemoryPropertyFlags uncachedNonCoherent = VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT;
    check(!emulationCoversThisAllocation(true, uncachedNonCoherent,
                                         EmulatedAllocationShape::kImageRange));
    emulatedContractCase(coherent, uncachedNonCoherent, false, VK_ERROR_FEATURE_NOT_PRESENT);
    // Non-coherent guests never needed the waiver.
    emulatedContractCase(nc, cached, false, VK_SUCCESS);
    emulatedContractCase(nc, cached, true, VK_SUCCESS);

    imageRangeCase();
    completionOrderingCase();

    std::cout << "PASS: " << scenarios << " host memory contract scenarios; exact shared helpers, "
              << "no index/flags rewrite, reject before dispatch/resources, borrowed RAII once; "
              << "alias host index never re-mapped, emulation waiver bounded to cached+tracked, "
              << "image ranges tracked, invalidate only after completion; no GPU\n";
}
