#!/usr/bin/env python3
"""Lower unregistered VkImage unbox handles from fatal to a one-shot error.

Guests can reference a VkImage before its create call is decoded. The
upstream default branch aborts the whole gfxstream daemon in that case.
Keep the fatal policy for every other handle type.
"""
import sys
from pathlib import Path

if len(sys.argv) != 2:
    sys.exit("usage: patch_vkimage_unbox_nofatal.py <gfxstream-source>")

path = Path(sys.argv[1]) / "host/vulkan/vulkan_boxed_handles.cpp"
with path.open('r', newline='') as handle:
    text = handle.read()

old = '''            } else if constexpr (std::is_same_v<VkObjectT, VkFence>) {
                // TODO: investigate.
            } else {
                GFXSTREAM_FATAL("Failed to unbox %s %p", GetTypeStr<VkObjectT>(), boxed);
            }'''
new = '''            } else if constexpr (std::is_same_v<VkObjectT, VkFence>) {
                // TODO: investigate.
            } else if constexpr (std::is_same_v<VkObjectT, VkImage>) {
                // A guest may reference an image before its create call has
                // been decoded, so an unregistered boxed handle is a
                // recoverable protocol error for this type. Log once with the
                // type/handle and return VK_NULL_HANDLE instead of aborting
                // the whole gfxstream daemon.
                static std::atomic<bool> loggedMissingImage{false};
                if (!loggedMissingImage.exchange(true)) {
                    GFXSTREAM_ERROR(
                        "Failed to unbox %s %p (unregistered guest handle, returning "
                        "VK_NULL_HANDLE)",
                        GetTypeStr<VkObjectT>(), boxed);
                }
            } else {
                GFXSTREAM_FATAL("Failed to unbox %s %p", GetTypeStr<VkObjectT>(), boxed);
            }'''

found = text.count(old)
if found != 1:
    sys.exit(f"expected exactly one unbox fatal block, found {found}")
text = text.replace(old, new)

anchor = '#include "vulkan_boxed_handles.h"\n'
if '#include <atomic>' not in text:
    if text.count(anchor) != 1:
        sys.exit("cannot locate the boxed-handles include anchor")
    text = text.replace(anchor, anchor + '\n#include <atomic>\n')

with path.open('w', newline='') as handle:
    handle.write(text)

with path.open('r', newline='') as handle:
    updated = handle.read()
if 'unregistered guest handle, returning' not in updated:
    sys.exit("verification failed: new diagnostic string is missing")
if updated.count('GFXSTREAM_FATAL("Failed to unbox %s %p"') != 1:
    sys.exit("verification failed: unexpected fatal policy count")
print("patched", path)
