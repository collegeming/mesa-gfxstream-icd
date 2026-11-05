#!/usr/bin/env python3
"""Static and patch tests for the AHB blob export guard.

Checks, in this order:

1. Production invariants (the real tree must already be patched):
   * the decoder calls exportMemoryHandle() exactly once, inside the guarded
     AndroidAHB-with-AHB-backing branch of vkGetBlobInternal();
   * the guard is `mode != AndroidAHB || ahbBacking || boundColorBuffer`, in that
     operand order, so every other mode short-circuits after one comparison;
   * the refused path leaves `exportedMemoryOpt` nullopt, is gated by
     ahb_blob_export_guard::ShouldLogSkip(), and logs one
     `AHB-EXPORT-SKIP blob no-ahb-backing size=%llu` line;
   * the existing `if (!exportedMemoryOpt)` fallback still follows the guard byte
     for byte;
   * the helper header is included exactly once and holds a once-per-process
     single-winner gate with no Vulkan dependency;
   * v1's allocExternalMemory() guard is still in vk_common_operations.cpp
     (defense in depth: the decoder guard must not replace it).
2. The patch script on minimal fixtures:
   * a fresh run inserts exactly the include line and the guarded block, and
     nothing else: reversing those two edits reproduces the input byte for byte,
     so the fallback and every other line cannot have changed;
   * a second run is a no-op;
   * a missing include anchor, a missing/duplicated export call, a missing
     fallback, a missing MemoryInfo backing field, and a divergent helper header
     are rejected without writing.
3. Workflow wiring: branch/paths filters, helper intent-to-add, patch -> Python
   test -> C++ test order, and the artifact copies.

Usage:
    python scripts/test_ahb_blob_export_guard.py <gfxstream-root> [workflow]
"""
import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PATCH = HERE / "patch_ahb_blob_export_guard.py"

DECODER_REL = "host/vulkan/vk_decoder_global_state.cpp"
STRUCTS_REL = "host/vulkan/vk_decoder_internal_structs.h"
HEADER_REL = "host/vulkan/ahb_blob_export_guard.h"
OPS_REL = "host/vulkan/vk_common_operations.cpp"

LOG_LINE = "AHB-EXPORT-SKIP blob no-ahb-backing size=%llu"
GATE = "::gfxstream::ahb_blob_export_guard::ShouldLogSkip()"
EXPORT_CALL = "exportMemoryHandle(deviceInfo, vk, device, memory)"
MODE_TEST = "m_vkEmulation->getExternalMemoryMode() != ExternalMemory::Mode::AndroidAHB"
AHB_TEST = "info->ahbBacking != nullptr"
CB_TEST = "info->boundColorBuffer.has_value()"
NULLOPT = "std::optional<BlobDescriptorType> exportedMemoryOpt = std::nullopt;"

V1_LOG_LINE = "AHB-EXPORT-SKIP no-ahb-backing size=%llu"
V1_GATE = "::gfxstream::ahb_export_guard::ShouldLogSkip()"
V1_GETTER_CALL = "vkGetMemoryAndroidHardwareBufferANDROID(mDevice"

INCLUDE_ANCHOR = '#include "vk_common_operations.h"\n'
INCLUDE_LINE = '#include "ahb_blob_export_guard.h"\n'
INCLUDE_BLOCK = INCLUDE_ANCHOR + INCLUDE_LINE

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

FALLBACK_HEAD = """\
            if (!exportedMemoryOpt) {
"""
FALLBACK_COMMENT = """\
                // Memory that arrived here as an import -- a colour buffer's dma-buf backing a
"""

FIXTURE_DECODER = """\
#include "vk_common_operations.h"

namespace gfxstream {
namespace host {
namespace vk {

    int vkGetBlobInternal(MemoryInfo* info) {
            auto exportedMemoryOpt = exportMemoryHandle(deviceInfo, vk, device, memory);
            if (!exportedMemoryOpt) {
                // Memory that arrived here as an import -- a colour buffer's dma-buf backing a
                // dedicated image, say.
                return -1;
            }
        return 0;
    }

}  // namespace vk
}  // namespace host
}  // namespace gfxstream
"""

FIXTURE_STRUCTS = """\
struct MemoryInfo {
    std::optional<HandleType> boundColorBuffer;
    void* ahbBacking = nullptr;
};
"""


def run_patch(root):
    return subprocess.run(
        [sys.executable, "-I", str(PATCH), str(root)],
        capture_output=True,
        text=True,
    )


def write_tree(base, decoder_text, structs_text=FIXTURE_STRUCTS, header_text=None):
    (base / "host" / "vulkan").mkdir(parents=True)
    (base / DECODER_REL).write_text(decoder_text, encoding="utf-8", newline="\n")
    (base / STRUCTS_REL).write_text(structs_text, encoding="utf-8", newline="\n")
    if header_text is not None:
        (base / HEADER_REL).write_text(header_text, encoding="utf-8", newline="\n")


