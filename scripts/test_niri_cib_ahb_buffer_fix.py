#!/usr/bin/env python3
"""Source invariants for the AHB plain-buffer fix; image/ColorBuffer AHB export retained."""
import argparse
from pathlib import Path
p = argparse.ArgumentParser(description=__doc__)
p.add_argument('root', type=Path)
p.add_argument('workflow', type=Path, nargs='?')
args = p.parse_args()
root = args.root.resolve()

def read(rel):
    return (root / rel).read_text()

ops = read('host/vulkan/vk_common_operations.cpp')
if args.workflow:
    workflow = args.workflow.resolve().read_text()
else:
    candidates = [
        root / '.github/workflows/build-droidvm-mali-host.yml',
        root.parent / '.github/workflows/build-droidvm-mali-host.yml',
        root.parent / 'ci/.github/workflows/build-droidvm-mali-host.yml',
    ]
    workflow_path = next((x for x in candidates if x.is_file()), None)
    assert workflow_path is not None, 'CI workflow not found'
    workflow = workflow_path.read_text()

setup = ops[ops.index('bool VkEmulation::setupVkBuffer('):
           ops.index('bool VkEmulation::teardownVkBuffer(')]
assert 'const bool cibAhbMode = cibExternalModeKnown &&' in setup
assert 'getExternalMemoryMode() == ExternalMemory::Mode::AndroidAHB' in setup
assert setup.count('CIB1-DIAG-BUF-VK-AHB-NO-EXTERNAL') == 1
assert 'CIB1-DIAG-BUF-VK-AHB-NO-EXTERNAL: handle=%u' in setup
assert setup.count('CIB1-DIAG-BUF-VK-EXTERNAL-DISABLED') == 1
assert 'if (!cibNoExternalBuffer && !cibAhbMode) {' in setup
attach = setup.split('if (!cibNoExternalBuffer && !cibAhbMode) {', 1)[1]
assert 'extBufferCiPtr = &extBufferCi;' in attach
assert setup.index('CIB1-DIAG-BUF-VK-AHB-NO-EXTERNAL') < setup.index('if (!cibNoExternalBuffer && !cibAhbMode) {')
# Existing buffer diagnostics remain.
for tag in ['CIB1-DIAG-BUF-VK-ENTRY', 'CIB1-DIAG-BUF-VK-EXTPROPS',
            'CIB1-DIAG-BUF-VK-CREATE', 'CIB1-DIAG-BUF-VK-REQ',
            'CIB1-DIAG-BUF-VK-ALLOC', 'CIB1-DIAG-BUF-VK-BIND']:
    assert tag in setup, tag
assert setup.count('ahbDiagnostics.fetch_add(1, std::memory_order_relaxed) < 16') == 1

alloc = ops[ops.index('bool VkEmulation::allocExternalMemory('):
           ops.index('void VkEmulation::freeExternalMemoryLocked(')]
assert alloc.count('CIB1-DIAG-BUF-VK-AHB-EXPORT-SKIPPED') == 1
assert 'CIB1-DIAG-BUF-VK-AHB-EXPORT-SKIPPED: size=%llu' in alloc
assert alloc.count('ahbSkipDiagnostics.fetch_add(1, std::memory_order_relaxed) < 16') == 1
skip = alloc.split('            if (!colorBufferInfo) {', 1)
assert len(skip) == 2
skip_block = skip[1][:skip[1].index('            }')]
assert 'CIB1-DIAG-BUF-VK-AHB-EXPORT-SKIPPED' in skip_block
assert 'return true;' in skip_block
# The export call remains after the skip and is still used by the image/ColorBuffer path.
export_pos = alloc.index('vkGetMemoryAndroidHardwareBufferANDROID')
skip_pos = alloc.index('if (!colorBufferInfo) {')
assert skip_pos < export_pos
assert alloc.count('vkGetMemoryAndroidHardwareBufferANDROID') == 1
assert 'if (colorBufferInfo) {' in alloc[export_pos:]
assert 'externalMemoryCompatible = true;' in alloc
# The ColorBuffer allocation context still passes its ColorBufferInfo; buffers do not.
assert 'deviceAlignment, kNullopt, dedicatedImage, infoPtr);' in ops
assert 'bool allocRes = allocExternalMemory(vk, &res.memory, deviceAlignment, dedicated_buffer);' in ops

assert workflow.index('patch_niri_cib_buffer_trace.py') < workflow.index('patch_niri_cib_ahb_buffer_fix.py')
assert workflow.index('test_niri_cib_ahb_buffer_fix.py') < workflow.index('cp ci/scripts/*niri_cib_ahb_buffer_fix* out/')
assert "scripts/*niri_cib_ahb_buffer_fix*" in workflow
assert '"niri_cib_ahb_buffer_fix"' in workflow
assert 'diagnostic/niri-cib-ahb-buffer-fix' in workflow

print('PASS: AHB create-info skipped for plain buffers, AHB export skipped without ColorBufferInfo, image/ColorBuffer AHB path retained')
