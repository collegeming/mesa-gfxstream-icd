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
                    ('#include <map>', '#include <chrono>\n')]:
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
                    bool requested = false;
                    for (int i = 0; i < 600; i++) {
                        if (access(requestPath.c_str(), F_OK) == 0) {
                            requested = true;
                            break;
                        }
                        std::this_thread::sleep_for(std::chrono::milliseconds(500));
                    }
                    if (!requested) return;
                    std::vector<uint32_t> resources;
                    {
                        std::lock_guard<std::mutex> lock(mMutex);
                        for (auto& entry : mImageInfo) {
                            const auto& img = entry.second;
                            if (img.classicImageResource && img.classicImageBound) {
                                resources.push_back(img.classicImageResource);
                            }
                        }
                    }
                    std::sort(resources.begin(), resources.end());
                    resources.erase(std::unique(resources.begin(), resources.end()),
                                    resources.end());
                    for (uint32_t resource : resources) {
                        std::vector<uint8_t> bytes;
                        if (!m_vkEmulation->readColorBufferToBytes(resource, &bytes)) {
                            continue;
                        }
                        char path[128];
                        snprintf(path, sizeof(path), "/data/local/tmp/cib-%u.rgba", resource);
                        int fd = open(path, O_CREAT | O_TRUNC | O_WRONLY, 0644);
                        if (fd >= 0) {
                            ssize_t written = write(fd, bytes.data(), bytes.size());
                            (void)written;
                            close(fd);
                        }
                    }
                    rename(requestPath.c_str(), "/data/local/tmp/cib-dump-done");
                }).detach();
            });
        }
        return VK_SUCCESS;'''
if text.count(OLD) != 1:
    raise SystemExit(f'expected one CIB allocation anchor, found {text.count(OLD)}')
text = text.replace(OLD, NEW)
d.write_text(text, encoding='utf-8', newline='')
print('CIB ColorBuffer on-request dump patched')
