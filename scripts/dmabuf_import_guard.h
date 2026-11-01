#pragma once
#include <cstdint>
#include <vulkan/vulkan.h>

namespace gfxstream::host::vk {
// Never substitute a memory index: that could silently change guest coherency.
// Query borrows the fd; only the rejection cleanup owns/consumes it here.
template <typename CloseFd, typename ReleasePool>
VkResult validateHostVisibleDmaBufImport(
    bool emulateHostVisible, VkDevice device, VkExternalMemoryHandleTypeFlagBits handleType,
    int& fd, uint32_t memoryTypeIndex, PFN_vkGetMemoryFdPropertiesKHR query,
    uint32_t* reportedBits, CloseFd closeFd, ReleasePool releasePool,
    VkResult* reportedQueryResult = nullptr) {
    *reportedBits = 0;
    if (reportedQueryResult) *reportedQueryResult = VK_ERROR_EXTENSION_NOT_PRESENT;
    if (!emulateHostVisible || handleType != VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT) {
        return VK_SUCCESS;
    }
    VkMemoryFdPropertiesKHR props = {VK_STRUCTURE_TYPE_MEMORY_FD_PROPERTIES_KHR, nullptr, 0};
    const VkResult queryResult = fd >= 0 && query
        ? query(device, handleType, fd, &props) : VK_ERROR_EXTENSION_NOT_PRESENT;
    if (reportedQueryResult) *reportedQueryResult = queryResult;
    const bool compatible = queryResult == VK_SUCCESS && memoryTypeIndex < 32 &&
        (props.memoryTypeBits & (uint32_t{1} << memoryTypeIndex));
    *reportedBits = props.memoryTypeBits;
    if (compatible) return VK_SUCCESS;
    if (fd >= 0) {
        const int ownedFd = fd;
        fd = -1; // Mark consumed before cleanup, so repeated rejection cannot double-close.
        closeFd(ownedFd);
    }
    releasePool();
    return VK_ERROR_INVALID_EXTERNAL_HANDLE;
}
} // namespace gfxstream::host::vk
