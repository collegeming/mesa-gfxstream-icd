#!/usr/bin/env python3
"""Explicit one-shot CPU crossview diagnostic; no GPU/cache behavior changes."""
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
h = 'host/vulkan/vk_decoder_global_state.h'
helper = Path(__file__).with_name('native_cpu_crossview.h').read_text().replace('#pragma once\n', '')
edit(d, '#include "vk_decoder_global_state.h"', '#include "vk_decoder_global_state.h"\n' + helper)
method = '''    VkResult nativeCpuCrossviewAfterFlush(VkDevice device, uint32_t count,
                                          const VkMappedMemoryRange* ranges, VkResult original) {
        // The native driver call has ALREADY completed. Never modify normal apps.
        if (original != VK_SUCCESS) return original;
        std::shared_ptr<native::Allocation> owner;
        native_crossview::Guard guard{};
        uint64_t generation = 0;
        uint64_t blob = 0;
        VkDeviceMemory memory = VK_NULL_HANDLE;
        {
            std::lock_guard<std::mutex> lock(mMutex);
            const auto* deviceInfo = gfxstream::base::find(mDeviceInfo, device);
            const auto* instance = deviceInfo ?
                gfxstream::base::find(mInstanceInfo, deviceInfo->parentInstance) : nullptr;
            if (!instance || !native_crossview::enabled(
                    gfxstream::base::getEnvironmentVariable("GFXSTREAM_NATIVE_CPU_CROSSVIEW").c_str(),
                    instance->applicationName.c_str())) return original;
            if (count != 1 || !ranges) {
                GFXSTREAM_ERROR("NATIVE-CPU-CROSSVIEW: invalid-range-count count=%u no-payload-access", count);
                return VK_ERROR_VALIDATION_FAILED_EXT;
            }
            memory = ranges[0].memory;
            const auto* info = gfxstream::base::find(mMemoryInfo, memory);
            std::lock_guard<std::mutex> lifecycle(native::Session::get().lifecycle);
            if (info && info->device == device && info->nativeOwner) {
                owner = info->nativeOwner;
                generation = owner->generation();
                blob = info->blobId;
                guard.size = owner->size();
                guard.offset = ranges[0].offset;
                guard.rangeSize = ranges[0].size;
                guard.owned = generation != 0 && info->size == owner->size();
                guard.epochCurrent = native::Session::get().ready(owner->epoch());
                guard.mapped = info->needUnmap && info->ptr == owner->address() && info->ptr;
                const auto* phys = gfxstream::base::find(mPhysdevInfo, deviceInfo->physicalDevice);
                const auto physical = phys ? phys->memoryPropertiesHelper
                    ->getHostMemoryInfoFromHostMemoryTypeIndex(info->memoryIndex) : std::nullopt;
                const auto required = VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT;
                guard.physicalCoherentVisible = physical &&
                    (physical->memoryType.propertyFlags & required) == required;
                guard.bound = info->boundBuffer.has_value() || info->boundColorBuffer.has_value();
                for (const auto& entry : mBufferInfo) {
                    if (entry.second.memory == memory) { guard.bound = true; break; }
                }
                for (const auto& entry : mImageInfo) {
                    if (entry.second.memory == memory) { guard.bound = true; break; }
                }
            }
        } // Retained owner protects backing, device and instance; drop D and L before CPU access.
        if (!owner || !native_crossview::valid(guard, owner->address())) {
            GFXSTREAM_ERROR("NATIVE-CPU-CROSSVIEW: invalid-state owner=%d gen=%llu size=%llu offset=%llu range-size=%llu "
                "owned=%d epoch=%d mapped=%d physical-coherent-visible=%d bound=%d no-payload-access",
                owner != nullptr, (unsigned long long)generation, (unsigned long long)guard.size,
                (unsigned long long)guard.offset, (unsigned long long)guard.rangeSize,
                guard.owned, guard.epochCurrent, guard.mapped, guard.physicalCoherentVisible, guard.bound);
            return VK_ERROR_VALIDATION_FAILED_EXT;
        }
        native_crossview::Observation observed;
        const auto result = native_crossview::exchange(guard, owner->address(),
                                                     native_crossview::processBudget(), observed);
        GFXSTREAM_INFO("NATIVE-CPU-CROSSVIEW: device=%p memory=%p gen=%llu blob=%llu outcome=%u "
            "observed-first=0x%08x observed-last=0x%08x reply-written=%d cpu-only-not-gpu-coherence",
            (void*)device, (void*)memory, (unsigned long long)generation, (unsigned long long)blob,
            (unsigned)result, observed.first, observed.last, result == native_crossview::Result::Replied);
        return result == native_crossview::Result::Replied ? original : VK_ERROR_VALIDATION_FAILED_EXT;
    }

'''
edit(d, '    bool nativeTraceDeviceLocked(VkDevice device) REQUIRES(mMutex) {', method + '    bool nativeTraceDeviceLocked(VkDevice device) REQUIRES(mMutex) {')
edit(h, '    static void reset();', '''    static void reset();
    VkResult nativeCpuCrossviewAfterFlush(VkDevice device, uint32_t count,
                                          const VkMappedMemoryRange* ranges, VkResult original);''')
edit(d, 'VkDecoderGlobalState::~VkDecoderGlobalState() = default;', '''VkDecoderGlobalState::~VkDecoderGlobalState() = default;
VkResult VkDecoderGlobalState::nativeCpuCrossviewAfterFlush(VkDevice device, uint32_t count,
                                                          const VkMappedMemoryRange* ranges, VkResult original) {
    return mImpl->nativeCpuCrossviewAfterFlush(device, count, ranges, original);
}''')
# Real generated decoder site, not a dead GlobalState override. Preserve original driver expression.
edit('host/vulkan/vk_decoder.cpp', '''                if ((vkFlushMappedMemoryRanges_VkResult_return) == VK_ERROR_DEVICE_LOST)''', '''                vkFlushMappedMemoryRanges_VkResult_return = m_state->nativeCpuCrossviewAfterFlush(
                    unboxed_device, memoryRangeCount, pMemoryRanges, vkFlushMappedMemoryRanges_VkResult_return);
                if ((vkFlushMappedMemoryRanges_VkResult_return) == VK_ERROR_DEVICE_LOST)''')
print('Applied explicit one-shot CPU crossview after real flush; no execution authorization implied')
