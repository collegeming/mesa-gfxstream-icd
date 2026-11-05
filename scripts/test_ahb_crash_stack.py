#!/usr/bin/env python3
"""Static checks for the diagnostic AHB lifecycle tracer.

Usage: test_ahb_crash_stack.py <patched-gfxstream-root> [workflow.yml]

The patch has already been applied by the workflow at this point. The test
verifies the shipped header, the exact breadcrumb insertion points, that the
tracer is default-off, that every AHardwareBuffer_* call site has exactly one
adjacent AHB-LIFE crumb, and that a second patch run is a byte-for-byte no-op.
"""
import hashlib
import re
import subprocess
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
workflow = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else None
here = Path(__file__).resolve().parent

header = root / "host/vulkan/ahb_crash_trace.h"
ops = root / "host/vulkan/vk_common_operations.cpp"
decoder = root / "host/vulkan/vk_decoder_global_state.cpp"
object_manager = root / "host/common/external_object_manager.cpp"


def read(path: Path) -> str:
    if not path.is_file():
        raise SystemExit(f"{path}: missing")
    return path.read_text(encoding="utf-8")


header_text = read(header)
assert header_text == (here / "ahb_crash_trace.h").read_text(encoding="utf-8"), \
    "host/vulkan/ahb_crash_trace.h differs from the CI copy"
print("PASS: host/vulkan/ahb_crash_trace.h matches the CI copy")

for needle in ("GFXSTREAM_CRASH_TRACE", "GFXSTREAM_CRASH_TRACE_FILE",
               "GFXSTREAM_CRASH_MAPS_PATH", "_Unwind_Backtrace", "sigaction",
               "SA_SIGINFO", "prev", "AHB-CRASH-TRACE"):
    assert needle in header_text, f"header missing {needle}"
assert "v[0] == '1' && v[1] == '\\0'" in header_text, \
    "enable gate is not the exact GFXSTREAM_CRASH_TRACE=1 check"
assert 'extern "C" inline void GfxAhbCrashTraceHandler' in header_text, \
    "the signal handler must be inline so all translation units share one definition"
assert "printf(" not in header_text and "malloc(" not in header_text, \
    "signal path must not use printf/malloc"
print("PASS: header is default-off, async-signal-safe, and chains to the previous handler")

# tid on every crumb and in the crash header; ring must hold a whole lifecycle.
assert header_text.count("TraceGettid())") == 2, \
    "tid must be written by both Breadcrumb() and the signal handler"
assert 'w.str("tid=")' in header_text and 'w.str(" tid=")' in header_text
assert "constexpr uint32_t kRingSlots = 64;" in header_text, \
    "the breadcrumb ring must hold at least 64 entries"
assert "static_assert(kRingSlots >= 64" in header_text, \
    "the ring size must be guarded by a static_assert"
assert "last 64" in header_text, "the ring dump must print the whole 64-entry window"
print("PASS: every crumb carries tid= and the ring holds 64 entries")

CRUMB = '"AHB-LIFE'
EXPECTED_CRUMBS = {
    # allocAhb(): enter + return
    ops: {
        '"AHB-LIFE alloc enter"': 1,
        '"AHB-LIFE alloc return"': 1,
        '"AHB-LIFE cb-import"': 1,
        '"AHB-LIFE cb-import buffer"': 1,
        '"AHB-LIFE cb-import skip-null"': 1,
        '"AHB-LIFE release"': 3,
        '"AHB-LIFE free pre"': 1,
        '"AHB-LIFE free post"': 1,
        '"AHB-LIFE free skip-null"': 1,
        '"AHB-LIFE alias-acquire"': 1,
        '"AHB-LIFE alias-acquire skip-null"': 1,
    },
    decoder: {
        '"AHB-LIFE alloc-ret enter"': 1,
        '"AHB-LIFE alloc-ret"': 1,
        '"AHB-LIFE import handle"': 1,
        '"AHB-LIFE import buffer"': 1,
        '"AHB-LIFE guest-import"': 1,
        '"AHB-LIFE guest-import null"': 1,
        '"AHB-LIFE ahbfirst-import buffer"': 1,
        '"AHB-LIFE release"': 2,
        '"AHB-LIFE release skip-null"': 1,
    },
    object_manager: {
        '"AHB-LIFE release"': 1,
    },
}

total_expected = 0
for path, labels in EXPECTED_CRUMBS.items():
    text = read(path)
    for label, expected in labels.items():
        found = text.count(label)
        assert found == expected, f"{path.name}: {label} appears {found} times, expected {expected}"
    crumbs = text.count(CRUMB)
    total_expected += crumbs
    print(f"PASS: {path.name}: {crumbs} AHB-LIFE crumb statements with the expected labels")
assert total_expected == 24, f"expected 24 crumb statements, found {total_expected}"

