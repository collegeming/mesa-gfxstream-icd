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

    patch_lifetime(root, changes)
    return changes


BLOB_CONTEXT_RS = '''// Copyright 2026 The ChromiumOS Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

//! Blob mapping lifetime bookkeeping shared with virtio-gpu context teardown.
//!
//! Blob mappings are keyed by BAR offset and owned by the resource, not by the context. A guest
//! that closes a context without sending UNMAP/UNREF leaves the registration (and the renderer's
//! view of it) alive while the renderer tears that context down. Remember the guest's
//! ATTACH/DETACH commands so teardown releases exactly the mappings whose last context is going
//! away, and take an owned handle to the mapper slot before locking it so the mutex and the
//! `VmRequester` inside cannot be destroyed while a mapping call is still in flight.

use std::collections::BTreeMap;
use std::collections::BTreeSet;
use std::sync::Arc;

use sync::Mutex;

use crate::virtio::SharedMemoryMapper;

/// Attachment edges between guest contexts and resources, tracked in both directions.
#[derive(Default, Debug)]
pub(super) struct ContextResources {
    by_context: BTreeMap<u32, BTreeSet<u32>>,
    by_resource: BTreeMap<u32, BTreeSet<u32>>,
}

impl ContextResources {
    pub(super) fn attach(&mut self, ctx_id: u32, resource_id: u32) {
        self.by_context
            .entry(ctx_id)
            .or_default()
            .insert(resource_id);
        self.by_resource
            .entry(resource_id)
            .or_default()
            .insert(ctx_id);
    }

    pub(super) fn detach(&mut self, ctx_id: u32, resource_id: u32) {
        if let Some(resources) = self.by_context.get_mut(&ctx_id) {
            resources.remove(&resource_id);
            if resources.is_empty() {
                self.by_context.remove(&ctx_id);
            }
        }
        if let Some(contexts) = self.by_resource.get_mut(&resource_id) {
            contexts.remove(&ctx_id);
            if contexts.is_empty() {
                self.by_resource.remove(&resource_id);
            }
        }
    }

    /// The resource is gone; no context may keep a stale edge to it.
    pub(super) fn forget_resource(&mut self, resource_id: u32) {
        if let Some(contexts) = self.by_resource.remove(&resource_id) {
            for ctx_id in contexts {
                if let Some(resources) = self.by_context.get_mut(&ctx_id) {
                    resources.remove(&resource_id);
                    if resources.is_empty() {
                        self.by_context.remove(&ctx_id);
                    }
                }
            }
        }
    }

    /// The context is gone; drop its edges and keep edges held by other contexts.
    pub(super) fn forget_context(&mut self, ctx_id: u32) {
        if let Some(resources) = self.by_context.remove(&ctx_id) {
            for resource_id in resources {
                if let Some(contexts) = self.by_resource.get_mut(&resource_id) {
                    contexts.remove(&ctx_id);
                    if contexts.is_empty() {
                        self.by_resource.remove(&resource_id);
                    }
                }
            }
        }
    }

    /// Resources for which `ctx_id` is the only remaining context, in ascending order.
    pub(super) fn resources_exclusive_to(&self, ctx_id: u32) -> Vec<u32> {
        self.by_context
            .get(&ctx_id)
            .map(|resources| {
                resources
                    .iter()
                    .copied()
                    .filter(|resource_id| {
                        self.by_resource
                            .get(resource_id)
                            .is_some_and(|contexts| {
                                contexts.len() == 1 && contexts.contains(&ctx_id)
                            })
                    })
                    .collect()
            })
            .unwrap_or_default()
    }
}

/// Remove one registered mapping while holding an owned handle to the shared mapper slot.
///
/// Cloning the `Arc` before locking is the lifetime guarantee: the mutex, the `Option` and the
/// `VmRequester` inside stay alive for the whole call even if the device is dropped or the mapper
/// slot is replaced concurrently. Locking through a borrowed field would end that borrow at the
/// last use, leaving the slot free to be destroyed while the VMM is still answering.
pub(super) fn remove_mapping(
    mapper: &Arc<Mutex<Option<Box<dyn SharedMemoryMapper>>>>,
    offset: u64,
) -> Result<(), ()> {
    let handle = Arc::clone(mapper);
    let mut slot = handle.lock();
    slot.as_mut().ok_or(())?.remove_mapping(offset).map_err(|_| ())
}

/// A resource still owns host state when any of these are set. Quarantine means the
/// registration outcome is unknown, so it must be detached (fail closed), never skipped.
pub(super) fn blob_state_requires_detach(
    offset: Option<u64>,
    renderer_mapped: bool,
    quarantined: bool,
    pool_offset: Option<u64>,
) -> bool {
    offset.is_some() || renderer_mapped || quarantined || pool_offset.is_some()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::AtomicUsize;
    use std::sync::atomic::Ordering;
    use std::sync::Barrier;
    use std::thread;

    use anyhow::Result;
    use base::Protection;
    use hypervisor::MemCacheType;
    use hypervisor::VmAccept;
    use vm_control::VmMemorySource;

    struct BlockingSpy {
        entered: Arc<Barrier>,
        release: Arc<Barrier>,
        removed: Arc<Mutex<Vec<u64>>>,
        drops: Arc<AtomicUsize>,
    }

    impl SharedMemoryMapper for BlockingSpy {
        fn add_mapping(
            &mut self,
            _source: VmMemorySource,
            _offset: u64,
            _prot: Protection,
            _cache: MemCacheType,
        ) -> Result<()> {
            Ok(())
        }

        fn remove_mapping(&mut self, offset: u64) -> Result<()> {
            self.removed.lock().push(offset);
            self.entered.wait();
            self.release.wait();
            Ok(())
        }
    }

    impl Drop for BlockingSpy {
        fn drop(&mut self) {
            self.drops.fetch_add(1, Ordering::SeqCst);
        }
    }

    fn blocking_slot() -> (
        Arc<Mutex<Option<Box<dyn SharedMemoryMapper>>>>,
        Arc<Barrier>,
        Arc<Barrier>,
        Arc<Mutex<Vec<u64>>>,
        Arc<AtomicUsize>,
    ) {
        let entered = Arc::new(Barrier::new(2));
        let release = Arc::new(Barrier::new(2));
        let removed = Arc::new(Mutex::new(Vec::new()));
        let drops = Arc::new(AtomicUsize::new(0));
        let slot: Arc<Mutex<Option<Box<dyn SharedMemoryMapper>>>> =
            Arc::new(Mutex::new(Some(Box::new(BlockingSpy {
                entered: Arc::clone(&entered),
                release: Arc::clone(&release),
                removed: Arc::clone(&removed),
                drops: Arc::clone(&drops),
            }))));
        (slot, entered, release, removed, drops)
    }

    /// The mapper slot (and the mutex + VmRequester it owns) must survive the device dropping
    /// its own handle while a detach is still inside the lock. A borrowed lock would leave the
    /// slot free to be destroyed and the next pthread_mutex_lock would hit a destroyed mutex.
    #[test]
    fn mapper_slot_outlives_device_arc_during_detach() {
        let (slot, entered, release, removed, drops) = blocking_slot();
        let worker_handle = Arc::clone(&slot);
        let worker = thread::spawn(move || remove_mapping(&worker_handle, 0x3600000));

        entered.wait();
        // Device teardown drops its own handle while the detach still holds the mutex.
        drop(slot);
        assert_eq!(
            drops.load(Ordering::SeqCst),
            0,
            "mapper slot destroyed while a detach still holds it"
        );

        release.wait();
        assert_eq!(worker.join().unwrap(), Ok(()));
        assert_eq!(removed.lock().clone(), vec![0x3600000]);
        assert_eq!(
            drops.load(Ordering::SeqCst),
            1,
            "mapper slot must be released exactly once, after the last caller returns"
        );
    }

    #[test]
    fn context_teardown_targets_only_exclusive_resources() {
        let mut map = ContextResources::default();
        map.attach(1, 10);
        map.attach(1, 11);
        map.attach(2, 11);
        map.attach(2, 12);
        map.attach(2, 12); // duplicate ATTACH is idempotent
        assert_eq!(map.resources_exclusive_to(1), vec![10]);
        assert_eq!(map.resources_exclusive_to(2), vec![12]);
        map.detach(2, 11);
        assert_eq!(map.resources_exclusive_to(1), vec![10, 11]);
        map.forget_resource(10);
        assert_eq!(map.resources_exclusive_to(1), vec![11]);
        map.forget_context(1);
        assert_eq!(map.resources_exclusive_to(1), Vec::<u32>::new());
        assert_eq!(map.resources_exclusive_to(2), vec![12]);
    }

    #[test]
    fn unknown_detach_does_not_create_state() {
        let mut map = ContextResources::default();
        map.detach(9, 99);
        map.forget_resource(99);
        map.forget_context(9);
        assert_eq!(map.resources_exclusive_to(9), Vec::<u32>::new());
    }

    /// Teardown must never read a quarantined or pooled blob as already detached: both still
    /// own host/renderer state, and Drop keeps the renderer alive until they are released.
    #[test]
    fn teardown_never_skips_a_quarantined_or_pooled_blob() {
        assert!(!blob_state_requires_detach(None, false, false, None));
        assert!(blob_state_requires_detach(Some(0x1000), false, false, None));
        assert!(blob_state_requires_detach(None, true, false, None));
        assert!(blob_state_requires_detach(None, false, true, None));
        assert!(blob_state_requires_detach(None, false, false, Some(0x2000)));
        assert!(blob_state_requires_detach(None, false, true, Some(0x2000)));
    }
}
'''


