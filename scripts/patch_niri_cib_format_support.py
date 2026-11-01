#!/usr/bin/env python3
"""CIB1: add MUTABLE-only BGRA8/RGBA8 default support combos and a bounded support summary."""
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
edit(formats, '''        {VK_FORMAT_B8G8R8A8_UNORM,
         VK_IMAGE_CREATE_MUTABLE_FORMAT_BIT | VK_IMAGE_CREATE_EXTENDED_USAGE_BIT},
''', '''        {VK_FORMAT_B8G8R8A8_UNORM,
         VK_IMAGE_CREATE_MUTABLE_FORMAT_BIT | VK_IMAGE_CREATE_EXTENDED_USAGE_BIT},

        // CIB1: classic guest BGRA8/RGBA8 color buffers are created with
        // MUTABLE_FORMAT only. EXTENDED_USAGE is not required for that path and
        // can make the native AHB query reject every BGRA8 combination. Keep the
        // default EXTENDED entries above; the real native query below decides
        // whether these entries are supported.
        {VK_FORMAT_R8G8B8A8_UNORM, VK_IMAGE_CREATE_MUTABLE_FORMAT_BIT},
        {VK_FORMAT_B8G8R8A8_UNORM, VK_IMAGE_CREATE_MUTABLE_FORMAT_BIT},
''')

ops = 'host/vulkan/vk_common_operations.cpp'
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
print('Added CIB1 MUTABLE-only BGRA8/RGBA8 default support combos and bounded support summary')
