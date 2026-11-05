#!/usr/bin/env python3
"""Static and patch tests for the AHB export guard.

Checks, in this order:

1. Production invariants (the real tree must already be patched):
   * the AndroidAHB case no longer calls vkGetMemoryAndroidHardwareBufferANDROID and
     contains no getAhbInfo/exportHandle plumbing;
   * the imported-AHB fast path is byte-identical to the pre-patch text and is still
     immediately followed by the guarded else;
   * the else records validHandle=false and VK_ERROR_INVALID_EXTERNAL_HANDLE and logs
     AHB-EXPORT-SKIP once, through the header's gate;
   * the helper header is included exactly once, inside the Android block, next to the
     AHB allocation switch include;
   * the helper is a once-per-process single-winner gate with no Vulkan dependency.
2. The patch script on minimal fixtures:
   * a fresh run inserts exactly the include line and the guarded else, and nothing
     else: reversing those two edits reproduces the input byte for byte, so the normal
     path cannot have changed;
   * a second run is a no-op;
   * a missing include anchor, a missing/duplicated fallback, a leftover getter call
     outside the fallback, and a divergent helper header are rejected without writing.
3. Workflow wiring: branch/paths filters, helper intent-to-add, patch -> Python test ->
   C++ test order, and the artifact copies.

Usage:
    python scripts/test_ahb_export_guard.py <gfxstream-root> [workflow]
"""
import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PATCH = HERE / "patch_ahb_export_guard.py"

OPS_REL = "host/vulkan/vk_common_operations.cpp"
HEADER_REL = "host/vulkan/ahb_export_guard.h"
LOG_LINE = "AHB-EXPORT-SKIP no-ahb-backing size=%llu"
GATE = "::gfxstream::ahb_export_guard::ShouldLogSkip()"
GETTER_CALL = "vkGetMemoryAndroidHardwareBufferANDROID(mDevice"
INCLUDE_ANCHOR = '#include "ahb_alloc_off_file.h"\n'
INCLUDE_LINE = '#include "ahb_export_guard.h"\n'
INCLUDE_BLOCK = INCLUDE_ANCHOR + INCLUDE_LINE

NORMAL = """\
            if (info->handleInfo &&
                info->handleInfo->streamHandleType == STREAM_HANDLE_TYPE_PLATFORM_AHB) {
                validHandle = true;
                exportRes = VK_SUCCESS;
            } else {
"""

OLD_ELSE = """\
            } else {
                // Fallback: use the old export path if AHB import wasn't used
                VkMemoryGetAndroidHardwareBufferInfoANDROID getAhbInfo = {
                    .sType = VK_STRUCTURE_TYPE_MEMORY_GET_ANDROID_HARDWARE_BUFFER_INFO_ANDROID,
                    .pNext = nullptr,
                    .memory = info->memory,
                };
                AHardwareBuffer* exportHandle = nullptr;
                exportRes =
                    vk->vkGetMemoryAndroidHardwareBufferANDROID(mDevice, &getAhbInfo, &exportHandle);
                validHandle = (VK_SUCCESS == exportRes) && (NULL != exportHandle);
                // AHB-NULL-GUARD-2: only publish a handle that was actually exported. Writing
                // {handle=0, PLATFORM_AHB} here makes the guest import a null AHB and the
                // driver SIGSEGV in AHardwareBuffer_acquire. On failure handleInfo stays unset.
                if (validHandle) {
                    info->handleInfo = ExternalHandleInfo{
                        .handle = reinterpret_cast<ExternalHandleType>(exportHandle),
                        .streamHandleType = STREAM_HANDLE_TYPE_PLATFORM_AHB,
                    };
                } else {
                    GFXSTREAM_ERROR(
                        "AHB-NULL-GUARD: Android hardware buffer export returned no handle "
                        "(result=%d)",
                        (int)exportRes);
                }
            }
"""

NEW_ELSE = """\
            } else {
                // No AHB backing: this memory was not imported from an AHB (that arm is
                // above) and was not allocated with an AHB export chain either, because
                // this function does not add VkExportMemoryAllocateInfo in AndroidAHB
                // mode. Asking the driver to export it is invalid, and Mali does not
                // check: vkGetMemoryAndroidHardwareBufferANDROID() loads the memory
                // object's AHB field (+0x198), written only by an AHB import or an AHB
                // export allocation, and passes it to AHardwareBuffer_acquire()
                // unguarded. Refuse here and report what the driver should have returned.
                if (::gfxstream::ahb_export_guard::ShouldLogSkip()) {
                    GFXSTREAM_WARNING("AHB-EXPORT-SKIP no-ahb-backing size=%llu",
                                      static_cast<unsigned long long>(info->size));
                }
                validHandle = false;
                exportRes = VK_ERROR_INVALID_EXTERNAL_HANDLE;
            }
"""