def check(condition, what):
    if not condition:
        raise SystemExit(f"FAIL: {what}")


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("root", type=Path, help="gfxstream source root")
parser.add_argument("workflow", type=Path, nargs="?", help="CI workflow file")
args = parser.parse_args()
root = args.root.resolve()

# ---------------------------------------------------------------------------
# 1. Production invariants.
# ---------------------------------------------------------------------------
decoder_path = root / DECODER_REL
structs_path = root / STRUCTS_REL
header_path = root / HEADER_REL
ops_path = root / OPS_REL
check(decoder_path.is_file(), f"{DECODER_REL} missing")
check(structs_path.is_file(), f"{STRUCTS_REL} missing")
check(header_path.is_file(), f"{HEADER_REL} missing")
check(ops_path.is_file(), f"{OPS_REL} missing")

decoder = decoder_path.read_text(encoding="utf-8")
structs = structs_path.read_text(encoding="utf-8")
header = header_path.read_text(encoding="utf-8")
ops = ops_path.read_text(encoding="utf-8")

check(decoder.count(INCLUDE_BLOCK) == 1,
      "blob guard include appears exactly once, next to the common operations include")
check(decoder.count(NEW_BLOCK) == 1, "guarded blob export block appears exactly once")
check(OLD_CALL not in decoder, "the unguarded blob export call is gone")
check(decoder.count(EXPORT_CALL) == 1, "one blob export call remains")
check(decoder.count(LOG_LINE) == 1, "one AHB-EXPORT-SKIP blob format string")
check(decoder.count(GATE) == 1, "the blob skip is gated by the helper")
check(decoder.count(NULLOPT) == 1, "the refused path records nullopt explicitly")

# The single export call sits inside the function and inside the guarded branch.
function_at = decoder.index("VkResult vkGetBlobInternal(")
definition_at = decoder.index("std::optional<BlobDescriptorType> exportMemoryHandle(")
block_at = decoder.index(NEW_BLOCK)
call_at = decoder.index(EXPORT_CALL)
check(function_at < block_at < call_at < definition_at,
      "the guard and the export call sit inside vkGetBlobInternal()")

# The guard tests the mode first and only then the two backing fields, so every
# other external memory mode pays one comparison and reads neither field.
mode_at = block_at + decoder[block_at:].index(MODE_TEST)
ahb_at = block_at + decoder[block_at:].index(AHB_TEST)
cb_at = block_at + decoder[block_at:].index(CB_TEST)
check(block_at < mode_at < ahb_at < cb_at < call_at,
      "the mode comparison short-circuits before the AHB backing fields")

# The existing fallback is untouched and still runs for a refused export.
check(decoder.count(FALLBACK_HEAD) == 1, "the single existing fallback is still present")
check(decoder.count(FALLBACK_COMMENT) == 1, "the documented blob fallback is unchanged")
check(decoder.index(FALLBACK_HEAD) == block_at + len(NEW_BLOCK),
      "the existing fallback immediately follows the guard")

# The decoder records AHB backing in these two fields.
check("void* ahbBacking = nullptr;" in structs, "MemoryInfo::ahbBacking field exists")
check("std::optional<HandleType> boundColorBuffer;" in structs,
      "MemoryInfo::boundColorBuffer field exists")

# Helper header: once-per-process gate, no Vulkan types.
check("#ifndef GFXSTREAM_HOST_VULKAN_AHB_BLOB_EXPORT_GUARD_H_" in header, "header guard")
check(header.count("inline bool ShouldLogSkip()") == 1, "single blob gate function")
check("static std::atomic<unsigned> sLogged{0};" in header, "atomic gate state")
check("sLogged.fetch_add(1, std::memory_order_relaxed) == 0" in header,
      "single winner via one relaxed fetch_add")
check("namespace gfxstream {" in header and "namespace ahb_blob_export_guard {" in header,
      "helper namespace matches the call site")
check("#include <vulkan" not in header and "VkResult" not in header,
      "helper has no Vulkan dependency")

# Defensive depth: v1's allocExternalMemory() guard must stay installed.
check(ops.count(V1_LOG_LINE) == 1, "v1 no-backing alloc skip line is still present")
check(ops.count(V1_GATE) == 1, "v1 no-backing alloc gate is still present")
check("exportRes = VK_ERROR_INVALID_EXTERNAL_HANDLE;" in ops,
      "v1 still reports VK_ERROR_INVALID_EXTERNAL_HANDLE for a no-backing alloc export")
check(V1_GETTER_CALL not in ops, "v1 still removes the allocExternalMemory getter call")

# ---------------------------------------------------------------------------
# 2. Patch script behaviour on minimal fixtures.
# ---------------------------------------------------------------------------
original = FIXTURE_DECODER

