#!/usr/bin/env python3
"""Metadata-only native roundtrip identity trace. No payload access or behavior changes."""
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
s = 'host/vulkan/vk_decoder_internal_structs.h'
helper = Path(__file__).with_name('native_roundtrip_trace.h').read_text().replace('#pragma once\n', '')
edit(d, '#include "vk_decoder_global_state.h"', '#include "vk_decoder_global_state.h"\n' + helper)
edit(s, 'struct DeviceInfo {', 'struct DeviceInfo {\n    bool nativeRoundtripTrace = false;')
edit(d, '        deviceInfo.parentInstance = physicalDeviceInfo.instance;', '''        deviceInfo.parentInstance = physicalDeviceInfo.instance;
        deviceInfo.nativeRoundtripTrace = native_trace::enabled(
            gfxstream::base::getEnvironmentVariable("GFXSTREAM_NATIVE_ROUNDTRIP_TRACE").c_str(),
            instanceInfo.applicationName.c_str());
        if (native_trace::take(native_trace::Stage::Capability, deviceInfo.nativeRoundtripTrace)) {
            GFXSTREAM_INFO("NATIVE-RT-CAPABILITY: device=%p app=%s aux-handler-implemented=0 aux-extension-advertised=0",
                (void*)*pDevice, instanceInfo.applicationName.c_str());
        }''')
methods = '''    bool nativeTraceDeviceLocked(VkDevice device) REQUIRES(mMutex) {
        const auto* info = gfxstream::base::find(mDeviceInfo, device);
        return info && info->nativeRoundtripTrace;
    }
    bool nativeTraceDevice(VkDevice device) {
        std::lock_guard<std::mutex> lock(mMutex);
        return nativeTraceDeviceLocked(device);
    }
    bool nativeTraceCommand(VkCommandBuffer command) {
        std::lock_guard<std::mutex> lock(mMutex);
        const auto* info = gfxstream::base::find(mCommandBufferInfo, command);
        return info && nativeTraceDeviceLocked(info->device);
    }
    void traceNativeCopyBuffer(VkCommandBuffer command, VkBuffer src, VkBuffer dst,
                               uint32_t count, const VkBufferCopy* regions, const char* path) {
        const bool allowed = nativeTraceCommand(command);
        if (!native_trace::take(native_trace::Stage::Copy, allowed)) return;
        GFXSTREAM_INFO("NATIVE-RT-COPY: path=%s cmd=%p src=%p dst=%p region-count=%u",
            path, (void*)command, (void*)src, (void*)dst, count);
        // Command metadata only. Never dereference buffer/allocation contents.
        for (uint32_t i = 0; regions && i < count && i < 16; ++i) {
            if (!native_trace::take(native_trace::Stage::CopyRegion, allowed)) break;
            GFXSTREAM_INFO("NATIVE-RT-COPY-REGION: cmd=%p index=%u src-offset=%llu dst-offset=%llu size=%llu",
                (void*)command, i, (unsigned long long)regions[i].srcOffset,
                (unsigned long long)regions[i].dstOffset, (unsigned long long)regions[i].size);
        }
    }
    template<typename Submit>
    bool traceNativeSubmit(bool allowed, VkQueue queue, uint32_t count, const Submit* submits, VkFence fence) {
        if (!native_trace::take(native_trace::Stage::Submit, allowed)) return false;
        GFXSTREAM_INFO("NATIVE-RT-SUBMIT: queue=%p fence=%p submit-count=%u", (void*)queue, (void*)fence, count);
        for (uint32_t i = 0; submits && i < count && i < 16; ++i) {
            const auto& submit = submits[i];
            const uint32_t commands = getCommandBufferCount(submit);
            for (uint32_t j = 0; j < commands && j < 16; ++j) {
                if (!native_trace::take(native_trace::Stage::SubmitCommand, allowed)) return true;
                GFXSTREAM_INFO("NATIVE-RT-SUBMIT-CMD: queue=%p fence=%p submit=%u command-index=%u cmd=%p command-count=%u",
                    (void*)queue, (void*)fence, i, j, (void*)getCommandBuffer(submit, j), commands);
            }
        }
        return true;
    }

'''
edit(d, '    VkResult setBufferMemoryBindInfoLocked(', methods + '    VkResult setBufferMemoryBindInfoLocked(')
edit(d, '''        if (memoryInfo->boundBuffer) {
            auto* deviceInfo''', '''        if (native_trace::take(native_trace::Stage::Bind, nativeTraceDeviceLocked(device))) {
            GFXSTREAM_INFO("NATIVE-RT-BIND: device=%p buffer=%p memory=%p offset=%llu buffer-size=%llu "
                "allocation-size=%llu gen=%llu blob=%llu",
                (void*)device, (void*)buffer, (void*)memory, (unsigned long long)memoryOffset,
                (unsigned long long)bufferInfo->size, (unsigned long long)memoryInfo->size,
                memoryInfo->nativeOwner ? (unsigned long long)memoryInfo->nativeOwner->generation() : 0ULL,
                (unsigned long long)memoryInfo->blobId);
        }
        if (memoryInfo->boundBuffer) {
            auto* deviceInfo''')
