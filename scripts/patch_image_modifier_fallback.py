#!/usr/bin/env python3
"""Retry a rejected DRM-modifier vkCreateImage once with tiling=LINEAR.

Why
---
VK_EXT_image_drm_format_modifier is deliberately exposed to the guest on this Mali host (see
patch_unhide_drm_format_modifier.py), so guest WSI asks for scanout images with a modifier:
VkImageDrmFormatModifierListCreateInfoEXT or the explicit plane-layout form. Mali rejects a few
modifier/extent combinations -- the Noctalia panel and popup swapchains at 3408x2272 are the
known case -- with VK_ERROR_INVALID_DRM_FORMAT_MODIFIER_PLANE_LAYOUT_EXT. A rejected swapchain
takes the whole panel down, and MESA_VK_WSI_DEBUG=linear does not help because the guest is what
puts the modifier in the chain. The modifier is a guest-side layout hint, not part of the
external-memory contract, so the same image is normally creatable as plain LINEAR.

What
----
Only the direct-driver branch of on_vkCreateImage() changes, and only after vkCreateImage has
already returned that exact error:

  * gate: result == VK_ERROR_INVALID_DRM_FORMAT_MODIFIER_PLANE_LAYOUT_EXT, the image did not go
    through VkNativeBufferANDROID (that path creates its own image), and the create info asks
    for a modifier (tiling == VK_IMAGE_TILING_DRM_FORMAT_MODIFIER_EXT or a modifier structure in
    pNext);
  * image_modifier_fallback::LinearCreateInfo builds a copy with tiling = VK_IMAGE_TILING_LINEAR
    and the two modifier structures unlinked from pNext, duplicating only the predecessor node
    whose pNext changes and relinking every other structure unchanged;
  * one retry through vk->vkCreateImage(device, linearCreateInfo.get(), ...) -- no loop;
  * success is returned and logs one rate-limited line per process:
        MODIFIER-FALLBACK fmt=0x%x %ux%u
    and, because the image really is linear, pCreateInfo is pointed at the replacement for the
    later imageInfo.bookkeeping (imageCreateInfoShallow/layout);
  * a failed retry returns the original error unchanged.

The caller's VkImageCreateInfo and every structure reachable from its pNext are read-only: the
replacement create info is a copy, and the one duplicated chain node lives in the helper object
for the rest of the function. Nothing is written through pCreateInfo.

The script is idempotent: a second run verifies the header, the include and the fallback block
instead of patching again. Every anchor must occur exactly once and all checks run before the
first write, so a rejected patch leaves the tree untouched.
"""
import argparse
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("root", type=Path)
root = parser.parse_args().root.resolve()

decoder = "host/vulkan/vk_decoder_global_state.cpp"
header = "host/vulkan/image_modifier_fallback.h"

decoder_path = root / decoder
header_path = root / header

if not decoder_path.is_file():
    raise SystemExit(f"{decoder}: file not found under {root}")

