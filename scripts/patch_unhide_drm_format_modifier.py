#!/usr/bin/env python3
"""Expose VK_EXT_image_drm_format_modifier to the guest instead of hiding it.

The host hides the extension so guest Mesa takes its LINEAR modifier-emulation path, because on
turnip/QCOM the only host modifier is QCOM-tiled and cannot agree with a LINEAR importer. This
Mali driver is the opposite shape: it rejects the emulated plain
VK_IMAGE_TILING_LINEAR external DMA_BUF image and accepts the explicit LINEAR modifier with its
plane layout. Only the hidden-extension list changes; every other filter rule stays as it is.
"""
import argparse
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('root', type=Path)
root = parser.parse_args().root.resolve()

path = root / 'host/vulkan/vk_decoder_global_state.cpp'
text = path.read_text()

OLD = '''        static const char* const kGuestHiddenDeviceExtensions[] = {
            VK_EXT_IMAGE_DRM_FORMAT_MODIFIER_EXTENSION_NAME,
        };
'''
NEW = '''        // Mali: keep the extension visible. The guest's emulated form is a plain
        // VK_IMAGE_TILING_LINEAR external DMA_BUF image, which this driver rejects; the explicit
        // LINEAR modifier plus plane layout is the shape it accepts. The sentinel keeps the
        // filtering path (and its ycbcr/protected additions) active while hiding nothing.
        static const char* const kGuestHiddenDeviceExtensions[] = {
            "__droidvm_never_hidden_extension",
        };
'''
if text.count(OLD) != 1:
    raise SystemExit(f'expected one hidden-extension anchor, found {text.count(OLD)}')
text = text.replace(OLD, NEW)
path.write_text(text, encoding='utf-8', newline='')
print('exposed VK_EXT_image_drm_format_modifier to the guest')