edit(d, '''        if (memoryInfo.nativeOwner) memoryInfo.nativeOwner->mapped(memoryInfo.ptr);''', '''        if (memoryInfo.nativeOwner) memoryInfo.nativeOwner->mapped(memoryInfo.ptr);
        if (memoryInfo.nativeOwner && native_trace::take(native_trace::Stage::Allocation, nativeTraceDeviceLocked(device))) {
            GFXSTREAM_INFO("NATIVE-RT-ALLOC: device=%p memory=%p gen=%llu blob=%llu ptr=%p size=%llu mapped=%d",
                (void*)device, (void*)*pMemory, (unsigned long long)memoryInfo.nativeOwner->generation(),
                (unsigned long long)memoryInfo.blobId, memoryInfo.ptr,
                (unsigned long long)memoryInfo.size, memoryInfo.needUnmap);
        }''')
edit(d, '''            if (info->nativeOwner) info->nativeOwner->mapped(info->ptr);''', '''            if (info->nativeOwner) info->nativeOwner->mapped(info->ptr);
            if (info->nativeOwner && native_trace::take(native_trace::Stage::Mapping, nativeTraceDeviceLocked(device))) {
                GFXSTREAM_INFO("NATIVE-RT-MAP: device=%p memory=%p gen=%llu blob=%llu ptr=%p size=%llu",
                    (void*)device, (void*)memory, (unsigned long long)info->nativeOwner->generation(),
                    (unsigned long long)hostBlobId, info->ptr, (unsigned long long)info->size);
            }''')
# Capture app gate while the existing on_vkQueueSubmit mMutex scope is held,
# before queueMutex. Never add a queueMutex -> mMutex lookup edge for tracing.
edit(d, '        bool sharedQueue = false;', '        bool sharedQueue = false;\n        bool nativeRoundtripAllowed = false;')
edit(d, '            deviceOpTracker = deviceInfo->deviceOpTracker.get();', '            deviceOpTracker = deviceInfo->deviceOpTracker.get();\n            nativeRoundtripAllowed = deviceInfo->nativeRoundtripTrace;')
edit(d, 'dispatchVkQueueSubmit(vk, queue, submitCount, pSubmits, usedFence)', 'dispatchVkQueueSubmit(vk, queue, submitCount, pSubmits, usedFence, nativeRoundtripAllowed)')
# Both overloads receive the immutable gate by value; original driver calls stay exact.
for submit_type in ['VkSubmitInfo', 'VkSubmitInfo2']:
    edit(d, f'                                   const {submit_type}* pSubmits, VkFence fence) {{',
         f'                                   const {submit_type}* pSubmits, VkFence fence, bool nativeRoundtripAllowed) {{')
# Preserve each original driver expression byte-for-byte; only wrap original return.
edit(d, '''        VkResult res = vk->vkQueueSubmit(unboxed_queue, submitCount, pSubmits, fence);''', '''        const bool nativeTraceSubmit = traceNativeSubmit(nativeRoundtripAllowed, unboxed_queue, submitCount, pSubmits, fence);
        VkResult res = vk->vkQueueSubmit(unboxed_queue, submitCount, pSubmits, fence);
        if (nativeTraceSubmit) GFXSTREAM_INFO("NATIVE-RT-SUBMIT-RESULT: queue=%p fence=%p result=%d",
            (void*)unboxed_queue, (void*)fence, (int)res);''')
