#!/usr/bin/env python3
"""Dump bound CIB1 ColorBuffers on request for pixel verification.

When the first CIB1 image is allocated, start a lazily-created diagnostic thread
(only when GFXSTREAM_CIB_DUMP_REQUEST names a path). The thread waits, without
touching the GPU, until that request file appears, then performs exactly one
pass over the bound classic ColorBuffers: VkEmulation::readColorBufferToBytes
and write /data/local/tmp/cib-<resource>.rgba. One manual pass at a known time
is the low-interference way to answer "did Mali render the compositor's frame":
periodic readbacks submit on the same physical queue as the compositor and can
stall the guest (observed), while a single pass after the desktop is up does not.
"""
import argparse
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('root', type=Path)
root = parser.parse_args().root.resolve()

d = root / 'host/vulkan/vk_decoder_global_state.cpp'
text = d.read_text(encoding='utf-8')

for inc, anchor in [('#include <thread>', '#include <mutex>\n'),
                    ('#include <chrono>', '#include <thread>\n'),
                    ('#include <map>', '#include <chrono>\n'),
                    ('#include <sys/mman.h>', '#include <unistd.h>\n'),
                    ('#include <cerrno>', '#include <sys/mman.h>\n')]:
    if inc not in text:
        if text.count(anchor) != 1:
            raise SystemExit(f'missing include anchor {anchor!r}')
        text = text.replace(anchor, anchor + inc + '\n', 1)

OLD = '''        mMemoryInfo[memory] = std::move(info);
        *output = new_boxed_non_dispatchable_VkDeviceMemory(memory);
        return VK_SUCCESS;'''
NEW = '''        mMemoryInfo[memory] = std::move(info);
        *output = new_boxed_non_dispatchable_VkDeviceMemory(memory);
        {
            // On-demand ground truth: wait for a request file, then read the bound
            // classic ColorBuffers once. No GPU work happens until the request.
            static std::once_flag cibDumpOnce;
            std::call_once(cibDumpOnce, [this]() {
                const char* requestEnv = getenv("GFXSTREAM_CIB_DUMP_REQUEST");
                if (!requestEnv || !*requestEnv) return;
                const std::string requestPath(requestEnv);
                std::thread([this, requestPath]() {
                  for (int round = 0; round < 20; round++) {
                    bool requested = false;
                    for (int i = 0; i < 600; i++) {
                        if (access(requestPath.c_str(), F_OK) == 0) {
                            requested = true;
                            break;
                        }
                        std::this_thread::sleep_for(std::chrono::milliseconds(500));
                    }
                    if (!requested) return;
                    GFXSTREAM_WARNING("CIB-DUMP: round %d requested", round);
                    std::vector<uint32_t> resources;
                    std::map<uint32_t, uint32_t> resourceWidths;
                    {
                        std::lock_guard<std::mutex> lock(mMutex);
                        for (auto& entry : mImageInfo) {
                            const auto& img = entry.second;
                            // Dump every CIB1 image, not only the ones the host
                            // marked bound: the compositor's output may be an alias
                            // whose bind flag is not recorded here.
                            if (img.classicImageResource) {
                                resources.push_back(img.classicImageResource);
                                resourceWidths[img.classicImageResource] =
                                    img.imageCreateInfoShallow.extent.width;
                            }
                        }
                    }
                    std::sort(resources.begin(), resources.end());
                    resources.erase(std::unique(resources.begin(), resources.end()),
                                    resources.end());
                    for (uint32_t resource : resources) {
                        std::vector<uint8_t> bytes;
                        if (!m_vkEmulation->readColorBufferToBytes(resource, &bytes)) {
                            GFXSTREAM_WARNING("CIB-DUMP: resource %u readback failed", resource);
                            continue;
                        }
                        GFXSTREAM_WARNING("CIB-DUMP: resource %u readback %zu bytes", resource,
                                          bytes.size());
                        char path[128];
                        snprintf(path, sizeof(path), "/data/local/tmp/cib-%u.rgba", resource);
                        int fd = open(path, O_CREAT | O_TRUNC | O_WRONLY, 0644);
                        if (fd >= 0) {
                            ssize_t written = write(fd, bytes.data(), bytes.size());
                            (void)written;
                            close(fd);
                        }
                        // On-request ColorBuffer -> guest scanout writeback. The guest's
                        // KMS/GBM buffer is not the host ColorBuffer; copy the rendered
                        // frame into the guest blob's own dma-buf pages so fb0, crosvm
                        // and the compositor's screenshot read the same pixels.
                        if (getenv("GFXSTREAM_CIB_WRITEBACK")) {
                            const uint32_t width = resourceWidths[resource];
                            // The readback rows are width*bpp (bpp=4 here); the guest
                            // stride is AlignUp(width*bpp, 16). Only widths that make
                            // those equal can be copied as one contiguous block.
                            if (width == 0 || (width & 3) != 0) {
                                GFXSTREAM_WARNING(
                                    "CIB-WRITEBACK: resource %u skip width=%u", resource, width);
                            } else {
                                int wbFd = ExternalObjectManager::get()
                                               ->dupGuestBlobResourceDescriptor(resource);
                                if (wbFd < 0) {
                                    GFXSTREAM_WARNING(
                                        "CIB-WRITEBACK: resource %u no guest fd", resource);
                                } else {
                                    void* wbDst = mmap(nullptr, bytes.size(),
                                                       PROT_READ | PROT_WRITE, MAP_SHARED,
                                                       wbFd, 0);
                                    if (wbDst == MAP_FAILED) {
                                        GFXSTREAM_WARNING(
                                            "CIB-WRITEBACK: resource %u mmap failed errno=%d",
                                            resource, errno);
                                    } else {
                                        memcpy(wbDst, bytes.data(), bytes.size());
                                        munmap(wbDst, bytes.size());
                                        GFXSTREAM_WARNING(
                                            "CIB-WRITEBACK: resource %u width=%u bytes=%zu ok",
                                            resource, width, bytes.size());
                                    }
                                    close(wbFd);
                                }
                            }
                        }
                    }
                    GFXSTREAM_WARNING("CIB-DUMP: round %d dumped %zu ColorBuffer(s)",
                                      round, resources.size());
                    const char* donePath = "/data/local/tmp/cib-dump-done";
                    rename(requestPath.c_str(), donePath);
                    unlink(donePath);
                    std::this_thread::sleep_for(std::chrono::milliseconds(1000));
                  }
                }).detach();
            });
        }
        return VK_SUCCESS;'''
if text.count(OLD) != 1:
    raise SystemExit(f'expected one CIB allocation anchor, found {text.count(OLD)}')
text = text.replace(OLD, NEW)
d.write_text(text, encoding='utf-8', newline='')
print('CIB ColorBuffer on-request dump patched')
