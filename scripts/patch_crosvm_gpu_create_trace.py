#!/usr/bin/env python3
"""Add bounded metadata-only resource_create_3d logs to the pinned crosvm source."""
import argparse
import hashlib
import subprocess
from pathlib import Path

PIN = '849e05f12d5c8d4c36af8e15763986d242cd43b5'


def replace(text, old, new):
    if text.count(old) != 1:
        raise RuntimeError(f'expected exactly one source anchor: {old[:100]!r}')
    return text.replace(old, new, 1)


def patch(root):
    changes = {}

    p = root / 'devices/src/virtio/gpu/virtio_gpu.rs'
    s = p.read_text()
    old = '''    pub fn resource_create_3d(
        &mut self,
        resource_id: u32,
        resource_create_3d: ResourceCreate3D,
    ) -> VirtioGpuResult {
        self.rutabaga
            .resource_create_3d(resource_id, resource_create_3d)?;

        let resource = VirtioGpuResource::new(
'''
    new = '''    pub fn resource_create_3d(
        &mut self,
        resource_id: u32,
        resource_create_3d: ResourceCreate3D,
    ) -> VirtioGpuResult {
        let trace = if std::env::var_os("GFXSTREAM_NIRI_IMAGE_TRACE").as_deref()
            == Some(std::ffi::OsStr::new("1"))
        {
            static USED: std::sync::atomic::AtomicU32 = std::sync::atomic::AtomicU32::new(0);
            USED.fetch_add(1, std::sync::atomic::Ordering::Relaxed) < 16
        } else {
            false
        };
        if trace {
            base::warn!(
                "CIB1-DIAG-CROSVM-GPU-CREATE3D: id={} width={} height={} format={} bind=0x{:x} target={} depth={} array={} last-level={} samples={} flags=0x{:x}",
                resource_id,
                resource_create_3d.width,
                resource_create_3d.height,
                resource_create_3d.format,
                resource_create_3d.bind,
                resource_create_3d.target,
                resource_create_3d.depth,
                resource_create_3d.array_size,
                resource_create_3d.last_level,
                resource_create_3d.nr_samples,
                resource_create_3d.flags
            );
        }
        let create_result = self.rutabaga.resource_create_3d(resource_id, resource_create_3d);
        if trace {
            match &create_result {
                Ok(()) => base::warn!(
                    "CIB1-DIAG-CROSVM-GPU-CREATE3D-RESULT: id={} result=ok",
                    resource_id
                ),
                Err(e) => base::warn!(
                    "CIB1-DIAG-CROSVM-GPU-CREATE3D-RESULT: id={} result=err error={:?}",
                    resource_id,
                    e
                ),
            }
        }
        create_result?;

        let resource = VirtioGpuResource::new(
'''
    changes[p] = replace(s, old, new)

    p = root / 'rutabaga_gfx/src/gfxstream.rs'
    s = p.read_text()
    old = '''        // SAFETY:
        // Safe because gfxstream is initialized by now, and the return value is checked before
        // returning a new resource. The backing buffers are not supplied with this call.
        let ret = unsafe { stream_renderer_resource_create(&mut args, null_mut(), 0) };
        ret_to_res(ret)?;
'''
    new = '''        let trace = if std::env::var_os("GFXSTREAM_NIRI_IMAGE_TRACE").as_deref()
            == Some(std::ffi::OsStr::new("1"))
        {
            static USED: std::sync::atomic::AtomicU32 = std::sync::atomic::AtomicU32::new(0);
            USED.fetch_add(1, std::sync::atomic::Ordering::Relaxed) < 16
        } else {
            false
        };
        if trace {
            log::warn!(
                "CIB1-DIAG-CROSVM-RUTABAGA-CREATE3D: id={} width={} height={} format={} bind=0x{:x} target={} depth={} array={} last-level={} samples={} flags=0x{:x}",
                resource_id,
                resource_create_3d.width,
                resource_create_3d.height,
                resource_create_3d.format,
                resource_create_3d.bind,
                resource_create_3d.target,
                resource_create_3d.depth,
                resource_create_3d.array_size,
                resource_create_3d.last_level,
                resource_create_3d.nr_samples,
                resource_create_3d.flags
            );
        }
        // SAFETY:
        // Safe because gfxstream is initialized by now, and the return value is checked before
        // returning a new resource. The backing buffers are not supplied with this call.
        let ret = unsafe { stream_renderer_resource_create(&mut args, null_mut(), 0) };
        if trace {
            log::warn!(
                "CIB1-DIAG-CROSVM-RUTABAGA-CREATE3D-RESULT: id={} ret={}",
                resource_id,
                ret
            );
        }
        ret_to_res(ret)?;
'''
    changes[p] = replace(s, old, new)
    return changes


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('source', type=Path)
    args = parser.parse_args()
    root = args.source.resolve()
    actual = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
    if actual != PIN:
        raise RuntimeError(f'unaudited crosvm revision: {actual}')
    # Resolve every unique anchor before changing any file. Run after paired patches and
    # Android source-compatibility fixes so no anchor depends on a partially patched tree.
    changes = patch(root)
    for p, text in changes.items():
        p.write_text(text)
        print(hashlib.sha256(p.read_bytes()).hexdigest(), p.relative_to(root))


if __name__ == '__main__':
    main()
