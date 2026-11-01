#!/usr/bin/env python3
"""Apply the offline detach increment only to the audited crosvm pin."""
import argparse
import hashlib
from pathlib import Path
import shutil
import subprocess

PIN = '849e05f12d5c8d4c36af8e15763986d242cd43b5'

def replace(text, old, new):
    if text.count(old) != 1:
        raise RuntimeError(f'expected exactly one source anchor: {old[:100]!r}')
    return text.replace(old, new, 1)

def main():
    p = argparse.ArgumentParser()
    p.add_argument('source', type=Path)
    a = p.parse_args()
    root = a.source.resolve()
    if subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip() != PIN:
        raise RuntimeError('unaudited crosvm revision')
    gpu = root / 'devices/src/virtio/gpu/virtio_gpu.rs'
    s = gpu.read_text()
    s = replace(s, 'struct VirtioGpuResource {', '#[path = "blob_detach.rs"]\nmod blob_detach;\n\nstruct VirtioGpuResource {')
    s = replace(s, '    rutabaga_external_mapping: bool,', '    rutabaga_external_mapping: bool,\n    // Unknown register response: no unmap/unref/reset until external reconciliation.\n    mapping_quarantined: bool,')
    s = replace(s, '            rutabaga_external_mapping: false,', '            rutabaga_external_mapping: false,\n            mapping_quarantined: false,')
    s = replace(s, '\n    rutabaga: Rutabaga,', '\n    rutabaga: std::mem::ManuallyDrop<Rutabaga>,')
    s = replace(s, '            rutabaga,', '            rutabaga: std::mem::ManuallyDrop::new(rutabaga),')
    start = s.index('        let mut resource = self\n', s.index('    pub fn unref_resource('))
    s = s[:start] + '        self.detach_blob_mapping(resource_id)?;\n' + s[start:]
    s = replace(s, '''            if resource.rutabaga_external_mapping {
                self.rutabaga.unmap(resource_id)?;
                resource.rutabaga_external_mapping = false;
            }

''', '')
    anchor = '    pub fn resource_map_blob(&mut self, resource_id: u32, offset: u64) -> VirtioGpuResult {\n'
    s = replace(s, anchor, anchor + '''        let resource = self.resources.get(&resource_id).ok_or(ErrInvalidResourceId)?;
        if resource.shmem_offset.is_some() || resource.rutabaga_external_mapping
            || resource.mapping_quarantined || resource.pool_offset.is_some() {
            return Err(ErrUnspec);
        }
''')
    # Validate access bits before renderer.map acquires an active mapping.
    prot = '''        let prot = match map_info & RUTABAGA_MAP_ACCESS_MASK {
            RUTABAGA_MAP_ACCESS_READ => Protection::read(),
            RUTABAGA_MAP_ACCESS_WRITE => Protection::write(),
            RUTABAGA_MAP_ACCESS_RW => Protection::read_write(),
            _ => return Err(ErrUnspec),
        };
'''
    s = replace(s, prot, '')
    marker = '        let map_info = self.rutabaga.map_info(resource_id).map_err(|_| ErrUnspec)?;'
    # There are separate pool and non-pool map_info queries; insert at the non-pool one.
    # Locate precisely by following display invalidation anchor, rather than pool early return.
    s = replace(s, marker + '\n\n        // A remap', marker + '\n' + prot + '\n        // A remap')
    s = replace(s, '        let res_size = resource.size;', '''        let res_size = resource.size;
        // Record the attempted offset BEFORE sending: lost replies can leave a live memslot.
        resource.shmem_offset = Some(offset);''')
    s = replace(s, '''                    e
                );
                return Err(ErrUnspec);
            }
        };

        resource.shmem_offset = Some(offset);''', '''                    e
                );
                // Even a server Err is not a universal rollback guarantee in this pin.
                resource.mapping_quarantined = true;
                return Err(ErrUnspec);
            }
        };

        resource.shmem_offset = Some(offset);''')
    start = s.index('    /// Uses the hypervisor to unmap the blob resource.')
    end = s.index('    /// Gets the EDID', start)
    s = s[:start] + '''    /// Shared detach transaction. Success is the only permission to unref the renderer.
    fn detach_blob_mapping(&mut self, resource_id: u32) -> VirtioGpuResult {
        let resource = self.resources.get_mut(&resource_id).ok_or(ErrInvalidResourceId)?;
        blob_detach::detach(
            &mut resource.shmem_offset,
            &mut resource.rutabaga_external_mapping,
            resource.mapping_quarantined,
            || ErrUnspec,
            |offset| self.mapper.lock().as_mut().ok_or(ErrUnspec)?
                .remove_mapping(offset).map_err(|_| ErrUnspec),
            || self.rutabaga.unmap(resource_id).map_err(Into::into),
        )?;
        resource.pool_offset = None;
        resource.transition_display_import(&mut self.display.borrow_mut(), DisplayImportState::Unknown);
        Ok(OkNoData)
    }

    /// Explicit UNMAP and UNREF use the identical acknowledged detach path.
    pub fn resource_unmap_blob(&mut self, resource_id: u32) -> VirtioGpuResult {
        self.detach_blob_mapping(resource_id)
    }

''' + s[end:]
    s = replace(s, '    pub fn reset(&mut self) -> anyhow::Result<()> {', '''    pub fn reset(&mut self) -> anyhow::Result<()> {
        let ids: Vec<u32> = self.resources.keys().copied().collect();
        for id in ids {
            self.detach_blob_mapping(id)
                .map_err(|e| anyhow::anyhow!("GPU reset blocked by live mapping {}: {:?}", id, e))?;
        }''')
    # Snapshot must not turn quarantined/renderer-only state into a clean remap.
    s = replace(s, '        assert!(self.scanout_data.is_none());', '''        assert!(!self.mapping_quarantined && !self.rutabaga_external_mapping,
            "native mapping snapshot/restore is not negotiated");
        assert!(self.scanout_data.is_none());''')
    s += '''
// VM-control may no longer be serviced during worker teardown. Do not issue a blocking
// detach request here or destroy the renderer while KVM can still reference its pointers.
// This deliberately retains the renderer until process exit; orderly VM retirement with
// explicit detach acknowledgements is a remaining integration requirement, not success.
impl Drop for VirtioGpu {
    fn drop(&mut self) {
        if self.resources.values().any(|r| r.shmem_offset.is_some()
            || r.rutabaga_external_mapping || r.mapping_quarantined) {
            error!("GPU shutdown quarantined: renderer retained; VM detach unproven");
            return;
        }
        // SAFETY: the renderer is dropped once and no live/uncertain registrations remain.
        unsafe { std::mem::ManuallyDrop::drop(&mut self.rutabaga); }
    }
}
'''
    gpu.write_text(s)
    shutil.copyfile(Path(__file__).with_name('blob_detach.rs'), gpu.with_name('blob_detach.rs'))
    pci = root / 'devices/src/virtio/virtio_pci_device.rs'
    s = pci.read_text()
    s = replace(s, '    mappings: BTreeMap<u64, VmMemoryRegionId>,', '    mappings: BTreeMap<u64, VmMemoryRegionId>,\n    blob_transport_quarantined: bool,')
    s = replace(s, '            mappings: BTreeMap::new(),', '            mappings: BTreeMap::new(),\n            blob_transport_quarantined: false,')
    anchor = '''    ) -> anyhow::Result<Option<u32>> {
        if !self.prepared {'''
    s = replace(s, anchor, '''    ) -> anyhow::Result<Option<u32>> {
        if self.blob_transport_quarantined || self.mappings.contains_key(&offset) {
            return Err(anyhow!("blob mapping duplicate or transport quarantined"));
        }
        if !self.prepared {''')
    s = replace(s, '''        let (id, gunyah_handle) = self
            .vm_memory_client''', '''        // Quarantine before the request: no error, including a lost reply, proves absence.
        self.blob_transport_quarantined = true;
        let (id, gunyah_handle) = self
            .vm_memory_client''')
    s = replace(s, '        self.mappings.insert(offset, id);\n        Ok(gunyah_handle)', '        self.mappings.insert(offset, id);\n        self.blob_transport_quarantined = false;\n        Ok(gunyah_handle)')
    s = replace(s, '''        let id = self.mappings.remove(&offset).context("invalid offset")?;
        self.vm_memory_client
            .unregister_memory(id)
            .context("unregister_memory failed")''', '''        if self.blob_transport_quarantined {
            return Err(anyhow!("blob transport quarantined; detach outcome unknown"));
        }
        let id = *self.mappings.get(&offset).context("invalid offset")?;
        match self.vm_memory_client.unregister_memory(id) {
            Ok(()) => { self.mappings.remove(&offset); Ok(()) }
            Err(e) => {
                // A server rejection preserves retry state. Transport/protocol failures may
                // leave unread responses: never reuse this uncorrelated request tube.
                if !matches!(e, vm_control::api::ApiClientError::RequestFailed(_)) {
                    self.blob_transport_quarantined = true;
                }
                Err(e).context("unregister_memory failed")
            }
        }''')
    s += '\n#[cfg(test)]\n#[path = "native_detach_requester_tests.rs"]\nmod native_detach_requester_tests;\n'
    pci.write_text(s)
    shutil.copyfile(Path(__file__).with_name('native_detach_requester_tests.rs'), pci.with_name('native_detach_requester_tests.rs'))
    print('Applied production detach patch to', PIN)

if __name__ == '__main__':
    main()
