#!/usr/bin/env python3
"""Static and patch tests for the DRM-modifier vkCreateImage fallback.

Checks, in this order:

1. Production invariants (the real tree must already be patched):
   * on_vkCreateImage() still makes the original driver call once, and the fallback appears
     immediately before the existing `if (createRes != VK_SUCCESS) return createRes;`;
   * the fallback is gated, in order, by the exact DRM plane-layout error, by
     nativeBufferANDROID == nullptr, by a modifier request and by a successful rewrite, so a
     successful create pays one error comparison;
   * exactly one retry call exists, with no loop around it;
   * the replacement tiling is LINEAR and pCreateInfo is repointed at the replacement only
     after the retry succeeds;
   * the caller's create info is not written: the fallback block contains no pCreateInfo->
     assignment and no const_cast;
   * the log line and its once-per-process gate appear once;
   * the helper recognises exactly the two modifier structures, copies the create info
     (`mCreateInfo = input`), never writes through `input`, refuses more than one modifier,
     and returns no replacement chain for a predecessor it cannot size.
2. The patch script on minimal fixtures:
   * a fresh run inserts exactly the include line and the fallback block, and nothing else:
     reversing those two edits reproduces the input byte for byte;
   * a second run is a no-op;
   * a missing include anchor, a missing early return, a duplicated original create call and a
     divergent existing helper header are rejected without writing.
3. Workflow wiring: branch/paths filters, helper intent-to-add, patch -> Python test -> C++
   test order, and the artifact copies.

Usage:
    python scripts/test_image_modifier_fallback.py <gfxstream-root> [workflow]
"""
import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PATCH = HERE / "patch_image_modifier_fallback.py"

DECODER_REL = "host/vulkan/vk_decoder_global_state.cpp"
HEADER_REL = "host/vulkan/image_modifier_fallback.h"

INCLUDE_ANCHOR = '#include "ahb_blob_export_guard.h"\n'
INCLUDE_LINE = '#include "image_modifier_fallback.h"\n'
INCLUDE_BLOCK = INCLUDE_ANCHOR + INCLUDE_LINE

ORIGINAL_CREATE = "        createRes = vk->vkCreateImage(device, pCreateInfo, nullptr, pImage);\n"
OLD_RETURN = "        if (createRes != VK_SUCCESS) return createRes;\n"

LOG_LINE = 'MODIFIER-FALLBACK fmt=0x%x %ux%u'
GATE = "image_modifier_fallback::ShouldLog()"
RETRY_LINE = "createRes = vk->vkCreateImage(device, linearCreateInfo.get(), nullptr, pImage);"
ERROR_TEST = "createRes == VK_ERROR_INVALID_DRM_FORMAT_MODIFIER_PLANE_LAYOUT_EXT"
NATIVE_BUFFER_TEST = "nativeBufferANDROID == nullptr"
MODIFIER_TEST = "image_modifier_fallback::HasDrmFormatModifier(*pCreateInfo)"
MAKE_LINEAR = "linearCreateInfo.makeLinear(*pCreateInfo)"
REPOINT = "pCreateInfo = linearCreateInfo.get();"

IMPL_SIGNATURE = "VkResult on_vkCreateImage(gfxstream::base::BumpPool* pool"

FIXTURE_DECODER = """\
#include "ahb_blob_export_guard.h"

namespace gfxstream {
namespace host {
namespace vk {

    VkResult on_vkCreateImage(gfxstream::base::BumpPool* pool,
                              VkDevice boxed_device, const VkImageCreateInfo* pCreateInfo,
                              VkImage* pImage) {
        VkResult createRes = VK_SUCCESS;
        if (!pImage) {
            createRes = vk->vkCreateImage(device, pCreateInfo, nullptr, pImage);
        }
        if (createRes != VK_SUCCESS) return createRes;
        return createRes;
    }

}  // namespace vk
}  // namespace host
}  // namespace gfxstream
"""


def run_patch(root):
    return subprocess.run(
        [sys.executable, "-I", str(PATCH), str(root)],
        capture_output=True,
        text=True,
    )


def write_tree(base, decoder_text, header_text=None):
    (base / "host" / "vulkan").mkdir(parents=True)
    (base / DECODER_REL).write_text(decoder_text, encoding="utf-8", newline="\n")
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
header_path = root / HEADER_REL
check(decoder_path.is_file(), f"{DECODER_REL} missing")
check(header_path.is_file(), f"{HEADER_REL} missing")

decoder = decoder_path.read_text(encoding="utf-8")
header = header_path.read_text(encoding="utf-8")

