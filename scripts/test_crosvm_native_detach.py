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
assert reset.index('self.detach_all_blob_mappings()?') < reset.index('self.resources.clear()') < reset.index('self.rutabaga.reset()')
worker = (a.source / 'devices/src/virtio/gpu/mod.rs').read_text()
worker_run = worker.split('    fn run(&mut self) {', 1)[1].split('    fn on_activate', 1)[0]
assert 'return;' not in worker_run
assert worker_run.index('if let WorkerStopReason::Kill') < worker_run.index('self.state.virtio_gpu.detach_all_blob_mappings()')
linux = (a.source / 'src/crosvm/sys/linux.rs').read_text()
shutdown = linux.split('    // Stop pci root worker thread', 1)[1].split('    // Drop the hotplug manager', 1)[0]
assert shutdown.index('device_ctrl_tube.send(&DeviceControlCommand::Exit)') < shutdown.index('mem::drop(linux)')
assert shutdown.index('mem::drop(linux)') < shutdown.index('vm_memory_handler_control.send(&VmMemoryHandlerRequest::Exit)') < shutdown.index('vm_memory_handler_thread.join()')
assert shutdown.index('irq_handler_thread.join()') < shutdown.index('mem::drop(linux)')
map_blob = gpu.split('    pub fn resource_map_blob(', 1)[1].split('    /// Shared detach', 1)[0]
assert map_blob.index('blob_detach::active(') < map_blob.index('self.rutabaga.map(resource_id)')
assert gpu.split('impl Drop for VirtioGpu', 1)[1].index('blob_detach::active(') < gpu.split('impl Drop for VirtioGpu', 1)[1].index('ManuallyDrop::drop(')
assert map_blob.index('resource.shmem_offset = Some(offset)') < map_blob.index('.add_mapping_blob(')
assert 'resource.mapping_quarantined = true' in map_blob
assert 'impl Drop for VirtioGpu' in gpu and 'ManuallyDrop::drop(&mut self.rutabaga)' in gpu
remove = pci.split('    fn remove_mapping(&mut self, offset: u64)', 1)[1].split('    fn as_raw_descriptor', 1)[0]
assert remove.index('unregister_memory(id)') < remove.index('self.mappings.remove(&offset)')
assert 'ApiClientError::RequestFailed' in remove
assert map_blob.index('raw_mapping_allowed(') < map_blob.index('self.rutabaga.map(resource_id)')
assert 'native_host3d_resource(resource_id)' in map_blob
assert 'no-host-ack' in map_blob and 'mapper-unsupported' in map_blob and 'non-HOST3D' in map_blob
assert '!gpu_params.allow_implicit_render_server_exec' in (a.source / 'src/crosvm/sys/linux/gpu.rs').read_text()
assert 'mapper.native_detach_supported()' in map_blob
main_gpu = (a.source / 'src/crosvm/sys/linux/gpu.rs').read_text()
assert main_gpu.index('let native_lease_offer =') < main_gpu.index('dev.set_native_lease_offer(native_lease_offer)')
assert 'cfg.vfio.is_empty() && cfg.coiommu_param.is_none()' in main_gpu
assert '!is_sandboxed && !gpu_params.fixed_blob_mapping && !gpu_params.udmabuf' in main_gpu
assert 'cfg.hypervisor = Some(hypervisor.clone())' in linux
core = (a.source / 'rutabaga_gfx/src/rutabaga_core.rs').read_text()
gfx = (a.source / 'rutabaga_gfx/src/gfxstream.rs').read_text()
assert 'native_lease_offer: false' in core
assert 'self.native_lease_offer,' in core
assert gfx.index('if native_lease_offer {') < gfx.index('ret_to_res(stream_renderer_init(') < gfx.index('native_contract.accepted_epoch()')
assert 'fn native_lease_epoch(&self) -> Option<u64> { self.native_lease_epoch }' in gfx
print('Pinned production source integration checks passed')
