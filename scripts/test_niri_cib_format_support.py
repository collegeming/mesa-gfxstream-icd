#!/usr/bin/env python3
"""Source invariants for CIB1 MUTABLE-only BGRA8/RGBA8 default support entries."""
import argparse
from pathlib import Path
p = argparse.ArgumentParser(description=__doc__)
p.add_argument('root', type=Path)
p.add_argument('workflow', type=Path, nargs='?')
args = p.parse_args()
root = args.root.resolve()

def read(rel):
    return (root / rel).read_text()

formats = read('host/vulkan/vk_format_support.cpp')
ops = read('host/vulkan/vk_common_operations.cpp')
if args.workflow:
    workflow = args.workflow.resolve().read_text()
else:
    candidates = [
        root / '.github/workflows/build-droidvm-mali-host.yml',
        root.parent / '.github/workflows/build-droidvm-mali-host.yml',
        root.parent / 'ci/.github/workflows/build-droidvm-mali-host.yml',
    ]
    workflow_path = next((x for x in candidates if x.is_file()), None)
    assert workflow_path is not None, 'CI workflow not found'
    workflow = workflow_path.read_text()

start = formats.index('ImageSupport ImageSupport::GetDefaultUnpopulatedImageSupport()')
defaults = formats[start:formats.index('return imageSupport;', start)]

# The guest classic BGRA8/RGBA8 color-buffer path uses MUTABLE_FORMAT only.
assert defaults.count('{VK_FORMAT_R8G8B8A8_UNORM, VK_IMAGE_CREATE_MUTABLE_FORMAT_BIT},') == 1
assert defaults.count('{VK_FORMAT_B8G8R8A8_UNORM, VK_IMAGE_CREATE_MUTABLE_FORMAT_BIT},') == 1
# Existing EXTENDED_USAGE entries remain; no other default combo is removed.
assert defaults.count('{VK_FORMAT_R8G8B8A8_UNORM,\n         VK_IMAGE_CREATE_MUTABLE_FORMAT_BIT | VK_IMAGE_CREATE_EXTENDED_USAGE_BIT},') == 1
assert defaults.count('{VK_FORMAT_B8G8R8A8_UNORM,\n         VK_IMAGE_CREATE_MUTABLE_FORMAT_BIT | VK_IMAGE_CREATE_EXTENDED_USAGE_BIT},') == 1
for token in ['for (auto combo : combos)', 'for (auto t : types)', 'for (auto ti : tilings)',
              'for (auto u : usageFlags)']:
    assert token in defaults
assert 'imageSupport.mSupportInfos.emplace_back' in defaults

# The summary is bounded and reports both supported MUTABLE-only entries. The
# actual enumerations are still decided by populateImageFormatExternalMemorySupportInfo.
assert ops.count('CIB1-DIAG-FMT-DEFAULT') == 1
assert 'static std::atomic<uint32_t> cibFormatLogs{0};' in ops
assert 'cibFormatLogs.fetch_add(1, std::memory_order_relaxed) < 4' in ops
summary = ops[ops.index('static std::atomic<uint32_t> cibFormatLogs{0};'):
              ops.index('CIB1-DIAG-FMT-DEFAULT')]
assert 'GFXSTREAM_NIRI_IMAGE_TRACE' in summary
for token in ['bgra-all=%u bgra-mutable=%u bgra-extended=%u',
              'bgra-supported=%u bgra-mutable-supported=%u',
              'rgba-supported=%u rgba-mutable-supported=%u']:
    assert token in ops
assert ops.index('populateImageFormatExternalMemorySupportInfo(ivk, emulation->mPhysicalDevice,') < \
    ops.index('CIB1-DIAG-FMT-DEFAULT')

# No bridge-specific branch was added to the generic ColorBuffer image creation path.
generate = ops[ops.index('std::unique_ptr<VkImageCreateInfo> VkEmulation::generateColorBufferVkImageCreateInfoLocked('):
               ops.index('std::unique_ptr<VkImageCreateInfo> VkEmulation::generateColorBufferVkImageCreateInfo(', 1)]
assert 'ClassicImageBridge' not in generate
assert 'cib::' not in generate
create_start = ops.index('bool VkEmulation::createVkColorBufferLocked(')
create_end = ops.index('\n}\n', create_start) + 3
create_cb = ops[create_start:create_end]
assert 'ClassicImageBridge' not in create_cb
assert 'cib::' not in create_cb

# Publication wiring.
assert workflow.index('patch_niri_cib_diagnostics.py') < workflow.index('patch_niri_cib_format_support.py')
assert workflow.index('test_niri_cib_format_support.py') < workflow.index('cp ci/scripts/*niri_cib_format_support* out/')
assert "scripts/*niri_cib_format_support*" in workflow
assert '"niri_cib_format_support"' in workflow
assert 'diagnostic/niri-cib-readiness' in workflow

print('PASS: CIB1 MUTABLE-only BGRA8/RGBA8 default combos added, EXTENDED entries retained, bounded support summary present, no bridge special-case')
