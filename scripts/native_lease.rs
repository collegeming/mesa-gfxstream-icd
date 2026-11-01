// Copyright 2026 The ChromiumOS Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

pub const NATIVE_LEASE_PARAM: u64 = 0x4456_4d4c;
pub const NATIVE_LEASE_MASK: u64 = 0x0f;
pub const NATIVE_LEASE_ID: [u8; 16] = [
    0x44, 0x56, 0x4d, 0x4e, 0x4c, 0x45, 0x41, 0x53,
    0x45, 0, 0, 0, 0, 0, 0, 1,
];

/// Private paired-crosvm/gfxstream ABI. Borrowed only during stream_renderer_init.
/// Old hosts ignore the key and leave the output fields zero (raw MAP stays off).
#[repr(C)]
#[derive(Clone, Debug)]
pub struct NativeLeaseContract {
    pub size: u32,
    pub version: u32,
    pub offered: u64,
    pub required: u64,
    pub host_supported: u64,
    pub accepted: u64,
    pub epoch: u64,
    pub contract_id: [u8; 16],
}

impl NativeLeaseContract {
    pub fn offer() -> Self {
        Self {
            size: 64,
            version: 1,
            offered: NATIVE_LEASE_MASK,
            required: NATIVE_LEASE_MASK,
            host_supported: 0,
            accepted: 0,
            epoch: 0,
            contract_id: NATIVE_LEASE_ID,
        }
    }

    /// Validate every input again: a malformed or partial acknowledgement is not readiness.
    /// Store only the returned epoch in the component, never this borrowed init pointer.
    pub fn accepted_epoch(&self) -> Option<u64> {
        (self.size == 64 && self.version == 1 && self.contract_id == NATIVE_LEASE_ID
            && self.offered == NATIVE_LEASE_MASK && self.required == NATIVE_LEASE_MASK
            && self.host_supported & NATIVE_LEASE_MASK == NATIVE_LEASE_MASK
            && self.accepted == NATIVE_LEASE_MASK && self.epoch != 0)
            .then_some(self.epoch)
    }
}

/// Config offer is deliberately narrower than runtime support. Actual mapper capability
/// and a host epoch must still be checked before acquiring any native renderer mapping.
pub fn raw_mapping_allowed(epoch: Option<u64>, mapper_supported: bool) -> bool {
    epoch.is_some_and(|e| e != 0) && mapper_supported
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn native_lease_abi_layout() {
        assert_eq!(std::mem::size_of::<NativeLeaseContract>(), 64);
        assert_eq!(std::mem::align_of::<NativeLeaseContract>(), 8);
        assert_eq!(std::mem::offset_of!(NativeLeaseContract, size), 0);
        assert_eq!(std::mem::offset_of!(NativeLeaseContract, version), 4);
        assert_eq!(std::mem::offset_of!(NativeLeaseContract, offered), 8);
        assert_eq!(std::mem::offset_of!(NativeLeaseContract, required), 16);
        assert_eq!(std::mem::offset_of!(NativeLeaseContract, host_supported), 24);
        assert_eq!(std::mem::offset_of!(NativeLeaseContract, accepted), 32);
        assert_eq!(std::mem::offset_of!(NativeLeaseContract, epoch), 40);
        assert_eq!(std::mem::offset_of!(NativeLeaseContract, contract_id), 48);
    }

    fn ack() -> NativeLeaseContract {
        let mut c = NativeLeaseContract::offer();
        c.host_supported = NATIVE_LEASE_MASK;
        c.accepted = NATIVE_LEASE_MASK;
        c.epoch = 123;
        c
    }

    #[test]
    fn native_lease_old_host_missing_ack_is_disabled() {
        assert_eq!(NativeLeaseContract::offer().accepted_epoch(), None);
        assert!(!raw_mapping_allowed(None, true));
    }

    #[test]
    fn native_lease_valid_pair_still_requires_actual_mapper() {
        assert_eq!(ack().accepted_epoch(), Some(123));
        assert!(!raw_mapping_allowed(ack().accepted_epoch(), false));
        assert!(raw_mapping_allowed(ack().accepted_epoch(), true));
        assert!(!raw_mapping_allowed(Some(0), true));
    }

    #[test]
    fn native_lease_rejects_malformed_or_partial_ack() {
        for field in 0..8 {
            let mut c = ack();
            match field {
                0 => c.size = 63,
                1 => c.version = 2,
                2 => c.offered = 7,
                3 => c.required = 7,
                4 => c.host_supported = 7,
                5 => c.accepted = 7,
                6 => c.epoch = 0,
                _ => c.contract_id[15] = 2,
            }
            assert_eq!(c.accepted_epoch(), None, "field {}", field);
        }
    }
}