check(decoder.count(INCLUDE_BLOCK) == 1, "fallback include appears once, after the AHB blob guard")
check(decoder.count(ORIGINAL_CREATE) == 1, "the original direct vkCreateImage call remains once")
check(decoder.count(RETRY_LINE) == 1, "exactly one LINEAR retry call")
check(decoder.count(LOG_LINE) == 1, "one MODIFIER-FALLBACK format string")
check(decoder.count(GATE) == 1, "the log is gated by the helper")
check(decoder.count(ERROR_TEST) == 1, "the fallback is gated by the exact driver error")
check(decoder.count(NATIVE_BUFFER_TEST) == 1, "the fallback excludes the native-buffer path")
check(decoder.count(MODIFIER_TEST) == 1, "the fallback requires a modifier request")
check(decoder.count(MAKE_LINEAR) == 1, "the fallback requires a successful rewrite")
check(decoder.count(REPOINT) == 1, "the recorded create info is repointed once")

function_at = decoder.index(IMPL_SIGNATURE)
create_at = decoder.index(ORIGINAL_CREATE)
block_at = decoder.index("        // MODIFIER-FALLBACK-1:")
return_at = decoder.index(OLD_RETURN)
check(function_at < create_at < block_at < return_at,
      "the fallback sits inside on_vkCreateImage() after the driver call")

# The original call and the early return are untouched; the block only sits between them.
check(decoder.count(OLD_RETURN) == 1, "the existing early return is still unique")
check(decoder[block_at:return_at].endswith("        }\n"),
      "the fallback block ends immediately before the early return")

block = decoder[block_at:return_at]
check("for (" not in block and "while (" not in block,
      "the fallback contains no loop, so at most one retry is possible")
check(re.search(r"pCreateInfo->\w+\s*=", block) is None,
      "the fallback writes no field through the caller's pointer")
check("const_cast" not in block, "the fallback does not cast away const on the caller's chain")
check("linearCreateInfo.makeLinear(*pCreateInfo)" in block,
      "the replacement is built from a copy, not from the caller's struct")

# Gate order: the error comparison first (successful creates pay one comparison), then the
# native-buffer exclusion, then the modifier scan, then the rewrite.
check(block.index(ERROR_TEST) < block.index(NATIVE_BUFFER_TEST) <
      block.index(MODIFIER_TEST) < block.index(MAKE_LINEAR),
      "the fallback gate short-circuits in cheap-to-expensive order")
check(block.index(MAKE_LINEAR) < block.index(RETRY_LINE), "the rewrite precedes the retry")
check(block.index(RETRY_LINE) < block.index(REPOINT), "the create info is repointed after retry")
check(block.index("if (createRes == VK_SUCCESS)") < block.index(REPOINT),
      "the repoint happens only on retry success")

# Helper invariants.
check("#ifndef GFXSTREAM_HOST_VULKAN_IMAGE_MODIFIER_FALLBACK_H_" in header, "header guard")
check(header.count("namespace gfxstream {") == 1 and
      header.count("namespace image_modifier_fallback {") == 1, "helper namespace")
check(header.count("VK_STRUCTURE_TYPE_IMAGE_DRM_FORMAT_MODIFIER_") == 2,
      "only the list and explicit modifier structures are named")
check("VK_STRUCTURE_TYPE_IMAGE_DRM_FORMAT_MODIFIER_LIST_CREATE_INFO_EXT" in header and
      "VK_STRUCTURE_TYPE_IMAGE_DRM_FORMAT_MODIFIER_EXPLICIT_CREATE_INFO_EXT" in header,
      "both modifier structures are filtered")
check(header.count("mCreateInfo = input;") == 1, "the replacement starts as a copy of the input")
check(header.count("mCreateInfo.tiling = VK_IMAGE_TILING_LINEAR;") == 1,
      "the replacement tiling is LINEAR")
check("input.tiling =" not in header and "input.pNext =" not in header,
      "the helper never writes through the input create info")
check("if (modifierCount > 1) return false;" in header,
      "more than one modifier structure is refused")
check("if (predecessorSize == 0 || predecessorSize > sizeof(mNodeStorage)) return false;" in header,
      "a predecessor of unknown size is refused")
check("default:\n            return 0;" in header, "unknown structures have no size")
check("std::memcpy(static_cast<void*>(&mNodeStorage), predecessor, predecessorSize);" in header,
      "only the predecessor is duplicated, into helper-owned storage")
