// DRM-modifier fallback for on_vkCreateImage(): the exact helper the device build installs.
//
// Guest WSI asks for scanout images with a DRM modifier because the host exposes
// VK_EXT_image_drm_format_modifier. Mali answers a few modifier/extent combinations with
// VK_ERROR_INVALID_DRM_FORMAT_MODIFIER_PLANE_LAYOUT_EXT, which kills the Noctalia panel or a
// popup swapchain. The fallback builds a LINEAR copy of the create info with the two modifier
// structures unlinked and retries once.
//
// This test pins the pieces the patch relies on:
//
//   * only the list and explicit modifier structures are recognised and removed;
//   * every other structure keeps its identity in the replacement chain, in order;
//   * tiling becomes VK_IMAGE_TILING_LINEAR in the copy;
//   * the caller's create info and every reachable node are not written to (byte compare);
//   * a head modifier needs no copy, a modifier after another node duplicates only that node;
//   * a chain with two modifiers, or a predecessor whose size is unknown, is refused without
//     exposing a half-built replacement, so the driver's original error is returned;
//   * the default path (no modifier) is not detected, so the production gate is not entered;
//   * a mirror of the call site retries exactly once and never loops;
//   * the log gate admits one MODIFIER-FALLBACK line per process under threads.
//
// It includes the same generated header the Android build includes and has no dependency
// beyond <vulkan/vulkan.h> plus the standard library.
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

#include <atomic>
#include <cstring>
#include <thread>
#include <type_traits>
#include <utility>
#include <vector>

#include "image_modifier_fallback.h"

namespace {

using gfxstream::image_modifier_fallback::HasDrmFormatModifier;
using gfxstream::image_modifier_fallback::IsDrmModifierStructure;
using gfxstream::image_modifier_fallback::LinearCreateInfo;
using gfxstream::image_modifier_fallback::NodeStorage;

void check(bool condition, const char* what) {
    if (!condition) {
        fprintf(stderr, "FAIL: %s\n", what);
        exit(1);
    }
}

VkImageCreateInfo baseCreateInfo() {
    VkImageCreateInfo info{};
    info.sType = VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO;
    info.imageType = VK_IMAGE_TYPE_2D;
    info.format = VK_FORMAT_B8G8R8A8_UNORM;
    info.extent = {3408, 2272, 1};
    info.mipLevels = 1;
    info.arrayLayers = 1;
    info.samples = VK_SAMPLE_COUNT_1_BIT;
    info.tiling = VK_IMAGE_TILING_DRM_FORMAT_MODIFIER_EXT;
    info.usage = VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT;
    info.sharingMode = VK_SHARING_MODE_EXCLUSIVE;
    info.initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;
    return info;
}

bool ChainHasModifier(const void* pNext) {
    for (const VkBaseInStructure* node = static_cast<const VkBaseInStructure*>(pNext);
         node != nullptr; node = node->pNext) {
        if (IsDrmModifierStructure(node->sType)) return true;
    }
    return false;
}

unsigned ChainLength(const void* pNext) {
    unsigned count = 0;
    for (const VkBaseInStructure* node = static_cast<const VkBaseInStructure*>(pNext);
         node != nullptr; node = node->pNext) {
        ++count;
    }
    return count;
}

struct RetryDriver {
    VkResult firstResult = VK_SUCCESS;
    VkResult retryResult = VK_SUCCESS;
    unsigned calls = 0;
    VkImageTiling lastTiling = VK_IMAGE_TILING_OPTIMAL;
    bool lastChainHasModifier = false;
};

VkResult fakeCreateImage(const VkImageCreateInfo& info, RetryDriver* driver) {
    ++driver->calls;
    driver->lastTiling = info.tiling;
    driver->lastChainHasModifier = ChainHasModifier(info.pNext);
    return driver->calls == 1 ? driver->firstResult : driver->retryResult;
}

// Mirror of the patched call site. nativeBufferANDROID is known to be null on this path.
VkResult createImageWithFallback(const VkImageCreateInfo& info, RetryDriver* driver) {
    VkResult result = fakeCreateImage(info, driver);
    LinearCreateInfo linear;
    if (result == VK_ERROR_INVALID_DRM_FORMAT_MODIFIER_PLANE_LAYOUT_EXT &&
        HasDrmFormatModifier(info) && linear.makeLinear(info)) {
        result = fakeCreateImage(*linear.get(), driver);
    }
    return result;
}

struct UnrelatedNode {
    VkStructureType sType;
    const void* pNext;
    uint64_t payload;
};

}  // namespace

