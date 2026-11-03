#!/usr/bin/env python3
"""Offline invariant test for the CIB1 LINEAR AHB alias (no GPU, no driver calls).

Two independent checks:

1. Mask arithmetic with the masks recorded from the device logs:
     - AHB importable mask                     0x2      (mali-dmabuf-gpu-device.log:332)
     - pre-fix OPTIMAL alias image requirements 0x1040   (desktop-ahbfix-host-live.log:694)
   The old combination is rejected; the LINEAR policy is accepted because a linear
   image belongs to the host-visible cached group (flags 0xb types {1,9..14},
   recorded as 0x7e02 by vkGetMemoryFdPropertiesKHR), and the intersection with the
   AHB mask selects memory type 1 exactly as getValidMemoryTypeIndex() does.

   This is a policy invariant over recorded masks, not a claim that Mali has been
   measured reporting 0x7e02 for a linear image; the production path stays
   fail-closed and the device probe verifies the real mask.

2. Production-source invariants: the LINEAR selection preserves format/usage/flags
   (niri: fmt 44/50, usage 0x97, MUTABLE_FORMAT/EXTENDED_USAGE), only accepts linear
   guest images and explicit DRM_FORMAT_MOD_LINEAR modifiers, and leaves the
   AHB-RETAIN / AHB-SIZE-MISMATCH / forced-AHB-bits guards untouched.
"""
import argparse
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('root', type=Path)
parser.add_argument('workflow', type=Path, nargs='?')
args = parser.parse_args()
root = args.root.resolve()

# Memory type table recorded in mali-dmabuf-gpu-device.log:225-246.
# 0x7 = DEVICE_LOCAL|HOST_VISIBLE|HOST_COHERENT, 0xb = DEVICE_LOCAL|HOST_VISIBLE|HOST_CACHED,
# 0x11 = DEVICE_LOCAL|LAZILY_ALLOCATED, 0x21 = DEVICE_LOCAL|PROTECTED.
MEMORY_TYPE_FLAGS = [
    0x7, 0xb, 0x11, 0x7, 0x7, 0x7, 0x7, 0x7, 0x7, 0xb, 0xb,
    0xb, 0xb, 0xb, 0xb, 0x11, 0x11, 0x11, 0x11, 0x11, 0x11, 0x21,
]
DEVICE_LOCAL = 0x1
HOST_VISIBLE = 0x2
HOST_COHERENT = 0x4
HOST_CACHED = 0x8

AHB_MASK = 0x2                 # vkGetAndroidHardwareBufferPropertiesANDROID
OPTIMAL_IMAGE_BITS = 0x1040    # pre-fix optimal alias image requirements
CACHED_HOST_VISIBLE_MASK = 0x7e02  # recorded fd-properties mask (types 1, 9-14)


def mask_of(flags_predicate):
    mask = 0
    for index, flags in enumerate(MEMORY_TYPE_FLAGS):
        if flags_predicate(flags):
            mask |= 1 << index
    return mask


def valid_memory_type(bits, required_properties):
    """Mirror VkEmulation::getValidMemoryTypeIndex: exact match first, else the
    last index whose flags overlap every requested property (C++ uses != 0)."""
    second_best = None
    for index, flags in enumerate(MEMORY_TYPE_FLAGS):
        if not (bits & (1 << index)):
            continue
        if flags == required_properties:
            return index
        if required_properties and (flags & required_properties) != 0:
            second_best = index
    return second_best


assert len(MEMORY_TYPE_FLAGS) == 22
assert mask_of(lambda f: (f & HOST_VISIBLE) != 0) == 0x7ffb
assert mask_of(lambda f: (f & HOST_VISIBLE) and (f & HOST_CACHED) and
               not (f & HOST_COHERENT)) == CACHED_HOST_VISIBLE_MASK
assert mask_of(lambda f: (f & DEVICE_LOCAL) and not (f & HOST_VISIBLE)) == 0x3f8004

# 1a. The old parameter combination is rejected: AHB 0x2 vs image 0x1040.
old_combined = AHB_MASK & OPTIMAL_IMAGE_BITS
assert old_combined == 0, hex(old_combined)
print('PASS: old OPTIMAL alias (image bits 0x1040) and AHB bits 0x2 intersect in nothing '
      '-> "No common memory type" / forced-AHB-bits path')

# 1b. The chosen LINEAR parameters are accepted under the recorded memory model.
linear_combined = AHB_MASK & CACHED_HOST_VISIBLE_MASK
assert linear_combined == 0x2, hex(linear_combined)
selected = valid_memory_type(linear_combined, DEVICE_LOCAL)
assert selected == 1, selected
assert MEMORY_TYPE_FLAGS[selected] == 0xb
assert MEMORY_TYPE_FLAGS[selected] & DEVICE_LOCAL
assert (MEMORY_TYPE_FLAGS[selected] & HOST_VISIBLE) and (MEMORY_TYPE_FLAGS[selected] & HOST_CACHED)
assert not (MEMORY_TYPE_FLAGS[selected] & HOST_COHERENT)
print('PASS: LINEAR alias (cached host-visible 0x7e02) and AHB bits 0x2 intersect in bit 1; '
      'getValidMemoryTypeIndex selects type 1 (flags 0xb)')

