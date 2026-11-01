// Tests the real VmRequester over production Tube serialization, without KVM or GPU.
use super::*;
use base::Tube;
use vm_control::{VmMemoryRequest, VmMemoryResponse};

fn requester() -> (VmRequester, Tube) {
    let (client, server) = Tube::pair().unwrap();
    (VmRequester::new(VmMemoryClient::new(client), Alloc::Anon(0),
        SharedMemoryPrepareType::DynamicPerMapping), server)
}

#[test]
fn native_detach_requester_rejection_then_ack_preserves_id() {
    let (mut requester, server) = requester();
    let id = VmMemoryRegionId(vm_memory::GuestAddress(0x10000));
    requester.mappings.insert(64, id);
    let worker = std::thread::spawn(move || {
        assert!(matches!(server.recv::<VmMemoryRequest>().unwrap(), VmMemoryRequest::UnregisterMemory(_)));
        server.send(&VmMemoryResponse::Err(base::Error::new(libc::EBUSY))).unwrap();
        assert!(matches!(server.recv::<VmMemoryRequest>().unwrap(), VmMemoryRequest::UnregisterMemory(_)));
        server.send(&VmMemoryResponse::Ok).unwrap();
    });
    assert!(requester.remove_mapping(64).is_err());
    assert_eq!(requester.mappings.get(&64), Some(&id));
    assert!(!requester.blob_transport_quarantined);
    requester.remove_mapping(64).unwrap();
    assert!(!requester.mappings.contains_key(&64));
    worker.join().unwrap();
}

#[test]
fn native_detach_requester_lost_ack_quarantines_tube() {
    let (mut requester, server) = requester();
    let id = VmMemoryRegionId(vm_memory::GuestAddress(0x10000));
    requester.mappings.insert(64, id);
    let worker = std::thread::spawn(move || {
        server.recv::<VmMemoryRequest>().unwrap();
        // Simulate a detach whose reply is lost, not definite rejection.
    });
    assert!(requester.remove_mapping(64).is_err());
    assert_eq!(requester.mappings.get(&64), Some(&id));
    assert!(requester.blob_transport_quarantined);
    assert!(requester.remove_mapping(64).is_err());
    worker.join().unwrap();
}

#[test]
fn native_detach_requester_register_reply_loss_blocks_remap() {
    let (mut requester, server) = requester();
    let worker = std::thread::spawn(move || {
        assert!(matches!(server.recv::<VmMemoryRequest>().unwrap(), VmMemoryRequest::RegisterMemory { .. }));
    });
    let source = || VmMemorySource::ExternalMapping { ptr: 0, size: 4096 };
    assert!(requester.add_mapping_blob(source(), 64, Protection::read_write(),
        MemCacheType::CacheCoherent, VmAccept::Sync).is_err());
    assert!(requester.blob_transport_quarantined);
    assert!(requester.add_mapping_blob(source(), 64, Protection::read_write(),
        MemCacheType::CacheCoherent, VmAccept::Sync).is_err());
    worker.join().unwrap();
}

#[test]
fn native_detach_requester_duplicate_offset_never_sends() {
    let (mut requester, _server) = requester();
    requester.mappings.insert(64, VmMemoryRegionId(vm_memory::GuestAddress(0x10000)));
    assert!(requester.add_mapping_blob(VmMemorySource::ExternalMapping { ptr: 0, size: 4096 },
        64, Protection::read_write(), MemCacheType::CacheCoherent, VmAccept::Sync).is_err());
    assert!(!requester.blob_transport_quarantined);
}
