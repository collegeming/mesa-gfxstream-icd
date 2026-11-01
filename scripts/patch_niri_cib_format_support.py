#!/usr/bin/env python3
"""CIB1: MUTABLE-only BGRA8/RGBA8 support, plain-query fallback, bounded summary."""
import argparse
from pathlib import Path
p = argparse.ArgumentParser(description=__doc__)
p.add_argument('root', type=Path)
root = p.parse_args().root.resolve()
def edit(rel, old, new):
    path = root/rel
    text = path.read_text()
    if text.count(old) != 1:
        raise SystemExit(f'{rel}: expected one anchor ({text.count(old)}): {old[:100]}')
    path.write_text(text.replace(old, new))

formats = 'host/vulkan/vk_format_support.cpp'
# Keep the classic MUTABLE-only BGRA8 entry ahead of the EXTENDED_USAGE gralloc
# entry so GetSupportedInfo selects the classic shape when the external query
# rejects both. The real native query still decides whether it is supported.
edit(formats, '''        {VK_FORMAT_B8G8R8A8_UNORM,
         VK_IMAGE_CREATE_MUTABLE_FORMAT_BIT | VK_IMAGE_CREATE_EXTENDED_USAGE_BIT},
''', '''        {VK_FORMAT_B8G8R8A8_UNORM, VK_IMAGE_CREATE_MUTABLE_FORMAT_BIT},
        {VK_FORMAT_B8G8R8A8_UNORM,
         VK_IMAGE_CREATE_MUTABLE_FORMAT_BIT | VK_IMAGE_CREATE_EXTENDED_USAGE_BIT},
''')
# RGBA8 already has a supported EXTENDED entry first; keep that preference and
# add the MUTABLE-only fallback after it.
edit(formats, '''        {VK_FORMAT_R8G8B8A8_UNORM,
         VK_IMAGE_CREATE_MUTABLE_FORMAT_BIT | VK_IMAGE_CREATE_EXTENDED_USAGE_BIT},
''', '''        {VK_FORMAT_R8G8B8A8_UNORM,
         VK_IMAGE_CREATE_MUTABLE_FORMAT_BIT | VK_IMAGE_CREATE_EXTENDED_USAGE_BIT},
        {VK_FORMAT_R8G8B8A8_UNORM, VK_IMAGE_CREATE_MUTABLE_FORMAT_BIT},
''')

ops = 'host/vulkan/vk_common_operations.cpp'
# A failed external-memory query must not hide a format the plain query and the
# native create still accept. Do not claim external support for this entry:
# ColorBuffer creation still appends the configured external pNext and the
# driver's create remains authoritative.
edit(ops, '''    if (res != VK_SUCCESS) {
        if (res == VK_ERROR_FORMAT_NOT_SUPPORTED) {
            GFXSTREAM_DEBUG("Not Supported: %s %s %s %s", string_VkFormat(info->format),
                            string_VkImageType(info->type), string_VkImageTiling(info->tiling),
                            string_VkImageUsageFlagBits((VkImageUsageFlagBits)info->usageFlags));

            info->supported = false;
            return true;
        } else {''', '''    if (res != VK_SUCCESS) {
        if (res == VK_ERROR_FORMAT_NOT_SUPPORTED) {
            GFXSTREAM_DEBUG("Not Supported: %s %s %s %s", string_VkFormat(info->format),
                            string_VkImageType(info->type), string_VkImageTiling(info->tiling),
                            string_VkImageUsageFlagBits((VkImageUsageFlagBits)info->usageFlags));

            VkImageFormatProperties plainProps{};
            VkResult plainRes = vk->vkGetPhysicalDeviceImageFormatProperties(
                physdev, info->format, info->type, info->tiling, info->usageFlags,
                info->createFlags, &plainProps);
            if (plainRes == VK_SUCCESS) {
                info->supported = true;
                info->supportsExternalMemory = false;
                info->requiresDedicatedAllocation = false;
                info->imageFormatProps2 = {
                    VK_STRUCTURE_TYPE_IMAGE_FORMAT_PROPERTIES_2,
                    nullptr,
                    plainProps,
                };
                info->extFormatProps = VkExternalImageFormatProperties{};
                static std::atomic<uint32_t> cibFallbackLogs{0};
                if (gfxstream::base::getEnvironmentVariable("GFXSTREAM_NIRI_IMAGE_TRACE") == "1" &&
                    cibFallbackLogs.fetch_add(1, std::memory_order_relaxed) < 16) {
                    GFXSTREAM_INFO("CIB1-DIAG-FMT-FALLBACK: fmt=%u tiling=%u usage=0x%x flags=0x%x "
                                   "plain=success external=0",
                                   (unsigned)info->format, (unsigned)info->tiling,
                                   (unsigned)info->usageFlags, (unsigned)info->createFlags);
                }
                return true;
            }
            if (plainRes == VK_ERROR_FORMAT_NOT_SUPPORTED) {
                info->supported = false;
                return true;
            }
            GFXSTREAM_ERROR(
                "vkGetPhysicalDeviceImageFormatProperties fallback failed with %s "
                "for format 0x%x type 0x%x usage 0x%x flags 0x%x",
                string_VkResult(plainRes), info->format, info->type, info->usageFlags,
                info->createFlags);
            return false;
        } else {''')

