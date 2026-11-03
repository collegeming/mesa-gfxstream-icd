#!/usr/bin/env python3
"""Source invariants for the bounded image-view/memory lifetime diagnostics."""
import argparse
from pathlib import Path

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('root', type=Path)
p.add_argument('workflow', type=Path, nargs='?')
args = p.parse_args()
root = args.root.resolve()


def read(rel):
    return (root / rel).read_text()


dec = read('host/vulkan/vk_decoder_global_state.cpp')
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

for tag in ['IV-DIAG-CREATE:', 'IV-DIAG-CREATED:', 'IV-DIAG-DONE:', 'IV-DIAG-LABEL:',
            'IV-DIAG-DESTROY-IMAGE:', 'IV-DIAG-DESTROY-VIEW:', 'IV-DIAG-DESTROY-DEVICE:',
            'IV-DIAG-FREE-MEMORY:', 'IV-DIAG-DESTROY-SEMAPHORE:', 'IV-DIAG-WAIT-SEMAPHORES:',
            'IV-DIAG-WAIT-SEMAPHORES-DONE:', 'IV-DIAG-INVALIDATE:']:
    assert dec.count(tag) == 1, tag
assert dec.count('GFXSTREAM_IV_DIAG') == 12
# The driver call is still the one and only image view creation; the diagnostics sit
# around it, and the debug-label call site is unchanged.
create_pos = dec.index('IV-DIAG-CREATE:')
driver_pos = dec.index('vk->vkCreateImageView(device, pCreateInfo, nullptr, pView);')
created_pos = dec.index('IV-DIAG-CREATED:')
done_pos = dec.index('IV-DIAG-DONE:')
assert create_pos < driver_pos < created_pos < done_pos
assert driver_pos - create_pos < 3400
# Direct fd-2 writes: logcat may lose the last buffered line before the abort.
assert dec.count('fflush(stderr);') >= 7
assert '#include <cstdio>' in dec
# Destroy paths still call the driver; diagnostics do not replace the calls.
assert 'destroyImageWithExclusiveInfo(device, deviceDispatch, image, imageInfo);' in dec
assert 'destroyImageViewWithExclusiveInfo(device, deviceDispatch, imageView, imageViewInfo);' in dec
assert 'freeMemoryLocked(device, deviceDispatch, memory);' in dec
# Bounded.
assert dec.count('fetch_add(1, std::memory_order_relaxed) < 512') == 7
assert dec.count('fetch_add(1, std::memory_order_relaxed) < 64') == 1
assert dec.count('fetch_add(1, std::memory_order_relaxed) < 256') == 4

ops = read('host/vulkan/vk_decoder.cpp')
assert ops.count('IV-DIAG-OP:') == 1
assert ops.count('GFXSTREAM_IV_DIAG') == 1

assert workflow.index('patch_cib_buffer_dmabuf_export.py') < workflow.index(
    'patch_image_view_diag.py')
assert workflow.index('patch_image_view_diag.py') < workflow.index(
    'test_image_view_diag.py')
assert "scripts/*image_view_diag*" in workflow
assert 'cp ci/scripts/*image_view_diag* out/' in workflow

print('PASS: bounded image-view/memory diagnostics present; driver calls and destroy paths unchanged')
