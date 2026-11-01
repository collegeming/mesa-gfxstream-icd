#!/usr/bin/env python3
"""Add bounded metadata-only registration logs to the pinned production source."""
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
    p = root / 'base/src/lib.rs'
    old = p.read_text()
    helper = '''// One process-wide line budget shared by VmMemory and KVM registration traces.
// Reserve a pair before the ioctl so budget exhaustion cannot hide its result.
#[doc(hidden)]
pub fn native_slot_trace_reserve(lines: u32) -> Option<u32> {
    use std::sync::atomic::{AtomicU32, Ordering};
    static USED: AtomicU32 = AtomicU32::new(0);
    if std::env::var_os("GFXSTREAM_NATIVE_SLOT_TRACE").as_deref()
        != Some(std::ffi::OsStr::new("1"))
    {
        return None;
    }
    USED.fetch_update(Ordering::Relaxed, Ordering::Relaxed, |used| {
        used.checked_add(lines).filter(|next| *next <= 32)
    })
    .ok()
    .map(|used| used + 1)
}

'''
    changes[p] = replace(old, 'mod alloc;\n', helper + 'mod alloc;\n')

    p = root / 'hypervisor/src/kvm/mod.rs'
    s = p.read_text()
    s = replace(s, '    let ret = if use_2_variant {', '''    let trace = base::native_slot_trace_reserve(2);
    let operation = if memory_size == 0 { "remove" } else { "set" };
    let variant = if use_2_variant { "REGION2" } else { "REGION" };
    let ret = if use_2_variant {''')
    for suffix, region in [('2', 'region2'), ('', 'region')]:
        call = f'        ioctl_with_ref(&kvm.vm, KVM_SET_USER_MEMORY_REGION{suffix}, &{region})'
        before = '''        if let Some(seq) = trace {
            base::warn!("NATIVE-SLOT seq={} stage=kvm-before vm_fd={} ioctl={} op={} slot={} flags={:#x} gpa={:#x} hva={:#x} size={:#x}",
                seq, kvm.vm.as_raw_descriptor(), variant, operation, slot, flags,
                guest_addr, userspace_addr as u64, memory_size);
        }
'''
        s = replace(s, call, before + call)
    s = replace(s, '''    if ret == 0 {
        Ok(())
    } else {
        errno_result()
    }
}''', '''    // Capture errno before any logging; return the original ioctl outcome unchanged.
    let result: Result<()> = if ret == 0 { Ok(()) } else { errno_result() };
    if let Some(seq) = trace {
        base::warn!("NATIVE-SLOT seq={} stage=kvm-after vm_fd={} ioctl={} op={} slot={} flags={:#x} gpa={:#x} hva={:#x} size={:#x} ret={} errno={} success={}",
            seq + 1, kvm.vm.as_raw_descriptor(), variant, operation, slot, flags,
            guest_addr, userspace_addr as u64, memory_size, ret,
            result.as_ref().err().map_or(0, |e| e.errno()), result.is_ok());
    }
    result
}''')
    changes[p] = s

    p = root / 'vm_control/src/lib.rs'
    s = p.read_text()
    s = replace(s, '''                let (mapped_region, size, descriptor) = match source.map(gralloc, prot) {''', '''                // Copy metadata before source.map consumes the request; never read payload.
                let source_kind = match &source {
                    VmMemorySource::SharedMemory(_) => "SharedMemory",
                    VmMemorySource::Descriptor { .. } => "Descriptor",
                    VmMemorySource::Vulkan { .. } => "Vulkan",
                    VmMemorySource::ExternalMapping { .. } => "ExternalMapping",
                };
                let source_ptr = match &source {
                    VmMemorySource::ExternalMapping { ptr, .. } => Some(*ptr),
                    _ => None,
                };
                let dest_offset = match &dest {
                    VmMemoryDestination::ExistingAllocation { offset, .. } => Some(*offset),
                    _ => None,
                };
                let (mapped_region, size, descriptor) = match source.map(gralloc, prot) {''')
    anchor = '''                // Universal runtime attach. Default (KVM / geniezone / Gunyah unprotected) is a'''
    s = replace(s, anchor, '''                let mapped_hva = mapped_region.as_ptr() as u64;
                let mapped_size = mapped_region.size();
                if let Some(seq) = base::native_slot_trace_reserve(1) {
                    base::warn!("NATIVE-SLOT seq={} stage=vm-register-before source={} source_ptr={:#x} source_offset={:#x} dest_is_bar={} dest_offset={:#x} gpa={:#x} hva={:#x} size={:#x} mapped_size={:#x}",
                        seq, source_kind, source_ptr.unwrap_or(0), source_offset,
                        dest_offset.is_some(), dest_offset.unwrap_or(0),
                        guest_addr.offset(), mapped_hva, size, mapped_size);
                }

''' + anchor)
    anchor = '''                VmMemoryResponse::RegisterMemory {
                    region_id,
                    slot,
                    accept_handle,
                }
            }
            #[cfg(any(target_os = "android", target_os = "linux"))]
            MmapAndRegisterMemory {'''
    s = replace(s, anchor, '''                if let Some(seq) = base::native_slot_trace_reserve(1) {
                    base::warn!("NATIVE-SLOT seq={} stage=vm-register-after source={} source_ptr={:#x} source_offset={:#x} dest_is_bar={} dest_offset={:#x} region_id={:#x} gpa={:#x} hva={:#x} size={:#x} mapped_size={:#x} slot={} success=true",
                        seq, source_kind, source_ptr.unwrap_or(0), source_offset,
                        dest_offset.is_some(), dest_offset.unwrap_or(0),
                        region_id.0.offset(), guest_addr.offset(), mapped_hva, size, mapped_size, slot);
                }
''' + anchor)
    changes[p] = s
    return changes


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('source', type=Path)
    args = parser.parse_args()
    root = args.source.resolve()
    actual = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
    if actual != PIN:
        raise RuntimeError(f'unaudited crosvm revision: {actual}')
    # Resolve all unique anchors before changing any file. Run after the paired patches.
    changes = patch(root)
    for p, text in changes.items():
        p.write_text(text)
        print(hashlib.sha256(p.read_bytes()).hexdigest(), p.relative_to(root))


if __name__ == '__main__':
    main()
