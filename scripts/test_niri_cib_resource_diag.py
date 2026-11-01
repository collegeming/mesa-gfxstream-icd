#!/usr/bin/env python3
"""Source invariants for bounded virtio resource create/CB/attach diagnostics."""
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

frontend = read('host/virtio_gpu_frontend.cpp')
resource = read('host/virtio_gpu_resource.cpp')
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

def function(text, start, end):
    begin = text.index(start)
    finish = text.index(end, begin)
    return text[begin:finish]

create = function(frontend, 'int VirtioGpuFrontend::createResource(',
                  'int VirtioGpuFrontend::importResource(')
assert create.count('CIB1-DIAG-RES-CREATE-ARGS') == 1
assert create.count('CIB1-DIAG-RES-CREATE-RESULT') == 1
for token in ['handle=%u target=%u format=%u bind=0x%x', 'width=%u height=%u depth=%u array=%u last-level=%u samples=%u flags=0x%x iovs=%u',
              'created=%d existed=%d', 'stride=%u map-size=%zu', 'resourceOpt->GetInfo(&info)']:
    assert token in create, token
assert 'auto resourceOpt = VirtioGpuResource::Create(args, iov, num_iovs);' in create
assert 'mResources[args->handle] = std::move(*resourceOpt);' in create
assert create.count('argsDiagnostics.fetch_add(1, std::memory_order_relaxed) < 16') == 1
assert create.count('resultDiagnostics.fetch_add(1, std::memory_order_relaxed) < 16') == 1
assert 'GFXSTREAM_ERROR("Failed to create resource %u.", args->handle);' in create

attach = function(frontend, 'void VirtioGpuFrontend::attachResource(',
                  'void VirtioGpuFrontend::detachResource(')
assert attach.count('CIB1-DIAG-ATTACH') == 1
for token in ['context=%u resource=%u context-found=%d', 'resource-found=%d map-size=%zu']:
    assert token in attach, token
assert attach.count('diagnostics.fetch_add(1, std::memory_order_relaxed) < 16') == 1
assert 'failed to attach resource %u to context %u: context not found.' in attach
assert 'failed to attach resource %u to context %u: resource not found.' in attach
assert 'context.AttachResource(resource);' in attach

branch_start = resource.index('    } else if (resourceType == VirtioGpuResourceType::COLOR_BUFFER) {\n        const char* trace')
branch_end = resource.index('        FrameBuffer::getFB()->setGuestManagedColorBufferLifetime', branch_start)
branch = resource[branch_start:branch_end]
assert branch.count('CIB1-DIAG-RES-CB-FORMAT') == 1
assert branch.count('CIB1-DIAG-RES-CB-CREATE') == 1
assert branch.count('CIB1-DIAG-RES-CB-RESULT') == 1
for token in ['format=%s width=%u height=%u depth=%u flags=0x%x',
              'handle=%u format=%s created=%d', 'ToString(format).c_str()',
              'createColorBufferWithResourceHandle', 'const bool cbCreated =']:
    assert token in branch, token
assert branch.count('created=%d') == 1
assert branch.count('formatDiagnostics.fetch_add(1, std::memory_order_relaxed) < 16') == 1
assert branch.count('createDiagnostics.fetch_add(1, std::memory_order_relaxed) < 16') == 1
assert branch.count('resultDiagnostics.fetch_add(1, std::memory_order_relaxed) < 16') == 1
assert 'Failed to create resource: unsupported format %d' in branch
assert 'Failed to create color buffer with resource handle %d.' in branch
assert 'std::getenv("GFXSTREAM_NIRI_IMAGE_TRACE")' in branch
assert 'std::strcmp(trace, "1") == 0' in branch

for text in [create, attach, branch]:
    assert not re.search(r'(?<![A-Za-z0-9_])vk->', text), 'diagnostics must not add Vulkan dispatch'

assert workflow.index('patch_niri_cib_create_diag.py') < workflow.index('patch_niri_cib_resource_diag.py')
assert workflow.index('test_niri_cib_resource_diag.py') < workflow.index('cp ci/scripts/*niri_cib_resource_diag* out/')
assert "scripts/*niri_cib_resource_diag*" in workflow
assert '"niri_cib_resource_diag"' in workflow

print('PASS: bounded virtio create/CB/attach diagnostics present; original create, error and attach behavior retained')
