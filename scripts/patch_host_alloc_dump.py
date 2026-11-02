#!/usr/bin/env python3
"""Dump host-alloc (SystemBlob) memory on request to locate the frame.

The compositor's images allocate host-visible memory through the SystemBlob
path (host memfd mapped as hostptr=..., fd=-1), so the frame (if rendered) lives
in those mappings. Register every host allocation and, on a request file, dump
the registered regions with plain CPU reads (no GPU submission, no layout
change). This tells whether the frame is in host memory and whether it matches
what the guest reads.
"""
import argparse
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('root', type=Path)
root = parser.parse_args().root.resolve()

d = root / 'host/vulkan/vk_decoder_global_state.cpp'
text = d.read_text(encoding='utf-8')

helper = '''
namespace {
struct HostAllocDumpRegistry {
    std::mutex mutex;
    std::vector<std::pair<void*, size_t>> regions;
    std::once_flag once;
};
HostAllocDumpRegistry& hostAllocDumpRegistry() {
    static HostAllocDumpRegistry registry;
    return registry;
}
void hostAllocDumpRegister(void* ptr, size_t size) {
    if (!ptr || !getenv("GFXSTREAM_DUMP_HOST_ALLOC")) return;
    auto& registry = hostAllocDumpRegistry();
    std::call_once(registry.once, []() {
        std::thread([]() {
            const char* requestPath = "/data/local/tmp/host-dump-request";
            bool requested = false;
            for (int i = 0; i < 600; i++) {
                if (access(requestPath, F_OK) == 0) {
                    requested = true;
                    break;
                }
                std::this_thread::sleep_for(std::chrono::milliseconds(500));
            }
            if (!requested) return;
            auto& r = hostAllocDumpRegistry();
            std::vector<std::pair<void*, size_t>> copy;
            {
                std::lock_guard<std::mutex> lock(r.mutex);
                copy = r.regions;
            }
            int n = 0;
            for (auto& region : copy) {
                if (n >= 6 || region.second < 1024 * 1024 || region.second > 32 * 1024 * 1024) {
                    continue;
                }
                char path[128];
                snprintf(path, sizeof(path), "/data/local/tmp/host-%d-%zu.rgba", n++,
                         region.second);
                int fd = open(path, O_CREAT | O_TRUNC | O_WRONLY, 0644);
                if (fd < 0) continue;
                size_t off = 0;
                while (off < region.second) {
                    const ssize_t written =
                        write(fd, static_cast<char*>(region.first) + off, region.second - off);
                    if (written <= 0) break;
                    off += static_cast<size_t>(written);
                }
                close(fd);
            }
            rename(requestPath, "/data/local/tmp/host-dump-done");
        }).detach();
    });
    std::lock_guard<std::mutex> lock(registry.mutex);
    if (registry.regions.size() < 64) registry.regions.emplace_back(ptr, size);
}
}  // namespace
'''
anchor = 'VkResult VkDecoderGlobalState::on_vkCreateImage('
if anchor not in text:
    raise SystemExit('missing on_vkCreateImage anchor')
# Insert the helper before the first anonymous namespace end is impractical; put it
# right before VkDecoderGlobalState::on_vkCreateImage definition.
if '#include <utility>' not in text:
    text = text.replace('#include <vector>', '#include <utility>
#include <vector>', 1)
idx = text.index(anchor)
text = text[:idx] + helper + text[idx:]

OLD = '''        VkResult result = vk->vkAllocateMemory(device, &localAllocInfo, nullptr, pMemory);
        if (result != VK_SUCCESS) {
            return result;
        }
'''
NEW = '''        VkResult result = vk->vkAllocateMemory(device, &localAllocInfo, nullptr, pMemory);
        if (result != VK_SUCCESS) {
            return result;
        }

        if (mappedPtr) {
            hostAllocDumpRegister(mappedPtr, localAllocInfo.allocationSize);
        }
'''
if text.count(OLD) != 1:
    raise SystemExit(f'expected one allocation-success anchor, found {text.count(OLD)}')
text = text.replace(OLD, NEW)

d.write_text(text, encoding='utf-8', newline='')
print('host allocation dump diagnostic patched')
