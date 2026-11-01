#!/usr/bin/env python3
"""Source invariants for the bounded CIB1 activation diagnostics."""
import argparse
import re
from pathlib import Path
p = argparse.ArgumentParser(description=__doc__)
p.add_argument('root', type=Path)
p.add_argument('workflow', type=Path, nargs='?')
args = p.parse_args()
root = args.root.resolve()

def read(rel):
    return (root / rel).read_text()

def block(text, start, end):
    begin = text.index(start)
    return text[begin:text.index(end, begin)]

common = read('host/vulkan/vk_common_operations.cpp')
frontend = read('host/virtio_gpu_frontend.cpp')
decoder = read('host/vulkan/vk_decoder_global_state.cpp')
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

DRIVER = re.compile(r'(?<![A-Za-z0-9_])vk->')
BOUND = 'diagnostics.fetch_add(1, std::memory_order_relaxed) < 8'

# (a) Emulation readiness: every subcondition is computed for the log and the
# returned boolean keeps the exact original conjunction.
assert '#include <algorithm>\n#include <atomic>' in common
ready = block(common, 'bool VkEmulation::classicImageBridgeReady() const {', '\n}\n')
assert ready.count('CIB1-DIAG-READY') == 1
assert 'GFXSTREAM_NIRI_IMAGE_TRACE' in ready
assert ready.count(BOUND) == 1
for token in ['featureEnabled', 'androidAhb', 'importSupported', 'dvkPresent', 'ahbPropsFn',
              'allocateFn', 'physical', 'typeCount', 'physicalValid', 'physicalFlags',
              'physicalFlagsOk', 'candidates', 'room', 'rgba', 'bgra']:
    assert token in ready
assert 'physical=%u type-count=%u physical-valid=%d physical-flags=0x%x flags-ok=%d candidates=%u room=%d rgba=%d bgra=%d' in ready
assert 'return featureEnabled && androidAhb && importSupported && dvkPresent && ahbPropsFn && allocateFn &&' in ready
assert not DRIVER.search(ready), 'readiness diagnostics must not dispatch driver calls'
assert 'VK_FORMAT_R8G8B8A8_UNORM' in ready and 'VK_FORMAT_B8G8R8A8_UNORM' in ready

# Decoder gates are logged separately so AndroidAHB/snapshot failures are visible
# even when the original && chain would short-circuit.
decoder_ready = block(decoder, '    bool classicImageEnabled() const {', '\n    }\n')
assert decoder_ready.count('CIB1-DIAG-DECODER-READY') == 1
assert 'GFXSTREAM_NIRI_IMAGE_TRACE' in decoder_ready
assert decoder_ready.count(BOUND) == 1
for token in ['feature', 'snapshots', 'ahbMode', 'emulationReady',
              'm_vkEmulation->getExternalMemoryMode()', 'm_vkEmulation->classicImageBridgeReady()']:
    assert token in decoder_ready
assert 'return feature && !snapshots && ahbMode && emulationReady;' in decoder_ready
assert not DRIVER.search(decoder_ready), 'decoder readiness diagnostics must not dispatch driver calls'
assert decoder.count('bool VkDecoderGlobalState::classicImageBridgeReady() { return mImpl->classicImageEnabled(); }') == 1

# (a) Capset fill: reports each gate and the exact fields written.
capset = block(frontend, '            {\n                const bool cibFeature =', '\n            }\n')
assert capset.count('CIB1-DIAG-CAPSET') == 1
assert 'GFXSTREAM_NIRI_IMAGE_TRACE' in capset
assert capset.count(BOUND) == 1
assert 'feature=%d has-emulation-vk=%d snapshots=%d ready=%d' in capset
assert 'wrote=%d version=%u contract=0x%x color-buffer-index=%u' in capset
assert 'if (cibReady) {' in capset
assert 'capset->classicImageBridgeVersion = 1;' in capset
assert 'capset->classicImageBridgeContract = 0x43494231;' in capset
assert '#include <atomic>' in frontend

# (b) Query marker/usage2 and view-format list, recorded before the paired gate.
query = block(decoder, '    std::optional<VkResult> classicImageQueryLocked(', '    bool classicImageLayout(')
assert query.count('CIB1-DIAG-QUERY') == 1
assert 'GFXSTREAM_NIRI_IMAGE_TRACE' in query
assert query.count(BOUND) == 1
for token in ['marker=%d state=%s handle-null=%d stride=%d', 'format=%d usage=%d',
              'consumer=0x%llx producer=0x%llx paired=%d cib-enabled=%d',
              'view-count=%u view0=%u view1=%u view2=%u view3=%u',
              'VkImageFormatListCreateInfo']:
    assert token in query
marker_start = query.index('        if (instanceInfo && instanceInfo->applicationName == "niri" && ext &&')
marker_end = query.index('        if (!paired ||', marker_start)
marker_block = query[marker_start:marker_end]
assert marker_start < marker_end
assert not DRIVER.search(marker_block), 'query diagnostics must not add driver queries'
assert 'const_cast' not in marker_block
assert 'classicImageEnabled()' in marker_block

# (c) Create NativeBuffer marker and view-format list, plus absent-marker fields.
create = block(decoder, '    VkResult on_vkCreateImage(', '    void destroyImageWithExclusiveInfo(')
assert create.count('CIB1-DIAG-CREATE') == 1
assert 'GFXSTREAM_NIRI_IMAGE_TRACE' in create
assert create.count(BOUND) == 1
for token in ['marker=%d state=%s handle-null=%d marker-stride=%d marker-format=%d marker-usage=%d',
              'consumer=0x%llx producer=0x%llx cib-enabled=%d view-count=%u',
              'view0=%u view1=%u view2=%u view3=%u pNext=%s', 'niriImagePNext(pCreateInfo->pNext)']:
    assert token in create
create_start = create.index('        const auto* classicMarker = vk_find_struct<VkNativeBufferANDROID>')
create_end = create.index('        if (classicMarker && classicMarker->usage2.consumer == cib::kConsumer)')
create_block = create[create_start:create_end]
assert create_start < create_end
assert not DRIVER.search(create_block), 'create diagnostics must not add driver calls'
assert 'pCreateInfo->format =' not in create_block
assert 'pCreateInfo->usage =' not in create_block
assert 'pCreateInfo->flags =' not in create_block

# Publication wiring: diagnostics must be applied after the CIB core and packaged.
assert workflow.index('patch_classic_image_bridge.py') < workflow.index('patch_niri_cib_diagnostics.py')
assert workflow.index('test_niri_cib_diagnostics.py') < workflow.index('cp ci/scripts/*niri_cib_diagnostics* out/')
assert 'diagnostic/niri-cib-diagnostics' in workflow
assert "scripts/*niri_cib_diagnostics*" in workflow
assert '"niri_cib_diagnostics"' in workflow

print('PASS: bounded CIB1 diagnostics cover readiness, capset, query marker/view list and create marker; logging only')
