#!/usr/bin/env python3
"""Scan retained guest blob descriptors for the scanout target of a CIB frame.

CIB1 image resources are host colour buffers, not guest blobs, so the resource
handle itself has no guest fd. The compositor's scanout buffer is a different
guest blob (created by the gbm winsys with CREATE_GUEST_HANDLE), retained by
ExternalObjectManager keyed by its resource. Add an enumeration helper and let
the on-request writeback copy the frame into every retained guest descriptor
whose dma-buf size matches the frame size.
"""
import argparse
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('root', type=Path)
root = parser.parse_args().root.resolve()


def edit(rel, old, new):
    path = root / rel
    text = path.read_text(encoding='utf-8')
    if text.count(old) != 1:
        raise SystemExit(f'{rel}: expected one anchor ({text.count(old)}): {old[:90]}')
    path.write_text(text.replace(old, new), encoding='utf-8', newline='')


# 1) Header: include <vector> and declare the enumeration helper.
h = root / 'host/common/include/gfxstream/host/external_object_manager.h'
text = h.read_text(encoding='utf-8')
if '#include <vector>' not in text:
    anchor = '#include <utility>\n'
    if text.count(anchor) != 1:
        raise SystemExit('missing <utility> include anchor')
    text = text.replace(anchor, anchor + '#include <vector>\n', 1)
    h.write_text(text, encoding='utf-8', newline='')

edit('host/common/include/gfxstream/host/external_object_manager.h',
     '    void removeGuestBlobResourceDescriptor(uint32_t resHandle);\n',
     '''    void removeGuestBlobResourceDescriptor(uint32_t resHandle);
    // Test-only: dup every retained guest blob resource descriptor (resource, fd).
    // Used by the scanout writeback when the CIB resource is a host colour buffer.
    std::vector<std::pair<uint32_t, int>> dupAllGuestBlobResourceDescriptors();
''')

# 2) Implementation next to the single-descriptor lookup.
edit('host/common/external_object_manager.cpp',
     '''int ExternalObjectManager::dupGuestBlobResourceDescriptor(uint32_t resHandle) {
    std::lock_guard<std::mutex> lock(mMutex);
    auto found = mGuestBlobResourceFds.find(resHandle);
    if (found == mGuestBlobResourceFds.end()) return -1;
    return dup(found->second);
}
''',
     '''int ExternalObjectManager::dupGuestBlobResourceDescriptor(uint32_t resHandle) {
    std::lock_guard<std::mutex> lock(mMutex);
    auto found = mGuestBlobResourceFds.find(resHandle);
    if (found == mGuestBlobResourceFds.end()) return -1;
    return dup(found->second);
}

std::vector<std::pair<uint32_t, int>>
ExternalObjectManager::dupAllGuestBlobResourceDescriptors() {
    std::lock_guard<std::mutex> lock(mMutex);
    std::vector<std::pair<uint32_t, int>> out;
    out.reserve(mGuestBlobResourceFds.size());
    for (const auto& entry : mGuestBlobResourceFds) {
        int dupFd = dup(entry.second);
        if (dupFd >= 0) out.emplace_back(entry.first, dupFd);
    }
    return out;
}
''')

# 3) Decoder: include sys/stat.h for fstat.
d = root / 'host/vulkan/vk_decoder_global_state.cpp'
text = d.read_text(encoding='utf-8')
if '#include <sys/stat.h>' not in text:
    anchor = '#include <sys/mman.h>\n'
    if text.count(anchor) != 1:
        raise SystemExit('missing <sys/mman.h> include anchor')
    text = text.replace(anchor, anchor + '#include <sys/stat.h>\n', 1)
    d.write_text(text, encoding='utf-8', newline='')

