#!/usr/bin/env python3
"""Stop the decoder from asking Mali to export a blob memory with no AHB backing.

Why
---
VkDecoderGlobalState::Impl::vkGetBlobInternal() calls exportMemoryHandle() for
every memory that reaches the external-blob branch. In AndroidAHB mode that
function ends in vkGetMemoryAndroidHardwareBufferANDROID(), which Mali
implements by loading the memory object's AHB field (+0x198) and handing it to
AHardwareBuffer_acquire() without a null test. The field is written only by an
AHB import or an AHB export allocation; a guest dma-buf blob, a host-pointer
/SystemBlob import, or a plain pool allocation leaves it clear, and the acquire
faults at 0x8 in RefBase::incStrong. This is the crash probe 1 still hit after
the allocExternalMemory() guard of patch_ahb_export_guard.py: that guard is in a
different caller and its skip line never appeared.

What
----
Before exportMemoryHandle() is called, the decoder now decides whether this
memory actually has an AHB backing. The decoder does not keep an
ExternalHandleInfo per memory (that type belongs to VkEmulation's
ExternalMemoryInfo); it records the same fact in two fields:

  * MemoryInfo::ahbBacking        -- the AHB-first allocation imported an AHB;
  * MemoryInfo::boundColorBuffer  -- a ColorBuffer's AHB was imported.

When the external memory mode is AndroidAHB and neither is set, the export call
is skipped, `exportedMemoryOpt` stays std::nullopt, one rate-limited line is
logged, and the existing `if (!exportedMemoryOpt)` fallback runs unchanged:

    AHB-EXPORT-SKIP blob no-ahb-backing size=%llu

A memory that imports an AHB through the guest's own pNext chain has no entry in
either field, so it is skipped too. That export produced a
STREAM_HANDLE_TYPE_PLATFORM_AHB descriptor, which this VMM cannot map (it has
no AHB branch), so no working path is lost; the fallback now runs instead.

Every mode other than AndroidAHB keeps calling exportMemoryHandle()
unconditionally. The mode comparison is the first operand of the condition, so
those paths evaluate one comparison and never read the two backing fields.

The helper header holds only the once-per-process log gate, with its own
counter: the allocExternalMemory() guard keeps logging the first no-backing
allocation, and this one logs the first no-backing blob.

The script is idempotent: a second run verifies the header, the include and the
guarded block instead of patching again. Every anchor must occur exactly once and
all checks run before the first write, so a rejected patch leaves the tree
untouched.
"""
import argparse
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("root", type=Path, help="Pinned gfxstream source checkout")
root = parser.parse_args().root.resolve()

decoder = "host/vulkan/vk_decoder_global_state.cpp"
structs = "host/vulkan/vk_decoder_internal_structs.h"
header = "host/vulkan/ahb_blob_export_guard.h"

decoder_path = root / decoder
structs_path = root / structs
header_path = root / header

if not decoder_path.is_file():
    raise SystemExit(f"{decoder}: file not found under {root}")
if not structs_path.is_file():
    raise SystemExit(f"{structs}: file not found under {root}")

HEADER = """\
// Rate limit for the AHB blob export guard in vkGetBlobInternal().
//
// In AndroidAHB mode the decoder only gives an AHardwareBuffer backing to two
// shapes: an AHB-first allocation (MemoryInfo::ahbBacking) and a ColorBuffer
// import (MemoryInfo::boundColorBuffer, imported from the ColorBuffer's AHB).
// Every other memory reaches the driver with its AHB field (+0x198) clear, and
// Mali's vkGetMemoryAndroidHardwareBufferANDROID() passes that field straight to
// AHardwareBuffer_acquire() without a null test. The caller therefore refuses to
// ask for the export of a memory without either backing and lets the existing
// fallback publish the mapping.
//
// This header holds only the once-per-process log gate; the decision stays at the
// call site so the AHB-backed path still calls the same export function.
#ifndef GFXSTREAM_HOST_VULKAN_AHB_BLOB_EXPORT_GUARD_H_
#define GFXSTREAM_HOST_VULKAN_AHB_BLOB_EXPORT_GUARD_H_

#include <atomic>

namespace gfxstream {
namespace ahb_blob_export_guard {

// One AHB-EXPORT-SKIP blob line per process: the first memory without AHB
// backing identifies the path and its size, and later exports stay silent.
// The function-local static in an inline function is shared by every caller in
// the process, and one relaxed fetch_add makes the gate single-winner under
// threads.
inline bool ShouldLogSkip() {
    static std::atomic<unsigned> sLogged{0};
    return sLogged.fetch_add(1, std::memory_order_relaxed) == 0;
}

}  // namespace ahb_blob_export_guard
}  // namespace gfxstream

#endif  // GFXSTREAM_HOST_VULKAN_AHB_BLOB_EXPORT_GUARD_H_
"""