check("mCreateInfo.pNext = &mNodeStorage;" in header, "the copy is linked from the helper")
check("static std::atomic<unsigned> sLogged{0};" in header, "atomic log gate state")
check("sLogged.fetch_add(1, std::memory_order_relaxed) == 0" in header,
      "single winner via one relaxed fetch_add")

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
    # The block is inserted before the early return; verify reversing the two edits restores
    # the fixture byte for byte.
    check(patched.count("// MODIFIER-FALLBACK-1:") == 1, "fresh patch inserts the fallback block")
    block_start = patched.index("        // MODIFIER-FALLBACK-1:")
    block_end = patched.index(OLD_RETURN, block_start)
    inserted = patched[block_start:block_end]
    reverted = patched.replace(INCLUDE_BLOCK, INCLUDE_ANCHOR).replace(inserted, "", 1)
    check(reverted == original, "reversing the two edits reproduces the fixture byte for byte")
    check((base / HEADER_REL).is_file(), "fresh patch creates the helper header")
    generated_header = (base / HEADER_REL).read_text(encoding="utf-8")

    again = run_patch(base)
    check(again.returncode == 0, f"second patch failed: {again.stderr}")
    check((base / DECODER_REL).read_text(encoding="utf-8") == patched, "second run is a no-op")
    check((base / HEADER_REL).read_text(encoding="utf-8") == generated_header,
          "second run does not rewrite the header")
    check("already patched" in again.stdout, "second run reports already patched")

with tempfile.TemporaryDirectory() as tmp:
    base = Path(tmp)
    write_tree(base, original.replace(INCLUDE_ANCHOR, "", 1))
    result = run_patch(base)
    check(result.returncode != 0, "a fixture without the AHB blob include anchor is rejected")
    check(not (base / HEADER_REL).exists(), "rejected patch writes no header")

with tempfile.TemporaryDirectory() as tmp:
    base = Path(tmp)
    write_tree(base, original.replace(OLD_RETURN, "", 1))
    result = run_patch(base)
    check(result.returncode != 0, "a fixture without the early return is rejected")

with tempfile.TemporaryDirectory() as tmp:
    base = Path(tmp)
    write_tree(base, original.replace(ORIGINAL_CREATE, ORIGINAL_CREATE + ORIGINAL_CREATE, 1))
    result = run_patch(base)
    check(result.returncode != 0, "a duplicated original vkCreateImage call is rejected")

with tempfile.TemporaryDirectory() as tmp:
    base = Path(tmp)
    write_tree(base, original, header_text="// different helper\n")
    result = run_patch(base)
    check(result.returncode != 0, "a divergent existing helper header is rejected")
    check((base / HEADER_REL).read_text(encoding="utf-8") == "// different helper\n",
          "rejected patch leaves the existing header untouched")

# ---------------------------------------------------------------------------
# 3. Workflow wiring.
# ---------------------------------------------------------------------------
if args.workflow is not None:
    workflow = args.workflow.read_text(encoding="utf-8")
    check("diagnostic/image-modifier-fallback" in workflow, "branch triggers the workflow")
    check("scripts/*image_modifier_fallback*" in workflow,
          "paths filter covers the fallback files")
    check("scripts/patch_image_modifier_fallback.py source" in workflow,
          "workflow applies the fallback patch")
    check("git -C source add -N host/vulkan/image_modifier_fallback.h" in workflow,
          "workflow marks the new header intent-to-add")
    check("scripts/test_image_modifier_fallback.py" in workflow,
          "workflow runs the Python test")
    check("test_image_modifier_fallback.cpp" in workflow, "workflow compiles the C++ test")
    check("cp source/host/vulkan/image_modifier_fallback.h out/" in workflow,
          "workflow copies the helper into the artifact")
    check("cp ci/scripts/*image_modifier_fallback* out/" in workflow,
          "workflow copies the fallback scripts into the artifact")
    check(workflow.index("patch_ahb_blob_export_guard.py") < workflow.index(
        "patch_image_modifier_fallback.py"), "the AHB guards are applied before this fallback")
    check(workflow.index("patch_image_modifier_fallback.py") < workflow.index(
        "test_image_modifier_fallback.py"), "the patch runs before the Python test")
    check(workflow.index("test_image_modifier_fallback.py") < workflow.index(
        "test_image_modifier_fallback.cpp"), "the Python test runs before the C++ test")
    check(workflow.index("test_image_modifier_fallback.cpp") < workflow.index(
        "test-image-modifier-fallback"), "the C++ test is compiled before it runs")
    check(workflow.index("patch_image_modifier_fallback.py") < workflow.rindex(
        "gfxstream-android-mali.patch"), "the fallback is applied before the final source snapshot")

print("PASS: on_vkCreateImage() retries a DRM-modifier plane-layout rejection exactly once with "
      "tiling=LINEAR, filters only the list/explicit modifier structures, leaves the caller's "
      "create info untouched, and keeps the default path at one comparison")
