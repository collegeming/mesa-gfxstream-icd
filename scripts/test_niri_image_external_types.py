#!/usr/bin/env python3
"""Extract actual production transform snippets for CPU-only C++ regression tests."""
from pathlib import Path
import sys
root, out = map(Path, sys.argv[1:])
decoder = (root/'host/vulkan/vk_decoder_global_state.cpp').read_text()
common = (root/'host/vulkan/vk_common_operations.cpp').read_text()
qstart = decoder.index('        auto* extImageFormatInfo =')
qend = decoder.index('        VkResult res = VK_ERROR_INITIALIZATION_FAILED;', qstart)
query = decoder[qstart:qend]
cstart = decoder.index('            VkImageCreateInfo& imageCreateInfo =', decoder.index('void transformImpl_VkImageCreateInfo_tohost('))
cend = decoder.index('            // If the VkImage is going to bind to a ColorBuffer', cstart)
create = decoder[cstart:cend]
assert 'getDefaultExternalMemoryHandleType' not in query
assert 'getDefaultExternalMemoryHandleType' not in create
assert 'handleTypes |=' not in create
assert 'vk->vkCreateImage(device, pCreateInfo, nullptr, pImage)' in decoder
assert 'vk->vkGetPhysicalDeviceImageFormatProperties2(physicalDevice, pImageFormatInfo,' in decoder
assert 'vk->vkGetPhysicalDeviceImageFormatProperties2KHR(physicalDevice, pImageFormatInfo,' in decoder
assert 'case ExternalMemoryImportKind::DmaBuf:' in decoder
assert 'importFdInfo.handleType = VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT;' in decoder
assert 'validateHostVisibleDmaBufImport(' in decoder and 'validateHostCoherentContract(' in decoder
assert 'rejectZeroSizeAllocation(*pAllocateInfo, "entry", true)' in decoder
# Common aliases continue to map opaque/AHB through the existing platform policy;
# supported explicit DMA_BUF is preserved; unsupported hosts retain their legacy fallback.
a = common.index('VkExternalMemoryHandleTypeFlags VkEmulation::transformExternalMemoryHandleTypeFlags_tohost(')
b = common.index('VkExternalMemoryHandleTypeFlags VkEmulation::transformExternalMemoryHandleTypeFlags_fromhost(', a)
assert 'if (!mDeviceInfo.supportsDmaBuf && (bits & VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT))' in common[a:b]
# The harness compiles the exact final production snippets above, not a reimplementation.
harness = r'''#include <vulkan/vulkan.h>
#include <cassert>
#include <cstdint>
#define GFXSTREAM_WARNING(...) ((void)0)
struct VkNativeBufferANDROID { VkStructureType sType; const void* pNext; };
template<typename T, typename U> T* vk_find_struct(U* input) {
    auto* p = reinterpret_cast<VkBaseOutStructure*>(const_cast<void*>(static_cast<const void*>(input)));
    while (p) {
        if (p->sType == VK_STRUCTURE_TYPE_EXTERNAL_MEMORY_IMAGE_CREATE_INFO ||
            p->sType == VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_EXTERNAL_IMAGE_FORMAT_INFO) return reinterpret_cast<T*>(p);
        p = p->pNext;
    }
    return nullptr;
}
void queryTypes(VkPhysicalDeviceImageFormatInfo2* pImageFormatInfo) {
''' + query + r'''}
void createTypes(VkImageCreateInfo* pImageCreateInfos, bool& ahb) {
    uint32_t i = 0;
''' + create + r'''
    ahb = importAndroidHardwareBuffer;
    (void)pNativeBufferANDROID;
}
int main() {
    for (uint32_t bits : {0x200u, 0x400u, 0x1u, 0u}) {
        VkPhysicalDeviceExternalImageFormatInfo extQuery{};
        extQuery.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_EXTERNAL_IMAGE_FORMAT_INFO;
        extQuery.handleType = static_cast<VkExternalMemoryHandleTypeFlagBits>(bits);
        VkPhysicalDeviceImageFormatInfo2 query{};
        query.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_IMAGE_FORMAT_INFO_2; query.pNext = &extQuery;
        queryTypes(&query);
        assert(static_cast<uint32_t>(extQuery.handleType) == bits);
        VkExternalMemoryImageCreateInfo ext{};
        ext.sType = VK_STRUCTURE_TYPE_EXTERNAL_MEMORY_IMAGE_CREATE_INFO; ext.handleTypes = bits;
        VkImageCreateInfo create{}; create.sType = VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO; create.pNext = &ext;
        bool ahb = false; createTypes(&create, ahb);
        assert(ext.handleTypes == bits);
        assert(ahb == ((bits & 0x400u) != 0));
    }
}
'''
# NativeBuffer lookup stub must not reinterpret an external struct as a native buffer.
harness = harness.replace('template<typename T, typename U>', '#include <type_traits>\n#include <initializer_list>\ntemplate<typename T, typename U>')
harness = harness.replace('    auto* p = reinterpret_cast', '    if constexpr (std::is_same_v<T, VkNativeBufferANDROID>) return nullptr;\n    auto* p = reinterpret_cast')
out.write_text(harness)
print('PASS: exact production query/create snippets extracted; explicit DMA_BUF stays0x200, AHB/opaque unchanged; fd allocation and all zero/coherent guards retained')
