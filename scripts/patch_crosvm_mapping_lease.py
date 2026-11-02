#!/usr/bin/env python3
"""Keep a BAR-offset blob mapping alive when a newer resource has taken the offset over.

The guest's drm_mm may hand a freed BAR offset to a new blob before the UNMAP for the old
blob reaches the VMM. The mapper is keyed by offset, so the new MAP overwrites the old entry
and the later UNMAP unregisters whatever is there now: the new blob's guest mapping is left
pointing at an unbacked aperture (reads 0xff, writes dropped). Observed as an ASG ring whose
guest side never reaches the host. Do not remove an offset a live resource has taken over, and
log the MAP offsets so the same scene can be read back from logcat.
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

    old_map = '''        resource.shmem_offset = Some(offset);'''
    new_map = '''        if std::env::var_os("GFXSTREAM_NIRI_IMAGE_TRACE").as_deref()
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
        resource.shmem_offset = Some(offset);'''
    s = replace(s, old_map, new_map)

    old_unmap_head = '''    pub fn resource_unmap_blob(&mut self, resource_id: u32) -> VirtioGpuResult {
'''
    new_unmap_head = '''    pub fn resource_unmap_blob(&mut self, resource_id: u32) -> VirtioGpuResult {
        // An offset is only a name. The guest can allocate a freed BAR offset for a new blob
        // before this UNMAP for the old one reaches us; the mapper is keyed by that offset, so
        // removing it now would unregister the new blob's mapping and leave its guest aperture
        // unbacked. Decide from a live resource that shares the offset, before taking the
        // mutable borrow below.
        let offset_taken_over = match self.resources.get(&resource_id) {
            Some(pre) => match pre.shmem_offset {
                Some(pre_offset) => self.resources.iter().any(|(id, other)| {
                    *id != resource_id && other.shmem_offset == Some(pre_offset)
                }),
                None => false,
            },
            None => false,
        };
'''
    s = replace(s, old_unmap_head, new_unmap_head)

    old_unmap = '''        let shmem_offset = resource.shmem_offset.ok_or(ErrUnspec)?;
        self.mapper
            .lock()
            .as_mut()
            .expect("No backend request connection found")
            .remove_mapping(shmem_offset)
            .map_err(|_| ErrUnspec)?;
        resource.shmem_offset = None;'''
    new_unmap = '''        let shmem_offset = resource.shmem_offset.ok_or(ErrUnspec)?;
        if offset_taken_over {
            base::warn!(
                "CIB1-DIAG-CROSVM-UNMAPBLOB: res={} offset={:#x} is owned by a live resource; keeping the mapping",
                resource_id,
                shmem_offset
            );
            resource.shmem_offset = None;
            return Ok(OkNoData);
        }
        self.mapper
            .lock()
            .as_mut()
            .expect("No backend request connection found")
            .remove_mapping(shmem_offset)
            .map_err(|_| ErrUnspec)?;
        resource.shmem_offset = None;'''
    s = replace(s, old_unmap, new_unmap)

    p.write_text(s, encoding='utf-8')
    changes[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest()
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
