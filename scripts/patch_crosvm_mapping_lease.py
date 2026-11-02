#!/usr/bin/env python3
"""Keep a BAR-offset blob mapping alive when a newer resource has taken the offset over.

Applied after the audited native detach patch. The guest's drm_mm can hand a freed BAR offset
to a new blob before the UNMAP for the old blob reaches the VMM; the offset-keyed mapper then
unregisters the new blob's mapping and leaves its guest aperture unbacked (reads 0xff, writes
dropped). Do not detach an offset a live resource has taken over, and log map offsets.
"""
import argparse
import hashlib
import subprocess
from pathlib import Path

PIN = '849e05f12d5c8d4c36af8e15763986d242cd43b5'


def replace(text, old, new):
    if text.count(old) != 1:
        raise RuntimeError(f'expected exactly one source anchor: {old[:120]!r}')
    return text.replace(old, new, 1)


def patch(root):
    changes = {}
    p = root / 'devices/src/virtio/gpu/virtio_gpu.rs'
    s = p.read_text(encoding='utf-8')

    old_lease = '''            return Ok(OkMapInfo {
                map_info: (map_info & RUTABAGA_MAP_CACHE_MASK) | VIRTIO_GPU_MAP_INFO_POOL,
                // A 2 GiB pool keeps the offset within u32.
                pool_offset: Some(pool_offset as u32),
            });'''
    new_lease = '''            if std::env::var_os("GFXSTREAM_NIRI_IMAGE_TRACE").as_deref()
                == Some(std::ffi::OsStr::new("1"))
            {
                base::warn!(
                    "CIB1-DIAG-CROSVM-MAPBLOB: res={} POOL offset={:#x}",
                    resource_id,
                    pool_offset
                );
            }
            return Ok(OkMapInfo {
                map_info: (map_info & RUTABAGA_MAP_CACHE_MASK) | VIRTIO_GPU_MAP_INFO_POOL,
                // A 2 GiB pool keeps the offset within u32.
                pool_offset: Some(pool_offset as u32),
            });'''
    s = replace(s, old_lease, new_lease)

    old_map = '''        let res_size = resource.size;
        // Record the attempted offset BEFORE sending: lost replies can leave a live memslot.
        resource.shmem_offset = Some(offset);'''
    new_map = '''        let res_size = resource.size;
        if std::env::var_os("GFXSTREAM_NIRI_IMAGE_TRACE").as_deref()
            == Some(std::ffi::OsStr::new("1"))
        {
            base::warn!(
                "CIB1-DIAG-CROSVM-MAPBLOB: res={} offset={:#x} size={:#x} cache={:?}",
                resource_id,
                offset,
                res_size,
                cache
            );
        }
        // Record the attempted offset BEFORE sending: lost replies can leave a live memslot.
        resource.shmem_offset = Some(offset);'''
    s = replace(s, old_map, new_map)

    old_detach = '''    fn detach_blob_mapping(&mut self, resource_id: u32) -> VirtioGpuResult {
        let resource = self.resources.get_mut(&resource_id).ok_or(ErrInvalidResourceId)?;
        blob_detach::detach('''
    new_detach = '''    fn detach_blob_mapping(&mut self, resource_id: u32) -> VirtioGpuResult {
        // An offset is only a name. The guest can allocate a freed BAR offset for a new blob
        // before this UNMAP for the old one reaches us; detaching now would unregister the new
        // blob's mapping and leave its guest aperture unbacked. Decide from a live resource that
        // shares the offset, before the mutable borrow below.
        {
            let offset_taken_over = match self.resources.get(&resource_id) {
                Some(pre) => match pre.shmem_offset {
                    Some(pre_offset) => self.resources.iter().any(|(id, other)| {
                        *id != resource_id && other.shmem_offset == Some(pre_offset)
                    }),
                    None => false,
                },
                None => false,
            };
            if offset_taken_over {
                base::warn!(
                    "CIB1-DIAG-CROSVM-UNMAPBLOB: res={} offset is owned by a live resource; keeping the mapping",
                    resource_id
                );
                let resource = self
                    .resources
                    .get_mut(&resource_id)
                    .ok_or(ErrInvalidResourceId)?;
                resource.shmem_offset = None;
                resource.rutabaga_external_mapping = false;
                resource.pool_offset = None;
                return Ok(OkNoData);
            }
        }
        let resource = self.resources.get_mut(&resource_id).ok_or(ErrInvalidResourceId)?;
        blob_detach::detach('''
    s = replace(s, old_detach, new_detach)

    p.write_text(s, encoding='utf-8')
    changes[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest()

    # Mapping the same BAR offset again means the previous owner's node was freed but its UNMAP
    # has not been processed. Leaving the stale registration in place makes the new blob share
    # or inherit a backing whose owner is gone; the guest can end up reading an aperture that
    # nothing backs (0xff reads, dropped writes). Replace the stale registration.
    pci = root / 'devices/src/virtio/virtio_pci_device.rs'
    s = pci.read_text(encoding='utf-8')
    old_pci = '''        let (id, gunyah_handle) = self
            .vm_memory_client
            .register_memory_for_blob('''
    new_pci = '''        if let Some(previous_id) = self.mappings.remove(&offset) {
            base::warn!(
                "CIB1-DIAG-CROSVM-MAPBLOB: offset={:#x} replaces mapping id={:?}; unregistering the stale owner",
                offset,
                previous_id
            );
            self.vm_memory_client
                .unregister_memory(previous_id)
                .context("unregister_memory (stale offset owner) failed")?;
        }

        let (id, gunyah_handle) = self
            .vm_memory_client
            .register_memory_for_blob('''
    s = replace(s, old_pci, new_pci)
    pci.write_text(s, encoding='utf-8')
    changes[str(pci)] = hashlib.sha256(pci.read_bytes()).hexdigest()
    return changes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    head = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
    if head != PIN:
        raise SystemExit(f'unexpected crosvm source: {head} != {PIN}')
    for path, sha in patch(root).items():
        print(f'{sha}  {path}')


if __name__ == '__main__':
    main()
