#!/usr/bin/env python3
"""Source invariants for the AHB null-handle fail-closed guards."""
import argparse
from pathlib import Path

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('root', type=Path)
p.add_argument('workflow', type=Path, nargs='?')
args = p.parse_args()
root = args.root.resolve()


def read(rel):
    return (root / rel).read_text(encoding='utf-8')


dec = read('host/vulkan/vk_decoder_global_state.cpp')
ops = read('host/vulkan/vk_common_operations.cpp')

# ---------------------------------------------------------------------------
# 1. Import refuses handle 0 before it reaches importInfo.buffer.
# ---------------------------------------------------------------------------
assert dec.count('AHB-NULL-GUARD-1') == 1
assign = 'importInfo.buffer = reinterpret_cast<AHardwareBuffer*>(handleInfo.handle);'
assert dec.count(assign) == 1
branch = dec.index('case ExternalMemoryImportKind::AndroidHardwareBuffer:')
assign_at = dec.index(assign)
assert branch < assign_at
guard = dec[branch:assign_at]
assert 'if (handleInfo.handle == 0) {' in guard
assert 'return false;' in guard
# The pinned dangerous sequence -- comment straight into the assignment -- is gone.
assert '''Select the import struct by the actual handle type.
                    importInfo.buffer = reinterpret_cast<AHardwareBuffer*>''' not in dec

# ---------------------------------------------------------------------------
# 2. A failed AHB export never publishes {handle=0, PLATFORM_AHB}.
# ---------------------------------------------------------------------------
assert ops.count('AHB-NULL-GUARD-2') == 1
assert '''                validHandle = (VK_SUCCESS == exportRes) && (NULL != exportHandle);
                info->handleInfo = ExternalHandleInfo{''' not in ops
guard_at = ops.index('AHB-NULL-GUARD-2')
guard = ops[guard_at:guard_at + 1200]
assert 'if (validHandle) {' in guard
assert '.handle = reinterpret_cast<ExternalHandleType>(exportHandle),' in guard
assert '.streamHandleType = STREAM_HANDLE_TYPE_PLATFORM_AHB,' in guard
assert 'validHandle = (VK_SUCCESS == exportRes) && (NULL != exportHandle);' in ops

# ---------------------------------------------------------------------------
# 3. Export success with a null handle returns no descriptor.
# ---------------------------------------------------------------------------
assert dec.count('AHB-NULL-GUARD-3') == 1
export_call = dec.index(
    'vk->vkGetMemoryAndroidHardwareBufferANDROID(device, &getAhbInfo, &exportHandle);')
guard_at = dec.index('AHB-NULL-GUARD-3')
result_check = dec.index('if (res != VK_SUCCESS) {', export_call)
ret_handle = dec.index(
    'ret.handle = reinterpret_cast<ExternalHandleType>(exportHandle);', export_call)
assert export_call < result_check < guard_at < ret_handle
guard = dec[guard_at:ret_handle]
assert 'if (exportHandle == nullptr) {' in guard
assert 'return std::nullopt;' in guard

# ---------------------------------------------------------------------------
# 4. Only a non-null handle is released.
# ---------------------------------------------------------------------------
assert ops.count('AHB-NULL-GUARD-4') == 1
assert '''            case STREAM_HANDLE_TYPE_PLATFORM_AHB:
                AHardwareBuffer_release(static_cast<AHardwareBuffer*>(
                    reinterpret_cast<void*>(info->handleInfo->handle)));''' not in ops
guard_at = ops.index('AHB-NULL-GUARD-4')
guard = ops[guard_at:guard_at + 500]
assert 'if (info->handleInfo->handle != 0) {' in guard
assert 'AHardwareBuffer_release(static_cast<AHardwareBuffer*>(' in guard
assert 'STREAM_HANDLE_TYPE_MEM_DMABUF' in ops[ops.index('void VkEmulation::freeExternalMemoryLocked'):guard_at]

if args.workflow:
    workflow = args.workflow.resolve().read_text(encoding='utf-8')
else:
    candidates = [
        root / '.github/workflows/build-droidvm-mali-host.yml',
        root.parent / '.github/workflows/build-droidvm-mali-host.yml',
        root.parent / 'ci/.github/workflows/build-droidvm-mali-host.yml',
    ]
    workflow_path = next((x for x in candidates if x.is_file()), None)
    assert workflow_path is not None, 'CI workflow not found'
    workflow = workflow_path.read_text(encoding='utf-8')

assert workflow.index('patch_ahb_null_guard.py') < workflow.index('test_ahb_null_guard.py')
assert "scripts/*ahb_null_guard*" in workflow
assert 'cp ci/scripts/*ahb_null_guard* out/' in workflow

print('PASS: all four AHB null-handle guards fail closed; dangerous writes are gone')