# vk_common_operations.h is included once and is the last gfxstream host header
# before the other vk_* includes; the helper has no dependency beyond <atomic>.
INCLUDE_ANCHOR = '#include "vk_common_operations.h"\n'
INCLUDE_LINE = '#include "ahb_blob_export_guard.h"\n'
INCLUDE_BLOCK = INCLUDE_ANCHOR + INCLUDE_LINE

# The only call site in the decoder. patch_native_backing_lease.py and
# patch_ahb_first_hostvisible.py run before this script and do not move it.
OLD_CALL = """\
            auto exportedMemoryOpt = exportMemoryHandle(deviceInfo, vk, device, memory);
"""

NEW_BLOCK = """\
            // AHB-EXPORT-GUARD-2: ask the driver for an AHB only when this memory has
            // an AHB backing. In AndroidAHB mode the decoder records exactly two:
            // AHB-first allocations (ahbBacking) and ColorBuffer imports
            // (boundColorBuffer, imported from the ColorBuffer's AHB). Every other
            // memory -- a guest dma-buf blob, a host-pointer/SystemBlob import, a pool
            // allocation -- reaches the driver with its AHB field (+0x198) clear, and
            // Mali's vkGetMemoryAndroidHardwareBufferANDROID() hands that field to
            // AHardwareBuffer_acquire() without a null test. Skip the export for those;
            // the fallback below still publishes the mapping. The mode test comes first,
            // so every other mode costs one comparison and changes nothing.
            std::optional<BlobDescriptorType> exportedMemoryOpt = std::nullopt;
            if (m_vkEmulation->getExternalMemoryMode() != ExternalMemory::Mode::AndroidAHB ||
                info->ahbBacking != nullptr || info->boundColorBuffer.has_value()) {
                exportedMemoryOpt = exportMemoryHandle(deviceInfo, vk, device, memory);
            } else if (::gfxstream::ahb_blob_export_guard::ShouldLogSkip()) {
                GFXSTREAM_WARNING("AHB-EXPORT-SKIP blob no-ahb-backing size=%llu",
                                  static_cast<unsigned long long>(info->size));
            }
"""

# The fallback must stay byte-identical: this script only prevents the call, it
# never rewrites what happens when the export is unavailable.
FALLBACK_HEAD = """\
            if (!exportedMemoryOpt) {
"""
FALLBACK_COMMENT = """\
                // Memory that arrived here as an import -- a colour buffer's dma-buf backing a
"""

LOG_LINE = "AHB-EXPORT-SKIP blob no-ahb-backing size=%llu"
GATE = "::gfxstream::ahb_blob_export_guard::ShouldLogSkip()"
EXPORT_CALL = "exportMemoryHandle(deviceInfo, vk, device, memory)"
MODE_TEST = "m_vkEmulation->getExternalMemoryMode() != ExternalMemory::Mode::AndroidAHB"
AHB_FIELD = "void* ahbBacking = nullptr;"
COLOR_BUFFER_FIELD = "std::optional<HandleType> boundColorBuffer;"

before = decoder_path.read_text(encoding="utf-8")
structs_text = structs_path.read_text(encoding="utf-8")