with tempfile.TemporaryDirectory() as tmp:
    base = Path(tmp)
    write_tree(base, original)
    result = run_patch(base)
    check(result.returncode == 0, f"fresh patch failed: {result.stderr}")
    patched = (base / DECODER_REL).read_text(encoding="utf-8")
    expected = original.replace(INCLUDE_ANCHOR, INCLUDE_BLOCK).replace(OLD_CALL, NEW_BLOCK)
    check(patched == expected, "fresh patch inserts only the include and the guarded block")
    check((base / HEADER_REL).is_file(), "fresh patch creates the helper header")
    reverted = patched.replace(INCLUDE_BLOCK, INCLUDE_ANCHOR).replace(NEW_BLOCK, OLD_CALL)
    check(reverted == original, "reversing the two edits reproduces the fixture byte for byte")

    again = run_patch(base)
    check(again.returncode == 0, f"second patch failed: {again.stderr}")
    check((base / DECODER_REL).read_text(encoding="utf-8") == patched, "second run is a no-op")
    check("already patched" in again.stdout, "second run reports already patched")

with tempfile.TemporaryDirectory() as tmp:
    base = Path(tmp)
    write_tree(base, original.replace(OLD_CALL, ""))
    result = run_patch(base)
    check(result.returncode != 0, "a fixture without the export call is rejected")
    check(not (base / HEADER_REL).exists(), "rejected patch writes no header")

with tempfile.TemporaryDirectory() as tmp:
    base = Path(tmp)
    write_tree(base, original.replace(OLD_CALL, OLD_CALL + OLD_CALL, 1))
    result = run_patch(base)
    check(result.returncode != 0, "a duplicated export call is rejected")

with tempfile.TemporaryDirectory() as tmp:
    base = Path(tmp)
    write_tree(base, original.replace(FALLBACK_HEAD, "", 1))
    result = run_patch(base)
    check(result.returncode != 0, "a missing fallback is rejected")

with tempfile.TemporaryDirectory() as tmp:
    base = Path(tmp)
    write_tree(base, original.replace(INCLUDE_ANCHOR, "", 1))
    result = run_patch(base)
    check(result.returncode != 0, "a missing common-operations include anchor is rejected")

with tempfile.TemporaryDirectory() as tmp:
    base = Path(tmp)
    write_tree(base, original, header_text="// different helper\n")
    result = run_patch(base)
    check(result.returncode != 0, "a divergent existing helper header is rejected")
    check((base / HEADER_REL).read_text(encoding="utf-8") == "// different helper\n",
          "rejected patch leaves the existing header untouched")

with tempfile.TemporaryDirectory() as tmp:
    base = Path(tmp)
    write_tree(base, original, structs_text="struct MemoryInfo {\n    bool needUnmap = false;\n};\n")
    result = run_patch(base)
    check(result.returncode != 0, "a missing ahbBacking field is rejected")
    check(not (base / HEADER_REL).exists(), "rejected patch writes no header for a missing field")

# ---------------------------------------------------------------------------
# 3. Workflow wiring.
# ---------------------------------------------------------------------------
if args.workflow is not None:
    workflow = args.workflow.read_text(encoding="utf-8")
    check("diagnostic/ahb-export-guard" in workflow, "branch triggers the workflow")
    check("scripts/*ahb_blob_export_guard*" in workflow, "paths filter covers the blob guard files")
    check("scripts/patch_ahb_blob_export_guard.py source" in workflow,
          "workflow applies the blob guard patch")
    check("git -C source add -N host/vulkan/ahb_blob_export_guard.h" in workflow,
          "workflow marks the new header intent-to-add")
    check("scripts/test_ahb_blob_export_guard.py" in workflow,
          "workflow runs the Python test")
    check("test_ahb_blob_export_guard.cpp" in workflow, "workflow compiles the C++ test")
    check("cp source/host/vulkan/ahb_blob_export_guard.h out/" in workflow,
          "workflow copies the helper into the artifact")
    check("cp ci/scripts/*ahb_blob_export_guard* out/" in workflow,
          "workflow copies the blob guard scripts into the artifact")
    check(workflow.index("patch_ahb_export_guard.py") < workflow.index(
        "patch_ahb_blob_export_guard.py"), "v1 guard is applied before the blob guard")
    check(workflow.index("patch_ahb_blob_export_guard.py") < workflow.index(
        "test_ahb_blob_export_guard.py"), "blob patch runs before the Python test")
    check(workflow.index("test_ahb_blob_export_guard.py") < workflow.index(
        "test_ahb_blob_export_guard.cpp"), "Python test runs before the C++ test")
    check(workflow.index("test_ahb_blob_export_guard.cpp") < workflow.index(
        "test-ahb-blob-export-guard"), "C++ test is compiled before it runs")

print("PASS: vkGetBlobInternal() only exports memories with an AHB backing in AndroidAHB mode, "
      "a refused export stays nullopt with one AHB-EXPORT-SKIP blob line and the existing "
      "fallback, other modes keep one comparison and the same call, and v1's alloc guard is "
      "still installed")
