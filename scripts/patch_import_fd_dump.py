#!/usr/bin/env python3
"""Dump imported image memory (dma-buf fd) to locate the actual frame memory.

The CIB1 ColorBuffers dump as zero and the screenshot never issues an
image->buffer copy, so the frame (if any) must live in the imported external
memory of the compositor's 1280x720 images. For an imported fd of exactly
1280*720*4 bytes and with GFXSTREAM_DUMP_IMPORT_FD set, dup the fd, mmap it and
write /data/local/tmp/import-<n>-<fd>.rgba. This is CPU-only and does not submit
GPU work; the file content tells whether Mali rendered into that memory.
"""
import argparse
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('root', type=Path)
root = parser.parse_args().root.resolve()

d = root / 'host/vulkan/vk_decoder_global_state.cpp'
text = d.read_text(encoding='utf-8')

OLD = '''        VkResult result = vk->vkAllocateMemory(device, &localAllocInfo, nullptr, pMemory);
        if (result != VK_SUCCESS) {
            return result;
        }
'''
NEW = '''        VkResult result = vk->vkAllocateMemory(device, &localAllocInfo, nullptr, pMemory);
        if (result != VK_SUCCESS) {
            return result;
        }

        if (getenv("GFXSTREAM_DUMP_IMPORT_FD") && importFdInfo.fd >= 0 &&
            localAllocInfo.allocationSize == 1280u * 720u * 4u) {
            // Frame-memory probe: map the imported dma-buf and dump its current
            // bytes. CPU-only; no GPU submission and no layout change.
            int dumpFd = dup(importFdInfo.fd);
            if (dumpFd >= 0) {
                void* dumpPtr = mmap(nullptr, localAllocInfo.allocationSize, PROT_READ,
                                     MAP_SHARED, dumpFd, 0);
                if (dumpPtr != MAP_FAILED) {
                    static std::atomic<unsigned> sImportDumps{0};
                    const unsigned n = sImportDumps.fetch_add(1);
                    if (n < 8) {
                        char path[128];
                        snprintf(path, sizeof(path), "/data/local/tmp/import-%u-%d.rgba", n,
                                 dumpFd);
                        int out = open(path, O_CREAT | O_TRUNC | O_WRONLY, 0644);
                        if (out >= 0) {
                            const ssize_t written =
                                write(out, dumpPtr, localAllocInfo.allocationSize);
                            (void)written;
                            close(out);
                        }
                    }
                    munmap(dumpPtr, localAllocInfo.allocationSize);
                }
                close(dumpFd);
            }
        }
'''
if text.count(OLD) != 1:
    raise SystemExit(f'expected one allocation-success anchor, found {text.count(OLD)}')
text = text.replace(OLD, NEW)

if '#include <sys/mman.h>' not in text:
    anchor = '#include <unistd.h>\n'
    if text.count(anchor) != 1:
        raise SystemExit(f'expected one unistd include, found {text.count(anchor)}')
    text = text.replace(anchor, anchor + '#include <sys/mman.h>\n', 1)

d.write_text(text, encoding='utf-8', newline='')
print('import fd dump diagnostic patched')
