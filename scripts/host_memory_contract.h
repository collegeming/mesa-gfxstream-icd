#pragma once
#include <cstdint>
#include <vulkan/vulkan.h>

namespace gfxstream::host::vk {
// This is rejection, not coherent emulation. Neither flags nor index are rewritten.
inline VkResult validateHostCoherentContract(VkMemoryPropertyFlags guestFlags,
                                             VkMemoryPropertyFlags physicalHostFlags) {
    return (guestFlags & VK_MEMORY_PROPERTY_HOST_COHERENT_BIT) &&
           !(physicalHostFlags & VK_MEMORY_PROPERTY_HOST_COHERENT_BIT)
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
