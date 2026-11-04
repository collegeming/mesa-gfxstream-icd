#!/usr/bin/env python3
"""Assert the blob-map quarantine invariants on a patched crosvm tree.

Run after patch_crosvm_mapping_lease.py and patch_crosvm_quarantine_recovery.py (CI runs it
after the patch step).

Checks in `VmRequester::add_mapping_blob` (devices/src/virtio/virtio_pci_device.rs):
  1. the guard rejects ONLY a quarantined transport, not an occupied offset -- otherwise the
     stale-owner replacement further down is unreachable, which is the second defect;
  2. an occupied offset is logged with the colliding region id and then replaced;
  3. a server-reported rejection (`ApiClientError::RequestFailed`) clears the flag before
     returning, so the transport stays usable;
  4. every other error leaves the flag set, same rule as `remove_mapping`;
  5. the flag is still cleared on the success path;
  6. the function is brace-balanced and never mentions `res_size` (a caller-side variable, not a
     parameter of this function -- referencing it would not compile);
  7. the MAPBLOB failure log in virtio_gpu.rs now prints the full error chain.
"""
import pathlib
import sys

root = pathlib.Path(sys.argv[1]).resolve()
out = pathlib.Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else None

pci_text = (root / 'devices/src/virtio/virtio_pci_device.rs').read_text(
    encoding='utf-8', errors='replace').replace('\r\n', '\n')
gpu_text = (root / 'devices/src/virtio/gpu/virtio_gpu.rs').read_text(
    encoding='utf-8', errors='replace').replace('\r\n', '\n')

start = pci_text.index('fn add_mapping_blob(')
end = pci_text.index('fn remove_mapping(', start)
fn = pci_text[start:end]
remove_fn = pci_text[end:end + 2000]

# 1. guard splits the two conditions.
old_guard = 'if self.blob_transport_quarantined || self.mappings.contains_key(&offset) {'
assert old_guard not in fn, 'the combined duplicate/quarantine guard must be gone'
assert 'if self.blob_transport_quarantined {' in fn, 'quarantine must still be checked'
assert 'contains_key(&offset)' not in fn[:fn.index('if !self.prepared')], \
    'an occupied offset must not be treated as fatal'

# 2. occupied offset is logged with the region id, then replaced by existing code.
assert 'occupied by live region id' in fn
occupied = fn.index('occupied by live region id')
replace_block = fn.index('replaces mapping id=')
assert occupied < replace_block, 'the collision must be reported before replacement'
assert 'self.mappings.remove(&offset)' in fn[replace_block - 200:replace_block + 120]

# 3. RequestFailed clears the flag before returning.
rf_start = fn.index('Err(vm_control::api::ApiClientError::RequestFailed(server_error)) => {')
rf_arm = fn[rf_start:fn.index('Err(other) => {', rf_start)]
assert 'self.blob_transport_quarantined = false;' in rf_arm, \
    'a server-reported rejection must leave the transport usable'
assert rf_arm.index('self.blob_transport_quarantined = false;') < rf_arm.index('return Err('), \
    'the flag must be cleared before the early return'
assert 'full error' in rf_arm or 'server_error' in rf_arm

# 4. every other error keeps it quarantined.
other_start = fn.index('Err(other) => {')
other_arm = fn[other_start:fn.index('};', other_start)]
assert 'self.blob_transport_quarantined = false;' not in other_arm, \
    'uncorrelated failures must stay quarantined'
assert 'quarantined' in other_arm

# 5. success path still clears it.
assert 'self.mappings.insert(offset, id);' in fn
assert 'self.blob_transport_quarantined = false;\n        Ok(gunyah_handle)' in fn

# 6. balance and scope.
assert 'res_size' not in fn, 'res_size is not a parameter of add_mapping_blob'
depth = 0
for line in fn.splitlines():
    code = line
    idx = code.find('//')
    if idx >= 0 and code[:idx].count('"') % 2 == 0:
        code = code[:idx]
    depth += code.count('{') - code.count('}')
assert depth == 0, f'add_mapping_blob is unbalanced (depth {depth})'

# remove_mapping keeps the original rule, which is what add_mapping_blob now mirrors.
assert 'ApiClientError::RequestFailed(_)' in remove_fn

# 7. the caller logs the whole chain.
assert 'full chain' in gpu_text
assert 'std::error::Error::source(&*e)' in gpu_text
assert gpu_text.count('GPU-MAPBLOB: res={} add_mapping_blob failed') == 1

report = (
    'PASS guard splits quarantine from occupied offset; stale-owner replacement reachable\n'
    'PASS occupied offset logged with the colliding region id, then replaced\n'
    'PASS RequestFailed clears the quarantine flag before returning\n'
    'PASS uncorrelated transport failures stay quarantined\n'
    'PASS success path still clears the flag\n'
    'PASS add_mapping_blob balanced; res_size not referenced\n'
    'PASS MAPBLOB failure line prints the full error chain\n'
)
print(report, end='')
if out:
    out.mkdir(parents=True, exist_ok=True)
    (out / 'crosvm-quarantine-tests.txt').write_text(report)
