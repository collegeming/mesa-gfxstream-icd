#!/usr/bin/env python3
"""Source invariants for bounded per-check createClassicImageLocked diagnostics."""
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

start = decoder.index('    VkResult createClassicImageLocked(')
end = decoder.index('\n    }\n', start) + len('\n    }\n')
create = decoder[start:end]

assert create.count('CIB1-DIAG-CREATE-FAIL') == 1
assert create.count('CIB1-DIAG-CREATE-STATE') == 1
assert 'GFXSTREAM_NIRI_IMAGE_TRACE' in create
assert create.count('diagnostics.fetch_add(1, std::memory_order_relaxed) < 16') == 1

# Every original early-return has a distinct reason.
for reason in ['not-enabled', 'marker-null', 'consumer', 'producer', 'marker-usage',
               'marker-handle-null', 'image-shape', 'staging', 'stride', 'format',
               'backing-null', 'cb-null', 'phys-null', 'cb-width', 'cb-height',
               'cb-format', 'handle-info', 'handle-type', 'cb-handle-null',
               'size-zero', 'bind-offset', 'shadow', 'usage-flags',
               'view-list-pnext', 'view-list-count', 'view-list-duplicate',
               'view-list-format', 'driver-create']:
    assert f'fail("{reason}"' in create, reason

# The requested state fields are present.
for token in ['resource=%u resource-known=%d', 'strong=%d cb=%d dev=%d phys=%d',
              'cb-width=%u cb-height=%u cb-format=%u', 'handle-info=%d handle-type=%u handle-null=%d',
              'size=%llu bind-offset=%llu type-index=%u shadow=%u',
              'image-ok=%d', 'stride-ok=%d format-ok=%d',
              'view-count=%u view0=%u view1=%u view2=%u view3=%u']:
    assert token in create, token

# No guard or native create was removed.
for token in ['cb->width != input->extent.width', 'cb->height != input->extent.height',
              'cb->imageCreateInfoShallow.format != input->format',
              '!cb->memory.handleInfo', 'STREAM_HANDLE_TYPE_PLATFORM_AHB',
              '!cb->memory.handleInfo->handle', '!cb->memory.size',
              'cb->memory.bindOffset != 0', 'classicImageShadow(cb->memory.typeIndex)',
              'shadow >= VK_MAX_MEMORY_TYPES', '(input->usage & ~ci.usage)',
              '(input->flags & ~(ci.flags | VK_IMAGE_CREATE_ALIAS_BIT))',
              'viewFormatCount > 2', 'cib::view(ci.format, list->pViewFormats[i])',
              'vk->vkCreateImage(device, &ci, nullptr, image)',
              'bridge.classicImageBacking = std::move(backing)',
              'mImageInfo[*image] = std::move(bridge)', 'GFXSTREAM_INFO("CIB1:']:
    assert token in create, token
assert 'REQUIRES(mMutex)' in create

# The diagnostic lambda performs internal lookups only, no Vulkan dispatch.
lambda_start = create.index('auto fail = [&](const char* reason')
lambda_end = create.index('        };', lambda_start)
fail_lambda = create[lambda_start:lambda_end]
assert not re.search(r'(?<![A-Za-z0-9_])vk->', fail_lambda), 'diagnostics must not dispatch driver calls'
assert 'FrameBuffer::getFB()->findColorBuffer(resource)' in fail_lambda
assert 'getColorBufferInfo(resource)' in fail_lambda
assert 'classicImageShadow(cb->memory.typeIndex)' in fail_lambda

# Publication wiring.
assert workflow.index('patch_niri_cib_format_support.py') < workflow.index('patch_niri_cib_create_diag.py')
assert workflow.index('test_niri_cib_create_diag.py') < workflow.index('cp ci/scripts/*niri_cib_create_diag* out/')
assert "scripts/*niri_cib_create_diag*" in workflow
assert '"niri_cib_create_diag"' in workflow

print('PASS: createClassicImageLocked logs every early-return reason and CB/marker/view state; guards and native create unchanged')