def patch_lifetime(root, changes):
    """Blob-map lifecycle hardening: context teardown + mapper slot lifetime.

    The detach patch makes UNMAP/UNREF release the host mapping, but a context destroy still
    tears the renderer context down while its resources' BAR registrations are alive, and the
    mapping calls lock a mapper slot they do not own. Track attachments so teardown detaches the
    mappings whose last context is going away, and hold the mapper slot for the whole call.
    """
    (root / 'devices/src/virtio/gpu/blob_context.rs').write_text(BLOB_CONTEXT_RS, encoding='utf-8')

    p = root / 'devices/src/virtio/gpu/virtio_gpu.rs'
    s = p.read_text(encoding='utf-8')

    s = replace(
        s,
        '#[path = "blob_detach.rs"]\nmod blob_detach;\n',
        '#[path = "blob_detach.rs"]\nmod blob_detach;\n\n#[path = "blob_context.rs"]\nmod blob_context;\n',
    )

    s = replace(
        s,
        '    resources: Map<u32, VirtioGpuResource>,\n',
        '    resources: Map<u32, VirtioGpuResource>,\n'
        '    /// Guest ATTACH/DETACH edges, used to release the last user of a blob mapping on\n'
        '    /// context teardown.\n'
        '    context_resources: blob_context::ContextResources,\n',
    )

    s = replace(
        s,
        '            resources: Default::default(),\n',
        '            resources: Default::default(),\n'
        '            context_resources: Default::default(),\n',
    )

    old_destroy = '''    pub fn destroy_context(&mut self, ctx_id: u32) -> VirtioGpuResult {
        self.rutabaga.destroy_context(ctx_id)?;
        Ok(OkNoData)
    }'''
    new_destroy = '''    pub fn destroy_context(&mut self, ctx_id: u32) -> VirtioGpuResult {
        // Context teardown is the last chance to release host blob mappings for resources the
        // guest will never UNMAP: the renderer drops the context while this registry still owns
        // BAR registrations. Detach first, but only resources this context is the last user of
        // and that no scanout still presents, so teardown cannot unmap the frame on screen.
        let mut exclusive = self.context_resources.resources_exclusive_to(ctx_id);
        exclusive.retain(|resource_id| {
            let id = NonZeroU32::new(*resource_id);
            !self
                .scanouts
                .values()
                .any(|scanout| scanout.resource_id == id)
                && self.cursor_scanout.resource_id != id
        });
        for resource_id in exclusive {
            let needs_teardown = self.resources.get(&resource_id).is_some_and(|resource| {
                blob_context::blob_state_requires_detach(
                    resource.shmem_offset,
                    resource.rutabaga_external_mapping,
                    resource.mapping_quarantined,
                    resource.pool_offset,
                )
            });
            if !needs_teardown {
                continue;
            }
            if let Err(e) = self.detach_blob_mapping(resource_id) {
                // Fail closed: keep the context and its attachment record so a retry can finish.
                // Already-detached mappings are no longer active, so the retry skips them.
                base::error!(
                    "CIB1-DIAG-CROSVM-CTXDESTROY: ctx={} res={} detach failed: {:?}",
                    ctx_id,
                    resource_id,
                    e
                );
                return Err(e);
            }
        }
        self.rutabaga.destroy_context(ctx_id)?;
        self.context_resources.forget_context(ctx_id);
        Ok(OkNoData)
    }'''
    s = replace(s, old_destroy, new_destroy)

    s = replace(
        s,
        '        self.rutabaga.context_attach_resource(ctx_id, resource_id)?;\n        Ok(OkNoData)',
        '        self.rutabaga.context_attach_resource(ctx_id, resource_id)?;\n'
        '        self.context_resources.attach(ctx_id, resource_id);\n'
        '        Ok(OkNoData)',
    )

    s = replace(
        s,
        '        self.rutabaga.context_detach_resource(ctx_id, resource_id)?;\n        Ok(OkNoData)',
        '        self.rutabaga.context_detach_resource(ctx_id, resource_id)?;\n'
        '        self.context_resources.detach(ctx_id, resource_id);\n'
        '        Ok(OkNoData)',
    )

    s = replace(
        s,
        '        if result.is_err() {\n'
        '            self.resources.insert(resource_id, resource);\n'
        '            return result;\n'
        '        }\n',
        '        if result.is_err() {\n'
        '            self.resources.insert(resource_id, resource);\n'
        '            return result;\n'
        '        }\n'
        '        self.context_resources.forget_resource(resource_id);\n',
    )

    s = replace(
        s,
        '        self.resources.clear();\n        self.rutabaga.reset().context("failed to reset rutabaga")?;',
        '        self.resources.clear();\n'
        '        self.context_resources = Default::default();\n'
        '        self.rutabaga.reset().context("failed to reset rutabaga")?;',
    )

    # The detach path must not borrow the mapper field for the duration of the call: clone the
    # Arc so the slot, the mutex and the VmRequester outlive every mapping/detach user.
    s = replace(
        s,
        '            |offset| self.mapper.lock().as_mut().ok_or(ErrUnspec)?\n'
        '                .remove_mapping(offset).map_err(|_| ErrUnspec),\n',
        '            |offset| blob_context::remove_mapping(&self.mapper, offset).map_err(|_| ErrUnspec),\n',
    )

    old_maplock = '''        match self
            .mapper
            .lock()
            .as_mut()
            .expect("No backend request connection found")
            .add_mapping_blob(source.unwrap(), offset, prot, cache, VmAccept::Sync)'''
    new_maplock = '''        // Own handle across the blocking request: the mapper slot and the VmRequester inside
        // it must stay alive until the VMM acknowledges, even if the device is torn down.
        let mapper = Arc::clone(&self.mapper);
        match mapper
            .lock()
            .as_mut()
            .expect("No backend request connection found")
            .add_mapping_blob(source.unwrap(), offset, prot, cache, VmAccept::Sync)'''
    s = replace(s, old_maplock, new_maplock)

    p.write_text(s, encoding='utf-8')
    changes[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest()

    # The fatal-signal handler exists so a crashing crosvm never reaches debuggerd, which is what
    # reboots the host. That costs the stack. Add an env-gated dump that runs BEFORE _exit: raw
    # addresses written with libc::write, plus a startup copy of /proc/self/maps so the addresses
    # can be attributed to modules offline. Off unless CROSVM_FATAL_BACKTRACE is set.
    m = root / 'src/main.rs'
    s = m.read_text(encoding='utf-8')
    old_handler = '''extern "C" fn exit_without_crash_dump(sig: libc::c_int) {
    // Async-signal-safe only. No allocation, no formatting, no logging.
    unsafe { libc::_exit(128 + sig) };
}'''
    new_handler = '''static FATAL_BACKTRACE: std::sync::atomic::AtomicBool =
    std::sync::atomic::AtomicBool::new(false);

extern "C" {
    fn backtrace(buffer: *mut *mut core::ffi::c_void, size: core::ffi::c_int) -> core::ffi::c_int;
}

fn fatal_write_hex(fd: libc::c_int, value: u64) {
    let mut buf = [0u8; 17];
    for i in 0..16 {
        let nib = ((value >> ((15 - i) * 4)) & 0xf) as u8;
        buf[i] = if nib < 10 { b'0' + nib } else { b'a' + nib - 10 };
    }
    buf[16] = b'\\n';
    unsafe { libc::write(fd, buf.as_ptr() as *const libc::c_void, buf.len()) };
}

extern "C" fn exit_without_crash_dump(sig: libc::c_int) {
    // Async-signal-safe only. No allocation, no formatting, no logging.
    if FATAL_BACKTRACE.load(std::sync::atomic::Ordering::Relaxed) {
        unsafe {
            libc::write(2, b"FATAL-BT\\n".as_ptr() as *const libc::c_void, 9);
            // Own address first: it fixes the PIE load bias for the crosvm frames.
            fatal_write_hex(2, exit_without_crash_dump as usize as u64);
            let mut frames = [core::ptr::null_mut::<core::ffi::c_void>(); 48];
            let n = backtrace(frames.as_mut_ptr(), frames.len() as core::ffi::c_int);
            for slot in frames.iter().take(n.max(0) as usize) {
                fatal_write_hex(2, *slot as usize as u64);
            }
        }
    }
    unsafe { libc::_exit(128 + sig) };
}'''
    s = replace(s, old_handler, new_handler)

    old_main = '''fn main() {
    syslog::early_init();
    install_fatal_signal_handlers();'''
    new_main = '''fn main() {
    syslog::early_init();
    let fatal_backtrace = std::env::var_os("CROSVM_FATAL_BACKTRACE").is_some();
    FATAL_BACKTRACE.store(fatal_backtrace, std::sync::atomic::Ordering::Relaxed);
    if fatal_backtrace {
        // Module bases for offline symbolization. The path is writable by the calling app user.
        if let Ok(maps) = std::fs::read("/proc/self/maps") {
            let path = std::env::var("CROSVM_FATAL_MAPS_PATH")
                .unwrap_or_else(|_| format!("/data/data/cn.classfun.droidvm/files/crosvm-maps-{}.txt", std::process::id()));
            let _ = std::fs::write(path, maps);
        }
    }
    install_fatal_signal_handlers();'''
    s = replace(s, old_main, new_main)

    m.write_text(s, encoding='utf-8')
    changes[str(m)] = hashlib.sha256(m.read_bytes()).hexdigest()




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
