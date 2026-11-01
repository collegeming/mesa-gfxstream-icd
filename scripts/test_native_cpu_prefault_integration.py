#!/usr/bin/env python3
from pathlib import Path
import re
import sys
before, after = map(Path, sys.argv[1:])
for name in ['vk_decoder_global_state.cpp', 'vk_decoder.cpp', 'vk_sub_decoder.cpp']:
    old = (before / name).read_text()
    new = (after / 'host/vulkan' / name).read_text()
    pattern = r'\b(?:vk|deviceDispatch|queueSubmit2)(?:->vk\w+)?\([^;]*?\)\s*;'
    calls = lambda text: [re.sub(r'\s+', ' ', call) for call in re.findall(pattern, text)]
    assert calls(old) == calls(new), f'driver expression changed: {name}'
d = (after / 'host/vulkan/vk_decoder_global_state.cpp').read_text()
helper = Path(__file__).with_name('native_cpu_prefault.h').read_text().replace('#pragma once\n', '')
assert helper in d
start = d.index('VkResult vkGetBlobInternal(')
end = d.index('VkResult on_vkGetBlobGOOGLE(', start)
blob = d[start:end]
call = 'nativeCpuPrewriteLocked(device, memory, *info, hostBlobId)'
assert blob.index('vk->vkMapMemory(') < blob.index('info->nativeOwner->mapped(info->ptr)') < blob.index(call) < blob.index('ExternalObjectManager::get()->addMapping(')
assert 'getEnvironmentVariable("GFXSTREAM_NATIVE_CPU_PREWRITE").c_str()' in d
assert 'GFXSTREAM_NATIVE_CPU_PREFAULT' not in d # read-only prefault cannot also operate
assert 'info.memoryIndex == 8 && physical' in d
assert 'native::Session::get().ready(owner->epoch())' in d
assert 'owner->size() == info.size' in d
assert 'phase=before-publication' in d
assert helper.count('*static_cast<volatile uint32_t*>(address) = 0x13579bdfu;') == 1
assert 'const volatile uint32_t' not in helper and 'initial' not in helper
assert '"dsb sy"' in helper
for forbidden in ['dc civac', 'dc cvac', 'dc ivac', 'memcpy(', 'memset(']:
    assert forbidden not in helper
assert helper.index('valid(guard, address)') < helper.index('budget.consume(app)') < helper.index('beforeStore();') < helper.index('*static_cast<volatile')
assert 'NATIVE-CPU-PREWRITE-BEGIN:' in d and 'NATIVE-CPU-PREWRITE-END:' in d
assert '!info.virtioGpuMapped && !info.directMapped && !info.hostmemId' in d
structs = (after / 'host/vulkan/vk_decoder_internal_structs.h').read_text()
assert 'bool directMapped = false;' in structs and 'bool virtioGpuMapped = false;' in structs
assert 'uint64_t hostmemId = 0;' in structs
assert 'for (const auto& entry : mBufferInfo)' in d and 'for (const auto& entry : mImageInfo)' in d
prewrite = blob.index(call)
lifecycle = blob.index('std::lock_guard<std::mutex> nativeLock(native::Session::get().lifecycle);')
assert prewrite < lifecycle
assert blob[:prewrite].count('native::Session::get().lifecycle') == 0
assert 'std::lock_guard<std::mutex> lock(mMutex);' in blob[:prewrite]
method = d[d.index('void nativeCpuPrewriteLocked('):d.index('bool nativeTraceDeviceLocked(')]
assert method.count('native::Session::get().lifecycle') == 1
print('PASS: exactly one optional offset0 store before publication after owner mapped; no payload reads; prior readonly env disabled; two-app one-shot fresh/unbound/physical/epoch guards; original driver expressions unchanged; BEGIN-store-DSB-END ordering; no extra cache maintenance')
