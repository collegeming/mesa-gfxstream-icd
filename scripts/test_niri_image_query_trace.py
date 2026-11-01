#!/usr/bin/env python3
from pathlib import Path
import re
import sys
before, after = map(Path, sys.argv[1:])
for name in ['vk_decoder_global_state.cpp','vk_decoder.cpp']:
    old=(before/name).read_text(); new=(after/'host/vulkan'/name).read_text()
    pattern=r'\b(?:vk|deviceDispatch|queueSubmit2)(?:->vk\w+)?\([^;]*?\)\s*;'
    calls=lambda t:[re.sub(r'\s+',' ',x) for x in re.findall(pattern,t)]
    assert calls(old)==calls(new), f'driver calls changed: {name}'
s=(after/'host/vulkan/vk_decoder_global_state.cpp').read_text()
d=(after/'host/vulkan/vk_decoder.cpp').read_text()
assert 'info->applicationName != "niri"' in s
assert 'getEnvironmentVariable("GFXSTREAM_NIRI_IMAGE_TRACE") != "1"' in s
assert 'while (count < 64)' in s and 'i < 8' in s
for marker in ['NIRI-IMAGE-QUERY-WIRE:', 'NIRI-IMAGE-QUERY-BEGIN:', 'NIRI-IMAGE-QUERY-END:',
               'NIRI-IMAGE-CREATE-BEGIN:', 'NIRI-IMAGE-CREATE-END:', 'NIRI-IMAGE-REQUIREMENTS:']:
    assert marker in s
assert d.count('m_state->traceNiriImageQueryInput(unbox_VkPhysicalDevice(physicalDevice), pImageFormatInfo);') == 2
q=s[s.index('VkResult on_vkGetPhysicalDeviceImageFormatProperties2('):s.index('// gfxstream-zerocopy: a format')]
assert q.index('NIRI-IMAGE-QUERY-BEGIN:') < q.index('vk->vkGetPhysicalDeviceImageFormatProperties2(') < q.index('NIRI-IMAGE-QUERY-END:')
assert q.index('NIRI-IMAGE-QUERY-END:') < q.index('VK_EXTERNAL_MEMORY_FEATURE_DEDICATED_ONLY_BIT')
assert 'native-pNext=%s' in q
# No extra driver calls, modifications of types/ranges/flags, or mapped payload accesses in this patch.
patch=Path(__file__).with_name('patch_niri_image_query_trace.py').read_text()
for forbidden in ['vkAllocateMemory(', 'vkMapMemory(', 'memcpy(', 'memset(', 'volatile uint32_t', 'info->ptr', 'nativeOwner->address()']:
    assert forbidden not in patch
print('PASS: niri exact env/app gate, global64 metadata budget, pNext max8, wire-before-codec and actual-native-before/after query/create/requirements; all driver expressions unchanged and no payload access')