HEADER = """\
// One-retry DRM-modifier fallback for vkCreateImage on Mali.
//
// Guest WSI (zink, with VK_EXT_image_drm_format_modifier exposed by
// patch_unhide_drm_format_modifier.py) asks for scanout images with a DRM modifier, either
// VkImageDrmFormatModifierListCreateInfoEXT or VkImageDrmFormatModifierExplicitCreateInfoEXT.
// Mali rejects some modifier/extent combinations with
// VK_ERROR_INVALID_DRM_FORMAT_MODIFIER_PLANE_LAYOUT_EXT even though the same image is creatable
// as VK_IMAGE_TILING_LINEAR, and a rejected swapchain takes the panel or popup down.
//
// LinearCreateInfo builds a replacement VkImageCreateInfo whose tiling is LINEAR and whose pNext
// chain has the two modifier structures unlinked. It never writes through the caller's pointers:
// the create info is a copy and the only node duplicated is the modifier's predecessor (the
// chain is otherwise relinked unchanged, so every other structure keeps its original identity).
// A chain with more than one modifier structure, or a predecessor this header cannot size,
// refuses the fallback so the original driver error is returned untouched.
//
// No dependency beyond <vulkan/vulkan.h>, <atomic> and <cstring>, so the unit test compiles the
// exact bytes that are installed into host/vulkan/.
#ifndef GFXSTREAM_HOST_VULKAN_IMAGE_MODIFIER_FALLBACK_H_
#define GFXSTREAM_HOST_VULKAN_IMAGE_MODIFIER_FALLBACK_H_

#include <vulkan/vulkan.h>

#include <atomic>
#include <cstddef>
#include <cstring>

namespace gfxstream {
namespace image_modifier_fallback {

inline bool IsDrmModifierStructure(VkStructureType sType) {
    return sType == VK_STRUCTURE_TYPE_IMAGE_DRM_FORMAT_MODIFIER_LIST_CREATE_INFO_EXT ||
           sType == VK_STRUCTURE_TYPE_IMAGE_DRM_FORMAT_MODIFIER_EXPLICIT_CREATE_INFO_EXT;
}

inline bool HasDrmFormatModifier(const VkImageCreateInfo& createInfo) {
    if (createInfo.tiling == VK_IMAGE_TILING_DRM_FORMAT_MODIFIER_EXT) return true;
    for (const VkBaseInStructure* node =
             static_cast<const VkBaseInStructure*>(createInfo.pNext);
         node != nullptr; node = node->pNext) {
        if (IsDrmModifierStructure(node->sType)) return true;
    }
    return false;
}

// Storage for the single duplicated chain node. A modifier's predecessor is one of the
// image-create pNext structures this build knows; the union also supplies its alignment.
union NodeStorage {
    VkExternalMemoryImageCreateInfo externalMemoryImage;
    VkExternalMemoryImageCreateInfoNV externalMemoryImageNv;
    VkImageFormatListCreateInfo formatList;
    VkImageCompressionControlEXT compressionControl;
    VkImageStencilUsageCreateInfo stencilUsage;
    VkImageSwapchainCreateInfoKHR swapchain;
    VkVideoProfileListInfoKHR videoProfileList;
    VkOpticalFlowImageFormatInfoNV opticalFlowImageFormat;
};

inline std::size_t KnownNodeSize(VkStructureType sType) {
    switch (sType) {
        case VK_STRUCTURE_TYPE_EXTERNAL_MEMORY_IMAGE_CREATE_INFO:
            return sizeof(VkExternalMemoryImageCreateInfo);
        case VK_STRUCTURE_TYPE_EXTERNAL_MEMORY_IMAGE_CREATE_INFO_NV:
            return sizeof(VkExternalMemoryImageCreateInfoNV);
        case VK_STRUCTURE_TYPE_IMAGE_FORMAT_LIST_CREATE_INFO:
            return sizeof(VkImageFormatListCreateInfo);
        case VK_STRUCTURE_TYPE_IMAGE_COMPRESSION_CONTROL_EXT:
            return sizeof(VkImageCompressionControlEXT);
        case VK_STRUCTURE_TYPE_IMAGE_STENCIL_USAGE_CREATE_INFO:
            return sizeof(VkImageStencilUsageCreateInfo);
        case VK_STRUCTURE_TYPE_IMAGE_SWAPCHAIN_CREATE_INFO_KHR:
            return sizeof(VkImageSwapchainCreateInfoKHR);
        case VK_STRUCTURE_TYPE_VIDEO_PROFILE_LIST_INFO_KHR:
            return sizeof(VkVideoProfileListInfoKHR);
        case VK_STRUCTURE_TYPE_OPTICAL_FLOW_IMAGE_FORMAT_INFO_NV:
            return sizeof(VkOpticalFlowImageFormatInfoNV);
        default:
            return 0;
    }
}

class LinearCreateInfo {
public:
    // Fills the replacement create info. Returns false without exposing a usable object when
    // the chain cannot be rewritten (more than one modifier structure, or a predecessor whose
    // size is unknown); the caller then returns the driver's original error.
    bool makeLinear(const VkImageCreateInfo& input) {
        const VkBaseInStructure* modifier = nullptr;
        const VkBaseInStructure* predecessor = nullptr;
        const VkBaseInStructure* previous = nullptr;
        unsigned modifierCount = 0;
        for (const VkBaseInStructure* node =
                 static_cast<const VkBaseInStructure*>(input.pNext);
             node != nullptr; node = node->pNext) {
            if (IsDrmModifierStructure(node->sType)) {
                ++modifierCount;
                modifier = node;
                predecessor = previous;
            }
            previous = node;
        }
        if (modifierCount > 1) return false;

        std::size_t predecessorSize = 0;
        if (modifier != nullptr && predecessor != nullptr) {
            predecessorSize = KnownNodeSize(predecessor->sType);
            if (predecessorSize == 0 || predecessorSize > sizeof(mNodeStorage)) return false;
        }

        mCreateInfo = input;
        mCreateInfo.tiling = VK_IMAGE_TILING_LINEAR;
        if (modifier == nullptr) return true;  // tiling-only request: chain unchanged
        if (predecessor == nullptr) {
            mCreateInfo.pNext = modifier->pNext;
            return true;
        }
        std::memcpy(static_cast<void*>(&mNodeStorage), predecessor, predecessorSize);
        reinterpret_cast<VkBaseOutStructure*>(&mNodeStorage)->pNext =
            reinterpret_cast<VkBaseOutStructure*>(const_cast<VkBaseInStructure*>(modifier->pNext));
        mCreateInfo.pNext = &mNodeStorage;
        return true;
    }

    const VkImageCreateInfo* get() const { return &mCreateInfo; }

private:
    NodeStorage mNodeStorage;
    VkImageCreateInfo mCreateInfo{};
};

// One MODIFIER-FALLBACK line per process: the first fallback identifies format and extent, and
// later swapchains stay silent. The function-local static is shared by every caller in the
// process, and one relaxed fetch_add makes the gate single-winner under threads.
inline bool ShouldLog() {
    static std::atomic<unsigned> sLogged{0};
    return sLogged.fetch_add(1, std::memory_order_relaxed) == 0;
}

}  // namespace image_modifier_fallback
}  // namespace gfxstream

#endif  // GFXSTREAM_HOST_VULKAN_IMAGE_MODIFIER_FALLBACK_H_
"""

