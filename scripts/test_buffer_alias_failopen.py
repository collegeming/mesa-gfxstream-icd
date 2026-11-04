#!/usr/bin/env python3
"""Source invariants for the classic-buffer alias fallback."""
import argparse
from pathlib import Path

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('root', type=Path)
p.add_argument('workflow', type=Path, nargs='?')
args = p.parse_args()
root = args.root.resolve()


def read(rel):
    return (root / rel).read_text()


dec = read('host/vulkan/vk_decoder_global_state.cpp')
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

assert dec.count('BUF-ALIAS-FALLBACK:') == 1
assert dec.count('bool bufferAliasPlainFallback = false;') == 1
# The old hard failure on a missing buffer handle is gone.
assert '''"with internal handle: %d",
                        importBufferInfoPtr->buffer);
                    return VK_ERROR_OUT_OF_DEVICE_MEMORY;''' not in dec
# The fallback allocates at the buffer's own host type, so the bind's memoryTypeBits check
# passes; the normal path keeps the guest-mapped shadow type.
assert 'bufferAliasPlainFallback ? aliasHostIndex : hostMemoryInfo.index' in dec
assert 'bufferAliasPlainFallback\n                ? physicalHostMemory->memoryType.propertyFlags' in dec
# The dup path is still reachable when a handle exists.
assert 'appendExternalMemoryImport(*dupHandleInfo)' in dec
assert 'dupBufferExtMemoryHandle(importBufferInfoPtr->buffer)' in dec

assert workflow.index('patch_cib_buffer_dmabuf_export.py') < workflow.index(
    'patch_buffer_alias_failopen.py')
assert workflow.index('patch_buffer_alias_failopen.py') < workflow.index(
    'test_buffer_alias_failopen.py')
assert "scripts/*buffer_alias_failopen*" in workflow
assert 'cp ci/scripts/*buffer_alias_failopen* out/' in workflow

print('PASS: buffer alias falls back to a fresh allocation at the buffer host type')
