#include "dmabuf_import_guard.h"
#include <cstdlib>
#include <iostream>

namespace {
int queries = 0;
VkResult queryResult = VK_SUCCESS;
uint32_t queryBits = 0x7e02;
VKAPI_ATTR VkResult VKAPI_CALL mockQuery(VkDevice, VkExternalMemoryHandleTypeFlagBits type,
                                        int fd, VkMemoryFdPropertiesKHR* props) {
    if (type != VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT || fd != 123) std::abort();
    ++queries;
    props->memoryTypeBits = queryBits;
    return queryResult;
}
void check(bool pass) { if (!pass) std::abort(); }
void run(uint32_t index, PFN_vkGetMemoryFdPropertiesKHR query, VkResult expected,
         bool hostVisible = true,
         VkExternalMemoryHandleTypeFlagBits type = VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT) {
    queries = 0;
    int closes = 0, releases = 0, allocations = 0;
    bool poolReserved = true;
    int fd = 123;
    uint32_t bits = 0;
    VkDeviceMemory output = reinterpret_cast<VkDeviceMemory>(uintptr_t{0x1234});
    auto closeFd = [&](int value) { check(value == 123); ++closes; };
    auto releasePool = [&] { if (poolReserved) { poolReserved = false; ++releases; } };
    auto result = gfxstream::host::vk::validateHostVisibleDmaBufImport(
        hostVisible, VK_NULL_HANDLE, type, fd, index, query, &bits, closeFd, releasePool);
    if (result == VK_SUCCESS) {
        ++allocations; // Mock dispatch: vkAllocateMemory would only run after the guard.
    } else {
        output = VK_NULL_HANDLE;
    }
    check(result == expected);
    if (expected != VK_SUCCESS) {
        check(fd == -1 && closes == 1 && releases == 1 && allocations == 0);
        check(output == VK_NULL_HANDLE && !poolReserved);
        auto repeated = gfxstream::host::vk::validateHostVisibleDmaBufImport(
            hostVisible, VK_NULL_HANDLE, type, fd, index, query, &bits, closeFd, releasePool);
        check(repeated == expected && closes == 1 && releases == 1);
    } else {
        check(fd == 123 && closes == 0 && releases == 0 && allocations == 1 && poolReserved);
    }
    if (!hostVisible || type != VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT || !query) {
        check(queries == 0);
    } else {
        check(queries == 1);
    }
}
}
int main() {
    run(8, mockQuery, VK_ERROR_INVALID_EXTERNAL_HANDLE); // 8 is outside 0x7e02.
    run(14, mockQuery, VK_SUCCESS); // Preserve the compatible requested index.
    run(32, mockQuery, VK_ERROR_INVALID_EXTERNAL_HANDLE);
    run(8, nullptr, VK_ERROR_INVALID_EXTERNAL_HANDLE);
    queryResult = VK_ERROR_INVALID_EXTERNAL_HANDLE;
    run(14, mockQuery, VK_ERROR_INVALID_EXTERNAL_HANDLE);
    queryResult = VK_SUCCESS;
    run(8, mockQuery, VK_SUCCESS, false); // Ordinary non-emulated allocation.
    run(8, mockQuery, VK_SUCCESS, true, VK_EXTERNAL_MEMORY_HANDLE_TYPE_OPAQUE_FD_BIT);
    run(8, mockQuery, VK_SUCCESS, true,
        VK_EXTERNAL_MEMORY_HANDLE_TYPE_ANDROID_HARDWARE_BUFFER_BIT_ANDROID);
    std::cout << "PASS: 8 scenarios; no GPU, no Vulkan loader, rejection closes once and releases pool\n";
}
