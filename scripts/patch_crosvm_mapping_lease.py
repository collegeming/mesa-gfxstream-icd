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

    # Say which guest address each blob actually lands on, and whether the mapping call accepted
    # it: the ring that never reaches the host needs its BAR offset checked against this line.
    vmc = root / 'vm_control/src/lib.rs'
    s = vmc.read_text(encoding='utf-8')
    old_map = '''    if let Err(err) = vm.add_fd_mapping(
        *slot,
        *dest_offset as usize,
        size,
        &descriptor,
        file_offset,
        *prot,
    ) {
        return Some(VmMemoryResponse::Err(err));
    }
'''
    new_map = '''    base::warn!(
        "CIB1-DIAG-CROSVM-MAPREGION: guest={:#x} offset={:#x} size={:#x} file_offset={:#x}",
        guest_address.0 + dest_offset,
        dest_offset,
        size,
        file_offset
    );
    if let Err(err) = vm.add_fd_mapping(
        *slot,
        *dest_offset as usize,
        size,
        &descriptor,
        file_offset,
        *prot,
    ) {
        base::error!(
            "CIB1-DIAG-CROSVM-MAPREGION: add_fd_mapping guest={:#x} size={:#x} failed: {:?}",
            guest_address.0 + dest_offset,
            size,
            err
        );
        return Some(VmMemoryResponse::Err(err));
    }
'''
    s = replace(s, old_map, new_map)

    old_share = '''                let (slot, accept_handle) = match vm.runtime_share('''
    new_share = '''                if std::env::var_os("GFXSTREAM_NIRI_IMAGE_TRACE").as_deref()
                    == Some(std::ffi::OsStr::new("1"))
                {
                    base::warn!(
                        "CIB1-DIAG-CROSVM-MAPREGION: fallback guest={:#x} size={:#x}",
                        guest_addr.offset(),
                        size
                    );
                }
                let (slot, accept_handle) = match vm.runtime_share('''
    s = replace(s, old_share, new_share)
    old_prep = """    } = region_state.mapped_regions.get(allocation)?;
"""
    new_prep = """    } = match region_state.mapped_regions.get(allocation) {
        Some(region) => region,
        None => {
            base::warn!(
                "CIB1-DIAG-CROSVM-MAPPREP: no prepared region for alloc={:?} offset={:#x}",
                allocation,
                dest_offset
            );
            return None;
        }
    };
"""
    s = replace(s, old_prep, new_prep)
    vmc.write_text(s, encoding='utf-8')
    changes[str(vmc)] = hashlib.sha256(vmc.read_bytes()).hexdigest()

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
    # Explain the missing prepared region: the prepare call's completion and the handler's own
    # should_prepare/enable state, plus whether the region was inserted.
    pci = root / 'devices/src/virtio/virtio_pci_device.rs'
    s = pci.read_text(encoding='utf-8')
    s = s.replace("""            self.prepared = true;
        }

        // Quarantine before the request: no error, including a lost reply, proves absence.""",
"""            self.prepared = true;
            base::warn!(
                "CIB1-DIAG-CROSVM-MAPPREP: prepare path completed alloc={:?}",
                self.alloc
            );
        }

        // Quarantine before the request: no error, including a lost reply, proves absence.""", 1)
    pci.write_text(s, encoding='utf-8')
    changes[str(pci)] = hashlib.sha256(pci.read_bytes()).hexdigest()

    vmc2 = root / 'vm_control/src/lib.rs'
    s = vmc2.read_text(encoding='utf-8')
    old_prep_req = """                if !sys::should_prepare_memory_region() {
                    return VmMemoryResponse::Ok;
                }"""
    new_prep_req = """                base::warn!(
                    "CIB1-DIAG-CROSVM-MAPPREP: prepare request alloc={:?} should={}",
                    alloc,
                    sys::should_prepare_memory_region()
                );
                if !sys::should_prepare_memory_region() {
                    return VmMemoryResponse::Ok;
                }"""
    s = replace(s, old_prep_req, new_prep_req)
    old_prep_res = """                match sys::prepare_shared_memory_region(vm, sys_allocator, alloc, cache) {
                    Ok(region) => {
                        region_state.mapped_regions.insert(alloc, region);
                        VmMemoryResponse::Ok
                    }
                    Err(e) => VmMemoryResponse::Err(e),
                }"""
    new_prep_res = """                match sys::prepare_shared_memory_region(vm, sys_allocator, alloc, cache) {
                    Ok(region) => {
                        base::warn!(
                            "CIB1-DIAG-CROSVM-MAPPREP: prepared alloc={:?} slot={}",
                            alloc,
                            region.slot
                        );
                        region_state.mapped_regions.insert(alloc, region);
                        VmMemoryResponse::Ok
                    }
                    Err(e) => {
                        base::error!(
                            "CIB1-DIAG-CROSVM-MAPPREP: prepare FAILED alloc={:?}: {:?}",
                            alloc,
                            e
                        );
                        VmMemoryResponse::Err(e)
                    }
                }"""
    s = replace(s, old_prep_res, new_prep_res)
    vmc2.write_text(s, encoding='utf-8')
    changes[str(vmc2)] = hashlib.sha256(vmc2.read_bytes()).hexdigest()

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
