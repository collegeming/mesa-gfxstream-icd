#!/usr/bin/env python3
"""Source invariants for the default-on AndroidHardwareBuffer import guard.

Checks, in this order:

1. host/vulkan/vk_decoder_global_state.cpp:
   * the helper header is included exactly once, inside __ANDROID__;
   * in case AndroidHardwareBuffer the guard runs after the __ANDROID__ gate, before
     the existing AHB-NULL-GUARD and before importInfo.buffer is assigned;
   * the guard refuses through the shared predicate, logs AHB-IMPORT-NULL once per
     process, records VK_ERROR_INVALID_EXTERNAL_HANDLE and returns false;
   * both appendExternalMemoryImport callers return the recorded result before the
     generic unsupported-type error;
   * OpaqueFd and DmaBuf still read getFd() and never cast a handle to a pointer; the
     pinned AHB assignment line is unchanged.
2. host/vulkan/ahb_import_guard.h: the one comparison, the 4096 threshold and the
   atomic once-per-process gate.
3. Workflow wiring: paths filter, helper intent-to-add, artifact copies, and the
   patch -> Python test -> C++ test order.

The C++ decision table (0/fd rejected, plausible address passed through unchanged,
log gate single winner) is covered by scripts/test_ahb_import_null_guard.cpp.

Usage:
    python scripts/test_ahb_import_null_guard.py <gfxstream-root> [workflow]
"""
import argparse
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("root", type=Path, help="gfxstream source root")
parser.add_argument("workflow", type=Path, nargs="?", help="CI workflow file")
args = parser.parse_args()
root = args.root.resolve()

decoder = (root / "host/vulkan/vk_decoder_global_state.cpp").read_text(encoding="utf-8")
header = (root / "host/vulkan/ahb_import_guard.h").read_text(encoding="utf-8")

ASSIGN = "importInfo.buffer = reinterpret_cast<AHardwareBuffer*>(handleInfo.handle);"
OLD_GUARD = "AHB-NULL-GUARD: refusing to import a null AHardwareBuffer handle"
LOG = "AHB-IMPORT-NULL handle=%p kind=0x%x"

# ---------------------------------------------------------------------------
# 1. Production invariants.
# ---------------------------------------------------------------------------
assert decoder.count('#include "ahb_import_guard.h"') == 1, "helper include is not unique"
assert decoder.count("AHB-IMPORT-NULL-1-BEGIN") == 1, "guard begin marker is not unique"
assert decoder.count("AHB-IMPORT-NULL-1-END") == 1, "guard end marker is not unique"
assert decoder.count("AHB-IMPORT-NULL-2") == 1, "ColorBuffer caller marker is not unique"
assert decoder.count("AHB-IMPORT-NULL-3") == 1, "Buffer caller marker is not unique"
assert decoder.count(ASSIGN) == 1, "the pinned AHB assignment line must stay unique and unchanged"
assert decoder.count("VkResult externalMemoryImportResult = VK_SUCCESS;") == 1
assert decoder.count("return externalMemoryImportResult;") == 2

case_at = decoder.index("case ExternalMemoryImportKind::AndroidHardwareBuffer:")
android_at = decoder.index("#if defined(__ANDROID__)", case_at)
begin_at = decoder.index("AHB-IMPORT-NULL-1-BEGIN")
end_at = decoder.index("AHB-IMPORT-NULL-1-END")
old_guard_at = decoder.index(OLD_GUARD)
assign_at = decoder.index(ASSIGN)
assert case_at < android_at < begin_at < end_at < old_guard_at < assign_at, (
    "the guard must sit inside the AndroidHardwareBuffer case, before the old guard "
    "and before the AHB assignment")

guard = decoder[begin_at:end_at]
assert "AhbImportHandleIsPlausible(" in guard
assert "handleInfo.handle" in guard
assert "ShouldLogImportFailure()" in guard
assert LOG in guard
assert "reinterpret_cast<void*>(" in guard
assert "externalMemoryImportResult = VK_ERROR_INVALID_EXTERNAL_HANDLE;" in guard
assert "return false;" in guard
assert "importInfo.buffer" not in guard, "the rejected path must not touch importInfo.buffer"

# The old 4 guards stay in place; this one only narrows the accepted handles.
assert "if (handleInfo.handle == 0) {" in decoder[begin_at:assign_at]

# FD-based kinds must not gain a pointer interpretation.
fd_cases = decoder[decoder.index("case ExternalMemoryImportKind::OpaqueFd:"):case_at]
assert fd_cases.count("getFd()") == 2, "OpaqueFd and DmaBuf must still read getFd()"
assert "reinterpret_cast<AHardwareBuffer*>" not in fd_cases
assert "ahb_import_guard" not in fd_cases

for marker, kind in (("AHB-IMPORT-NULL-2", "ColorBuffer"), ("AHB-IMPORT-NULL-3", "Buffer")):
    at = decoder.index(marker)
    call = decoder[at:at + 700]
    assert 'GFXSTREAM_ERROR("Unsupported %s external memory handle type: %%u"' % kind in call
    assert "if (externalMemoryImportResult != VK_SUCCESS) {" in call
    assert "return externalMemoryImportResult;" in call
    assert call.index("return externalMemoryImportResult;") < call.index(
        "return VK_ERROR_OUT_OF_DEVICE_MEMORY;")

# ---------------------------------------------------------------------------
# 2. Helper header.
# ---------------------------------------------------------------------------
assert header.count("constexpr uint64_t kMinPlausibleAhbAddress = 4096;") == 1
assert header.count("return static_cast<uint64_t>(handle) >= kMinPlausibleAhbAddress;") == 1
assert header.count("inline bool ShouldLogImportFailure()") == 1
assert header.count("std::atomic<unsigned> sLogged{0};") == 1
assert "fetch_add(1, std::memory_order_relaxed) == 0" in header
# The guard is one comparison: no second range check on the handle.
assert "handle <" not in header and "handle >" not in header

# ---------------------------------------------------------------------------
# 3. Workflow wiring.
# ---------------------------------------------------------------------------
if args.workflow:
    workflow = args.workflow.resolve().read_text(encoding="utf-8")
else:
    candidates = [
        root / ".github/workflows/build-droidvm-mali-host.yml",
        root.parent / ".github/workflows/build-droidvm-mali-host.yml",
        root.parent / "ci/.github/workflows/build-droidvm-mali-host.yml",
    ]
    workflow_path = next((x for x in candidates if x.is_file()), None)
    assert workflow_path is not None, "CI workflow not found"
    workflow = workflow_path.read_text(encoding="utf-8")

assert "scripts/*ahb_import_null_guard*" in workflow
assert "git -C source add -N host/vulkan/ahb_import_guard.h" in workflow
assert "cp source/host/vulkan/ahb_import_guard.h out/" in workflow
assert "cp ci/scripts/*ahb_import_null_guard* out/" in workflow
assert "test_ahb_import_null_guard.cpp" in workflow
assert workflow.index("patch_ahb_null_guard.py") < workflow.index(
    "patch_ahb_import_null_guard.py")
assert workflow.index("patch_ahb_import_null_guard.py") < workflow.index(
    "test_ahb_import_null_guard.py")
assert workflow.index("test_ahb_import_null_guard.py") < workflow.index(
    "test_ahb_import_null_guard.cpp")
assert workflow.index("test_ahb_import_null_guard.cpp") < workflow.index(
    "test-ahb-import-null-guard")

print("PASS: AHB import guard is default-on, AndroidHardwareBuffer-only, and wired "
      "before the generic import errors")