# 4) Writeback: when the CIB resource has no guest fd, scan retained guest
#    descriptors for a same-size dma-buf and copy the frame there.
edit('host/vulkan/vk_decoder_global_state.cpp',
     '''                                if (wbFd < 0) {
                                    GFXSTREAM_WARNING(
                                        "CIB-WRITEBACK: resource %u no guest fd", resource);
                                } else {''',
     '''                                if (wbFd < 0) {
                                    // CIB1 image resources are host colour buffers, not
                                    // guest blobs. The compositor's scanout buffer is a
                                    // different guest blob; find a retained descriptor
                                    // whose dma-buf size matches this frame.
                                    auto descriptors =
                                        ExternalObjectManager::get()
                                            ->dupAllGuestBlobResourceDescriptors();
                                    size_t matched = 0;
                                    for (auto& desc : descriptors) {
                                        int candFd = desc.second;
                                        struct stat st = {};
                                        if (fstat(candFd, &st) == 0) {
                                            if (descriptors.size() <= 16) {
                                                GFXSTREAM_WARNING(
                                                    "CIB-WRITEBACK: cand res=%u size=%lld "
                                                    "want=%zu",
                                                    desc.first, (long long)st.st_size,
                                                    bytes.size());
                                            }
                                        }
                                        if (fstat(candFd, &st) == 0 &&
                                            (uint64_t)st.st_size == bytes.size()) {
                                            void* candDst =
                                                mmap(nullptr, bytes.size(),
                                                     PROT_READ | PROT_WRITE, MAP_SHARED,
                                                     candFd, 0);
                                            if (candDst != MAP_FAILED) {
                                                memcpy(candDst, bytes.data(), bytes.size());
                                                munmap(candDst, bytes.size());
                                                matched++;
                                                GFXSTREAM_WARNING(
                                                    "CIB-WRITEBACK: resource %u -> guest res "
                                                    "%u bytes=%zu ok",
                                                    resource, desc.first, bytes.size());
                                            }
                                        }
                                        close(candFd);
                                    }
                                    GFXSTREAM_WARNING(
                                        "CIB-WRITEBACK: resource %u scanned %zu guest res, "
                                        "%zu matched",
                                        resource, descriptors.size(), matched);
                                } else {''')

print('CIB guest descriptor scan patched')

# 5) Register guest blob descriptors even without CREATE_GUEST_HANDLE. The VMM
#    hands the descriptor over for MEM_GUEST blobs; retaining it lets the
#    writeback reach the guest pages of the compositor's scanout buffer.
edit('host/virtio_gpu_resource.cpp',
     '''    } else if (features.ExternalBlob.enabled() ||
               (createBlobArgs->blob_mem == STREAM_BLOB_MEM_GUEST &&
                (createBlobArgs->blob_flags & STREAM_BLOB_FLAG_CREATE_GUEST_HANDLE))) {
        if (createBlobArgs->blob_mem == STREAM_BLOB_MEM_GUEST &&
            (createBlobArgs->blob_flags & STREAM_BLOB_FLAG_CREATE_GUEST_HANDLE)) {''',
     '''    } else if (features.ExternalBlob.enabled() ||
               (createBlobArgs->blob_mem == STREAM_BLOB_MEM_GUEST &&
                (createBlobArgs->blob_flags & STREAM_BLOB_FLAG_CREATE_GUEST_HANDLE))) {
        // GUESTBLOB-FD-REGISTER: keep any guest blob descriptor the VMM handed
        // over, even without the CREATE_GUEST_HANDLE flag, so the scanout
        // writeback can reach the guest pages.
        if (createBlobArgs->blob_mem == STREAM_BLOB_MEM_GUEST && handle &&
            (int)handle->os_handle >= 0) {
            ExternalObjectManager::get()->addGuestBlobResourceDescriptor(
                resourceId, (int)handle->os_handle);
            GFXSTREAM_DIAG_PRINT("GUESTBLOB-FD-REGISTER: resource=%u fd=%d size=%llu\\n",
                                 resourceId, (int)handle->os_handle,
                                 (unsigned long long)createBlobArgs->size);
        }
        if (createBlobArgs->blob_mem == STREAM_BLOB_MEM_GUEST &&
            (createBlobArgs->blob_flags & STREAM_BLOB_FLAG_CREATE_GUEST_HANDLE)) {''')

print('guest blob descriptor registration patched')