# 1c. Preserved niri parameters: fmt 44/50, usage 0x97, MUTABLE_FORMAT (0x8) /
#     EXTENDED_USAGE (0x100).  Only tiling changes, so these remain valid.
NIRI_FORMATS = (44, 50)
NIRI_USAGE = 0x97
MUTABLE_FORMAT = 0x8
EXTENDED_USAGE = 0x100
assert NIRI_USAGE == 0x80 | 0x10 | 0x4 | 0x2 | 0x1  # INPUT|COLOR|SAMPLED|TRANSFER_DST|TRANSFER_SRC
assert MUTABLE_FORMAT | EXTENDED_USAGE == 0x108
print('PASS: preserved guest contract fmt 44/50-class %s usage=0x%x flags=0x8/0x108'
      % (NIRI_FORMATS, NIRI_USAGE))

common = (root / 'host/vulkan/vk_common_operations.cpp').read_text(encoding='utf-8')
decoder = (root / 'host/vulkan/vk_decoder_global_state.cpp').read_text(encoding='utf-8')

# 2a. ColorBuffer host image: LINEAR only under ClassicImageBridge + AndroidAHB,
#     with the optimal usage set preserved and a coverage gate.
assert common.count('const bool cibLinearAlias = mFeatures.ClassicImageBridge.enabled()') == 1
assert '(imageCi->usage & ~linearCi->usage) == 0' in common
assert 'linearCi->usage = imageCi->usage;' in common
assert 'GFXSTREAM_INFO("CIB1-LINEAR-ALIAS: color-buffer=' in common
assert 'linearCi->flags' not in common
start = common.index('const bool cibLinearAlias')
block = common[start:start + 1600]
assert 'VK_IMAGE_TILING_LINEAR' in block and 'VK_IMAGE_TILING_OPTIMAL' not in block
assert block.count('usage =') == 1, block.count('usage =')
assert 'flags =' not in block and 'format =' not in block
print('PASS: ColorBuffer LINEAR selection preserves factory format/usage/flags and falls back '
      'to OPTIMAL unless linear features cover every usage bit')

# 2b. AHB diagnostic and untouched guards.
assert 'GFXSTREAM_INFO("CIB1-LINEAR-ALIAS: resource=' in common
assert 'ahb-usage=0x%llx' in common and 'image-bits=0x%x' in common
assert 'AHB-SIZE-MISMATCH: ahb=%llu image=%llu; refusing import' in common
assert 'AHB-RETAIN: keep the AHB reference for the alias allocation' in common
assert 'Using AHB memory type bits for import.' in common
print('PASS: AHB usage/format/size diagnostics added; AHB-RETAIN, AHB-SIZE-MISMATCH and the '
      'forced-AHB-bits fallback are unchanged')

# 2c. Alias image: LINEAR tiling, linear guest tiling/modifier only, no usage/flags rewrite.
assert 'ci.tiling = VK_IMAGE_TILING_LINEAR;' in decoder
assert 'VK_IMAGE_TILING_DRM_FORMAT_MODIFIER_EXT' in decoder
assert 'explicitModifier->drmFormatModifier == 0' in decoder
assert 'return fail("tiling", VK_ERROR_FORMAT_NOT_SUPPORTED);' in decoder
assert 'ci.usage =' not in decoder and 'ci.flags =' not in decoder
assert 'GFXSTREAM_INFO("CIB1-LINEAR-ALIAS: image=' in decoder
# The guest-visible requirements stay the synthesized shadow mask, size from the AHB.
assert 'bridge.classicImageRequirements = {cb->memory.size, 1, uint32_t(1u << shadow)};' in decoder
print('PASS: alias creation forces LINEAR, accepts only LINEAR/DRM_FORMAT_MOD_LINEAR, and '
      'leaves usage/flags/format plus the guest shadow requirement unchanged')

# 2d. The CIB capability query describes the same LINEAR image.
assert decoder.count(
    'generateColorBufferVkImageCreateInfo(input->format, 1, 1, VK_IMAGE_TILING_LINEAR, 1)') == 1
print('PASS: CIB capability query uses the LINEAR native create info, matching creation')

if args.workflow is not None:
    workflow = args.workflow.read_text(encoding='utf-8')
    apply_line = 'python3 -I ci/scripts/patch_cib_linear_alias.py source'
    test_line = 'python3 -I ci/scripts/test_cib_linear_alias.py source'
    assert apply_line in workflow
    assert test_line in workflow
    assert workflow.index('patch_niri_cib_ahb_buffer_fix.py source') < workflow.index(apply_line)
    assert 'diagnostic/cib-linear-alias' in workflow
    assert 'scripts/*cib_linear_alias*' in workflow
    assert 'cp ci/scripts/*cib_linear_alias* out/' in workflow
    print('PASS: workflow applies the patch after the CIB patches, runs this test, copies the '
          'scripts to the artifact and triggers on the new files/branch')
print('PASS: CIB1 LINEAR AHB alias offline invariants')
