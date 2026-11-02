#!/usr/bin/env python3
"""Accept guest-handle blob creates without turning the ExternalBlob feature on.

The guest-handle blob is the guest-alloc path: the guest hands the host a descriptor for pages
it owns, and the allocation imports that descriptor as a dma-buf. On this Mali driver the raw
dma-buf import works, while the feature's other consequences do not: with ExternalBlob enabled
every host-allocated host-visible allocation switches to the AHB export path, which Mali
rejects (VK_ERROR_INVALID_EXTERNAL_HANDLE). Keep the feature off for host allocations and admit
only the guest-handle create, which is the one shape this route needs.
"""
import argparse
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('root', type=Path)
root = parser.parse_args().root.resolve()

path = root / 'host/virtio_gpu_resource.cpp'
text = path.read_text(encoding='utf-8')

OLD = '''    } else if (features.ExternalBlob.enabled()) {
        if (createBlobArgs->blob_mem == STREAM_BLOB_MEM_GUEST &&
            (createBlobArgs->blob_flags & STREAM_BLOB_FLAG_CREATE_GUEST_HANDLE)) {
'''
NEW = '''    } else if (features.ExternalBlob.enabled() ||
               (createBlobArgs->blob_mem == STREAM_BLOB_MEM_GUEST &&
                (createBlobArgs->blob_flags & STREAM_BLOB_FLAG_CREATE_GUEST_HANDLE))) {
        if (createBlobArgs->blob_mem == STREAM_BLOB_MEM_GUEST &&
            (createBlobArgs->blob_flags & STREAM_BLOB_FLAG_CREATE_GUEST_HANDLE)) {
'''
if text.count(OLD) != 1:
    raise SystemExit(f'expected one blob-create anchor, found {text.count(OLD)}')
text = text.replace(OLD, NEW)
path.write_text(text, encoding='utf-8', newline='')
print('guest-handle blob creates admitted with ExternalBlob off')
