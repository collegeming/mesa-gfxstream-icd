#!/usr/bin/env python3
"""Keep a removed arena range writable so a guest write cannot fault KVM_RUN with EFAULT.

The virtio-gpu shared-memory BAR is prepared as one `MemoryMappingArena`, and crosvm registers
the whole arena as a single KVM memslot (`prepare_shared_memory_region`). Each blob is then mapped
into a sub-range with `add_fd_mapping`; a blob UNMAP (or a stale-offset replacement) calls
`MemoryMappingArena::remove`, which currently maps a *read-only* anonymous page over the range.

A guest write into such a range produces a stage-2 permission fault. arm64 KVM resolves it with
FOLL_WRITE against a PROT_READ VMA, gets KVM_PFN_ERR_RO_FAULT and returns -EFAULT from KVM_RUN
(`kvm_s2_fault_pin_pfn` -> `__kvm_faultin_pfn`). crosvm's vCPU loop treats every errno other than
EINTR/EAGAIN as fatal, so the VM exits with "vcpu hit unknown error: Bad address (os error 14)".
Any Windows display-adapter restart (pnputil /restart-device) tears blobs down and makes that
window reachable.

Fill the hole with an anonymous private read-write mapping instead. It still exposes no blob data
(writes land in private zero pages, reads return them), but the guest's stage-2 write path stays
resolvable. The range is the guest's own BAR window; it is not a host security boundary.

Applied to the pinned production crosvm source (collegeming/crosvm 849e05f). Run with -I.
"""
import argparse
import hashlib
from pathlib import Path

OLD_REMOVE = '''    pub fn remove(&mut self, offset: usize, size: usize) -> Result<()> {
        self.try_add(offset, size, Protection::read(), None)
    }
'''

NEW_REMOVE = '''    pub fn remove(&mut self, offset: usize, size: usize) -> Result<()> {
        // The arena backs guest memory (the virtio-gpu shared-memory BAR is one KVM memslot).
        // A read-only fill here is fatal: a guest write to the removed range takes a stage-2
        // permission fault, and arm64 KVM turns FOLL_WRITE on a PROT_READ VMA into -EFAULT from
        // KVM_RUN, which crosvm's vCPU loop treats as an unrecoverable error. Fill with an
        // anonymous private RW mapping: no blob's data becomes reachable (writes go to private
        // zero pages), but the guest write path still resolves.
        self.try_add(offset, size, Protection::read_write(), None)
    }
'''

OLD_TEST_ANCHOR = '''    #[test]
    fn arena_remove_unaligned() {
'''

NEW_TEST = '''    #[test]
    fn arena_remove_leaves_a_writable_gap() {
        let ps = pagesize();
        let mut arena = MemoryMappingArena::new(4 * ps).unwrap();
        arena
            .add_anon_protection(0, 2 * ps, Protection::read_write())
            .expect("failed to add sub-mapping");
        arena.remove(0, 2 * ps).expect("failed to remove sub-mapping");

        // The removed range is still covered by the arena's KVM memslot, so the guest may write
        // it. A PROT_READ fill would turn that write into KVM_RUN EFAULT; this write would
        // SIGSEGV if the gap were not writable.
        let ptr = arena.as_ptr();
        // SAFETY: the arena owns [ptr, ptr + 4 * ps) and the range is mapped.
        unsafe {
            std::ptr::write_bytes(ptr, 0xa5, 2 * ps);
        }
        // SAFETY: same range, read back to keep the write from being optimized out.
        assert_eq!(unsafe { *ptr }, 0xa5);
    }

    #[test]
    fn arena_remove_unaligned() {
'''


def patch(root: Path) -> dict:
    path = root / "base/src/sys/linux/mmap.rs"
    with open(path, encoding="utf-8", newline="") as f:
        text = f.read()
    if text.count(OLD_REMOVE) != 1:
        raise RuntimeError(
            f"expected exactly one remove() anchor, found {text.count(OLD_REMOVE)}"
        )
    if OLD_TEST_ANCHOR not in text:
        raise RuntimeError("missing arena_remove_unaligned test anchor")
    text = text.replace(OLD_REMOVE, NEW_REMOVE, 1)
    text = text.replace(OLD_TEST_ANCHOR, NEW_TEST, 1)
    # newline="" keeps the tree's original line endings byte for byte.
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the patched text is present instead of patching",
    )
    args = parser.parse_args()
    root = args.root.resolve()
    path = root / "base/src/sys/linux/mmap.rs"
    if args.check:
        text = path.read_text(encoding="utf-8")
        assert "self.try_add(offset, size, Protection::read_write(), None)" in text
        assert "fn arena_remove_leaves_a_writable_gap()" in text
        print("arena-remove-writable: present")
        return
    for name, digest in patch(root).items():
        print(f"patched {name} sha256={digest}")


if __name__ == "__main__":
    main()
