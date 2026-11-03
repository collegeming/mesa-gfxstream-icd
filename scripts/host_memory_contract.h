#pragma once
#include <cstdint>
#include <vulkan/vulkan.h>

namespace gfxstream::host::vk {
// What the decoder can maintain cache state for. VulkanDisableCoherentMemoryAndEmulate only
// makes a cached physical type usable as coherent if the host can flush that allocation before
// a submit and invalidate it after the GPU work that wrote it has completed, and the decoder
// tracks exactly two shapes: buffer bindings and image bindings.
enum class EmulatedAllocationShape : uint32_t {
    kNone = 0,
    kBufferRange = 1,
    kImageRange = 2,
};

// The one waiver the emulation provides, stated as narrowly as it can be checked at allocation
// time. All three must hold:
//   - the feature is enabled, so the guest was actually told cached types are coherent;
//   - the physical type is HOST_CACHED, the only family this host can import and the only one
//     the feature rewrites;
//   - the allocation has a shape whose byte ranges the decoder tracks.
// Nothing here rewrites the index or adds HOST_COHERENT to the physical flags.
//
// The feature flag no longer gates the waiver. This host always maps a guest coherent
// type onto a cached non-coherent physical type (the guest is told the cached type is
// coherent), so the maintenance has to run in every configuration; requiring the flag
// only rejected allocations the decoder already knows how to maintain, e.g. the CIB1
// ColorBuffer aliases the compositor's swapchain needs. The flag remains an input so the
// call sites do not change, but coverage is decided by the physical type and the shape.
inline bool emulationCoversThisAllocation(bool /*emulationEnabled*/,
                                          VkMemoryPropertyFlags physicalHostFlags,
                                          EmulatedAllocationShape shape) {
    return (physicalHostFlags & VK_MEMORY_PROPERTY_HOST_CACHED_BIT) != 0 &&
           shape != EmulatedAllocationShape::kNone;
}

// This is rejection, not coherent emulation. Neither flags nor index are rewritten.
inline VkResult validateHostCoherentContract(VkMemoryPropertyFlags guestFlags,
                                             VkMemoryPropertyFlags physicalHostFlags) {
    return (guestFlags & VK_MEMORY_PROPERTY_HOST_COHERENT_BIT) &&
           !(physicalHostFlags & VK_MEMORY_PROPERTY_HOST_COHERENT_BIT)
        ? VK_ERROR_FEATURE_NOT_PRESENT : VK_SUCCESS;
}

// Same contract with the emulation waiver applied. The caller decides coverage with
// emulationCoversThisAllocation(); a false waiver is byte-for-byte the old reject.
inline VkResult validateHostCoherentContractEmulated(VkMemoryPropertyFlags guestFlags,
                                                     VkMemoryPropertyFlags physicalHostFlags,
                                                     bool emulationCovers) {
    return !emulationCovers &&
           validateHostCoherentContract(guestFlags, physicalHostFlags) != VK_SUCCESS
        ? VK_ERROR_FEATURE_NOT_PRESENT : VK_SUCCESS;
}
// Validate the import found in the FINAL allocation chain, including a null pointer.
// This helper only borrows pHostPointer: it never frees/unmaps it or owns an fd/pool.
// Only HOST_ALLOCATION is intentionally supported by this host's import producers.
inline VkResult validateHostPointerImport(
    VkDevice device, const VkImportMemoryHostPointerInfoEXT* importInfo,
    uint32_t memoryTypeIndex, PFN_vkGetMemoryHostPointerPropertiesEXT query,
    uint32_t* reportedBits, VkResult* reportedQueryResult = nullptr) {
    *reportedBits = 0;
    if (reportedQueryResult) *reportedQueryResult = VK_ERROR_EXTENSION_NOT_PRESENT;
    if (!importInfo) return VK_SUCCESS;
    if (!query || !importInfo->pHostPointer || memoryTypeIndex >= 32 ||
        importInfo->handleType != VK_EXTERNAL_MEMORY_HANDLE_TYPE_HOST_ALLOCATION_BIT_EXT) {
        return VK_ERROR_INVALID_EXTERNAL_HANDLE;
    }
    VkMemoryHostPointerPropertiesEXT props = {
        VK_STRUCTURE_TYPE_MEMORY_HOST_POINTER_PROPERTIES_EXT, nullptr, 0};
    const VkResult result = query(device, importInfo->handleType, importInfo->pHostPointer, &props);
    *reportedBits = props.memoryTypeBits;
    if (reportedQueryResult) *reportedQueryResult = result;
    return result == VK_SUCCESS && (props.memoryTypeBits & (uint32_t{1} << memoryTypeIndex))
        ? VK_SUCCESS : VK_ERROR_INVALID_EXTERNAL_HANDLE;
}
} // namespace gfxstream::host::vk
