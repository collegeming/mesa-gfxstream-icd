#!/usr/bin/env python3
"""Keep the emulated-completion drain off fences the driver may have destroyed.

The coherent maintenance records a submission by the fence the guest will wait on.
A guest fence can be destroyed as soon as the guest's wait observed it, and a
host-created fence belongs to the op tracker; querying either after that is a
driver use-after-free (observed as FORTIFY "pthread_mutex_lock called on a
destroyed mutex" inside vkGetFenceStatus right after a successful
vkWaitSemaphores). Only query fences still present in mFenceInfo, query them
under mMutex so a concurrent destroy cannot free them mid-call, and drain the
record for a guest fence before vkDestroyFence destroys it.

No maintenance is invented: records whose fence is gone are dropped, and the
queue-idle path still drains every record for a queue that has gone idle.
"""
import argparse
from pathlib import Path

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('root', type=Path)
root = p.parse_args().root.resolve()


def edit(rel, old, new):
    path = root / rel
    text = path.read_text(encoding='utf-8')
    if text.count(old) != 1:
        raise SystemExit(f'{rel}: expected one anchor ({text.count(old)}): {old[:100]}')
    path.write_text(text.replace(old, new), encoding='utf-8', newline='')


d = 'host/vulkan/vk_decoder_global_state.cpp'

# 1) The semaphore-wait drain: query only live, tracked fences and do it under mMutex.
edit(d, '''    void drainSignalledEmulatedCompletions(VkDevice device, VulkanDispatch* vk) {
        std::vector<VkFence> candidates;
        {
            std::lock_guard<std::mutex> lock(mMutex);
            for (auto& it : mEmulatedCompletions) {
                if (it.second.device != device) continue;
                candidates.push_back(it.first);
            }
        }
        if (candidates.empty()) return;
        std::vector<VkFence> completed;
        for (VkFence fence : candidates) {
            if (vk->vkGetFenceStatus(device, fence) == VK_SUCCESS) completed.push_back(fence);
        }
        if (completed.empty()) return;
        PendingMaintenance maintenance;
        {
            std::lock_guard<std::mutex> lock(mMutex);
            maintenance = takeEmulatedCompletionMaintenanceLocked(
                device, completed.data(), static_cast<uint32_t>(completed.size()));
        }
        applyCacheMaintenance(vk, maintenance);
    }''',
     '''    void drainSignalledEmulatedCompletions(VkDevice device, VulkanDispatch* vk) {
        // A record's fence is only a driver object while the host still tracks it: the guest
        // may destroy a fence the moment its own wait observed it, and a host-created fence is
        // owned by the op tracker. vkGetFenceStatus on either is a use-after-free, so skip
        // every fence that is no longer in mFenceInfo, and keep the query under mMutex so a
        // concurrent vkDestroyFence cannot free it between the check and the call.
        PendingMaintenance maintenance;
        {
            std::lock_guard<std::mutex> lock(mMutex);
            std::vector<VkFence> completed;
            for (auto& it : mEmulatedCompletions) {
                if (it.second.device != device) continue;
                if (mFenceInfo.find(it.first) == mFenceInfo.end()) continue;
                if (vk->vkGetFenceStatus(device, it.first) == VK_SUCCESS) {
                    completed.push_back(it.first);
                }
            }
            if (completed.empty()) return;
            maintenance = takeEmulatedCompletionMaintenanceLocked(
                device, completed.data(), static_cast<uint32_t>(completed.size()));
        }
        applyCacheMaintenance(vk, maintenance);
    }''')

# 2) Destroying a guest fence: settle the maintenance it still owes while the fence is
#    alive, then let the normal destroy path run. The record must not outlive the fence.
edit(d, '''        std::lock_guard<std::mutex> lock(mMutex);
        destroyFenceLocked(device, deviceDispatch, fence, /*allowExternalFenceRecycling=*/true);''',
     '''        std::lock_guard<std::mutex> lock(mMutex);
        {
            auto completionIt = mEmulatedCompletions.find(fence);
            if (completionIt != mEmulatedCompletions.end()) {
                PendingMaintenance maintenance;
                if (deviceDispatch->vkGetFenceStatus(device, fence) == VK_SUCCESS) {
                    maintenance = takeEmulatedCompletionMaintenanceLocked(device, &fence, 1);
                } else {
                    // Destroying an unfinished fence: no completion to invalidate for.
                    mEmulatedCompletions.erase(completionIt);
                }
                // The memories in the record cannot be freed while mMutex is held.
                applyCacheMaintenance(deviceDispatch, maintenance);
            }
        }
        destroyFenceLocked(device, deviceDispatch, fence, /*allowExternalFenceRecycling=*/true);''')

print('completion drain: live tracked fences only, queried under mMutex; fence destroy settles first')
