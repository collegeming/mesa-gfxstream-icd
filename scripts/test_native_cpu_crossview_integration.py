#!/usr/bin/env python3
from pathlib import Path
import re
import sys
before, after = map(Path, sys.argv[1:])
files = ['vk_decoder_global_state.cpp', 'vk_decoder.cpp', 'vk_sub_decoder.cpp']
pattern = r'\b(?:vk|deviceDispatch|queueSubmit2)(?:->vk\w+)?\([^;]*?\)\s*;'
for name in files:
    old = (before / name).read_text()
    new = (after / 'host/vulkan' / name).read_text()
    calls = lambda text: [re.sub(r'\s+', ' ', call) for call in re.findall(pattern, text)]
    assert calls(old) == calls(new), f'driver expressions changed: {name}'
d = (after / 'host/vulkan/vk_decoder.cpp').read_text()
g = (after / 'host/vulkan/vk_decoder_global_state.cpp').read_text()
helper = Path(__file__).with_name('native_cpu_crossview.h').read_text().replace('#pragma once\n', '')
assert helper in g
start = d.index('case OP_vkFlushMappedMemoryRanges:')
end = d.index('case OP_vkInvalidateMappedMemoryRanges:', start)
flush = d[start:end]
assert flush.index('vk->vkFlushMappedMemoryRanges(') < flush.index('m_state->nativeCpuCrossviewAfterFlush(') < flush.index('vkStream->write(&vkFlushMappedMemoryRanges_VkResult_return')
assert flush.count('vk->vkFlushMappedMemoryRanges(') == 1
assert 'if (!m_state->usingDirectMapping())' in flush
assert 'if (original != VK_SUCCESS) return original;' in g
assert 'getEnvironmentVariable("GFXSTREAM_NATIVE_CPU_CROSSVIEW").c_str()' in g
assert 'instance->applicationName.c_str()' in g
assert 'if (count != 1 || !ranges)' in g
assert 'std::shared_ptr<native::Allocation> owner;' in g
assert 'native::Session::get().ready(owner->epoch())' in g
assert 'getHostMemoryInfoFromHostMemoryTypeIndex(info->memoryIndex)' in g
assert 'VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT' in g
assert 'for (const auto& entry : mBufferInfo)' in g and 'for (const auto& entry : mImageInfo)' in g
assert g.index('} // Retained owner protects backing') < g.index('native_crossview::exchange(guard, owner->address()')
assert helper.index('if (!valid(guard, pointer))') < helper.index('if (!budget.consume())') < helper.index('seen.first = *first;')
assert helper.index('return Result::Mismatch;') < helper.index('*first = 0x89abcdefu;')
assert 'bytes + 4092' in helper
assert 'atomic_thread_fence' in helper and 'fetch_add' not in helper
assert '*first' in helper and '*last' in helper
patch = Path(__file__).with_name('patch_native_cpu_crossview.py').read_text()
for forbidden in ['vkInvalidateMappedMemoryRanges(', 'vkFlushMappedMemoryRanges(', 'vkQueueSubmit(',
                  'vkBindBufferMemory(', 'memcpy(', 'memset(', 'DMA_BUF_IOCTL_SYNC',
                  'memoryTypeIndex =', 'propertyFlags =']:
    assert forbidden not in patch, forbidden
print('PASS: real post-driver/pre-response flush hook; original Vulkan driver arguments unchanged; exact env/app + physical owner/range/epoch/unbound guards; locks released before two-word one-shot exchange; no added cache/GPU/flag/index operations')
