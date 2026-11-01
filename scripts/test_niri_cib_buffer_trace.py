#!/usr/bin/env python3
"""Source invariants for bounded buffer-create diagnostics and the no-external experiment."""
import argparse
from pathlib import Path
p = argparse.ArgumentParser(description=__doc__)
p.add_argument('root', type=Path)
p.add_argument('workflow', type=Path, nargs='?')
args = p.parse_args()
root = args.root.resolve()

def read(rel):
    return (root / rel).read_text()

buffer_cpp = read('host/buffer.cpp')
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

assert '#include <atomic>' in buffer_cpp
assert '#include "gfxstream/system/System.h"' in buffer_cpp

buf_start = buffer_cpp.index('std::unique_ptr<Buffer::Impl> Buffer::Impl::create(')
buf_end = buffer_cpp.index('std::unique_ptr<Buffer::Impl> Buffer::Impl::onLoad(', buf_start)
buffer_create = buffer_cpp[buf_start:buf_end]
for tag in ['CIB1-DIAG-BUF-CREATE', 'CIB1-DIAG-BUF-GL', 'CIB1-DIAG-BUF-VK']:
    assert buffer_create.count(tag) == 1, tag
for token in ['handle=%u size=%llu emulation-gl=%d emulation-vk=%d',
              'CIB1-DIAG-BUF-GL: handle=%u created=%d',
              'CIB1-DIAG-BUF-VK: handle=%u created=%d vulkan-only=%d',
              'buffer->mBufferGl = emulationGl->createBuffer(size, handle);',
              'buffer->mBufferVk = vk::BufferVk::create(*emulationVk, handle, size, vulkanOnly);']:
    assert token in buffer_create, token
assert buffer_create.count('< 16') == 3
assert buffer_create.count('GFXSTREAM_NIRI_IMAGE_TRACE') == 1
assert 'if (!buffer->mBufferGl) {' in buffer_create
assert 'if (!buffer->mBufferVk) {' in buffer_create

setup_start = ops.index('bool VkEmulation::setupVkBuffer(')
setup_end = ops.index('bool VkEmulation::teardownVkBuffer(', setup_start)
setup = ops[setup_start:setup_end]
for tag in ['CIB1-DIAG-BUF-VK-ENTRY', 'CIB1-DIAG-BUF-VK-EXTPROPS',
            'CIB1-DIAG-BUF-VK-EXTERNAL-DISABLED', 'CIB1-DIAG-BUF-VK-CREATE',
            'CIB1-DIAG-BUF-VK-REQ', 'CIB1-DIAG-BUF-VK-ALLOC', 'CIB1-DIAG-BUF-VK-BIND']:
    assert setup.count(tag) == 1, tag
for token in ['handle=%u size=%llu vulkan-only=%d',
              'memory-property=0x%x import=%d export=%d default-handle=0x%x',
              'CIB1-DIAG-BUF-VK-EXTPROPS: handle=%u available=%d queried=%d',
              'features=0x%x export=0x%x compatible=0x%x',
              'CIB1-DIAG-BUF-VK-EXTERNAL-DISABLED: handle=%u',
              'CIB1-DIAG-BUF-VK-CREATE: handle=%u result=%d ext-ci=%d',
              'size=%llu alignment=%llu bits=0x%x',
              'type-index=%u dedicated=%d',
              'CIB1-DIAG-BUF-VK-ALLOC: handle=%u result=%d memory=%llu',
              'CIB1-DIAG-BUF-VK-BIND: handle=%u result=%d']:
    assert token in setup, token
assert setup.count('< 16') == 7
assert setup.count('GFXSTREAM_NIRI_IMAGE_TRACE') == 1
assert setup.count('GFXSTREAM_NIRI_BUFFER_NO_EXTERNAL') == 1

# Original decisions and driver calls remain; the experiment only skips attaching
# the external create-info structure when its env is exactly 1.
for token in ['if (vulkanOnly == false) {', 'VkResult createRes = vk->vkCreateBuffer(mDevice, &bufferCi, nullptr, &res.buffer);',
              'if (createRes != VK_SUCCESS) {', 'bool allocRes = allocExternalMemory(vk, &res.memory, deviceAlignment, dedicated_buffer);',
              'if (!allocRes) {', 'VkResult bindBufferMemoryRes =',
              'vk->vkBindBufferMemory(mDevice, res.buffer, res.memory.memory, 0);',
              'if (bindBufferMemoryRes != VK_SUCCESS) {', 'return bindBufferMemoryRes;',
              'return allocRes;', 'extBufferCiPtr = &extBufferCi;',
              'vk->vkGetPhysicalDeviceExternalBufferProperties(mPhysicalDevice, &extInfo, &extProps);']:
    assert token in setup, token
assert 'if (cibNoExternalBuffer) {' in setup
assert 'extBufferCiPtr = &extBufferCi;' in setup.split('if (cibNoExternalBuffer) {', 1)[1]
assert 'getDefaultExternalMemoryHandleType()' in setup
assert 'vkGetPhysicalDeviceExternalBufferPropertiesKHR' not in setup
assert 'available = vk->vkGetPhysicalDeviceExternalBufferProperties != nullptr' in setup

assert workflow.index('patch_niri_cib_resource_diag.py') < workflow.index('patch_niri_cib_buffer_trace.py')
assert workflow.index('test_niri_cib_buffer_trace.py') < workflow.index('cp ci/scripts/*niri_cib_buffer_trace* out/')
assert "scripts/*niri_cib_buffer_trace*" in workflow
assert '"niri_cib_buffer_trace"' in workflow
assert 'diagnostic/niri-cib-buffer-trace' in workflow

print('PASS: bounded buffer create/entry/create/req/alloc/bind diagnostics present; default behavior and external experiment switch preserved')