CASE_HEAD = """\
        case ExternalMemory::Mode::AndroidAHB: {
#ifdef __ANDROID__
"""

FIXTURE_HEAD = """\
#include <vulkan/vulkan.h>

#ifdef __ANDROID__
#include "ahb_alloc_off_file.h"
#endif

namespace gfxstream::host::vk {

bool allocExternalMemory(bool colorBufferInfo) {
    switch (mode) {
"""

FIXTURE_MIDDLE = """\
            // If the AHB was already allocated and imported, handleInfo is set. Skip export.
"""

FIXTURE_TAIL = """\
#endif
            break;
        }
    }
}

}  // namespace gfxstream::host::vk
"""


def fixture(with_fallback=True):
    body = CASE_HEAD + FIXTURE_MIDDLE + NORMAL
    if with_fallback:
        body += OLD_ELSE
    else:
        body += "            }\n"
    return FIXTURE_HEAD + body + FIXTURE_TAIL


def run_patch(root):
    return subprocess.run(
        [sys.executable, "-I", str(PATCH), str(root)],
        capture_output=True,
        text=True,
    )


def write_tree(base, ops_text, header_text=None):
    (base / "host" / "vulkan").mkdir(parents=True)
    (base / OPS_REL).write_text(ops_text, encoding="utf-8", newline="\n")
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
ops_path = root / OPS_REL
header_path = root / HEADER_REL
check(ops_path.is_file(), f"{OPS_REL} missing")
check(header_path.is_file(), f"{HEADER_REL} missing")
ops = ops_path.read_text(encoding="utf-8")
header = header_path.read_text(encoding="utf-8")

check(ops.count(INCLUDE_BLOCK) == 1, "guard include appears exactly once, inside the Android block")
check(ops.count(NEW_ELSE) == 1, "guarded else appears exactly once")
check(OLD_ELSE not in ops, "the old AHB export fallback is gone")
check(GETTER_CALL not in ops, "no AHB export call remains in vk_common_operations.cpp")
check("getAhbInfo" not in ops, "no export-info struct remains in vk_common_operations.cpp")
check("AHB-NULL-GUARD-2" not in ops, "the superseded publish wrapper is gone")
check(ops.count(LOG_LINE) == 1, "one AHB-EXPORT-SKIP format string")
check(ops.count(GATE) == 1, "the skip is gated by the helper")
check(ops.count("validHandle = false;") >= 1, "guard clears validHandle")
check(ops.count("exportRes = VK_ERROR_INVALID_EXTERNAL_HANDLE;") >= 1,
      "guard reports VK_ERROR_INVALID_EXTERNAL_HANDLE")

# The normal path is still the imported-AHB fast path, immediately followed by the guard.
check(ops.count(NORMAL + "                // No AHB backing") == 1,
      "normal path is unchanged and immediately precedes the guarded else")
check("                validHandle = true;\n                exportRes = VK_SUCCESS;\n" in ops,
      "imported-AHB path still succeeds without an export call")

# The log is inside the guard, not the fast path.
else_at = ops.index(NEW_ELSE)
log_at = ops.index(LOG_LINE)
check(else_at < log_at < else_at + len(NEW_ELSE), "skip log sits inside the guarded else")
check(ops.index("                validHandle = false;", else_at) < else_at + len(NEW_ELSE),
      "validHandle=false sits inside the guarded else")

# With AHB backing, no skip log and no error path is reachable.
fast_path = ops[:else_at]
check(LOG_LINE not in fast_path, "fast path does not log a skip")

# Helper header: once-per-process gate, no Vulkan types.
check("#ifndef GFXSTREAM_HOST_VULKAN_AHB_EXPORT_GUARD_H_" in header, "header guard")
check(header.count("inline bool ShouldLogSkip()") == 1, "single gate function")
check("static std::atomic<unsigned> sLogged{0};" in header, "atomic gate state")
check("sLogged.fetch_add(1, std::memory_order_relaxed) == 0" in header,
      "single winner via one relaxed fetch_add")
check("namespace gfxstream {" in header and "namespace ahb_export_guard {" in header,
      "helper namespace matches the call site")
check("#include <vulkan" not in header and "VkResult" not in header,
      "helper has no Vulkan dependency")

# ---------------------------------------------------------------------------
# 2. Patch script behaviour on minimal fixtures.
# ---------------------------------------------------------------------------
original = fixture()

