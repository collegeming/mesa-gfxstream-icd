#!/usr/bin/env python3
"""Source invariants for the classic-buffer dma-buf export; AHB image path retained."""
import argparse
from pathlib import Path

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('root', type=Path)
p.add_argument('workflow', type=Path, nargs='?')
args = p.parse_args()
root = args.root.resolve()


def read(rel):
    return (root / rel).read_text()


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

setup = ops[ops.index('bool VkEmulation::setupVkBuffer('):
           ops.index('bool VkEmulation::teardownVkBuffer(')]
# The buffer is created external for the fd handle type in AHB mode.
assert setup.count('VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT') == 1
assert 'if (cibAhbMode && !cibNoExternalBuffer && mDeviceInfo.supportsDmaBuf &&' in setup
assert 'GFXSTREAM_BUFFER_HOST_PTR") != "1"' in setup
assert 'extBufferCiPtr = &extBufferCi;' in setup

alloc = ops[ops.index('bool VkEmulation::allocExternalMemory('):
           ops.index('void VkEmulation::freeExternalMemoryLocked(')]
# The allocation is exportable as a dma-buf for plain buffers only.
assert 'if (getExternalMemoryMode() == ExternalMemory::Mode::AndroidAHB &&' in alloc
assert '!colorBufferInfo.hasValue() && mDeviceInfo.supportsDmaBuf)' in alloc
# Two declarations before allocation (the pre-existing exportAi OR and this patch's plain
# buffer exportAi) and one in the buffer export block; the OpaqueFd case keeps its own.
prealloc = alloc[:alloc.index('switch (getExternalMemoryMode())')]
assert prealloc.count('VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT') == 2
# The skipped AHB export is replaced by a real fd export for plain buffers.
skip_pos = alloc.index('if (!colorBufferInfo) {')
export_pos = alloc.index('vk->vkGetMemoryAndroidHardwareBufferANDROID(')
assert skip_pos < export_pos
skip_block = alloc[skip_pos:export_pos]
assert 'vk->vkGetMemoryFdKHR(' in skip_block
assert 'VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT' in skip_block
assert 'STREAM_HANDLE_TYPE_MEM_DMABUF' in skip_block
assert 'CIB1-DIAG-BUF-VK-DMABUF-EXPORT' in skip_block
assert 'return true;' in skip_block
# The ColorBuffer/image AHB export path is unchanged and still used.
assert alloc.count('vk->vkGetMemoryAndroidHardwareBufferANDROID(') == 1
assert 'if (colorBufferInfo) {' in alloc[export_pos:]
assert 'externalMemoryCompatible = true;' in alloc

# Ordering: after the AHB skip exists, before the memfd host-pointer experiment.
assert workflow.index('patch_niri_cib_ahb_buffer_fix.py') < workflow.index(
    'patch_cib_buffer_dmabuf_export.py')
assert workflow.index('patch_cib_buffer_dmabuf_export.py') < workflow.index(
    'test_cib_buffer_dmabuf_export.py')
assert "scripts/*cib_buffer_dmabuf_export*" in workflow
assert 'cp ci/scripts/*cib_buffer_dmabuf_export* out/' in workflow

print('PASS: classic buffers export dma-buf in AHB mode; ColorBuffer/image AHB path retained')
