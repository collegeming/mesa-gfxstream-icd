#!/usr/bin/env python3
"""Source invariants for the completion-drain fence liveness fix."""
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

drain_start = dec.index('void drainSignalledEmulatedCompletions(VkDevice device, VulkanDispatch* vk) {')
drain_end = dec.index('void invalidateEmulatedCompletionsAfterWait(', drain_start)
drain = dec[drain_start:drain_end]
# Only tracked fences are queried, and the query happens under mMutex.
assert 'mFenceInfo.find(it.first) == mFenceInfo.end()' in drain
assert 'vk->vkGetFenceStatus(device, it.first)' in drain
assert 'std::vector<VkFence> candidates;' not in drain
lock_pos = drain.index('std::lock_guard<std::mutex> lock(mMutex);')
query_pos = drain.index('vk->vkGetFenceStatus(device, it.first)')
assert lock_pos < query_pos
assert 'takeEmulatedCompletionMaintenanceLocked(' in drain
# No maintenance is invented for a fence that is gone.
assert 'if (completed.empty()) return;' in drain

# The destroy path settles the record before the driver object is destroyed.
destroy_start = dec.index('    void on_vkDestroyFence(')
destroy_end = dec.index('    VkResult on_vkCreateDescriptorSetLayout(', destroy_start)
destroy = dec[destroy_start:destroy_end]
assert 'mEmulatedCompletions.find(fence)' in destroy
assert 'takeEmulatedCompletionMaintenanceLocked(device, &fence, 1)' in destroy
assert destroy.index('applyCacheMaintenance') < destroy.index('destroyFenceLocked')

assert workflow.index('patch_droidvm_mali_host.py') < workflow.index(
    'patch_completion_fence_liveness.py')
assert workflow.index('patch_completion_fence_liveness.py') < workflow.index(
    'test_completion_fence_liveness.py')
assert "scripts/*completion_fence_liveness*" in workflow
assert 'cp ci/scripts/*completion_fence_liveness* out/' in workflow

print('PASS: drain queries only live tracked fences under mMutex; fence destroy settles first')
