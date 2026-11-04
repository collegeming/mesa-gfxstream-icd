#!/usr/bin/env python3
"""Distinguish the three ways a Vulkan-info blob import can fail, instead of one EINVAL.

Applied after patch_crosvm_bar_offset_diag.py.

`VmMemorySource::map` currently collapses the `Vulkan` arm to a single line
(`vm_control/src/lib.rs:498-518`):

    Err(e) => { error!("gralloc failed to import and map: {}", e); return Err(SysError::new(EINVAL)); }

Three different situations reach that arm and they need different fixes:

  1. no Vulkan info at all   -- `rutabaga.vulkan_info(resource_id)` failed, so crosvm never builds
     `VmMemorySource::Vulkan` and takes the plain-mmap `Descriptor` arm instead. The guest then
     sees `source.map failed: Error(22)` with no further detail (this is the current symptom).
  2. backend missing         -- `GrallocBackend::Vulkano` was not registered in
     `RutabagaGrallocBackendFlags`, so `RutabagaGralloc::import_and_map` returns
     `RutabagaError::InvalidGrallocBackend` (`gralloc.rs:381-385`).
  3. import failed           -- the Vulkano backend exists but refused this handle/size; the error
     is `RutabagaError::Unsupported` when the feature is compiled out (`gralloc.rs:231-238`,
     the trait default) or a real Vulkano error otherwise.

This patch logs which of the three happened, with the resource id, handle type, size and memory
index, and keeps the guest-visible EINVAL. It also logs the case-1 decision in virtio_gpu.rs (the
`else if handle_type != OPAQUE_FD` arm), so one device run says whether we ever reached the
Vulkan arm.

No behavioural change: the same sources are chosen for the same inputs.
"""
import argparse
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('root', type=Path)
root = parser.parse_args().root.resolve()


def replace(text, old, new):
    if text.count(old) != 1:
        raise RuntimeError(f'expected exactly one anchor: {old[:160]!r} (found {text.count(old)})')
    return text.replace(old, new, 1)


# ---------------------------------------------------------------------------
# 1. vm_control: name the failure, distinguish backend-missing from import-failed.
# ---------------------------------------------------------------------------
vmc = root / 'vm_control/src/lib.rs'
s = vmc.read_text(encoding='utf-8')

OLD = '''                let mapped_region = match gralloc.import_and_map(
                    RutabagaHandle {
                        os_handle: to_rutabaga_desciptor(descriptor),
                        handle_type,
                    },
                    VulkanInfo {
                        memory_idx,
                        device_id,
                    },
                    size,
                ) {
                    Ok(mapped_region) => {
                        let mapped_region: Box<dyn MappedRegion> =
                            Box::new(RutabagaMemoryRegion::new(mapped_region));
                        mapped_region
                    }
                    Err(e) => {
                        error!("gralloc failed to import and map: {}", e);
                        return Err(SysError::new(EINVAL));
                    }
                };'''

NEW = '''                let mapped_region = match gralloc.import_and_map(
                    RutabagaHandle {
                        os_handle: to_rutabaga_desciptor(descriptor),
                        handle_type,
                    },
                    VulkanInfo {
                        memory_idx,
                        device_id,
                    },
                    size,
                ) {
                    Ok(mapped_region) => {
                        let mapped_region: Box<dyn MappedRegion> =
                            Box::new(RutabagaMemoryRegion::new(mapped_region));
                        mapped_region
                    }
                    Err(rutabaga_gfx::RutabagaError::InvalidGrallocBackend) => {
                        // The Vulkano backend was not compiled in, or was not registered in
                        // RutabagaGrallocBackendFlags. Nothing about the handle is wrong.
                        error!(
                            "gralloc import rejected: NO VULKANO BACKEND registered \\
(handle_type=0x{:x} memory_idx={} size={:#x}) -- the gralloc backend table has no \\
GrallocBackend::Vulkano entry; this is a build/runtime-configuration gap, not a bad handle",
                            handle_type, memory_idx, size
                        );
                        return Err(SysError::new(EINVAL));
                    }
                    Err(rutabaga_gfx::RutabagaError::Unsupported) => {
                        // The trait default: rutabaga_gfx was built without the `vulkano` feature,
                        // so import_and_map exists but has no implementation.
                        error!(
                            "gralloc import rejected: RUTABAGO BUILT WITHOUT vulkano \\
(handle_type=0x{:x} memory_idx={} size={:#x}) -- import_and_map fell through to the trait \\
default; rebuild crosvm with the `vulkano` feature",
                            handle_type, memory_idx, size
                        );
                        return Err(SysError::new(EINVAL));
                    }
                    Err(e) => {
                        error!(
                            "gralloc import rejected: IMPORT FAILED (handle_type=0x{:x} \\
memory_idx={} size={:#x}): {}",
                            handle_type, memory_idx, size, e
                        );
                        return Err(SysError::new(EINVAL));
                    }
                };'''
s = replace(s, OLD, NEW)
vmc.write_text(s, encoding='utf-8')
print('patched vm_control - three-way Vulkan import diagnosis')

# ---------------------------------------------------------------------------
# 2. virtio_gpu: log the case-1 decision (no vulkan info -> Descriptor arm).
# ---------------------------------------------------------------------------
gpu = root / 'devices/src/virtio/gpu/virtio_gpu.rs'
g = gpu.read_text(encoding='utf-8')

OLD_GPU = '''                } else if export.handle_type != RUTABAGA_HANDLE_TYPE_MEM_OPAQUE_FD {
                    source = Some(VmMemorySource::Descriptor {
                        descriptor: to_safe_descriptor(export.os_handle),
                        offset: 0,
                        size: resource.size,
                    });
                }'''

NEW_GPU = '''                } else if export.handle_type != RUTABAGA_HANDLE_TYPE_MEM_OPAQUE_FD {
                    // Reached only when rutabaga had no VulkanInfo for this resource. The guest
                    // maps through a plain mmap of the descriptor, which fails for anything that
                    // is not a linear mmap-able dma-buf/shmem.
                    base::warn!(
                        "GPU-MAPBLOB: res={} NO VULKAN INFO (handle_type=0x{:x} size={:#x}) -> \\
plain-mmap Descriptor source; a non-linear or vendor-specific handle will fail here",
                        resource_id,
                        export.handle_type,
                        resource.size
                    );
                    source = Some(VmMemorySource::Descriptor {
                        descriptor: to_safe_descriptor(export.os_handle),
                        offset: 0,
                        size: resource.size,
                    });
                }'''
g = replace(g, OLD_GPU, NEW_GPU)
gpu.write_text(g, encoding='utf-8')
print('patched virtio_gpu - log the missing-vulkan-info decision')