# The AHB blob export guard is the last host header in the include block; the decoder source and
# the helper share no names beyond the image_modifier_fallback namespace.
INCLUDE_ANCHOR = '#include "ahb_blob_export_guard.h"\n'
INCLUDE_LINE = '#include "image_modifier_fallback.h"\n'
INCLUDE_BLOCK = INCLUDE_ANCHOR + INCLUDE_LINE

# The direct-driver creation is the only vkCreateImage call in this function; the deferred AHB
# replacement path re-enters on_vkCreateImage() and is gated out below by nativeBufferANDROID.
ORIGINAL_CREATE = "        createRes = vk->vkCreateImage(device, pCreateInfo, nullptr, pImage);\n"

# Unique in the decoder: on_vkCreateImage() is the only function with this early return.
OLD_RETURN = "        if (createRes != VK_SUCCESS) return createRes;\n"

NEW_BLOCK = """\
        // MODIFIER-FALLBACK-1: Mali rejects some guest DRM-modifier layouts with
        // VK_ERROR_INVALID_DRM_FORMAT_MODIFIER_PLANE_LAYOUT_EXT -- Noctalia's panel and popup
        // swapchains hit it while the same compositor works at other sizes. The modifier is a
        // guest-side layout hint, not part of the external-memory contract, so one retry with
        // the modifier structures unlinked and tiling=LINEAR keeps the swapchain alive. The
        // caller's create info is never modified: LinearCreateInfo fills a copy and duplicates
        // only the chain node whose pNext changes. Every other result path is untouched.
        image_modifier_fallback::LinearCreateInfo linearCreateInfo;
        if (createRes == VK_ERROR_INVALID_DRM_FORMAT_MODIFIER_PLANE_LAYOUT_EXT &&
            nativeBufferANDROID == nullptr &&
            image_modifier_fallback::HasDrmFormatModifier(*pCreateInfo) &&
            linearCreateInfo.makeLinear(*pCreateInfo)) {
            createRes = vk->vkCreateImage(device, linearCreateInfo.get(), nullptr, pImage);
            if (createRes == VK_SUCCESS) {
                pCreateInfo = linearCreateInfo.get();
                if (image_modifier_fallback::ShouldLog()) {
                    GFXSTREAM_INFO("MODIFIER-FALLBACK fmt=0x%x %ux%u",
                                   (unsigned)pCreateInfo->format,
                                   pCreateInfo->extent.width, pCreateInfo->extent.height);
                }
            }
        }
"""

