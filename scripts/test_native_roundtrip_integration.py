#!/usr/bin/env python3
"""Actions-only invariants against exact pre/post trace source."""
from pathlib import Path
import re
import sys
before, after = map(Path, sys.argv[1:])
files = ['vk_decoder_global_state.cpp', 'vk_decoder.cpp', 'vk_sub_decoder.cpp']
# All Vulkan driver expressions (not only selected sites) remain in the exact
# same order with identical operands. The tracer adds no cache/wait/submit calls.
pattern = r'\b(?:vk|deviceDispatch|queueSubmit2)(?:->vk\w+)?\([^;]*?\)\s*;'
for name in files:
    old = (before / name).read_text()
    new = (after / 'host/vulkan' / name).read_text()
    calls = lambda text: [re.sub(r'\s+', ' ', call) for call in re.findall(pattern, text)]
    assert calls(old) == calls(new), f'driver call expressions changed: {name}'
d = (after / 'host/vulkan/vk_decoder_global_state.cpp').read_text()
s = (after / 'host/vulkan/vk_decoder_internal_structs.h').read_text()
helper = Path(__file__).with_name('native_roundtrip_trace.h').read_text().replace('#pragma once\n', '')
assert helper in d
assert 'bool nativeRoundtripTrace = false;' in s
assert 'getEnvironmentVariable("GFXSTREAM_NATIVE_ROUNDTRIP_TRACE").c_str()' in d
assert 'instanceInfo.applicationName.c_str()' in d
for marker in ['NATIVE-RT-ALLOC:', 'NATIVE-RT-MAP:', 'NATIVE-RT-BIND:', 'NATIVE-RT-COPY:',
               'NATIVE-RT-COPY-REGION:', 'NATIVE-RT-SUBMIT:', 'NATIVE-RT-SUBMIT-CMD:',
               'NATIVE-RT-WAIT:', 'NATIVE-RT-FLUSH-INLINE:', 'NATIVE-RT-FLUSH-AUX:']:
    assert marker in d
assert 'nativeRoundtripAllowed = deviceInfo->nativeRoundtripTrace;' in d
assert 'nativeTraceQueue' not in d
assert 'Stage::Submit, allowed' in d
assert 'aux-handler-implemented=0 aux-extension-advertised=0' in d
assert 'traceNativeCopyBuffer(unboxed_commandBuffer' in (after / 'host/vulkan/vk_decoder.cpp').read_text()
assert 'traceNativeCopyBuffer((VkCommandBuffer)dispatchHandle' in (after / 'host/vulkan/vk_sub_decoder.cpp').read_text()
# Only metadata member dereferences in added trace bodies; no payload primitives.
patch = Path(__file__).with_name('patch_native_roundtrip_trace.py').read_text()
for forbidden in ['vkFlushMappedMemoryRanges', 'vkInvalidateMappedMemoryRanges',
                  'DMA_BUF_IOCTL_SYNC', 'memcpy(', 'memset(', 'reinterpret_cast<uint32_t*>',
                  'volatile uint32_t', 'owner->address()', 'nativeOwner->address()']:
    assert forbidden not in patch
assert 'regions[i].srcOffset' in d and 'regions[i].dstOffset' in d and 'regions[i].size' in d
# Honest auxiliary capability source: no such capset field; guest RC query uses
# an extension which pinned host never advertises. Do not invent a toggle/fix.
frontend = (after / 'host/virtio_gpu_frontend.cpp').read_text()
assert 'hasVulkanAuxCommandMemory' not in frontend
assert 'ANDROID_EMU_vulkan_aux_command_memory' not in (after / 'host/RenderControl.cpp').read_text() if (after / 'host/RenderControl.cpp').exists() else True
print('PASS: trace helper exact env+app gate and bounded metadata sites; direct/subdecoder copy present; all original Vulkan driver expressions unchanged; no payload/cache/synchronization additions')
