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
assert blob.index('vk->vkMapMemory(') < blob.index('info->nativeOwner->mapped(info->ptr)') < blob.index('nativeCpuPrefaultLocked(device, memory, *info, hostBlobId)') < blob.index('ExternalObjectManager::get()->addMapping(')
assert 'getEnvironmentVariable("GFXSTREAM_NATIVE_CPU_PREFAULT").c_str()' in d
assert 'info.memoryIndex == 8 && physical' in d
assert 'native::Session::get().ready(owner->epoch())' in d
assert 'owner->size() == info.size' in d
assert 'phase=before-publication' in d
assert helper.count('*static_cast<const volatile uint32_t*>(address)') == 1
assert '"dsb sy"' in helper
for forbidden in ['dc civac', 'dc cvac', 'dc ivac', 'memcpy(', 'memset(', 'volatile uint32_t* first', '*address =']:
    assert forbidden not in helper
assert helper.index('valid(guard, address)') < helper.index('budget.consume(app)') < helper.index('beforeRead();') < helper.index('initial = *static_cast')
assert 'NATIVE-CPU-PREFAULT-BEGIN:' in d
# Verify actual fully patched vkGetBlobInternal caller lock order, not patch text.
prefault = blob.index('nativeCpuPrefaultLocked(device, memory, *info, hostBlobId)')
lifecycle = blob.index('std::lock_guard<std::mutex> nativeLock(native::Session::get().lifecycle);')
assert prefault < lifecycle # caller acquires L only LATER for publication
assert blob[:prefault].count('native::Session::get().lifecycle') == 0
assert 'std::lock_guard<std::mutex> lock(mMutex);' in blob[:prefault]
method = d[d.index('void nativeCpuPrefaultLocked('):d.index('bool nativeTraceDeviceLocked(')]
assert method.count('native::Session::get().lifecycle') == 1
print('PASS: exactly one optional offset0 read before publication, after owner mapped; two-app one-shot bounds; actual physical type8/coherent/size/epoch guards; original driver calls unchanged; no payload writes or extra cache maintenance')