edit(d, '''        VkResult res = queueSubmit2(unboxed_queue, submitCount, pSubmits, fence);''', '''        const bool nativeTraceSubmit = traceNativeSubmit(nativeRoundtripAllowed, unboxed_queue, submitCount, pSubmits, fence);
        VkResult res = queueSubmit2(unboxed_queue, submitCount, pSubmits, fence);
        if (nativeTraceSubmit) GFXSTREAM_INFO("NATIVE-RT-SUBMIT2-RESULT: queue=%p fence=%p result=%d",
            (void*)unboxed_queue, (void*)fence, (int)res);''')
edit(d, '''        return vk->vkWaitForFences(unboxed_device, fenceCount, pFences, waitAll, timeoutLeft);''', '''        const bool nativeTraceWait = native_trace::take(native_trace::Stage::Wait, nativeTraceDevice(unboxed_device));
        const VkResult nativeWaitResult = vk->vkWaitForFences(unboxed_device, fenceCount, pFences, waitAll, timeoutLeft);
        if (nativeTraceWait) {
            GFXSTREAM_INFO("NATIVE-RT-WAIT: device=%p fence-count=%u first-fence=%p wait-all=%u timeout=%llu result=%d",
                (void*)unboxed_device, fenceCount, fenceCount && pFences ? (void*)pFences[0] : nullptr,
                waitAll, (unsigned long long)timeoutLeft, (int)nativeWaitResult);
        }
        return nativeWaitResult;''')
edit(d, '''        subDecode(readStream, vk, apiCallHandle, boxed_commandBuffer, commandBuffer, dataSize,''', '''        if (native_trace::take(native_trace::Stage::FlushInline, nativeTraceCommand(commandBuffer))) {
            GFXSTREAM_INFO("NATIVE-RT-FLUSH-INLINE: cmd=%p bytes=%llu", (void*)commandBuffer, (unsigned long long)dataSize);
        }
        subDecode(readStream, vk, apiCallHandle, boxed_commandBuffer, commandBuffer, dataSize,''')
edit(d, '''                                                    const VkDecoderContext& context) {
        // TODO : implement
    }''', '''                                                    const VkDecoderContext& context) {
        if (native_trace::take(native_trace::Stage::FlushAux, nativeTraceCommand(unbox_VkCommandBuffer(commandBuffer)))) {
            GFXSTREAM_INFO("NATIVE-RT-FLUSH-AUX: cmd=%p memory=%p offset=%llu bytes=%llu handler-implemented=0",
                (void*)unbox_VkCommandBuffer(commandBuffer), (void*)deviceMemory,
                (unsigned long long)dataOffset, (unsigned long long)dataSize);
        }
        // TODO : implement
    }''')
edit(h, '    static void reset();', '''    static void reset();
    // Metadata-only opt-in diagnostic; never accesses allocation payload.
    void traceNativeCopyBuffer(VkCommandBuffer command, VkBuffer src, VkBuffer dst,
                               uint32_t count, const VkBufferCopy* regions, const char* path);''')
edit(d, '''VkDecoderGlobalState::~VkDecoderGlobalState() = default;''', '''VkDecoderGlobalState::~VkDecoderGlobalState() = default;
void VkDecoderGlobalState::traceNativeCopyBuffer(VkCommandBuffer command, VkBuffer src, VkBuffer dst,
                                                uint32_t count, const VkBufferCopy* regions, const char* path) {
    mImpl->traceNativeCopyBuffer(command, src, dst, count, regions, path);
}''')
edit('host/vulkan/vk_decoder.cpp', '''                    vk->vkCmdCopyBuffer(unboxed_commandBuffer, srcBuffer, dstBuffer, regionCount,''', '''                    m_state->traceNativeCopyBuffer(unboxed_commandBuffer, srcBuffer, dstBuffer,
                                                    regionCount, pRegions, "direct");
                    vk->vkCmdCopyBuffer(unboxed_commandBuffer, srcBuffer, dstBuffer, regionCount,''')
edit('host/vulkan/vk_sub_decoder.cpp', '''                    vk->vkCmdCopyBuffer((VkCommandBuffer)dispatchHandle, srcBuffer, dstBuffer,''', '''                    traceNativeCopyBuffer((VkCommandBuffer)dispatchHandle, srcBuffer, dstBuffer,
                                          regionCount, pRegions, "subdecoder");
                    vk->vkCmdCopyBuffer((VkCommandBuffer)dispatchHandle, srcBuffer, dstBuffer,''')
print('Applied bounded metadata-only native roundtrip trace; driver arguments unchanged')
