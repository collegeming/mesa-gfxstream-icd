#!/usr/bin/env python3
"""One-word prewrite diagnostic before native backing publication; read-prefault disabled."""
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
method = '''    void nativeCpuPrewriteLocked(VkDevice device, VkDeviceMemory memory, MemoryInfo& info,
                                 uint64_t blob) REQUIRES(mMutex) {
        const auto* deviceInfo = gfxstream::base::find(mDeviceInfo, device);
        const auto* instance = deviceInfo ?
            gfxstream::base::find(mInstanceInfo, deviceInfo->parentInstance) : nullptr;
        const int app = instance ? native_prewrite::application(
            gfxstream::base::getEnvironmentVariable("GFXSTREAM_NATIVE_CPU_PREWRITE").c_str(),
            instance->applicationName.c_str()) : -1;
        if (app < 0) return;
        std::lock_guard<std::mutex> lifecycle(native::Session::get().lifecycle);
        const auto owner = info.nativeOwner;
        const auto* phys = gfxstream::base::find(mPhysdevInfo, deviceInfo->physicalDevice);
        const auto physical = phys ? phys->memoryPropertiesHelper
            ->getHostMemoryInfoFromHostMemoryTypeIndex(info.memoryIndex) : std::nullopt;
        const auto required = VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT;
        bool freshUnbound = !info.virtioGpuMapped && !info.directMapped && !info.hostmemId &&
            !info.boundBuffer.has_value() && !info.boundColorBuffer.has_value();
        for (const auto& entry : mBufferInfo) {
            if (entry.second.memory == memory) { freshUnbound = false; break; }
        }
        for (const auto& entry : mImageInfo) {
            if (entry.second.memory == memory) { freshUnbound = false; break; }
        }
        const long page = sysconf(_SC_PAGESIZE);
        native_prewrite::Guard guard{
            info.size, page > 0 ? static_cast<uint64_t>(page) : 0,
            owner && owner->generation() && owner->size() == info.size,
            owner && native::Session::get().ready(owner->epoch()),
            owner && info.needUnmap && info.ptr && info.ptr == owner->address(),
            info.memoryIndex == 8 && physical &&
                (physical->memoryType.propertyFlags & required) == required, freshUnbound};
        if (native_prewrite::touch(app, guard, info.ptr, native_prewrite::processBudget(), [&] {
                GFXSTREAM_INFO("NATIVE-CPU-PREWRITE-BEGIN: phase=before-publication app=%s memory=%p gen=%llu blob=%llu "
                    "ptr=%p size=%llu offset=0 width=4 marker=0x13579bdf operation=store",
                    instance->applicationName.c_str(), (void*)memory,
                    (unsigned long long)owner->generation(), (unsigned long long)blob,
                    info.ptr, (unsigned long long)info.size);
            })) {
            GFXSTREAM_INFO("NATIVE-CPU-PREWRITE-END: phase=before-publication app=%s memory=%p gen=%llu blob=%llu "
                "ptr=%p size=%llu offset=0 width=4 marker=0x13579bdf operation=store ordering=dsb-sy no-cache-maintenance",
                instance->applicationName.c_str(), (void*)memory,
                (unsigned long long)owner->generation(), (unsigned long long)blob,
                info.ptr, (unsigned long long)info.size);
        } else {
            static std::atomic<unsigned> rejected{0};
            if (rejected.fetch_add(1) < 2) GFXSTREAM_INFO(
                "NATIVE-CPU-PREWRITE: skipped invalid-or-budget app=%s no-payload-access",
                instance->applicationName.c_str());
        }
    }

'''
edit(d, '    bool nativeTraceDeviceLocked(VkDevice device) REQUIRES(mMutex) {', method + '    bool nativeTraceDeviceLocked(VkDevice device) REQUIRES(mMutex) {')
edit(d, '''            if (info->nativeOwner) info->nativeOwner->mapped(info->ptr);''', '''            if (info->nativeOwner) info->nativeOwner->mapped(info->ptr);
            nativeCpuPrewriteLocked(device, memory, *info, hostBlobId);''')
print('Applied optional one-word prewrite before native publication; read-prefault disabled; diagnostic only')
