#!/usr/bin/env python3
"""Dump bound CIB1 ColorBuffers to /data/local/tmp for pixel verification.

When a CIB1 image is allocated, start a lazily-created diagnostic thread (only
when GFXSTREAM_CIB_DUMP_MS is set) that every N milliseconds reads the most
recently bound classic ColorBuffer through VkEmulation::readColorBufferToBytes
and writes the raw pixels to /data/local/tmp/cib-<resource>-<n>.rgba. This is
the host-side ground truth for "did Mali actually render the compositor's
frame": if these files contain the bar/wallpaper, the remaining problem is the
guest readback path; if they are black, the composition itself never reached the
ColorBuffer.
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
            // Host-side ground truth: dump bound classic ColorBuffers so the
            // composition can be checked without the guest readback path.
            static std::once_flag cibDumpOnce;
            std::call_once(cibDumpOnce, [this]() {
                const char* intervalEnv = getenv("GFXSTREAM_CIB_DUMP_MS");
                if (!intervalEnv) return;
                const long intervalMs = strtol(intervalEnv, nullptr, 0);
                if (intervalMs <= 0) return;
                std::thread([this, intervalMs]() {
                    std::map<uint32_t, int> dumps;
                    while (true) {
                        std::this_thread::sleep_for(std::chrono::milliseconds(intervalMs));
                        uint32_t resource = 0;
                        {
                            std::lock_guard<std::mutex> lock(mMutex);
                            for (auto& entry : mImageInfo) {
                                const auto& img = entry.second;
                                if (img.classicImageResource && img.classicImageBound) {
                                    resource = img.classicImageResource;
                                }
                            }
                        }
                        if (!resource) continue;
                        if (dumps[resource] >= 3) continue;
                        std::vector<uint8_t> bytes;
                        if (!m_vkEmulation->readColorBufferToBytes(resource, &bytes)) {
                            continue;
                        }
                        dumps[resource]++;
                        char path[128];
                        snprintf(path, sizeof(path), "/data/local/tmp/cib-%u-%d.rgba",
                                 resource, dumps[resource]);
                        int fd = open(path, O_CREAT | O_TRUNC | O_WRONLY, 0644);
                        if (fd >= 0) {
                            ssize_t written = write(fd, bytes.data(), bytes.size());
                            (void)written;
                            close(fd);
                        }
                    }
                }).detach();
            });
        }
        return VK_SUCCESS;'''
if text.count(OLD) != 1:
    raise SystemExit(f'expected one CIB allocation anchor, found {text.count(OLD)}')
text = text.replace(OLD, NEW)
d.write_text(text, encoding='utf-8', newline='')
print('CIB ColorBuffer dump thread patched')
