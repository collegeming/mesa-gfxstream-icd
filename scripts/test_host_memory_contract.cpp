#include "host_memory_contract.h"
#include <cstdlib>
#include <iostream>
#include <limits>
#include <memory>

using namespace gfxstream::host::vk;
namespace {
void check(bool ok) { if (!ok) std::abort(); }
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
    std::cout << "PASS: " << scenarios << " host memory contract scenarios; exact shared helpers, "
              << "no index/flags rewrite, reject before dispatch/resources, borrowed RAII once; no GPU\n";
}