for needle, label in (
    (AHB_FIELD, "MemoryInfo::ahbBacking (patch_ahb_first_hostvisible.py)"),
    (COLOR_BUFFER_FIELD, "MemoryInfo::boundColorBuffer"),
):
    if needle not in structs_text:
        raise SystemExit(f"{structs}: missing {label}; the guard needs that backing record")

existing_header = header_path.read_text(encoding="utf-8") if header_path.is_file() else None
if existing_header is not None and existing_header != HEADER:
    raise SystemExit(
        f"{header}: exists but does not match the AHB blob export guard helper; "
        "refusing to overwrite")

# Validate every anchor before writing anything.
text = before
if INCLUDE_BLOCK in text:
    if text.count(INCLUDE_BLOCK) != 1:
        raise SystemExit(f"{decoder}: guard include block appears {text.count(INCLUDE_BLOCK)} times")
elif INCLUDE_LINE in text:
    raise SystemExit(
        f"{decoder}: guard include exists without the vk_common_operations include; "
        "start from a clean pinned tree")
else:
    if text.count(INCLUDE_ANCHOR) != 1:
        raise SystemExit(
            f"{decoder}: expected one vk_common_operations include anchor, "
            f"found {text.count(INCLUDE_ANCHOR)}")

if NEW_BLOCK in text:
    if text.count(NEW_BLOCK) != 1:
        raise SystemExit(f"{decoder}: guarded blob export block appears {text.count(NEW_BLOCK)} times")
    if OLD_CALL in text:
        raise SystemExit(f"{decoder}: both the old blob export call and the guard are present")
else:
    if text.count(OLD_CALL) != 1:
        raise SystemExit(
            f"{decoder}: expected one blob export call, found {text.count(OLD_CALL)}")

# There must be exactly one export entry point and one fallback in the function;
# a second call site would keep an unguarded path alive.
if text.count(EXPORT_CALL) != 1:
    raise SystemExit(
        f"{decoder}: expected one {EXPORT_CALL} call site, found {text.count(EXPORT_CALL)}")
if text.count(FALLBACK_HEAD) != 1:
    raise SystemExit(
        f"{decoder}: expected one `if (!exportedMemoryOpt)` fallback, "
        f"found {text.count(FALLBACK_HEAD)}")
if text.count(FALLBACK_COMMENT) != 1:
    raise SystemExit(
        f"{decoder}: the documented blob fallback changed; the guard must not rewrite it")

# Checks passed: write the helper first, then the include and the guarded block.
if existing_header is None:
    header_path.write_text(HEADER, encoding="utf-8", newline="\n")
    print("created", header)
else:
    print("already created", header)

if INCLUDE_BLOCK not in text:
    text = text.replace(INCLUDE_ANCHOR, INCLUDE_BLOCK, 1)
    print("patched", decoder, "(blob guard include)")
else:
    print("already patched", decoder, "(blob guard include)")

if NEW_BLOCK not in text:
    text = text.replace(OLD_CALL, NEW_BLOCK, 1)
    print("patched", decoder, "(AHB-EXPORT-GUARD-2)")
else:
    print("already patched", decoder, "(AHB-EXPORT-GUARD-2)")

if text != before:
    decoder_path.write_text(text, encoding="utf-8", newline="\n")

after = decoder_path.read_text(encoding="utf-8")
if after.count(EXPORT_CALL) != 1:
    raise SystemExit(
        f"{decoder}: expected exactly one guarded export call after patching, "
        f"found {after.count(EXPORT_CALL)}")
if after.count(LOG_LINE) != 1 or after.count(GATE) != 1:
    raise SystemExit(f"{decoder}: guarded blob export block is incomplete")
if after.index(MODE_TEST) > after.index("info->ahbBacking != nullptr"):
    raise SystemExit(f"{decoder}: the mode test must short-circuit before reading the backing fields")
if after.index(FALLBACK_HEAD) != after.index(NEW_BLOCK) + len(NEW_BLOCK):
    raise SystemExit(f"{decoder}: the existing fallback no longer follows the guard")

print("AHB blob export guard installed (AndroidAHB without AHB backing only)")
