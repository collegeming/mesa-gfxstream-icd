#!/usr/bin/env python3
"""Source integration checks; behavioral tests are Rust tests in production modules."""
import argparse
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument('source', type=Path)
a = p.parse_args()
gpu = (a.source / 'devices/src/virtio/gpu/virtio_gpu.rs').read_text()
pci = (a.source / 'devices/src/virtio/virtio_pci_device.rs').read_text()
assert '#[path = "blob_detach.rs"]' in gpu
unref = gpu.split('    pub fn unref_resource(', 1)[1].split('    /// Copies', 1)[0]
assert unref.index('self.detach_blob_mapping(resource_id)?') < unref.index('.remove(&resource_id)') < unref.index('self.rutabaga.unref_resource(resource_id)?')
assert 'self.rutabaga.unmap' not in unref
unmap = gpu.split('    pub fn resource_unmap_blob(', 1)[1].split('    /// Gets', 1)[0]
assert 'self.detach_blob_mapping(resource_id)' in unmap
reset = gpu.split('    pub fn reset(', 1)[1].split('    pub fn snapshot(', 1)[0]
assert reset.index('self.detach_blob_mapping(id)') < reset.index('self.resources.clear()') < reset.index('self.rutabaga.reset()')
map_blob = gpu.split('    pub fn resource_map_blob(', 1)[1].split('    /// Shared detach', 1)[0]
assert map_blob.index('blob_detach::active(') < map_blob.index('self.rutabaga.map(resource_id)')
assert gpu.split('impl Drop for VirtioGpu', 1)[1].index('blob_detach::active(') < gpu.split('impl Drop for VirtioGpu', 1)[1].index('ManuallyDrop::drop(')
assert map_blob.index('resource.shmem_offset = Some(offset)') < map_blob.index('.add_mapping_blob(')
assert 'resource.mapping_quarantined = true' in map_blob
assert 'impl Drop for VirtioGpu' in gpu and 'ManuallyDrop::drop(&mut self.rutabaga)' in gpu
remove = pci.split('    fn remove_mapping(&mut self, offset: u64)', 1)[1].split('    fn as_raw_descriptor', 1)[0]
assert remove.index('unregister_memory(id)') < remove.index('self.mappings.remove(&offset)')
assert 'ApiClientError::RequestFailed' in remove
print('Pinned production source integration checks passed')
