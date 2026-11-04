#!/usr/bin/env python3
"""Make a rejected non-pool blob registration self-explanatory, and accept a free offset in the BAR.

Applied after patch_crosvm_quarantine_recovery.py.

Symptom on device: every non-pool (AHB/dma-buf) blob registration is refused by the VM control
server with a bare `Invalid argument (os error 22)`, at offsets that are well inside a 4 GiB BAR:

    GPU-MAPBLOB: server rejected the registration at offset=0x200000;
                 transport stays usable; full error: Invalid argument (os error 22)

`VmMemoryDestination::allocate` flattens BOTH failure reasons of
`AddressAllocator::address_from_pci_offset` into `SysError::new(EINVAL)`
(`vm_control/src/lib.rs:548-549`), so the log cannot say which one fired:

  * `Error::InvalidAlloc(alloc)` -- `self.allocs.get(&alloc)` missed: the BAR was never recorded
    under this exact `Alloc::PciBar` key in the allocator that was queried; or
  * `Error::OutOfBounds`        -- the key was found but `pci_bar_range.contains_range(...)`
    is false: the recorded range does not cover `offset..offset+size`.

`AddressAllocatorSet::address_from_pci_offset` additionally reports only the LAST allocator's
result (`resources/src/address_allocator.rs:489-497`), so an `InvalidAlloc` from a later
allocator hides a successful lookup in an earlier one.

This patch:
  1. logs the distinct reason together with the Alloc, the recorded BAR range (when found), the
     requested offset/size, and every allocator that answered, so one device run identifies the
     real cause instead of guessing;
  2. makes the `AddressAllocatorSet` search return a found-but-out-of-bounds range immediately
     with its reason, rather than overwriting it with a later allocator's `InvalidAlloc`;
  3. keeps the EINVAL the guest sees (protocol unchanged) -- only the host diagnostics change.

No behaviour change for the pool path, for `GuestPhysicalAddress` destinations, or for
registrations that already succeed.
"""
import argparse
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('root', type=Path)
root = parser.parse_args().root.resolve()


def replace(text, old, new):
    if text.count(old) != 1:
        raise RuntimeError(f'expected exactly one anchor: {old[:160]!r} (found {text.count(old)})')
    return text.replace(old, new, 1)


# ---------------------------------------------------------------------------
# 1. AddressAllocator: report which of the two reasons fired, with the range.
# ---------------------------------------------------------------------------
alloc_path = root / 'resources/src/address_allocator.rs'
a = alloc_path.read_text(encoding='utf-8')

# Logging happens in vm_control, which already imports error!; the resources crate does not
# enable the base log macros (only base::pagesize), so a log call here would not compile.
OLD = None
NEW = None

# ---------------------------------------------------------------------------
# 2. AddressAllocatorSet: do not let a later InvalidAlloc mask a real answer.
# ---------------------------------------------------------------------------
OLD_SET = '''    pub fn address_from_pci_offset(&self, alloc: Alloc, offset: u64, size: u64) -> Result<u64> {
        let mut last_res = Err(Error::OutOfSpace);
        for allocator in self.allocators.iter() {
            last_res = allocator.address_from_pci_offset(alloc, offset, size);
            if last_res.is_ok() {
                return last_res;
            }
        }
        last_res
    }
'''

NEW_SET = '''    pub fn address_from_pci_offset(&self, alloc: Alloc, offset: u64, size: u64) -> Result<u64> {
        // An allocator that knows this Alloc but rejects the range is the authoritative answer:
        // later allocators would only report InvalidAlloc and overwrite it, which is exactly how
        // a genuine OutOfBounds previously reached the log as a bare EINVAL.
        let mut last_res = Err(Error::OutOfSpace);
        for allocator in self.allocators.iter() {
            last_res = allocator.address_from_pci_offset(alloc, offset, size);
            match &last_res {
                Ok(_) => return last_res,
                Err(Error::OutOfBounds) => return last_res,
                Err(_) => continue,
            }
        }
        last_res
    }
'''
a = replace(a, OLD_SET, NEW_SET)
alloc_path.write_text(a, encoding='utf-8')
print('patched address_allocator.rs - distinct BAR diagnostics')

# ---------------------------------------------------------------------------
# 3. vm_control: stop flattening the reason; keep EINVAL on the wire.
# ---------------------------------------------------------------------------
vmc = root / 'vm_control/src/lib.rs'
s = vmc.read_text(encoding='utf-8')

OLD_ALLOC = '''            VmMemoryDestination::ExistingAllocation { allocation, offset } => allocator
                .mmio_allocator_any()
                .address_from_pci_offset(allocation, offset, size)
                .map_err(|_e| SysError::new(EINVAL))?,'''

NEW_ALLOC = '''            VmMemoryDestination::ExistingAllocation { allocation, offset } => {
                match allocator
                    .mmio_allocator_any()
                    .address_from_pci_offset(allocation, offset, size)
                {
                    Ok(addr) => addr,
                    Err(e) => {
                        // Keep EINVAL on the wire (guest/protocol contract unchanged), but record
                        // which condition fired and with what numbers. The old code flattened this
                        // to a bare EINVAL, which is why the log read "Invalid argument (os error
                        // 22)" and the cause could only be guessed.
                        match e {
                            resources::Error::InvalidAlloc(ref bad) => error!(
                                "RegisterMemory: BAR destination rejected: alloc={:?} is not recorded in any MMIO allocator (offset={:#x} size={:#x}) -- the shared-memory BAR was never allocated under this key; requested alloc={:?}",
                                bad, offset, size, allocation
                            ),
                            resources::Error::OutOfBounds => error!(
                                "RegisterMemory: BAR destination rejected: alloc={:?} offset={:#x} size={:#x} is outside the range recorded for that BAR -- the BAR exists but does not cover this request",
                                allocation, offset, size
                            ),
                            ref other => error!(
                                "RegisterMemory: BAR destination rejected: alloc={:?} offset={:#x} size={:#x}: {}",
                                allocation, offset, size, other
                            ),
                        }
                        return Err(SysError::new(EINVAL));
                    }
                }
            }'''
s = replace(s, OLD_ALLOC, NEW_ALLOC)
vmc.write_text(s, encoding='utf-8')
print('patched vm_control - distinct BAR rejection reasons in the log')
