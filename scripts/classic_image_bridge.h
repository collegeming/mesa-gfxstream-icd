#pragma once
#include <vulkan/vulkan.h>
#include <cstdint>
#include <limits>
namespace gfxstream { namespace host { namespace cib {
constexpr uint32_t kVersion = 1;
constexpr uint32_t kContract = 0x43494231;
constexpr uint64_t kConsumer = 0x4349423100000001ULL;
constexpr VkImageUsageFlags kUsage = VK_IMAGE_USAGE_TRANSFER_SRC_BIT | VK_IMAGE_USAGE_TRANSFER_DST_BIT |
    VK_IMAGE_USAGE_SAMPLED_BIT | VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT | VK_IMAGE_USAGE_INPUT_ATTACHMENT_BIT;
inline bool format(VkFormat value) { return value == VK_FORMAT_R8G8B8A8_UNORM || value == VK_FORMAT_B8G8R8A8_UNORM; }
inline bool view(VkFormat base, VkFormat value) {
    return value == base || (base == VK_FORMAT_R8G8B8A8_UNORM && value == VK_FORMAT_R8G8B8A8_SRGB) ||
        (base == VK_FORMAT_B8G8R8A8_UNORM && value == VK_FORMAT_B8G8R8A8_SRGB);
}
inline bool staging(uint32_t width, uint32_t height, uint32_t& stride, uint32_t& size) {
    if (!width || !height) return false;
    const uint64_t row = (uint64_t(width) * 4 + 15) & ~uint64_t(15);
    const uint64_t bytes = row * height;
    if (row > INT32_MAX || bytes > UINT32_MAX) return false;
    stride = uint32_t(row); size = uint32_t(bytes); return true;
}
inline bool image(const VkImageCreateInfo& ci) {
    return format(ci.format) && ci.imageType == VK_IMAGE_TYPE_2D && ci.extent.depth == 1 &&
        ci.extent.width && ci.extent.height && ci.mipLevels == 1 && ci.arrayLayers == 1 &&
        ci.samples == VK_SAMPLE_COUNT_1_BIT && ci.sharingMode == VK_SHARING_MODE_EXCLUSIVE &&
        ci.initialLayout == VK_IMAGE_LAYOUT_UNDEFINED &&
        !(ci.flags & ~(VK_IMAGE_CREATE_MUTABLE_FORMAT_BIT | VK_IMAGE_CREATE_ALIAS_BIT)) &&
        ci.usage && !(ci.usage & ~kUsage);
}
}}} // namespace gfxstream::host::cib