for stale in ("allocAhb.enter", "allocAhb.result", "allocExternalMemory.import",
              "importExternalMemory.importedAhb", "decoder.import.handleInfo",
              "decoder.guestAhbImport", "allocateAhbFirstMemory.result"):
    for path in (ops, decoder):
        assert stale not in read(path), f"{path.name}: stale crumb label {stale} still present"
print("PASS: the old ad-hoc labels are gone; every label uses the AHB-LIFE namespace")

# One crumb per AHardwareBuffer_* call site. Scan real code lines (skip
# comments), allow the crumb to sit a few lines before or after the call.
CALL_RE = re.compile(r"AHardwareBuffer_(acquire|release|allocate|fromHardwareBuffer)\s*\(")


def call_sites(path: Path):
    sites = []
    lines = read(path).splitlines()
    for index, line in enumerate(lines):
        match = CALL_RE.search(line)
        if match is None:
            continue
        comment = line.find("//")
        if comment != -1 and comment < match.start():
            continue
        sites.append(index)
    return lines, sites


for path, expected_calls in ((ops, 6), (decoder, 3), (object_manager, 1)):
    lines, sites = call_sites(path)
    assert len(sites) == expected_calls, \
        f"{path.name}: found {len(sites)} AHardwareBuffer_* calls, expected {expected_calls}"
    for index in sites:
        window = "\n".join(lines[max(0, index - 5):index + 4])
        assert CRUMB in window, \
            f"{path.name}:{index + 1}: AHardwareBuffer_* call has no adjacent AHB-LIFE crumb"
    print(f"PASS: {path.name}: {expected_calls} call sites, each with an adjacent AHB-LIFE crumb")
assert 6 + 3 + 1 == 10
print("PASS: all 10 AHardwareBuffer_acquire/release/allocate sites are covered")

# The alias acquire must not run on a null handle in the diagnostic build.
assert 'if (!importedAhb) {' in ops.read_text(encoding="utf-8")
assert '"AHB-LIFE alias-acquire skip-null"' in ops.read_text(encoding="utf-8")
print("PASS: the alias path records a null handle and returns before AHardwareBuffer_acquire")

for marker in ("AHB-CRASH-TRACE-INCLUDE-BEGIN", "AHB-CRASH-TRACE-ALLOC-BEGIN",
               "AHB-CRASH-TRACE-ALLOC-RETURN-BEGIN", "AHB-CRASH-TRACE-IMPORT-CB-BEGIN",
               "AHB-CRASH-TRACE-ALIAS-BEGIN", "AHB-CRASH-TRACE-FREE-PRE-BEGIN"):
    assert read(ops).count(marker) == 1, \
        f"{marker} must appear exactly once in vk_common_operations.cpp"
assert read(ops).count('#include "ahb_crash_trace.h"') == 1
print("PASS: vk_common_operations.cpp include and the deployed AHB sites are marked exactly once")

decoder_text = read(decoder)
assert decoder_text.count('#include "ahb_crash_trace.h"') == 1
assert decoder_text.count("AHB-CRASH-TRACE-INCLUDE-DECODER-BEGIN") == 1
for marker in ("AHB-CRASH-TRACE-DECODER-HANDLE-BEGIN", "AHB-CRASH-TRACE-GUEST-AHB-BEGIN",
               "AHB-CRASH-TRACE-FIRST-BEGIN", "AHB-CRASH-TRACE-DESTROY-RELEASE-BEGIN"):
    assert decoder_text.count(marker) == 1, \
        f"{marker} must appear exactly once in vk_decoder_global_state.cpp"
print("PASS: decoder includes the tracer; handle import, guest pNext AHB import, "
      "AHB-first helper and teardown are traced")

om_text = read(object_manager)
assert om_text.count('#include "../vulkan/ahb_crash_trace.h"') == 1
assert om_text.count("AHB-CRASH-TRACE-OBJECT-MANAGER-RELEASE-BEGIN") == 1
print("PASS: host/common blob teardown releases are traced too")

# Re-running the patch must not change one byte.
before = {p: hashlib.sha256(p.read_bytes()).hexdigest()
          for p in (header, ops, decoder, object_manager)}
subprocess.run([sys.executable, "-I", str(here / "patch_ahb_crash_stack.py"), str(root)],
               check=True, stdout=subprocess.PIPE, text=True)
after = {p: hashlib.sha256(p.read_bytes()).hexdigest()
         for p in (header, ops, decoder, object_manager)}
assert before == after, "second patch run modified the tree"
print("PASS: patch is idempotent (second run is a byte-for-byte no-op)")

if workflow is not None:
    workflow_text = read(workflow)
    assert "diagnostic/ahb-crash-stack" in workflow_text, "workflow does not list the branch"
    assert "scripts/*ahb_crash_stack*" in workflow_text, "workflow does not watch the scripts"
    assert "python3 -I ci/scripts/patch_ahb_crash_stack.py source" in workflow_text
    assert "test_ahb_crash_stack.cpp" in workflow_text
    print("PASS: workflow applies and tests the tracer on the diagnostic branch")

print("ALL AHB-LIFE CRUMB STATIC TESTS PASSED")
