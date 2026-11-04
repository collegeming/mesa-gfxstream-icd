#!/usr/bin/env python3
"""Stop one rejected blob map from poisoning every later one, and replace stale offsets.

Applied on top of patch_crosvm_mapping_lease.py (same tree, run after it; anchors are the
post-lease-patch text).

Two defects in `VmRequester::add_mapping_blob` (devices/src/virtio/virtio_pci_device.rs):

1. The quarantine flag is set before `register_memory_for_blob` and cleared only on the success
   path, so ANY failure leaves it true and the guard at the top of the function then rejects
   every later blob map in the process with "blob mapping duplicate or transport quarantined".
   The code's own rule in `remove_mapping` says only transport/protocol failures justify
   quarantining, because only they can leave unread replies on the tube. A server-reported
   rejection (`ApiClientError::RequestFailed`) is a correlated, fully-answered request: the tube
   is clean and the transport must stay usable.

   On this device that difference IS the "first blob works, everything after fails" symptom:
   the first 2 MB map was rejected for its own reason, and every smaller buffer after it -- the
   bar, the terminal, the 4096 B probe -- was refused before it reached the server.

2. The duplicate-offset check returns before the "replace the stale owner" block further down,
   so that block is unreachable for the case it was written for: a legitimate re-registration
   of an offset whose previous owner is gone is reported as "duplicate" instead of replaced.

Also prints the chosen offset, the colliding region id, and the server's complete error text
(the previous `{:#}` produced one letter because the innermost message began with
"Invalid argument" and the formatter truncated it).
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


pci = root / 'devices/src/virtio/virtio_pci_device.rs'
s = pci.read_text(encoding='utf-8')

OLD_GUARD = '''        if self.blob_transport_quarantined || self.mappings.contains_key(&offset) {
            return Err(anyhow!("blob mapping duplicate or transport quarantined"));
        }
        self.blob_transport_quarantined = true;
'''

NEW_GUARD = '''        // A quarantined tube is fatal: a lost reply cannot be told apart from a rejected
        // request, so no further request may be sent. An occupied offset is NOT fatal -- the
        // stale-owner block below replaces it, which is what the guest's drm_mm expects when it
        // reuses a freed BAR slot.
        if self.blob_transport_quarantined {
            return Err(anyhow!(
                "blob transport quarantined after an uncorrelated failure; offset={:#x} not attempted",
                offset
            ));
        }
        if let Some(previous_id) = self.mappings.get(&offset) {
            base::warn!(
                "CIB1-DIAG-CROSVM-MAPBLOB: offset={:#x} occupied by live region id={:?}; \\
replacing it rather than failing the map",
                offset,
                previous_id
            );
        }
        self.blob_transport_quarantined = true;
'''
s = replace(s, OLD_GUARD, NEW_GUARD)

OLD_REG = '''        let (id, gunyah_handle) = self
            .vm_memory_client
            .register_memory_for_blob(
                source,
                VmMemoryDestination::ExistingAllocation {
                    allocation: self.alloc,
                    offset,
                },
                prot,
                cache,
                vm_accept,
            )
            .context("register_memory_for_blob failed")?;
'''

NEW_REG = '''        let registration = self.vm_memory_client.register_memory_for_blob(
            source,
            VmMemoryDestination::ExistingAllocation {
                allocation: self.alloc,
                offset,
            },
            prot,
            cache,
            vm_accept,
        );
        let (id, gunyah_handle) = match registration {
            Ok(registered) => registered,
            Err(vm_control::api::ApiClientError::RequestFailed(server_error)) => {
                // The server received this request and answered with an error: the tube is
                // correlated and clean, so the transport stays usable. Quarantining here is what
                // turned one rejected BAR registration into "every later blob map fails".
                base::error!(
                    "GPU-MAPBLOB: server rejected the registration at offset={:#x}; \
transport stays usable; full error: {}",
                    offset,
                    server_error
                );
                self.blob_transport_quarantined = false;
                return Err(anyhow!(
                    "register_memory_for_blob rejected at offset={:#x}: {}",
                    offset,
                    server_error
                ));
            }
            Err(other) => {
                // Recv/Send/SendFds/UnexpectedResponse: an unread reply may still be on the
                // tube, so this must stay quarantined. Same rule as remove_mapping().
                base::error!(
                    "GPU-MAPBLOB: uncorrelated transport failure at offset={:#x}: {}; \
transport quarantined",
                    offset,
                    other
                );
                return Err(anyhow!(
                    "register_memory_for_blob transport failure at offset={:#x}: {}",
                    offset,
                    other
                ));
            }
        };
'''
s = replace(s, OLD_REG, NEW_REG)

pci.write_text(s, encoding='utf-8')
print('patched', pci.name, '- quarantine lifecycle and stale-offset replacement')

gpu = root / 'devices/src/virtio/gpu/virtio_gpu.rs'
g = gpu.read_text(encoding='utf-8')

OLD_LOG = '''                base::error!(
                    "GPU-MAPBLOB: res={} add_mapping_blob failed offset={:#x} res_size={:#x} prot={:?}: {:#}",
                    resource_id,
                    offset,
                    res_size,
                    prot,
                    e
                );'''

NEW_LOG = '''                base::error!(
                    "GPU-MAPBLOB: res={} add_mapping_blob failed offset={:#x} res_size={:#x} prot={:?}: {:#} \\
| full chain: {}",
                    resource_id,
                    offset,
                    res_size,
                    prot,
                    e,
                    {
                        let mut chain = format!("{}", e);
                        let mut cursor = std::error::Error::source(&*e);
                        while let Some(cause) = cursor {
                            chain.push_str(" <- ");
                            chain.push_str(&format!("{}", cause));
                            cursor = cause.source();
                        }
                        chain
                    }
                );'''
g = replace(g, OLD_LOG, NEW_LOG)
gpu.write_text(g, encoding='utf-8')
print('patched', gpu.name, '- full error chain in the MAPBLOB failure line')
