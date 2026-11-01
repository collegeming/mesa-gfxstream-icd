#!/usr/bin/env python3
"""One-word read-only diagnostic before native backing publication."""
import argparse
from pathlib import Path
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('root', type=Path)
root = parser.parse_args().root.resolve()
def edit(rel, old, new):
    path = root / rel
    text = path.read_text()
    if text.count(old) != 1:
        raise SystemExit(f'{rel}: expected one anchor ({text.count(old)}): {old[:80]}')
    path.write_text(text.replace(old, new))
d = 'host/vulkan/vk_decoder_global_state.cpp'
helper = Path(__file__).with_name('native_cpu_prefault.h').read_text().replace('#pragma once\n', '')
edit(d, '#include "vk_decoder_global_state.h"', '#include "vk_decoder_global_state.h"\n' + helper)
method = '''    void nativeCpuPrefaultLocked(VkDevice device, VkDeviceMemory memory, MemoryInfo& info,
                                 uint64_t blob) REQUIRES(mMutex) {
        const auto* deviceInfo = gfxstream::base::find(mDeviceInfo, device);
        const auto* instance = deviceInfo ?
            gfxstream::base::find(mInstanceInfo, deviceInfo->parentInstance) : nullptr;
        const int app = instance ? native_prefault::application(
            gfxstream::base::getEnvironmentVariable("GFXSTREAM_NATIVE_CPU_PREFAULT").c_str(),
            instance->applicationName.c_str()) : -1;
        if (app < 0) return;
        std::lock_guard<std::mutex> lifecycle(native::Session::get().lifecycle);
        const auto owner = info.nativeOwner;
        const auto* phys = gfxstream::base::find(mPhysdevInfo, deviceInfo->physicalDevice);
        const auto physical = phys ? phys->memoryPropertiesHelper
            ->getHostMemoryInfoFromHostMemoryTypeIndex(info.memoryIndex) : std::nullopt;
        const auto required = VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT;
        const long page = sysconf(_SC_PAGESIZE);
        native_prefault::Guard guard{
            info.size, page > 0 ? static_cast<uint64_t>(page) : 0,
            owner && owner->generation() && owner->size() == info.size,
            owner && native::Session::get().ready(owner->epoch()),
            owner && info.needUnmap && info.ptr && info.ptr == owner->address(),
            info.memoryIndex == 8 && physical &&
                (physical->memoryType.propertyFlags & required) == required};
        uint32_t initial = 0;
        if (native_prefault::touch(app, guard, info.ptr, native_prefault::processBudget(), initial, [&] {
                GFXSTREAM_INFO("NATIVE-CPU-PREFAULT-BEGIN: phase=before-publication app=%s memory=%p gen=%llu blob=%llu "
                    "ptr=%p size=%llu offset=0 width=4 read-only=1",
                    instance->applicationName.c_str(), (void*)memory,
                    (unsigned long long)owner->generation(), (unsigned long long)blob,
                    info.ptr, (unsigned long long)info.size);
            })) {
            GFXSTREAM_INFO("NATIVE-CPU-PREFAULT: phase=before-publication app=%s memory=%p gen=%llu blob=%llu "
                "ptr=%p size=%llu offset=0 width=4 initial=0x%08x read-only=1 ordering=dsb-sy no-cache-maintenance",
                instance->applicationName.c_str(), (void*)memory,
                (unsigned long long)owner->generation(), (unsigned long long)blob,
                info.ptr, (unsigned long long)info.size, initial);
        } else {
            static std::atomic<unsigned> rejected{0};
            if (rejected.fetch_add(1) < 2) GFXSTREAM_INFO(
                "NATIVE-CPU-PREFAULT: skipped invalid-or-budget app=%s no-payload-access",
                instance->applicationName.c_str());
        }
    }

'''
edit(d, '    bool nativeTraceDeviceLocked(VkDevice device) REQUIRES(mMutex) {', method + '    bool nativeTraceDeviceLocked(VkDevice device) REQUIRES(mMutex) {')
edit(d, '''            if (info->nativeOwner) info->nativeOwner->mapped(info->ptr);''', '''            if (info->nativeOwner) info->nativeOwner->mapped(info->ptr);
            nativeCpuPrefaultLocked(device, memory, *info, hostBlobId);''')
print('Applied optional bounded one-word prefault before native publication; diagnostic only')