edit(ops, '''    // Collect image support info of the selected device
    for (size_t i = 0; i < emulation->mImageSupportInfo.mSupportInfos.size(); ++i) {
        emulation->populateImageFormatExternalMemorySupportInfo(ivk, emulation->mPhysicalDevice,
                                                                &emulation->mImageSupportInfo.mSupportInfos[i]);
    }
''', '''    // Collect image support info of the selected device
    for (size_t i = 0; i < emulation->mImageSupportInfo.mSupportInfos.size(); ++i) {
        emulation->populateImageFormatExternalMemorySupportInfo(ivk, emulation->mPhysicalDevice,
                                                                &emulation->mImageSupportInfo.mSupportInfos[i]);
    }
    {
        static std::atomic<uint32_t> cibFormatLogs{0};
        if (gfxstream::base::getEnvironmentVariable("GFXSTREAM_NIRI_IMAGE_TRACE") == "1" &&
            cibFormatLogs.fetch_add(1, std::memory_order_relaxed) < 4) {
            uint32_t bgraAll = 0, bgraMutable = 0, bgraExtended = 0;
            uint32_t bgraSupported = 0, bgraMutableSupported = 0;
            uint32_t rgbaAll = 0, rgbaMutable = 0, rgbaExtended = 0;
            uint32_t rgbaSupported = 0, rgbaMutableSupported = 0;
            for (const auto& info : emulation->mImageSupportInfo.mSupportInfos) {
                if (info.format == VK_FORMAT_B8G8R8A8_UNORM) {
                    ++bgraAll;
                    if (info.createFlags & VK_IMAGE_CREATE_MUTABLE_FORMAT_BIT) ++bgraMutable;
                    if (info.createFlags & VK_IMAGE_CREATE_EXTENDED_USAGE_BIT) ++bgraExtended;
                    if (info.supported) {
                        ++bgraSupported;
                        if ((info.createFlags & (VK_IMAGE_CREATE_MUTABLE_FORMAT_BIT |
                                                 VK_IMAGE_CREATE_EXTENDED_USAGE_BIT)) ==
                            VK_IMAGE_CREATE_MUTABLE_FORMAT_BIT) ++bgraMutableSupported;
                    }
                } else if (info.format == VK_FORMAT_R8G8B8A8_UNORM) {
                    ++rgbaAll;
                    if (info.createFlags & VK_IMAGE_CREATE_MUTABLE_FORMAT_BIT) ++rgbaMutable;
                    if (info.createFlags & VK_IMAGE_CREATE_EXTENDED_USAGE_BIT) ++rgbaExtended;
                    if (info.supported) {
                        ++rgbaSupported;
                        if ((info.createFlags & (VK_IMAGE_CREATE_MUTABLE_FORMAT_BIT |
                                                 VK_IMAGE_CREATE_EXTENDED_USAGE_BIT)) ==
                            VK_IMAGE_CREATE_MUTABLE_FORMAT_BIT) ++rgbaMutableSupported;
                    }
                }
            }
            GFXSTREAM_INFO("CIB1-DIAG-FMT-DEFAULT: bgra-all=%u bgra-mutable=%u bgra-extended=%u "
                           "bgra-supported=%u bgra-mutable-supported=%u rgba-all=%u rgba-mutable=%u "
                           "rgba-extended=%u rgba-supported=%u rgba-mutable-supported=%u",
                           bgraAll, bgraMutable, bgraExtended, bgraSupported, bgraMutableSupported,
                           rgbaAll, rgbaMutable, rgbaExtended, rgbaSupported, rgbaMutableSupported);
        }
    }
''')
print('Added CIB1 MUTABLE-only BGRA8/RGBA8 entries, plain-query fallback and bounded support summary')
