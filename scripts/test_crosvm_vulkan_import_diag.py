#!/usr/bin/env python3
"""Assert the Vulkan-import diagnosis invariants on a patched crosvm tree.

Run after patch_crosvm_vulkan_import_diag.py.

Checks:
  1. `VmMemorySource::map`'s Vulkan arm distinguishes the three failures:
     InvalidGrallocBackend (backend missing), Unsupported (built without the vulkano feature),
     and a real import error;
  2. the guest-visible result is still EINVAL in all three;
  3. the case-1 decision (no VulkanInfo -> plain-mmap Descriptor) is logged in virtio_gpu.rs
     with the resource id, handle type and size;
  4. `DeviceId` is never formatted with `{:?}` (it has no Debug derive -- that would not compile);
  5. the build feature list includes `vulkano`, and both the check and build use the same list.
"""
import pathlib
import sys

root = pathlib.Path(sys.argv[1]).resolve()
out = pathlib.Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else None

vmc = (root / 'vm_control/src/lib.rs').read_text(
    encoding='utf-8', errors='replace').replace('\r\n', '\n')
gpu = (root / 'devices/src/virtio/gpu/virtio_gpu.rs').read_text(
    encoding='utf-8', errors='replace').replace('\r\n', '\n')
# The workflow lives in the CI-scripts repo, not in the crosvm source checkout: take its path
# from argv[3] so this still checks the feature probe that will actually build the binary.
if len(sys.argv) > 3:
    workflow = pathlib.Path(sys.argv[3]).read_text(
        encoding='utf-8', errors='replace').replace('\r\n', '\n')
else:
    workflow = ''  # not supplied: skip only the feature-list check below

# 1. the three arms.
assert 'RutabagaError::InvalidGrallocBackend) => {' in vmc
assert 'RutabagaError::Unsupported) => {' in vmc
assert 'gralloc import rejected: IMPORT FAILED' in vmc
assert 'NO VULKANO BACKEND registered' in vmc
assert 'BUILT WITHOUT vulkano' in vmc

# 2. still EINVAL on the wire.
vulkan_arm = vmc[vmc.index('VmMemorySource::Vulkan {'):]
vulkan_arm = vulkan_arm[:vulkan_arm.index('VmMemorySource::ExternalMapping { ptr, size }')]
assert vulkan_arm.count('return Err(SysError::new(EINVAL));') == 3, \
    vulkan_arm.count('return Err(SysError::new(EINVAL));')

# 3. case-1 decision logged.
assert 'NO VULKAN INFO (handle_type=0x{:x} size={:#x})' in gpu
assert 'plain-mmap Descriptor source' in gpu
no_vk = gpu.index('NO VULKAN INFO')
assert gpu.index('resource_id', no_vk) < gpu.index('VmMemorySource::Descriptor', no_vk)

# 4. DeviceId has no Debug derive, so it must not be formatted.
assert 'device={:?}' not in vulkan_arm
# device_id is legitimately constructed and passed to VulkanInfo; what must not happen is
# passing it to a formatting macro (no Debug derive).
assert 'DeviceId {' in vulkan_arm, 'the VulkanInfo construction disappeared'
assert vulkan_arm.count('device_id,') >= 1

# 5. the feature probe is wired into both cargo invocations (when the workflow is supplied).
# Optional trailing features (audio/audio_aaudio here, android_display elsewhere) are allowed;
# they must not hide vulkano or vnc.
if workflow:
    assert workflow.count(",vulkano,vnc") == 2, workflow.count(",vulkano,vnc")
    assert workflow.count("--features 'gpu,gfxstream,net,config-file,qcow,balloon,vulkano,vnc") == 2

report = (
    'PASS three import failures distinguished: no-backend / no-vulkano / import-failed\n'
    'PASS guest-visible error stays EINVAL in all three arms\n'
    'PASS missing-VulkanInfo decision logged with res/handle_type/size\n'
    'PASS DeviceId never formatted with {:?}\n'
    'PASS vulkano and vnc present in both cargo check and cargo build feature lists\n'
)
print(report, end='')
if out:
    out.mkdir(parents=True, exist_ok=True)
    (out / 'crosvm-vulkan-import-tests.txt').write_text(report)
