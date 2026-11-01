#!/usr/bin/env python3
"""Paired native lease ABI, applied after patch_crosvm_native_detach.py."""
from pathlib import Path
import argparse
import shutil

def replace(s, old, new):
    if s.count(old) != 1:
        raise RuntimeError(f'lease anchor count {s.count(old)}: {old[:90]!r}')
    return s.replace(old, new, 1)

def main():
    p = argparse.ArgumentParser()
    p.add_argument('source', type=Path)
    root = p.parse_args().source.resolve()
    lib = root / 'rutabaga_gfx/src/lib.rs'
    lib.write_text(replace(lib.read_text(), 'mod rutabaga_core;', 'mod rutabaga_core;\npub mod native_lease;'))
    shutil.copyfile(Path(__file__).with_name('native_lease.rs'), lib.with_name('native_lease.rs'))
    core = root / 'rutabaga_gfx/src/rutabaga_core.rs'
    s = core.read_text()
    s = replace(s, 'pub trait RutabagaComponent {', '''pub trait RutabagaComponent {
    fn native_lease_epoch(&self) -> Option<u64> { None }
''')
    s = replace(s, '    renderer_features: Option<String>,', '    renderer_features: Option<String>,\n    native_lease_offer: bool,')
    s = replace(s, '            renderer_features: None,', '            renderer_features: None,\n            native_lease_offer: false,')
    s = replace(s, '    /// Set display width for the RutabagaBuilder', '''    /// Only a VMM with audited detach/reset/shutdown wiring may offer this contract.
    pub fn set_native_lease_offer(mut self, offered: bool) -> Self {
        self.native_lease_offer = offered;
        self
    }

    pub fn native_lease_offer(&self) -> bool { self.native_lease_offer }

    /// Set display width for the RutabagaBuilder''')
    s = replace(s, '''                    self.debug_handler.clone(),
                )?;

                rutabaga_components.insert(RutabagaComponentType::Gfxstream''', '''                    self.debug_handler.clone(),
                    self.native_lease_offer,
                )?;

                rutabaga_components.insert(RutabagaComponentType::Gfxstream''')
    s = replace(s, '    pub fn map(&mut self, resource_id: u32)', '''    pub fn native_lease_epoch(&self) -> Option<u64> {
        self.components.get(&RutabagaComponentType::Gfxstream)?.native_lease_epoch()
    }

    pub fn native_host3d_resource(&self, resource_id: u32) -> bool {
        self.resources.get(&resource_id).is_some_and(|r|
            r.blob_mem == RUTABAGA_BLOB_MEM_HOST3D
            && r.component_mask == 1 << (RutabagaComponentType::Gfxstream as u8))
    }

    pub fn map(&mut self, resource_id: u32)''')
    core.write_text(s)
    gfx = root / 'rutabaga_gfx/src/gfxstream.rs'
    s = gfx.read_text()
    s = replace(s, 'pub struct Gfxstream {', 'pub struct Gfxstream {\n    native_lease_epoch: Option<u64>,')
    s = replace(s, '''        debug_handler: Option<RutabagaDebugHandler>,
    ) -> RutabagaResult<Box<dyn RutabagaComponent>>''', '''        debug_handler: Option<RutabagaDebugHandler>,
        native_lease_offer: bool,
    ) -> RutabagaResult<Box<dyn RutabagaComponent>>''')
    s = replace(s, '''        // TODO(b/315870313): Add safety comment
        #[allow(clippy::undocumented_unsafe_blocks)]
        unsafe {
            ret_to_res(stream_renderer_init(''', '''        let mut native_contract = crate::native_lease::NativeLeaseContract::offer();
        if native_lease_offer {
            stream_renderer_params.push(stream_renderer_param {
                key: crate::native_lease::NATIVE_LEASE_PARAM,
                // Borrow valid only during synchronous init; host must not retain it.
                value: &mut native_contract as *mut _ as u64,
            });
        }

        // TODO(b/315870313): Add safety comment
        #[allow(clippy::undocumented_unsafe_blocks)]
        unsafe {
            ret_to_res(stream_renderer_init(''')
    s = replace(s, '        Ok(Box::new(Gfxstream {', '''        let native_lease_epoch = if native_lease_offer {
            native_contract.accepted_epoch()
        } else { None };
        log::info!("DVML native lease init: offered={} accepted={:#x} host_supported={:#x} epoch={} ready={}",
            native_lease_offer, native_contract.accepted, native_contract.host_supported,
            native_contract.epoch, native_lease_epoch.is_some());
        Ok(Box::new(Gfxstream {
            native_lease_epoch,''')
    s = replace(s, 'impl RutabagaComponent for Gfxstream {', '''impl RutabagaComponent for Gfxstream {
    fn native_lease_epoch(&self) -> Option<u64> { self.native_lease_epoch }
''')
    gfx.write_text(s)
    # Actual device capability is default-false for all other transports/devices.
    device = root / 'devices/src/virtio/virtio_device.rs'
    s = device.read_text()
    s = replace(s, 'pub trait SharedMemoryMapper: Send {', '''pub trait SharedMemoryMapper: Send {
    fn native_detach_supported(&self) -> bool { false }
''')
    s = replace(s, '    fn set_shared_memory_mapper(&mut self, _mapper: Box<dyn SharedMemoryMapper>) {}', '''    fn set_shared_memory_mapper(&mut self, _mapper: Box<dyn SharedMemoryMapper>) {}

    fn native_detach_supported(&self) -> bool { false }''')
    device.write_text(s)
    pci = root / 'devices/src/virtio/virtio_pci_device.rs'
    s = pci.read_text()
    s = replace(s, '    blob_transport_quarantined: bool,', '    blob_transport_quarantined: bool,\n    native_detach_wiring: bool,')
    s = replace(s, '            blob_transport_quarantined: false,', '            blob_transport_quarantined: false,\n            native_detach_wiring: false,')
    s = replace(s, '        let vm_requester = Box::new(VmRequester::new(vm_memory_client, alloc, prepare_type));', '''        let native_detach_wiring = virtio_pci_device.device.native_detach_supported()
            && matches!(prepare_type, SharedMemoryPrepareType::DynamicPerMapping);
        let mut vm_requester = Box::new(VmRequester::new(vm_memory_client, alloc, prepare_type));
        vm_requester.native_detach_wiring = native_detach_wiring;''')
    s = replace(s, 'impl SharedMemoryMapper for VmRequester {', '''impl SharedMemoryMapper for VmRequester {
    fn native_detach_supported(&self) -> bool {
        self.native_detach_wiring && !self.blob_transport_quarantined
            && matches!(self.prepare_type, SharedMemoryPrepareType::DynamicPerMapping)
    }
''')
    pci.write_text(s)
    gpu = root / 'devices/src/virtio/gpu/mod.rs'
    s = gpu.read_text()
    s = replace(s, '    fn set_shared_memory_mapper(&mut self, mapper: Box<dyn SharedMemoryMapper>) {', '''    fn native_detach_supported(&self) -> bool {
        self.rutabaga_builder.native_lease_offer() && !self.fixed_blob_mapping
    }

    fn set_shared_memory_mapper(&mut self, mapper: Box<dyn SharedMemoryMapper>) {''')
    s = replace(s, '    pub fn initialize_frontend(', '''    pub fn set_native_lease_offer(&mut self, offered: bool) {
        self.rutabaga_builder = self.rutabaga_builder.clone().set_native_lease_offer(offered);
    }

    pub fn initialize_frontend(''')
    gpu.write_text(s)
    # The source worker starts before mapper installation. Offer states concrete VMM wiring;
    # raw MAP additionally verifies the actual installed mapper and host ack epoch.
    main_gpu = root / 'src/crosvm/sys/linux/gpu.rs'
    s = main_gpu.read_text()
    s = replace(s, '    #[allow(unused_mut)]\n    let mut dev = virtio::Gpu::new(', '''    #[cfg(not(feature = "gfxstream"))]
    let native_lease_offer = false;
    #[cfg(feature = "gfxstream")]
    let native_lease_offer = matches!(cfg.hypervisor,
        Some(crate::crosvm::config::HypervisorKind::Kvm { .. }))
        && !is_sandboxed && !gpu_params.fixed_blob_mapping && !gpu_params.udmabuf
        && !gpu_params.external_blob && render_server_fd.is_none()
        && !gpu_params.allow_implicit_render_server_exec && !has_vfio_gfx_device
        && cfg.vfio.is_empty() && cfg.coiommu_param.is_none()
        && cfg.protection_type == hypervisor::ProtectionType::Unprotected
        && gpu_params.mode == devices::virtio::GpuMode::ModeGfxstream;
    #[allow(unused_mut)]
    let mut dev = virtio::Gpu::new(''')
    s = replace(s, '    #[cfg(any(feature = "vnc", feature = "android_display"))]\n    if !transport_cap', '    dev.set_native_lease_offer(native_lease_offer);\n    #[cfg(any(feature = "vnc", feature = "android_display"))]\n    if !transport_cap')
    main_gpu.write_text(s)
    linux = root / 'src/crosvm/sys/linux.rs'
    s = linux.read_text()
    s = replace(s, 'pub fn run_config(cfg: Config)', 'pub fn run_config(mut cfg: Config)')
    s = replace(s, '    debug!("creating hypervisor: {:?}", hypervisor);', '''    // Preserve the actual selected backend (including default selection) for device capability wiring.
    cfg.hypervisor = Some(hypervisor.clone());
    debug!("creating hypervisor: {:?}", hypervisor);''')
    linux.write_text(s)
    virtio_gpu = root / 'devices/src/virtio/gpu/virtio_gpu.rs'
    s = virtio_gpu.read_text()
    s = replace(s, '''        if source.is_none() {
            if self.fixed_blob_mapping {''', '''        if source.is_none() {
            let mapper_supported = self.mapper.lock().as_ref()
                .is_some_and(|mapper| mapper.native_detach_supported());
            let epoch = self.rutabaga.native_lease_epoch();
            let host3d = self.rutabaga.native_host3d_resource(resource_id);
            if !rutabaga_gfx::native_lease::raw_mapping_allowed(epoch, mapper_supported) || !host3d {
                let reason = if epoch.is_none() { "no-host-ack" }
                    else if !mapper_supported { "mapper-unsupported" }
                    else { "non-HOST3D" };
                static REJECTION_LOGGED: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);
                if !REJECTION_LOGGED.swap(true, std::sync::atomic::Ordering::Relaxed) {
                    base::warn!("DVML raw mapping rejected: reason={} res={} epoch={:?} mapper={} host3d={}",
                        reason, resource_id, epoch, mapper_supported, host3d);
                }
                return Err(ErrUnspec);
            }
            if self.fixed_blob_mapping {''')
    virtio_gpu.write_text(s)
    print('Applied mapper-aware paired native lease offer and acknowledged raw-map gate')

if __name__ == '__main__':
    main()