with tempfile.TemporaryDirectory() as tmp:
    base = Path(tmp)
    write_tree(base, original)
    result = run_patch(base)
    check(result.returncode == 0, f"fresh patch failed: {result.stderr}")
    patched = (base / OPS_REL).read_text(encoding="utf-8")
    expected = original.replace(INCLUDE_ANCHOR, INCLUDE_BLOCK).replace(OLD_ELSE, NEW_ELSE)
    check(patched == expected, "fresh patch inserts only the include and the guarded else")
    check((base / HEADER_REL).is_file(), "fresh patch creates the helper header")
    reverted = patched.replace(INCLUDE_BLOCK, INCLUDE_ANCHOR).replace(NEW_ELSE, OLD_ELSE)
    check(reverted == original, "reversing the two edits reproduces the fixture byte for byte")

    again = run_patch(base)
    check(again.returncode == 0, f"second patch failed: {again.stderr}")
    check((base / OPS_REL).read_text(encoding="utf-8") == patched, "second run is a no-op")
    check("already patched" in again.stdout, "second run reports already patched")

with tempfile.TemporaryDirectory() as tmp:
    base = Path(tmp)
    write_tree(base, fixture(with_fallback=False))
    result = run_patch(base)
    check(result.returncode != 0, "a fixture without the fallback is rejected")
    check(not (base / HEADER_REL).exists(), "rejected patch writes no header")
    check((base / OPS_REL).read_text(encoding="utf-8") == fixture(with_fallback=False),
          "rejected patch leaves the source untouched")

with tempfile.TemporaryDirectory() as tmp:
    base = Path(tmp)
    duplicated = fixture().replace(
        FIXTURE_MIDDLE, FIXTURE_MIDDLE + "            // duplicate arm\n" + OLD_ELSE, 1)
    write_tree(base, duplicated)
    result = run_patch(base)
    check(result.returncode != 0, "a duplicated fallback is rejected")

with tempfile.TemporaryDirectory() as tmp:
    base = Path(tmp)
    stray = fixture().replace(
        FIXTURE_TAIL, "void other() { vk->vkGetMemoryAndroidHardwareBufferANDROID(mDevice, 0, 0); }\n"
        + FIXTURE_TAIL)
    write_tree(base, stray)
    result = run_patch(base)
    check(result.returncode != 0, "a getter call outside the fallback is rejected")

with tempfile.TemporaryDirectory() as tmp:
    base = Path(tmp)
    write_tree(base, fixture().replace(INCLUDE_ANCHOR, ""))
    result = run_patch(base)
    check(result.returncode != 0, "a missing AHB switch include anchor is rejected")

with tempfile.TemporaryDirectory() as tmp:
    base = Path(tmp)
    write_tree(base, fixture(), header_text="// different helper\n")
    result = run_patch(base)
    check(result.returncode != 0, "a divergent existing helper header is rejected")
    check((base / HEADER_REL).read_text(encoding="utf-8") == "// different helper\n",
          "rejected patch leaves the existing header untouched")

# ---------------------------------------------------------------------------
# 3. Workflow wiring.
# ---------------------------------------------------------------------------
if args.workflow is not None:
    workflow = args.workflow.read_text(encoding="utf-8")
    check("diagnostic/ahb-export-guard" in workflow, "branch triggers the workflow")
    check("scripts/*ahb_export_guard*" in workflow, "paths filter covers the guard files")
    check("scripts/patch_ahb_export_guard.py source" in workflow, "workflow applies the patch")
    check("git -C source add -N host/vulkan/ahb_export_guard.h" in workflow,
          "workflow marks the new header intent-to-add")
    check("scripts/test_ahb_export_guard.py" in workflow, "workflow runs the Python test")
    check("test_ahb_export_guard.cpp" in workflow, "workflow compiles the C++ test")
    check("cp source/host/vulkan/ahb_export_guard.h out/" in workflow,
          "workflow copies the helper into the artifact")
    check("cp ci/scripts/*ahb_export_guard* out/" in workflow,
          "workflow copies the guard scripts into the artifact")
    check(workflow.index("patch_ahb_export_guard.py") < workflow.index(
        "test_ahb_export_guard.py"), "patch runs before the Python test")
    check(workflow.index("test_ahb_export_guard.py") < workflow.index(
        "test_ahb_export_guard.cpp"), "Python test runs before the C++ test")
    check(workflow.index("test_ahb_export_guard.cpp") < workflow.index(
        "test-ahb-export-guard"), "C++ test is compiled before it runs")

print("PASS: AndroidAHB export only uses an imported AHB; the no-backing fallback is "
      "refused locally with VK_ERROR_INVALID_EXTERNAL_HANDLE and one AHB-EXPORT-SKIP line, "
      "the imported-AHB path is byte-identical, and the patch is idempotent and anchored")