int main() {
    static_assert(std::is_same<decltype(HasDrmFormatModifier(std::declval<const VkImageCreateInfo&>())),
                               bool>::value,
                  "the production gate returns bool");
    static_assert(sizeof(NodeStorage) >= sizeof(VkExternalMemoryImageCreateInfo),
                  "the node storage holds a known predecessor");

    // Exactly the two modifier create-info types are filtered; the sibling query structures
    // and every other image create-info structure are not.
    check(IsDrmModifierStructure(VK_STRUCTURE_TYPE_IMAGE_DRM_FORMAT_MODIFIER_LIST_CREATE_INFO_EXT),
          "the list modifier structure is recognised");
    check(IsDrmModifierStructure(VK_STRUCTURE_TYPE_IMAGE_DRM_FORMAT_MODIFIER_EXPLICIT_CREATE_INFO_EXT),
          "the explicit modifier structure is recognised");
    check(!IsDrmModifierStructure(VK_STRUCTURE_TYPE_EXTERNAL_MEMORY_IMAGE_CREATE_INFO),
          "external memory is not a modifier");
    check(!IsDrmModifierStructure(VK_STRUCTURE_TYPE_IMAGE_FORMAT_LIST_CREATE_INFO),
          "a format list is not a modifier");
    check(!IsDrmModifierStructure(VK_STRUCTURE_TYPE_IMAGE_SWAPCHAIN_CREATE_INFO_KHR),
          "a swapchain create info is not a modifier");

    // The log gate: sixteen simultaneous first calls still produce one line.
    {
        std::atomic<unsigned> winners{0};
        std::vector<std::thread> threads;
        for (int i = 0; i < 16; ++i) {
            threads.emplace_back([&winners]() {
                if (gfxstream::image_modifier_fallback::ShouldLog()) {
                    winners.fetch_add(1, std::memory_order_relaxed);
                }
            });
        }
        for (auto& thread : threads) thread.join();
        check(winners.load() == 1, "the MODIFIER-FALLBACK gate admits exactly one line");
    }

    // The shape seen on the device: external memory, then the explicit modifier, then a view
    // format list. The modifier is removed; every other node survives in the same order.
    {
        VkExternalMemoryImageCreateInfo external{};
        external.sType = VK_STRUCTURE_TYPE_EXTERNAL_MEMORY_IMAGE_CREATE_INFO;
        external.handleTypes = VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT;

        VkImageDrmFormatModifierExplicitCreateInfoEXT explicitModifier{};
        explicitModifier.sType =
            VK_STRUCTURE_TYPE_IMAGE_DRM_FORMAT_MODIFIER_EXPLICIT_CREATE_INFO_EXT;
        explicitModifier.drmFormatModifier = 0;
        explicitModifier.drmFormatModifierPlaneCount = 1;

        VkImageFormatListCreateInfo formatList{};
        formatList.sType = VK_STRUCTURE_TYPE_IMAGE_FORMAT_LIST_CREATE_INFO;
        formatList.viewFormatCount = 2;
        static const VkFormat kViewFormats[2] = {VK_FORMAT_B8G8R8A8_UNORM,
                                                 VK_FORMAT_B8G8R8A8_SRGB};
        formatList.pViewFormats = kViewFormats;

        external.pNext = &explicitModifier;
        explicitModifier.pNext = &formatList;
        formatList.pNext = nullptr;

        VkImageCreateInfo input = baseCreateInfo();
        input.pNext = &external;
        input.flags = VK_IMAGE_CREATE_MUTABLE_FORMAT_BIT;

        const VkImageCreateInfo inputSnapshot = input;
        const VkExternalMemoryImageCreateInfo externalSnapshot = external;
        const VkImageDrmFormatModifierExplicitCreateInfoEXT modifierSnapshot = explicitModifier;
        const VkImageFormatListCreateInfo formatListSnapshot = formatList;

        LinearCreateInfo linear;
        check(linear.makeLinear(input), "the modifier chain is transformable");

        const VkImageCreateInfo* out = linear.get();
        check(out->tiling == VK_IMAGE_TILING_LINEAR, "the copy is tiled LINEAR");
        check(out->format == input.format && out->extent.width == input.extent.width &&
                  out->extent.height == input.extent.height &&
                  out->usage == input.usage && out->flags == input.flags &&
                  out->imageType == input.imageType && out->mipLevels == input.mipLevels &&
                  out->arrayLayers == input.arrayLayers && out->samples == input.samples &&
                  out->sharingMode == input.sharingMode &&
                  out->initialLayout == input.initialLayout,
              "every non-tiling field is copied unchanged");
        check(out->pNext != input.pNext, "the replacement chain starts at a copy");
        check(!ChainHasModifier(out->pNext), "the replacement chain has no modifier structure");
        check(ChainLength(out->pNext) == 2, "the two other structures are preserved");

        const auto* copy = reinterpret_cast<const VkExternalMemoryImageCreateInfo*>(
            static_cast<const VkBaseInStructure*>(out->pNext));
        check(copy->sType == VK_STRUCTURE_TYPE_EXTERNAL_MEMORY_IMAGE_CREATE_INFO,
              "external memory stays first");
        check(copy->handleTypes == external.handleTypes, "external memory contents are copied");
        check(copy->pNext == &formatList, "the modifier is unlinked, the format list is kept");
        check(formatList.pViewFormats == kViewFormats && formatList.viewFormatCount == 2,
              "the untouched nodes keep their payloads");

        // Read-only contract: the caller's create info and all of its nodes are byte-identical,
        // and the predecessor still points at the modifier.
        check(std::memcmp(&input, &inputSnapshot, sizeof(input)) == 0,
              "the caller's create info is not rewritten");
        check(std::memcmp(&external, &externalSnapshot, sizeof(external)) == 0,
              "the predecessor node is not rewritten");
        check(std::memcmp(&explicitModifier, &modifierSnapshot, sizeof(explicitModifier)) == 0,
              "the modifier node is not rewritten");
        check(std::memcmp(&formatList, &formatListSnapshot, sizeof(formatList)) == 0,
              "a preserved node is not rewritten");
        check(external.pNext == &explicitModifier,
              "the caller's chain still contains the modifier");

        // The duplicate is owned by LinearCreateInfo: writing to it cannot reach the caller.
        auto* mutableCopy = const_cast<VkExternalMemoryImageCreateInfo*>(copy);
        mutableCopy->handleTypes = 0;
        check(external.handleTypes == VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT,
              "the copied predecessor is independent of the caller");
    }

    // A modifier at the head of the chain needs no copy: the replacement links straight to the
    // modifier's successor.
    {
        VkExternalMemoryImageCreateInfo external{};
        external.sType = VK_STRUCTURE_TYPE_EXTERNAL_MEMORY_IMAGE_CREATE_INFO;
        external.handleTypes = VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT;

        VkImageDrmFormatModifierListCreateInfoEXT list{};
        list.sType = VK_STRUCTURE_TYPE_IMAGE_DRM_FORMAT_MODIFIER_LIST_CREATE_INFO_EXT;
        static const uint64_t kModifiers[2] = {0, 1};
        list.drmFormatModifierCount = 2;
        list.pDrmFormatModifiers = kModifiers;
        list.pNext = &external;

        VkImageCreateInfo input = baseCreateInfo();
        input.pNext = &list;

        LinearCreateInfo linear;
        check(linear.makeLinear(input), "a head modifier is transformable");
        check(linear.get()->pNext == &external,
              "removing a head modifier links its successor directly");
        check(linear.get()->tiling == VK_IMAGE_TILING_LINEAR, "head-modifier copy is LINEAR");
        check(list.pNext == &external, "the head modifier node is untouched");
    }

    // tiling == DRM_FORMAT_MODIFIER_EXT with no modifier structure (malformed but seen in
    // traces): the fallback still switches to LINEAR and leaves the chain alone.
    {
        VkExternalMemoryImageCreateInfo external{};
        external.sType = VK_STRUCTURE_TYPE_EXTERNAL_MEMORY_IMAGE_CREATE_INFO;
        external.handleTypes = VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT;

        VkImageCreateInfo input = baseCreateInfo();
        input.pNext = &external;

        check(HasDrmFormatModifier(input), "DRM tiling alone reaches the fallback");
        LinearCreateInfo linear;
        check(linear.makeLinear(input), "a tiling-only request is transformable");
        check(linear.get()->tiling == VK_IMAGE_TILING_LINEAR, "tiling-only copy is LINEAR");
        check(linear.get()->pNext == &external, "tiling-only fallback keeps the chain");
    }

    // Default path: no modifier in tiling or pNext, so the production gate never runs.
    {
        VkExternalMemoryImageCreateInfo external{};
        external.sType = VK_STRUCTURE_TYPE_EXTERNAL_MEMORY_IMAGE_CREATE_INFO;
        external.handleTypes = VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT;

        VkImageCreateInfo input = baseCreateInfo();
        input.tiling = VK_IMAGE_TILING_OPTIMAL;
        input.pNext = &external;
        check(!HasDrmFormatModifier(input), "an ordinary optimal image is not a fallback target");
    }

    // A predecessor whose size this header cannot know refuses the fallback, and the
    // replacement object is not exposed half-built.
    {
        UnrelatedNode unrelated{};
        unrelated.sType = VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO;
        unrelated.payload = 0x1234;

        VkImageDrmFormatModifierExplicitCreateInfoEXT modifier{};
        modifier.sType = VK_STRUCTURE_TYPE_IMAGE_DRM_FORMAT_MODIFIER_EXPLICIT_CREATE_INFO_EXT;
        unrelated.pNext = &modifier;

        VkImageCreateInfo input = baseCreateInfo();
        input.pNext = &unrelated;

        check(HasDrmFormatModifier(input), "the unknown-predecessor chain is detected");
        LinearCreateInfo linear;
        check(!linear.makeLinear(input), "an unknown predecessor is refused");
        check(linear.get()->pNext == nullptr,
              "a refused fallback exposes no replacement chain");
    }

    // Both modifier structures in one chain is invalid input; refuse rather than guess.
    {
        VkImageDrmFormatModifierListCreateInfoEXT list{};
        list.sType = VK_STRUCTURE_TYPE_IMAGE_DRM_FORMAT_MODIFIER_LIST_CREATE_INFO_EXT;
        VkImageDrmFormatModifierExplicitCreateInfoEXT explicitModifier{};
        explicitModifier.sType =
            VK_STRUCTURE_TYPE_IMAGE_DRM_FORMAT_MODIFIER_EXPLICIT_CREATE_INFO_EXT;
        list.pNext = &explicitModifier;

        VkImageCreateInfo input = baseCreateInfo();
        input.pNext = &list;

        LinearCreateInfo linear;
        check(!linear.makeLinear(input), "two modifier structures are refused");
    }

    // Call-site mirror: the retry happens only for the modifier error with a modifier request,
    // and at most once.
    {
        VkExternalMemoryImageCreateInfo external{};
        external.sType = VK_STRUCTURE_TYPE_EXTERNAL_MEMORY_IMAGE_CREATE_INFO;
        external.handleTypes = VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT;
        VkImageDrmFormatModifierExplicitCreateInfoEXT modifier{};
        modifier.sType = VK_STRUCTURE_TYPE_IMAGE_DRM_FORMAT_MODIFIER_EXPLICIT_CREATE_INFO_EXT;
        modifier.drmFormatModifierPlaneCount = 1;
        external.pNext = &modifier;
        VkImageCreateInfo input = baseCreateInfo();
        input.pNext = &external;

        {
            RetryDriver driver;
            driver.firstResult = VK_ERROR_INVALID_DRM_FORMAT_MODIFIER_PLANE_LAYOUT_EXT;
            driver.retryResult = VK_SUCCESS;
            check(createImageWithFallback(input, &driver) == VK_SUCCESS,
                  "a rejected modifier request succeeds on the LINEAR retry");
            check(driver.calls == 2, "exactly one retry follows the modifier error");
            check(driver.lastTiling == VK_IMAGE_TILING_LINEAR, "the retry asks for LINEAR");
            check(!driver.lastChainHasModifier, "the retry chain has no modifier structure");
        }
        {
            RetryDriver driver;
            driver.firstResult = VK_ERROR_OUT_OF_DEVICE_MEMORY;
            check(createImageWithFallback(input, &driver) == VK_ERROR_OUT_OF_DEVICE_MEMORY,
                  "another driver error is returned unchanged");
            check(driver.calls == 1, "another driver error is not retried");
        }
        {
            RetryDriver driver;
            check(createImageWithFallback(input, &driver) == VK_SUCCESS,
                  "a successful create is returned unchanged");
            check(driver.calls == 1, "a successful create is not retried");
        }
        {
            RetryDriver driver;
            driver.firstResult = VK_ERROR_INVALID_DRM_FORMAT_MODIFIER_PLANE_LAYOUT_EXT;
            driver.retryResult = VK_ERROR_INVALID_DRM_FORMAT_MODIFIER_PLANE_LAYOUT_EXT;
            check(createImageWithFallback(input, &driver) ==
                      VK_ERROR_INVALID_DRM_FORMAT_MODIFIER_PLANE_LAYOUT_EXT,
                  "a failed retry returns the original error");
            check(driver.calls == 2, "a failed retry is not attempted again");
        }
        {
            VkImageCreateInfo plain = baseCreateInfo();
            plain.tiling = VK_IMAGE_TILING_OPTIMAL;
            RetryDriver driver;
            driver.firstResult = VK_ERROR_INVALID_DRM_FORMAT_MODIFIER_PLANE_LAYOUT_EXT;
            check(createImageWithFallback(plain, &driver) ==
                      VK_ERROR_INVALID_DRM_FORMAT_MODIFIER_PLANE_LAYOUT_EXT,
                  "the modifier error without a modifier request is returned unchanged");
            check(driver.calls == 1, "no modifier request means no retry");
        }
    }

    puts("PASS: the DRM-modifier fallback filters only the list/explicit structures, copies the "
         "create info with tiling=LINEAR and an unmodified caller chain, retries exactly once, "
         "refuses ambiguous chains, and logs one MODIFIER-FALLBACK line per process");
    return 0;
}