LOG_LINE = 'MODIFIER-FALLBACK fmt=0x%x %ux%u'
RETRY_LINE = "createRes = vk->vkCreateImage(device, linearCreateInfo.get(), nullptr, pImage);"
ERROR_TEST = "createRes == VK_ERROR_INVALID_DRM_FORMAT_MODIFIER_PLANE_LAYOUT_EXT"
NATIVE_BUFFER_TEST = "nativeBufferANDROID == nullptr"
MODIFIER_TEST = "image_modifier_fallback::HasDrmFormatModifier(*pCreateInfo)"
MAKE_LINEAR = "linearCreateInfo.makeLinear(*pCreateInfo)"

before = decoder_path.read_text(encoding="utf-8")

existing_header = header_path.read_text(encoding="utf-8") if header_path.is_file() else None
if existing_header is not None and existing_header != HEADER:
    raise SystemExit(
        f"{header}: exists but does not match the image modifier fallback helper; "
        "refusing to overwrite")

# Validate every anchor before writing anything.
text = before
if INCLUDE_BLOCK in text:
    if text.count(INCLUDE_BLOCK) != 1:
        raise SystemExit(f"{decoder}: fallback include block appears {text.count(INCLUDE_BLOCK)} times")
elif INCLUDE_LINE in text:
    raise SystemExit(
        f"{decoder}: fallback include exists without the AHB blob guard include; "
        "start from a clean pinned tree")
elif text.count(INCLUDE_ANCHOR) != 1:
    raise SystemExit(
        f"{decoder}: expected one AHB blob guard include anchor, found {text.count(INCLUDE_ANCHOR)}")

if NEW_BLOCK in text:
    if text.count(NEW_BLOCK) != 1:
        raise SystemExit(f"{decoder}: fallback block appears {text.count(NEW_BLOCK)} times")
    if OLD_RETURN not in text:
        raise SystemExit(f"{decoder}: fallback block is present but the early return is gone")
else:
    if text.count(OLD_RETURN) != 1:
        raise SystemExit(
            f"{decoder}: expected one image-creation early return, found {text.count(OLD_RETURN)}")
    if text.count(ORIGINAL_CREATE) != 1:
        raise SystemExit(
            f"{decoder}: expected one direct vkCreateImage call, found {text.count(ORIGINAL_CREATE)}")

# The retry must not exist before the block, and the fallback block must not be able to loop.
if text.count(RETRY_LINE) != (1 if NEW_BLOCK in text else 0):
    raise SystemExit(f"{decoder}: unexpected fallback retry call sites")

# Checks passed: write the helper first, then the include and the fallback block.
if existing_header is None:
    header_path.write_text(HEADER, encoding="utf-8", newline="\n")
    print("created", header)
else:
    print("already created", header)

if INCLUDE_BLOCK not in text:
    text = text.replace(INCLUDE_ANCHOR, INCLUDE_BLOCK, 1)
    print("patched", decoder, "(modifier fallback include)")
else:
    print("already patched", decoder, "(modifier fallback include)")

if NEW_BLOCK not in text:
    text = text.replace(OLD_RETURN, NEW_BLOCK + OLD_RETURN, 1)
    print("patched", decoder, "(MODIFIER-FALLBACK-1)")
else:
    print("already patched", decoder, "(MODIFIER-FALLBACK-1)")

if text != before:
    decoder_path.write_text(text, encoding="utf-8", newline="\n")

after = decoder_path.read_text(encoding="utf-8")
if after.count(RETRY_LINE) != 1:
    raise SystemExit(f"{decoder}: expected exactly one fallback retry call, found {after.count(RETRY_LINE)}")
if after.count(LOG_LINE) != 1 or after.count("image_modifier_fallback::ShouldLog()") != 1:
    raise SystemExit(f"{decoder}: fallback log line or its gate is incomplete")
if after.count(ERROR_TEST) != 1 or after.count(MAKE_LINEAR) != 1:
    raise SystemExit(f"{decoder}: fallback gate is incomplete")
if after.index(ORIGINAL_CREATE) > after.index(NEW_BLOCK):
    raise SystemExit(f"{decoder}: the fallback must follow the original vkCreateImage call")
if after.index(NEW_BLOCK + OLD_RETURN) != after.index(NEW_BLOCK):
    raise SystemExit(f"{decoder}: the existing early return no longer follows the fallback")

print("Image modifier fallback installed (DRM modifier plane-layout error only, one retry)")
